"""Cisco-style output filters for the ``|`` pipe.

Kept free of rich/prompt_toolkit so the semantics can be tested without a
terminal. The shell captures a command's text output, hands the lines to
:func:`apply_filters` and prints whatever comes back.
"""
from __future__ import annotations

import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

from makemeswitch.core.models import ConfigError

# Exactly the descriptions IOS prints after ``| ?`` so muscle memory matches.
FILTER_HELP: list[tuple[str, str]] = [
    ("append", "Append redirected output to URL (URLs supporting append operation only)"),
    ("begin", "Begin with the line that matches"),
    ("count", "Count number of lines which match regexp"),
    ("exclude", "Exclude lines that match"),
    ("format", "Format the output using the specified spec file"),
    ("include", "Include lines that match"),
    ("redirect", "Redirect output to URL"),
    ("section", "Filter a section of output"),
    ("tee", "Copy output to URL"),
]

FILTER_NAMES = [name for name, _ in FILTER_HELP]


def resolve_filter_name(name: str) -> str | None:
    """IOS-style abbreviation: ``inc`` -> ``include`` when unambiguous."""
    lowered = name.lower()
    if lowered in FILTER_NAMES:
        return lowered
    matches = [candidate for candidate in FILTER_NAMES if candidate.startswith(lowered)]
    if len(matches) == 1:
        return matches[0]
    return None

# Filters that consume the pipeline instead of forwarding it.
_TERMINAL_FILTERS = {"redirect", "append"}


@dataclass(frozen=True)
class PipeFilter:
    name: str
    arg: str = ""

    def __str__(self) -> str:  # for error messages
        return f"{self.name} {self.arg}".strip()


def _split_tokens(text: str) -> list[str]:
    """Split on whitespace but keep quoted arguments together."""
    tokens: list[str] = []
    current: list[str] = []
    quote: str | None = None
    for char in text:
        if quote:
            if char == quote:
                quote = None
            else:
                current.append(char)
        elif char in "\"'":
            quote = char
        elif char.isspace():
            if current:
                tokens.append("".join(current))
                current = []
        else:
            current.append(char)
    if quote:
        raise ConfigError(f"unterminated quote in pipe expression: {text.strip()}")
    if current:
        tokens.append("".join(current))
    return tokens


def _segments(line: str) -> list[str]:
    """Split on every top-level ``|`` (quoted pipes stay inside the argument)."""
    parts: list[str] = []
    current: list[str] = []
    quote: str | None = None
    for char in line:
        if quote:
            current.append(char)
            if char == quote:
                quote = None
        elif char in "\"'":
            quote = char
            current.append(char)
        elif char == "|":
            parts.append("".join(current))
            current = []
        else:
            current.append(char)
    parts.append("".join(current))
    return parts


def split_pipe(line: str) -> tuple[str, list[PipeFilter] | None]:
    """Split ``show run | include vlan`` into command and filters.

    Returns ``(command, None)`` when the line has no pipe at all. A trailing
    pipe without a filter returns an empty filter list, which the caller
    reports as an incomplete command (IOS does the same).
    """
    if "|" not in line:
        return line, None
    segments = _segments(line)
    command = segments[0].strip()
    normalised: list[PipeFilter] = []
    for raw in segments[1:]:
        tokens = _split_tokens(raw)
        if not tokens:
            continue
        canonical = resolve_filter_name(tokens[0])
        if canonical is None:
            hint = ", ".join(FILTER_NAMES)
            raise ConfigError(f"unknown pipe filter: {tokens[0]} (available: {hint})")
        normalised.append(PipeFilter(canonical, " ".join(tokens[1:]).strip()))
    return command, normalised


def _compile(pattern: str, name: str) -> re.Pattern[str]:
    if not pattern:
        raise ConfigError(f"pipe filter '{name}' needs an argument")
    try:
        return re.compile(pattern)
    except re.error as exc:
        raise ConfigError(f"invalid regular expression for '{name}': {exc}") from exc


def _filter_include(lines: list[str], arg: str) -> list[str]:
    regex = _compile(arg, "include")
    return [line for line in lines if regex.search(line)]


def _filter_exclude(lines: list[str], arg: str) -> list[str]:
    regex = _compile(arg, "exclude")
    return [line for line in lines if not regex.search(line)]


def _filter_begin(lines: list[str], arg: str) -> list[str]:
    regex = _compile(arg, "begin")
    for position, line in enumerate(lines):
        if regex.search(line):
            return lines[position:]
    return []


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _filter_section(lines: list[str], arg: str) -> list[str]:
    regex = _compile(arg, "section")
    out: list[str] = []
    start: int | None = None
    for position, line in enumerate(lines):
        if regex.search(line):
            start = position
            break
    if start is None:
        return []
    base = _indent(lines[start])
    out.append(lines[start])
    for line in lines[start + 1 :]:
        if not line.strip():
            out.append(line)
            continue
        if _indent(line) <= base:
            break
        out.append(line)
    return out


