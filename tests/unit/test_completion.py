"""Completion uses the same mode grammar as dispatch, with real VLAN values."""
import pytest
from prompt_toolkit.completion import CompleteEvent
from prompt_toolkit.document import Document
from makemeswitch.cli.completion import SwitchCompleter
from makemeswitch.cli.modes import Session
from makemeswitch.cli.shell import PARSER, execute_line


def completions(engine, mode, line):
    session = Session(engine=engine, role="admin", interactive=False)
    execute_line(session, "enable")
    execute_line(session, "configure terminal")
    execute_line(session, "interface " + ("range Ethernet1-2" if mode == "ifrange" else "Ethernet1"))
    assert session.mode == mode
    return list(SwitchCompleter(PARSER, session).get_completions(Document(line), CompleteEvent(completion_requested=True)))


@pytest.mark.parametrize("mode", ["if", "ifrange"])
def test_access_vlan_tab_lists_configured_ids_once(engine, mode):
    assert [c.text for c in completions(engine, mode, "switchport access vlan ")] == ["1", "10", "20"]


@pytest.mark.parametrize("mode", ["if", "ifrange"])
def test_switchport_mode_values(engine, mode):
    assert [c.text for c in completions(engine, mode, "switchport mode ")] == ["access", "trunk"]


@pytest.mark.parametrize("mode", ["if", "ifrange"])
@pytest.mark.parametrize("line, expected", [
    ("switchport ", ["access", "attach", "mode", "trunk"]),
    ("sw acc v 2", ["20"]),
    ("switchport trunk native vlan ", ["1", "10", "20"]),
    ("switchport trunk allowed vlan ", ["1", "10", "20", "add", "all", "none", "remove"]),
    ("sw tr al vl add ", ["1", "10", "20"]),
    ("switchport trunk allowed vlan remove 2", ["20"]),
])
def test_context_values(engine, mode, line, expected):
    items = completions(engine, mode, line)
    assert [c.text for c in items] == expected
    if line.endswith("2"):
        assert all(c.start_position == -1 for c in items)


def test_no_switchport_in_exec_or_global_config(engine):
    session = Session(engine=engine)
    completer = SwitchCompleter(PARSER, session)
    for mode in ("exec", "priv", "config", "vlan", "svi"):
        session.mode = mode
        assert list(completer.get_completions(Document("switchport "), CompleteEvent())) == []


def test_range_help_shares_interface_grammar():
    assert PARSER.help_for(["switchport"], "ifrange") == PARSER.help_for(["switchport"], "if")
