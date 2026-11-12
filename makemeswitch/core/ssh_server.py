"""Switch CLI over SSH (port 22), with the host's own sshd moved to 8022.

Design notes
------------
* Authentication uses the *appliance* user database — the same accounts as the
  web panel and the local console — never the OS accounts (those stay on 8022).
* The CLI is a small **line editor implemented here**, driven directly by the
  SSH channel. Two earlier designs failed on real hardware:
  - spawning ``makemeswitch.cli.shell`` on a PTY and bridging bytes produced
    cursor-position garbage (``;1R``) and deadlocked after a few seconds;
  - running prompt_toolkit in a worker thread with a piped input worked for
    plain commands but lost Tab completion over the channel.
  Handling keys ourselves keeps the whole session in one asyncio task: no
  threads, no terminal handshakes, no CPR requests, no backpressure deadlocks.
* No exec, no SFTP/SCP, no local/remote forwarding, no agent or X11 forwarding.
  A logged-in appliance user only ever reaches the CLI, so there is no path from
  a switch account to a root shell.
"""
from __future__ import annotations

import asyncio
import io
import os
from pathlib import Path

import asyncssh
from rich.console import Console

DEFAULT_HOST_KEY = "/etc/makemeswitch/ssh_host_ed25519_key"
DEFAULT_DB = "/var/lib/makemeswitch/makemeswitch.db"

#: privilege -> application role
PRIVILEGE_ROLES = ((15, "admin"), (2, "operator"), (0, "readonly"))


def role_for_privilege(privilege: int) -> str:
    for minimum, role in PRIVILEGE_ROLES:
        if privilege >= minimum:
            return role
    return "readonly"


class SwitchSSHServer(asyncssh.SSHServer):
    """Authentication against the appliance user database."""

    def __init__(self, db_path: str | None = None, log=None) -> None:
        self.db_path = db_path or os.environ.get("MMS_DB", DEFAULT_DB)
        self.log = log or (lambda message: None)
        self.username: str | None = None
        self.peer: str = "?"
        self.privilege: int = 0

    # -- connection ------------------------------------------------------
    def connection_made(self, conn) -> None:
        self.peer = conn.get_extra_info("peername", ("?", 0))[0]

    def begin_auth(self, username: str) -> bool:
        from makemeswitch.core.store import Store

        if not Store(self.db_path).get_user(username):
            self.log(f"ssh: unknown user {username!r} from {self.peer}")
            return False
        self.username = username
        return True

    def password_auth_supported(self) -> bool:
        return True

    def validate_password(self, username: str, password: str) -> bool:
        from makemeswitch.core.security import SecurityService, role_to_privilege
        from makemeswitch.core.store import Store

        user = SecurityService(Store(self.db_path)).authenticate(username, password, self.peer)
        if not user:
            self.log(f"ssh: failed password for {username!r} from {self.peer}")
            return False
        privilege = role_to_privilege(user.get("role") or "readonly")
        self.username = user["username"]
        self.privilege = privilege
        self.log(f"ssh: {self.username} logged in from {self.peer} (privilege {privilege})")
        return True

    # -- refused capabilities -------------------------------------------
    def public_key_auth_supported(self) -> bool:
        return False

    def keyboard_interactive_auth_supported(self) -> bool:
        return False

    def kbdint_auth_supported(self) -> bool:  # asyncssh spelling
        return False

    def session_requested(self):
        # The channel itself is served by the process_factory registered on
        # create_server(); here we only accept the session request.
        return True

    def connection_requested(self, dest_host, dest_port, orig_host, orig_port):
        self.log(f"ssh: refused direct-tcpip {dest_host}:{dest_port}")
        return False

    def server_requested(self, listen_host, listen_port):
        self.log(f"ssh: refused remote forwarding to {listen_host}:{listen_port}")
        return False

    def agent_requested(self, request):
        return False

    def x11_requested(self, single_connection, auth_protocol, auth_cookie, screen_number):
        return False


# --- line editor -----------------------------------------------------------