def _filter_count(lines: list[str], arg: str) -> list[str]:
    regex = _compile(arg, "count")
    return [str(sum(1 for line in lines if regex.search(line)))]


def _filter_format(lines: list[str], arg: str) -> list[str]:
    spec = Path(arg).expanduser()
    if not spec.is_file():
        raise ConfigError(f"format spec file not found: {arg}")
    template_lines = [line for line in spec.read_text().splitlines() if line.strip() and not line.lstrip().startswith("#")]
    if not template_lines:
        raise ConfigError(f"format spec file has no template lines: {arg}")
    # One template per spec file; extra lines are joined with a space so the
    # mapping stays one-output-line-per-input-line.
    template = " ".join(template_lines)
    out: list[str] = []
    for line in lines:
        fields = line.split()
        try:
            out.append(template.format(*fields, line=line))
        except (IndexError, KeyError) as exc:
            raise ConfigError(f"format spec {arg} does not match the output: {exc}") from exc
    return out


# --- output destinations ---------------------------------------------------


def _write(text: str, url: str, *, append: bool) -> None:
    scheme = url.split(":", 1)[0].lower() if ":" in url else ""
    if scheme in ("http", "https"):
        data = text.encode()
        method = "POST" if append else "PUT"
        request = urllib.request.Request(url, data=data, method=method)
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                if response.status >= 400:
                    raise ConfigError(f"{method} {url} failed: HTTP {response.status}")
        except urllib.error.URLError as exc:
            raise ConfigError(f"could not send output to {url}: {exc.reason}") from exc
        return
    if scheme in ("", "file"):
        path = Path(url[len("file:") :] if scheme == "file" else url).expanduser()
        mode = "a" if append else "w"
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open(mode, encoding="utf-8") as handle:
                handle.write(text)
        except OSError as exc:
            raise ConfigError(f"could not write {path}: {exc.strerror or exc}") from exc
        return
    raise ConfigError(
        f"unsupported URL scheme '{scheme}:' - use file:, http:// or https:// "
        "(tftp:/ftp: are not implemented)"
    )


def apply_filters(
    lines: Iterable[str],
    filters: list[PipeFilter],
    *,
    printer: Callable[[str], None] | None = None,
) -> str | None:
    """Run the filter chain. Returns text to print, or ``None`` for silence."""
    current = [line.rstrip() for line in lines]
    for position, item in enumerate(filters):
        if item.name in _TERMINAL_FILTERS and position != len(filters) - 1:
            raise ConfigError(f"'| {item.name}' must be the last filter in the pipe")
        if item.name == "include":
            current = _filter_include(current, item.arg)
        elif item.name == "exclude":
            current = _filter_exclude(current, item.arg)
        elif item.name == "begin":
            current = _filter_begin(current, item.arg)
        elif item.name == "section":
            current = _filter_section(current, item.arg)
        elif item.name == "count":
            current = _filter_count(current, item.arg)
        elif item.name == "format":
            current = _filter_format(current, item.arg)
        elif item.name in ("redirect", "append", "tee"):
            text = "\n".join(current) + ("\n" if current else "")
            _write(text, item.arg, append=(item.name == "append"))
            if item.name != "tee":
                return None
        else:  # pragma: no cover - guarded by split_pipe
            raise ConfigError(f"unknown pipe filter: {item.name}")
    return "\n".join(current)


def help_rows() -> list[tuple[str, str]]:
    return list(FILTER_HELP)


def _last_pipe_text(line: str) -> str:
    """Return the text typed after the most recent ``|``."""
    segments = _segments(line)
    return segments[-1] if len(segments) > 1 else ""


def filter_candidates(line: str) -> list[tuple[str, str]]:
    """Completions for the filter word being typed after a ``|``.

    Returns ``(word, description)`` pairs, matching what IOS shows for
    ``sh run | ?`` and for Tab completion.
    """
    text = _last_pipe_text(line)
    if not text.strip():
        return list(FILTER_HELP)
    word = text.strip()
    if " " in word:  # argument of an already-complete filter
        return []
    lowered = word.lower()
    rows = [(name, desc) for name, desc in FILTER_HELP if name.startswith(lowered)]
    return rows or []


def next_filter_argument(line: str) -> str | None:
    """Name of the filter whose argument is being typed, if any.

    Used for messages such as "filter 'include' needs a regular expression".
    """
    text = _last_pipe_text(line)
    tokens = _split_tokens(text)
    if len(tokens) == 1 and tokens[0].lower() in FILTER_NAMES:
        return tokens[0].lower()
    return None
