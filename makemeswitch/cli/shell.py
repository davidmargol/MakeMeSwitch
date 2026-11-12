"""Interactive Cisco-inspired shell (prompt_toolkit front-end)."""
from __future__ import annotations

import argparse
import io
import os
import re
import sys
from pathlib import Path

from prompt_toolkit import PromptSession
from prompt_toolkit.application import run_in_terminal
from prompt_toolkit.history import FileHistory, InMemoryHistory
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.shortcuts import CompleteStyle
from prompt_toolkit.styles import Style
from rich.console import Console

from makemeswitch import PROJECT_NAME, __version__
from makemeswitch.cli import pipe as pipe_filters
from makemeswitch.cli.commands import COMMANDS
from makemeswitch.cli.completion import SwitchCompleter
from makemeswitch.cli.modes import MODE_PROMPT, Session
from makemeswitch.cli.parser import CommandParser
from makemeswitch.core.engine import Engine, load_settings
from makemeswitch.core.models import ConfigError
from makemeswitch.utils.logging import setup_logging

PROMPT_STYLE = Style.from_dict(
    {
        "prompt": "ansicyan bold",
        "prompt-config": "ansiyellow bold",
        "banner": "ansibrightcyan",
    }
)

PARSER = CommandParser(COMMANDS)

INTERFACE_WORDS = {"ethernet", "eth", "et", "e", "gigabitethernet", "gi"}

# Commands that carry a credential must never be written to the on-disk shell
# history: the history file is plain text and readable by the account that ran
# the CLI.
_SENSITIVE_HISTORY = re.compile(r"(^|\s)(secret|password)\s+\S", re.I)


class SafeFileHistory(FileHistory):
    def store_string(self, string: str) -> None:  # noqa: D401 - prompt_toolkit hook
        if _SENSITIVE_HISTORY.search(string):
            return
        super().store_string(string)


def _scrub_history(path: Path) -> None:
    """Remove credential-bearing lines left by older releases."""
    try:
        lines = path.read_text(errors="replace").splitlines()
    except OSError:
        return
    kept = [line for line in lines if not _SENSITIVE_HISTORY.search(line)]
    if len(kept) != len(lines):
        try:
            path.write_text("\n".join(kept) + ("\n" if kept else ""))
            path.chmod(0o600)
        except OSError:
            pass


def _join_interface_tokens(tokens: list[str]) -> list[str]:
    """Turn ``interface ethernet 1/1`` into ``interface ethernet1/1``.

    Cisco users type the interface either as one token (``Ethernet1/1``) or as
    two (``ethernet 1/1``); the parser only needs the former.
    """
    out: list[str] = []
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if (
            token.lower() in ("interface", "int")
            and index + 2 < len(tokens)
            and tokens[index + 1].lower() in INTERFACE_WORDS
            and re.fullmatch(r"\d+(/\d+)?", tokens[index + 2])
        ):
            out.extend([token, tokens[index + 1] + tokens[index + 2]])
            index += 3
            continue
        out.append(token)
        index += 1
    return out


def _help_rows(session: Session, line: str) -> list[tuple[str, str]]:
    return PARSER.help_for(_join_interface_tokens(PARSER.tokenize(line)), session.mode)


def _key_bindings(session: Session) -> KeyBindings:
    """Cisco behaviour: ``?`` prints context help immediately, without Enter."""
    bindings = KeyBindings()

    @bindings.add("?")
    def _context_help(event) -> None:  # type: ignore[no-untyped-def]
        line = event.current_buffer.document.text_before_cursor
        rows = pipe_filters.filter_candidates(line) if "|" in line else _help_rows(session, line)

        def render_help() -> None:
            if not rows:
                session.console.print("  <no matching commands>", markup=False)
                return
            width = max(len(word) for word, _ in rows) + 2
            for word, help_text in rows:
                session.console.print(f"  {word.ljust(width)}{help_text}", markup=False)

        run_in_terminal(render_help)

    return bindings


def _caret_error(session: Session, line: str, tokens: list[str], message: str) -> None:
    session.error(message)
    if not tokens:
        return
    marker = " " * len(" ".join(tokens[:-1]))
    if marker:
        marker += " "
    session.console.print(f"  {line}\n  {marker}^", markup=False, highlight=False)


# Commands that must run once while in ``interface range`` mode instead of being
# fanned out to every member.
_RANGE_CONTROL = {"cmd_interface", "cmd_exit", "cmd_end", "cmd_do", "cmd_help"}


def _resolve_mode(session: Session) -> str:
    """Range mode behaves exactly like interface mode for command lookup."""
    return "if" if session.mode == "ifrange" else session.mode


