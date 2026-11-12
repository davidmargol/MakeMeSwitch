"""The SSH line editor must feel like a plain console: editable line, arrows,
history, backspace, Ctrl keys — and never leak escape sequences onto the line."""
from __future__ import annotations

import io
from types import SimpleNamespace

from makemeswitch.core.ssh_server import SwitchCLISession


class _Stdout:
    def __init__(self, sink: io.StringIO) -> None:
        self.sink = sink

    def write(self, data: str) -> None:
        self.sink.write(data)


class _StubProcess:
    def __init__(self) -> None:
        self.out = io.StringIO()
        self.stdout = _Stdout(self.out)

    def get_terminal_size(self) -> tuple[int, int]:
        return 120, 40


def make_editor(prompt: str = "Switch# ") -> tuple[SwitchCLISession, io.StringIO]:
    editor = SwitchCLISession(_StubProcess(), "admin", "admin", log=lambda message: None)
    editor._sink = io.StringIO()
    editor.session = SimpleNamespace(prompt=prompt, running=True, console=None)
    return editor, editor._sink


def feed(editor: SwitchCLISession, text: str) -> None:
    """Push raw keystrokes through the same state machine the channel uses."""
    for char in text:
        if editor._escape is not None:
            editor._escape += char
            if len(editor._escape) >= 2 and (editor._escape[-1].isalpha() or editor._escape[-1] == "~"):
                editor._handle_escape(editor._escape)
                editor._escape = None
            elif len(editor._escape) > 8:
                editor._escape = None
            continue
        if char in ("\r", "\n"):
            editor.submit()
        elif char in ("\x7f", "\b"):
            editor._delete_before()
        elif char == "\t":
            editor.complete()
        elif char == "?":
            editor.show_help()
        elif char == "\x1b":
            editor._escape = char
        elif char < " ":
            editor._handle_control(char)
        elif char.isprintable():
            editor._insert(char)


def test_typing_echoes_nothing_but_the_characters():
    editor, _sink = make_editor()
    editor.show_prompt()
    feed(editor, "show version")
    echoed = editor.process.stdout.sink.getvalue()
    assert editor.buffer == "show version"
    assert echoed.endswith("show version")
    # redraws may move the cursor, but no terminal reply (CPR) or stray key
    # escape may ever reach the command line
    assert ";1R" not in echoed
    assert "[D" not in editor.buffer


def test_arrow_keys_do_not_leak_into_the_command_line():
    editor, sink = make_editor()
    editor.show_prompt()
    feed(editor, "abc\x1b[D\x1b[DX")  # two cursor-lefts, then insert X
    assert editor.buffer == "aXbc"
    assert "\x1b[D" not in editor.buffer
    assert "[D" not in editor.buffer
    assert sink.getvalue().count("\x07") == 0


def test_backspace_and_delete_edit_in_place():
    editor, _ = make_editor()
    editor.show_prompt()
    feed(editor, "shw\x7fow")          # fix a typo mid-word
    assert editor.buffer == "show"
    feed(editor, "\x1b[D\x1b[D\x1b[D\x1b[3~")  # cursor after "s", delete "h"
    assert editor.buffer == "sow"


def test_up_arrow_walks_history():
    editor, _ = make_editor()
    editor.show_prompt()
    editor.history = ["show version", "show interfaces"]
    editor.hindex = len(editor.history)
    feed(editor, "\x1b[A")
    assert editor.buffer == "show interfaces"
    feed(editor, "\x1b[A")
    assert editor.buffer == "show version"
    feed(editor, "\x1b[B")
    assert editor.buffer == "show interfaces"
    feed(editor, "\x1b[B")
    assert editor.buffer == ""


def test_ctrl_u_and_ctrl_k_kill_the_line():
    editor, _ = make_editor()
    editor.show_prompt()
    feed(editor, "configure terminal")
    feed(editor, "\x01")               # Ctrl-A -> start of line
    feed(editor, "\x0b")               # Ctrl-K -> kill to end
    assert editor.buffer == ""
    feed(editor, "show run")
    feed(editor, "\x15")               # Ctrl-U -> kill whole line
    assert editor.buffer == ""


def test_ctrl_c_abandons_the_line_without_closing_the_session():
    editor, _sink = make_editor()
    editor.show_prompt()
    feed(editor, "reload")
    feed(editor, "\x03")
    assert editor.buffer == ""
    assert editor.session.running is True
    assert "^C" in editor.process.stdout.sink.getvalue()


def test_ctrl_d_closes_only_on_an_empty_line():
    editor, _ = make_editor()
    editor.show_prompt()
    feed(editor, "x\x04")              # non-empty: Ctrl-D deletes forward, no exit
    assert editor.session.running is True
    feed(editor, "\x7f")               # erase the last character
    feed(editor, "\x04")               # empty line: leaves the CLI
    assert editor.session.running is False


def test_cursor_is_where_the_next_character_lands():
    editor, _ = make_editor()
    editor.show_prompt()
    feed(editor, "show interface")
    feed(editor, "\x1b[D" * 3)
    feed(editor, "X")
    assert editor.buffer == "show interfXace"
