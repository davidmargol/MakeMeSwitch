"""Parser for the Cisco-inspired command language.

Supports:

* abbreviations of any unambiguous prefix (``sh int st``);
* ``?`` context help;
* dynamic arguments (``<interface>``, ``<vlan>``);
* free-text trailing arguments (``description uplink to core``);
* deterministic Cisco-style error messages.
"""
from __future__ import annotations

import shlex
from dataclasses import dataclass, field
from typing import Callable, Iterable


@dataclass
class CommandSpec:
    path: tuple[str, ...]
    handler: Callable[..., object]
    modes: tuple[str, ...]
    help: str = ""
    trailing: bool = False
    privilege: str = "readonly"  # readonly | operator | admin

    @property
    def keywords(self) -> tuple[str, ...]:
        return tuple(w for w in self.path if not w.startswith("<"))

    @property
    def usage(self) -> str:
        return " ".join(self.path)


@dataclass
class Match:
    spec: CommandSpec | None = None
    argv: list[str] = field(default_factory=list)
    error: str | None = None
    marker: int | None = None
    candidates: list[str] = field(default_factory=list)


class ParseError(Exception):
    def __init__(self, message: str, marker: int | None = None, candidates: list[str] | None = None):
        super().__init__(message)
        self.message = message
        self.marker = marker
        self.candidates = candidates or []


class CommandParser:
    def __init__(self, specs: Iterable[CommandSpec]):
        self.specs = list(specs)

    # --- tokenising ------------------------------------------------------
    @staticmethod
    def tokenize(line: str) -> list[str]:
        try:
            return shlex.split(line, comments=False, posix=True)
        except ValueError:
            return line.split()

    # --- matching --------------------------------------------------------
    def _mode_specs(self, mode: str) -> list[CommandSpec]:
        # Interface ranges share the interface grammar for dispatch, Tab and ?.
        mode = "if" if mode == "ifrange" else mode
        return [s for s in self.specs if mode in s.modes]

    @staticmethod
    def _word_match(token: str, word: str) -> bool:
        return word.lower().startswith(token.lower())

    def resolve(self, tokens: list[str], mode: str) -> Match:
        specs = self._mode_specs(mode)
        if not tokens:
            return Match(error="incomplete", candidates=self.candidates([], mode))

        best: tuple[int, int, int, CommandSpec] | None = None  # (literals, exact, depth, spec)
        for spec in specs:
            pos = 0
            literals = 0
            exact = 0
            ok = True
            for token in tokens:
                if pos >= len(spec.path):
                    if not spec.trailing:
                        ok = False
                    break
                word = spec.path[pos]
                if word.startswith("<"):
                    pos += 1
                    continue
                if not self._word_match(token, word):
                    ok = False
                    break
                literals += 1
                if token.lower() == word.lower():
                    exact += 1
                pos += 1
            if not ok:
                continue
            if len(tokens) < len(spec.path) and not spec.trailing:
                continue  # typed prefix only - reported as incomplete
            if len(tokens) > len(spec.path) and not spec.trailing:
                continue  # too many tokens
            depth = len(spec.path)
            score = (literals, exact, depth, spec)
            if best is None or score[:3] > best[:3]:
                best = score

        if best is None:
            candidates = self.candidates(tokens, mode)
            if candidates:
                return Match(error="ambiguous" if len(candidates) > 1 else "incomplete", candidates=candidates)
            return Match(error="invalid", marker=max(len(tokens) - 1, 0))

        spec = best[3]
        # Cisco-style ambiguity: an abbreviated keyword that could expand to more
        # than one command at this position is rejected even if one path is complete.
        for pos, token in enumerate(tokens[: len(spec.path)]):
            word = spec.path[pos]
            if word.startswith("<") or token.lower() == word.lower():
                continue
            options = set()
            for other in specs:
                if len(other.path) <= pos:
                    continue
                prefix_ok = True
                for earlier in range(pos):
                    previous_word = other.path[earlier]
                    if previous_word.startswith("<"):
                        continue
                    if not self._word_match(tokens[earlier], previous_word):
                        prefix_ok = False
                        break
                if prefix_ok and not other.path[pos].startswith("<") and self._word_match(token, other.path[pos]):
                    options.add(other.path[pos])
            if len(options) > 1:
                return Match(error="ambiguous", candidates=sorted(options))
        argv: list[str] = []
        token_index = 0
        for word in spec.path:
            if token_index >= len(tokens):
                break
            if word.startswith("<"):
                argv.append(tokens[token_index])
            token_index += 1
        if spec.trailing:
            argv.extend(tokens[token_index:])
        if len(tokens) != token_index and not spec.trailing:
            return Match(error="invalid", marker=max(len(tokens) - 1, 0))
        return Match(spec=spec, argv=argv)

    # --- help / completion ----------------------------------------------
    def candidates(self, tokens: list[str], mode: str) -> list[str]:
        """Keywords that may legally follow ``tokens`` in this mode."""
        result: set[str] = set()
        for spec in self._mode_specs(mode):
            if len(tokens) > len(spec.path):
                continue
            ok = True
            for index, token in enumerate(tokens):
                word = spec.path[index]
                if word.startswith("<"):
                    continue
                if not self._word_match(token, word):
                    ok = False
                    break
            if not ok:
                continue
            if len(tokens) < len(spec.path):
                word = spec.path[len(tokens)]
                if word.startswith("<"):
                    result.add(word)
                else:
                    result.add(word)
        return sorted(result)

    def help_for(self, tokens: list[str], mode: str) -> list[tuple[str, str]]:
        """(keyword, help) pairs for ``?`` at the current position.

        When several commands share a keyword, the most generic one (shortest
        path) supplies the description - ``interface`` reads as "select a port",
        not as the SVI or port-channel variant.
        """
        rows: dict[str, tuple[int, str]] = {}
        for spec in self._mode_specs(mode):
            if len(tokens) > len(spec.path):
                continue
            ok = True
            for index, token in enumerate(tokens):
                word = spec.path[index]
                if word.startswith("<"):
                    continue
                if not self._word_match(token, word):
                    ok = False
                    break
            if not ok or len(tokens) >= len(spec.path):
                continue
            word = spec.path[len(tokens)]
            candidate = (len(spec.path), spec.help or ("value" if word.startswith("<") else ""))
            current = rows.get(word)
            if current is None or candidate[0] < current[0]:
                rows[word] = candidate
        return sorted((word, help_text) for word, (_, help_text) in rows.items())

    def complete(self, text: str, line: str, mode: str) -> list[str]:
        tokens = self.tokenize(line)
        if line.endswith(" ") or not tokens:
            prefix = ""
            context = tokens
        else:
            prefix = tokens[-1]
            context = tokens[:-1]
        options = [c for c in self.candidates(context, mode) if c.lower().startswith(prefix.lower())]
        return [c.strip("<>") for c in options]


def split_interface_arg(value: str) -> str | None:
    """``interface ethernet 1/1`` yields two tokens; join them back."""
    return value