def _run_for_range(session: Session, spec, argv: list[str]) -> None:
    """Apply one command to every interface of the range, like IOS does."""
    members = list(session.range_members)
    failures = 0
    for member in members:
        session.interface = member
        # Interface handlers test `session.mode == "if"`, so run each iteration
        # in interface context and restore the range afterwards.
        session.mode = "if"
        try:
            spec.handler(session, argv)
        except ConfigError as exc:
            failures += 1
            session.error(f"{member}: {exc.message}")
        except Exception as exc:  # noqa: BLE001 - one port must not abort the rest
            failures += 1
            session.error(f"{member}: command failed: {exc}")
        session.mode = "ifrange"
    session.interface = members[0] if members else None
    if failures and not session.last_error:
        session.error(f"{failures} of {len(members)} interfaces failed")


def execute_line(session: Session, line: str) -> None:
    """Dispatch one line, honouring Cisco-style output filters after ``|``."""
    session.last_error = None
    stripped = line.rstrip()

    # ``sh run | ?`` lists the filters, like IOS does.
    if "|" in stripped and stripped.endswith("?"):
        rows = pipe_filters.filter_candidates(stripped[:-1].rstrip())
        if not rows:
            session.error("no matching pipe filters")
            return
        width = max(len(word) for word, _ in rows) + 2
        for word, help_text in rows:
            session.console.print(f"  {word.ljust(width)}{help_text}", markup=False)
        return

    try:
        command_text, filters = pipe_filters.split_pipe(line)
    except ConfigError as exc:
        session.error(exc.message)
        return

    if filters is None:
        _execute_plain(session, line)
        return

    if not filters:
        session.error("incomplete command")
        return

    captured = _run_captured(session, command_text)
    if captured is None:
        return  # the command failed and already printed its own error
    try:
        result = pipe_filters.apply_filters(captured.splitlines(), filters)
    except ConfigError as exc:
        session.error(exc.message)
        return
    if result is None:  # redirected away, nothing to show
        return
    for out_line in result.splitlines():
        session.console.print(out_line, markup=False, highlight=False)


def _run_captured(session: Session, line: str) -> str | None:
    """Run a command with its output captured instead of printed."""
    buffer = io.StringIO()
    original = session.console
    session.console = Console(
        file=buffer,
        width=original.width or 80,
        no_color=True,
        highlight=False,
        force_terminal=False,
        soft_wrap=False,
    )
    try:
        _execute_plain(session, line)
    finally:
        session.console = original
    if session.last_error:
        session.error(session.last_error)
        return None
    return buffer.getvalue()


def _execute_plain(session: Session, line: str) -> None:
    tokens = _join_interface_tokens(PARSER.tokenize(line))
    session.last_command_tokens = tokens
    if not tokens:
        return

    if line.rstrip().endswith("?"):
        prefix = tokens[:-1] if tokens and tokens[-1] == "?" else tokens
        rows = PARSER.help_for(prefix, _resolve_mode(session))
        if not rows:
            session.error("no matching commands")
            return
        width = max(len(word) for word, _ in rows) + 2
        for word, help_text in rows:
            session.console.print(f"  {word.ljust(width)}{help_text}", markup=False)
        return

    match = PARSER.resolve(tokens, _resolve_mode(session))
    if match.error == "ambiguous":
        session.error(f"ambiguous command: '{tokens[-1]}'")
        session.console.print("  candidates: " + ", ".join(match.candidates), markup=False)
        return
    if match.error == "incomplete":
        session.error("incomplete command")
        if match.candidates:
            session.console.print("  available: " + ", ".join(match.candidates[:12]), markup=False)
        return
    if match.error == "invalid" or match.spec is None:
        _caret_error(session, line, tokens, "invalid input detected")
        return

    spec = match.spec
    if not session.can(spec.privilege):
        session.error("authorization failed: insufficient privilege")
        return
    if session.mode == "ifrange" and spec.handler.__name__ not in _RANGE_CONTROL:
        _run_for_range(session, spec, match.argv)
        return
    try:
        spec.handler(session, match.argv)
    except ConfigError as exc:
        session.error(exc.message)
    except KeyboardInterrupt:
        session.console.print("")
        session.error("command aborted")
    except Exception as exc:  # noqa: BLE001 - never crash the shell on a command
        session.error(f"command failed: {exc}")


def _banner(session: Session) -> None:
    cfg = session.engine.config
    if cfg.banner.enabled and cfg.banner.login:
        session.console.print(cfg.banner.login, markup=False, highlight=False)
        session.console.print("")
    session.console.print(f"[bold cyan]{PROJECT_NAME} v{__version__}[/bold cyan]")
    session.console.print("Type '?' for help, 'enable' to enter privileged mode.", markup=False)
    if cfg.banner.enabled and cfg.banner.motd:
        session.console.print("")
        session.console.print(cfg.banner.motd, markup=False, highlight=False)