class SwitchCLISession:
    """One interactive CLI session bound to an SSH channel."""

    def __init__(self, process, username: str, role: str, log=None) -> None:
        self.process = process
        self.username = username
        self.role = role
        self.log = log or (lambda message: None)
        self.buffer = ""
        self.cursor = 0
        self.history: list[str] = []
        self.hindex = 0
        self._escape: str | None = None
        self._sink = io.StringIO()

    # -- output ---------------------------------------------------------
    completer: object | None = None

    def _console(self) -> Console:
        return Console(file=self._sink, force_terminal=False, width=self._width(),
                       markup=True, highlight=False, soft_wrap=False)

    def _width(self) -> int:
        try:
            columns, _rows = self.process.get_terminal_size()
            return max(40, min(int(columns), 200))
        except Exception:  # noqa: BLE001
            return 100

    def write(self, text: str) -> None:
        if text:
            self.process.stdout.write(text)

    def write_lines(self, text: str) -> None:
        # Normalise first: Rich already emits LF, but some paths hand us CRLF and
        # a blind replace would produce CR CR LF (a stray blank line per command).
        normalised = text.replace("\r\n", "\n").replace("\r", "\n")
        self.write(normalised.replace("\n", "\r\n"))

    def flush_console(self) -> None:
        text = self._sink.getvalue()
        if text:
            self._sink.seek(0)
            self._sink.truncate(0)
            self.write_lines(text)

    # -- prompt / line rendering -----------------------------------------
    def prompt(self) -> str:
        text = self.session.prompt
        return text if text.endswith(" ") else text + " "

    def show_prompt(self) -> None:
        self.cursor = 0
        self.write(self.prompt() + self.buffer)

    def redraw(self) -> None:
        """Repaint only the current input line, keeping the cursor in place."""
        tail = len(self.buffer) - self.cursor
        out = "\r\x1b[K" + self.prompt() + self.buffer
        if tail > 0:
            out += f"\x1b[{tail}D"
        self.write(out)

    def _insert(self, text: str) -> None:
        if not text:
            return
        self.buffer = self.buffer[: self.cursor] + text + self.buffer[self.cursor :]
        self.cursor += len(text)
        self.redraw()

    def _delete_before(self) -> None:
        if self.cursor == 0:
            self.write("\x07")
            return
        self.buffer = self.buffer[: self.cursor - 1] + self.buffer[self.cursor :]
        self.cursor -= 1
        self.redraw()

    def _delete_at(self) -> None:
        if self.cursor >= len(self.buffer):
            self.write("\x07")
            return
        self.buffer = self.buffer[: self.cursor] + self.buffer[self.cursor + 1 :]
        self.redraw()

    # -- history ---------------------------------------------------------
    def _history_step(self, delta: int) -> None:
        if not self.history:
            self.write("\x07")
            return
        target = self.hindex + delta
        if target < 0 or target > len(self.history):
            self.write("\x07")
            return
        self.hindex = target
        self.buffer = "" if target == len(self.history) else self.history[target]
        self.cursor = len(self.buffer)
        self.redraw()

    # -- completion ------------------------------------------------------
    def complete(self) -> None:
        from prompt_toolkit.completion import CompleteEvent
        from prompt_toolkit.document import Document

        text = self.buffer
        word = "" if text.endswith(" ") or not text else text.split(" ")[-1]
        try:
            completions = list(self.completer.get_completions(Document(text, len(text)), CompleteEvent()))
        except Exception as exc:  # noqa: BLE001
            self.log(f"ssh: completion failed: {exc}")
            completions = []
        if not completions:
            self.write("\x07")
            return
        if len(completions) == 1:
            completion = completions[0]
            self.buffer = text[: len(text) + completion.start_position] + completion.text
            self.cursor = len(self.buffer)
            self.redraw()
            return
        common = os.path.commonprefix([c.text for c in completions])
        if len(common) > len(word):
            start = len(text) + completions[0].start_position
            self.buffer = text[:start] + common
            self.cursor = len(self.buffer)
            self.redraw()
            return
        self.write("\x07\r\n")
        rows = [(c.text, c.display_meta_text or "") for c in completions]
        width = max((len(name) for name, _ in rows), default=0) + 2
        for name, meta in rows:
            self.write(f"  {name.ljust(width)}{meta}\r\n")
        self.redraw()

    def show_help(self) -> None:
        from makemeswitch.cli import pipe as pipe_filters
        from makemeswitch.cli.shell import _help_rows

        rows = pipe_filters.filter_candidates(self.buffer) if "|" in self.buffer else _help_rows(self.session, self.buffer)
        self.write("\r\n")
        if not rows:
            self.write("  <no matching commands>\r\n")
        else:
            width = max(len(word) for word, _ in rows) + 2
            for word, help_text in rows:
                self.write(f"  {word.ljust(width)}{help_text}\r\n")
        self.redraw()

    # -- command execution ------------------------------------------------
    def submit(self) -> None:
        line = self.buffer.strip()
        self.write("\r\n")
        self.buffer = ""
        self.cursor = 0
        self.hindex = len(self.history)
        if not line:
            self.show_prompt()
            return
        from makemeswitch.cli.shell import execute_line

        if not self.history or self.history[-1] != line:
            self.history.append(line)
            self.hindex = len(self.history)
        try:
            execute_line(self.session, line)
        except Exception as exc:  # noqa: BLE001 - a bad command never kills the session
            self.log(f"ssh: command {line!r} raised: {exc}")
            self.write_lines(f"% internal error: {exc}\r\n")
        self.flush_console()
        if not self.session.running:
            return
        self.show_prompt()

    # -- escape sequences -------------------------------------------------
    def _handle_escape(self, sequence: str) -> None:
        if sequence.endswith("A"):
            self._history_step(-1)
        elif sequence.endswith("B"):
            self._history_step(1)
        elif sequence.endswith("C"):
            if self.cursor < len(self.buffer):
                self.cursor += 1
                self.write("\x1b[C")
            else:
                self.write("\x07")
        elif sequence.endswith("D"):
            if self.cursor > 0:
                self.cursor -= 1
                self.write("\x1b[D")
            else:
                self.write("\x07")
        elif sequence.endswith("H") or sequence.endswith("1~") or sequence.endswith("7~"):
            self.cursor = 0
            self.redraw()
        elif sequence.endswith("F") or sequence.endswith("4~") or sequence.endswith("8~"):
            self.cursor = len(self.buffer)
            self.redraw()
        elif sequence.endswith("3~"):
            self._delete_at()
        # everything else (PgUp/PgDn/F-keys) is ignored on purpose

    def _handle_control(self, char: str) -> bool:
        """Handle Ctrl-<key>. Returns True when the key was consumed."""
        if char == "\x03":  # Ctrl-C: abandon the line, like IOS
            self.buffer = ""
            self.cursor = 0
            self.hindex = len(self.history)
            self.write("^C\r\n")
            self.show_prompt()
        elif char == "\x04":  # Ctrl-D: leave the CLI
            if not self.buffer:
                self.write("\r\n")
                self.session.running = False
            else:
                self._delete_at()
        elif char == "\x01":  # Ctrl-A
            self.cursor = 0
            self.redraw()
        elif char == "\x05":  # Ctrl-E
            self.cursor = len(self.buffer)
            self.redraw()
        elif char == "\x15":  # Ctrl-U
            self.buffer = self.buffer[self.cursor :]
            self.cursor = 0
            self.redraw()
        elif char == "\x0b":  # Ctrl-K
            self.buffer = self.buffer[: self.cursor]
            self.redraw()
        elif char == "\x17":  # Ctrl-W
            head = self.buffer[: self.cursor].rstrip()
            cut = head.rfind(" ") + 1
            self.buffer = head[:cut] + self.buffer[self.cursor :]
            self.cursor = cut
            self.redraw()
        elif char == "\x0c":  # Ctrl-L: clear screen, keep the line
            self.write("\x1b[H\x1b[2J")
            self.redraw()
        else:
            return False
        return True

    # -- session bootstrap ------------------------------------------------
    def prepare(self, engine) -> None:
        from makemeswitch.cli.completion import SwitchCompleter
        from makemeswitch.cli.modes import Session
        from makemeswitch.cli.shell import PARSER

        self._sink = io.StringIO()
        self.session = Session(
            engine=engine,
            username=self.username,
            role=self.role,
            interactive=True,
            auto_confirm=False,
            console=self._console(),
        )
        self.completer = SwitchCompleter(PARSER, self.session)

    # -- main loop --------------------------------------------------------
    async def run(self, engine) -> None:
        from makemeswitch.cli.shell import _banner

        self.prepare(engine)
        _banner(self.session)
        self.flush_console()
        self.show_prompt()

        while self.session.running:
            try:
                chunk = await self.process.stdin.read(1024)
            except (asyncssh.Error, OSError):
                break
            if not chunk:
                break
            for char in chunk:
                if not self.session.running:
                    break
                if self._escape is not None:
                    self._escape += char
                    if len(self._escape) >= 2 and (self._escape[-1].isalpha() or self._escape[-1] == "~"):
                        self._handle_escape(self._escape)
                        self._escape = None
                    elif len(self._escape) > 8:
                        self._escape = None
                    continue
                if char in ("\r", "\n"):
                    self.submit()
                elif char in ("\x7f", "\b"):
                    self._delete_before()
                elif char == "\t":
                    self.complete()
                elif char == "?":
                    self.show_help()
                elif char == "\x1b":
                    self._escape = char
                elif char < " ":
                    self._handle_control(char)
                elif char.isprintable():
                    self._insert(char)
        self.log(f"ssh: CLI session for {self.username} ended")