def _read_multiline_banner(prompt_session: PromptSession, session: Session, line: str) -> str:
    """Collect a Cisco ``banner login ^C`` block until the delimiter line."""
    match = re.match(r"^\s*banner\s+(login|motd)\s+(\S+)(.*)$", line, re.I)
    if not match or session.mode != "config":
        return line
    kind, delimiter, rest = match.group(1).lower(), match.group(2), match.group(3)
    if delimiter in rest:
        return line
    body = [rest] if rest else []
    session.console.print(f"Enter TEXT message. End with the character '{delimiter}'.", markup=False)
    while True:
        try:
            entry = prompt_session.prompt("")
        except (EOFError, KeyboardInterrupt):
            session.error("banner input aborted")
            return ""
        if entry.strip() == delimiter:
            break
        body.append(entry)
    session.pending_banner = (kind, "\n".join(body))
    return f"banner {kind}"


def run_cli(
    engine: Engine | None = None,
    username: str | None = None,
    role: str | None = None,
    command: list[str] | None = None,
    auto_confirm: bool = False,
    banner: bool = True,
    input=None,
    output=None,
    console_file=None,
    sync_ports: bool = True,
) -> int:
    settings = engine.settings if engine else load_settings()
    setup_logging("makemeswitch.cli", settings.get("log_dir"), os.environ.get("MMS_LOG_LEVEL", "INFO"))
    engine = engine or Engine(settings=settings)
    if sync_ports:
        try:
            engine.sync_ports()
        except Exception as exc:  # noqa: BLE001
            engine.store.add_event("warning", "cli", f"Port scan failed: {exc}")

    session = Session(
        engine=engine,
        username=username or os.environ.get("SUDO_USER") or os.environ.get("USER", "admin"),
        role=role or os.environ.get("MMS_ROLE", "admin"),
        interactive=command is None,
        auto_confirm=auto_confirm,
    )
    if console_file is not None:
        # SSH sessions must write into the channel, not into the service log.
        session.console = Console(
            file=console_file,
            force_terminal=True,
            color_system=None,
            width=100,
            markup=True,
            highlight=False,
            soft_wrap=False,
        )
    if command:
        for line in command:
            execute_line(session, line)
            if session.last_error:
                session.exit_code = 1
        return session.exit_code

    if banner:
        _banner(session)
        if os.geteuid() != 0:
            session.console.print(
                "[yellow]Note: configuration changes require root (sudo makemeswitch).[/yellow]"
            )
        if not engine.ovs.installed:
            session.console.print("[yellow]Warning: Open vSwitch tools not found - switching is unavailable.[/yellow]")

    history_path = settings.get("cli", {}).get("history_file", str(Path.home() / ".makemeswitch_history"))
    history = InMemoryHistory()
    try:
        Path(history_path).parent.mkdir(parents=True, exist_ok=True)
        _scrub_history(Path(history_path))
        # Prove the file is writable now: a read-only HOME (service accounts,
        # SSH sessions) used to crash the whole prompt on the first command.
        with open(history_path, "ab"):
            pass
        history = SafeFileHistory(history_path)
    except OSError:
        history = InMemoryHistory()

    prompt_session: PromptSession = PromptSession(
        input=input,
        output=output,
        history=history,
        completer=SwitchCompleter(PARSER, session),
        complete_while_typing=True,
        complete_style=CompleteStyle.READLINE_LIKE,
        key_bindings=_key_bindings(session),
        style=PROMPT_STYLE,
        enable_history_search=True,
    )
    session.ask = lambda message: prompt_session.prompt(message)

    while session.running:
        style_class = "class:prompt-config" if session.mode in ("config", "if", "vlan", "pc", "svi") else "class:prompt"
        try:
            line = prompt_session.prompt([(style_class, session.prompt + " ")])
        except KeyboardInterrupt:
            session.console.print("^C", markup=False)
            continue
        except EOFError:
            break
        try:
            line = _read_multiline_banner(prompt_session, session, line)
            if line:
                execute_line(session, line)
        except KeyboardInterrupt:
            session.console.print("")
    session.console.print("")
    return session.exit_code


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="makemeswitch-shell", description="MakeMeSwitch interactive CLI")
    parser.add_argument("-c", "--command", action="append", help="run command(s) and exit")
    parser.add_argument("--user", help="username (web terminal)")
    parser.add_argument("--role", choices=("admin", "operator", "readonly"), help="authorization role")
    parser.add_argument("--yes", action="store_true", help="auto-confirm disruptive changes")
    parser.add_argument("--no-banner", action="store_true")
    args = parser.parse_args(argv)
    return run_cli(
        username=args.user,
        role=args.role,
        command=args.command,
        auto_confirm=args.yes,
        banner=not args.no_banner,
    )


if __name__ == "__main__":
    sys.exit(main())