async def run_cli_session(process, username: str, privilege: int, log=None) -> None:
    from makemeswitch.core.engine import Engine, load_settings

    log = log or (lambda message: None)
    role = role_for_privilege(privilege)
    settings = load_settings()
    engine = Engine(settings=settings)
    session = SwitchCLISession(process, username, role, log=log)
    try:
        await session.run(engine)
    except Exception as exc:  # noqa: BLE001
        log(f"ssh: CLI session for {username} crashed: {exc}")
        try:
            process.stdout.write(f"\r\nCLI error: {exc}\r\n")
            process.exit(1)
        except Exception:  # noqa: BLE001
            pass


def _privilege_for(db_path: str, username: str) -> int:
    """Map the account's stored role onto a Cisco privilege level.

    Accounts live in the users table with a ``role`` column; privileges are
    derived from it (an existing install has no per-user privilege column, so
    reading one used to demote every admin to privilege 1 and lock them out of
    ``enable``).
    """
    from makemeswitch.core.security import role_to_privilege
    from makemeswitch.core.store import Store

    user = Store(db_path).get_user(username) or {}
    if not user:
        return 0
    try:
        return role_to_privilege(user.get("role") or "readonly")
    except (TypeError, ValueError):
        return 1


def make_process_factory(db_path: str | None = None, log=None):
    """asyncssh process_factory: one CLI session per accepted channel."""
    log = log or (lambda message: None)

    async def factory(process) -> None:
        if getattr(process, "command", None):
            # ``ssh host <command>`` is never executed: only the CLI is offered.
            process.stderr.write("MakeMeSwitch: command execution over SSH is not permitted.\r\n"
                                 "Log in interactively to use the switch CLI.\r\n")
            process.exit(1)
            return
        username = process.get_extra_info("username") or "admin"
        privilege = _privilege_for(db_path or os.environ.get("MMS_DB", DEFAULT_DB), username)
        await run_cli_session(process, username, privilege, log=log)

    return factory


async def serve(
    host: str = "0.0.0.0",
    port: int = 22,
    host_key: str | None = None,
    db_path: str | None = None,
    log=None,
) -> None:
    log = log or (lambda message: print(f"makemeswitch-ssh: {message}", flush=True))
    key_path = Path(host_key or os.environ.get("MMS_SSH_HOST_KEY", DEFAULT_HOST_KEY))
    if not key_path.exists():
        raise SystemExit(f"missing SSH host key {key_path}; run install.sh again")

    factory = make_process_factory(db_path, log=log)
    while True:
        try:
            await asyncssh.create_server(
                lambda: SwitchSSHServer(db_path=db_path, log=log),
                host,
                port,
                server_host_keys=[str(key_path)],
                process_factory=factory,
                encoding="utf-8",
                # asyncssh ships its own line editor for pty sessions: it
                # swallows Tab (its completion, not ours), re-echoes input and
                # would fight our editor. We do the editing ourselves.
                line_editor=False,
                line_echo=False,
            )
            log(f"serving the switch CLI on {host}:{port}")
            await asyncio.Future()
        except (OSError, asyncssh.Error) as exc:
            log(f"listener failed ({exc}); retrying in 5s")
            await asyncio.sleep(5)


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(prog="makemeswitch-ssh", description="MakeMeSwitch switch CLI over SSH")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=22)
    parser.add_argument("--host-key", default=os.environ.get("MMS_SSH_HOST_KEY", DEFAULT_HOST_KEY))
    parser.add_argument("--db", default=os.environ.get("MMS_DB", DEFAULT_DB))
    args = parser.parse_args(argv)
    try:
        asyncio.run(serve(args.host, args.port, args.host_key, args.db))
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
