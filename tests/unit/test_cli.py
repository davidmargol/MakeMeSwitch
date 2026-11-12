"""CLI end-to-end test against the fake OVS backend."""
import io

from makemeswitch.cli.modes import Session
from makemeswitch.cli.shell import execute_line
from makemeswitch.core.render import render_running_config
from rich.console import Console


def make_session(engine):
    session = Session(engine=engine, username="tester", role="admin", interactive=False, auto_confirm=True)
    session.console = Console(file=io.StringIO(), force_terminal=False, width=120)
    return session


def run(session, *lines):
    for line in lines:
        execute_line(session, line)


def test_full_configuration_session(engine):
    session = make_session(engine)
    run(
        session,
        "enable",
        "configure terminal",
        "hostname CLI-SW",
        "vlan 40",
        "name MANAGEMENT",
        "exit",
        "interface ethernet 1/1",
        "description cli edited",
        "switchport access vlan 20",
        "end",
        "write memory",
    )
    assert session.last_error is None
    assert engine.config.hostname == "CLI-SW"
    assert 40 in engine.config.vlans
    assert engine.config.vlans[40].name == "MANAGEMENT"
    assert engine.config.interfaces["Ethernet1"].access_vlan == 20
    assert engine.config.interfaces["Ethernet1"].description == "cli edited"
    assert not engine.unsaved_changes()
    text = render_running_config(engine.config)
    assert "hostname CLI-SW" in text
    assert "switchport access vlan 20" in text
    assert "vlan 40" in text


def test_mode_prompt_and_exit(engine):
    session = make_session(engine)
    run(session, "enable", "configure terminal")
    assert session.mode == "config"
    run(session, "vlan 20")
    assert session.mode == "vlan" and session.vlan_id == 20
    run(session, "end")
    assert session.mode == "priv"
    run(session, "exit")
    assert session.mode == "exec"


def test_trunk_allowed_all_and_none_are_distinct(engine, fake_ovs):
    from makemeswitch.cli.commands import cmd_switchport_trunk_allowed

    session = make_session(engine)
    run(session, "enable", "configure terminal", "interface ethernet 1/3")
    assert session.interface == "Ethernet3"

    cmd_switchport_trunk_allowed(session, ["all"])
    assert session.last_error is None
    iface = engine.config.interfaces["Ethernet3"]
    assert iface.trunk_allowed == list(range(1, 4095))
    assert fake_ovs.port_details()["eth11"]["trunks"] == []  # OVS empty == all
    assert fake_ovs.port_details()["eth11"]["vlan_mode"] == "trunk"

    cmd_switchport_trunk_allowed(session, ["none"])
    assert session.last_error is None
    assert engine.config.interfaces["Ethernet3"].trunk_allowed == []
    assert "eth11" not in fake_ovs.port_details()  # none != all
    assert engine.config.interfaces["Ethernet3"].attached

    cmd_switchport_trunk_allowed(session, ["add", "10"])
    assert engine.config.interfaces["Ethernet3"].trunk_allowed == [10]
    assert fake_ovs.port_details()["eth11"]["trunks"] == [10]


def test_invalid_command_sets_error(engine):
    session = make_session(engine)
    run(session, "enable", "configure terminal", "hostname -bad-")
    assert session.last_error is not None
    assert "hostname" in session.last_error.lower() or "invalid" in session.last_error.lower()
    run(session, "hostname GOOD-NAME")
    assert session.last_error is None
    assert engine.config.hostname == "GOOD-NAME"


def test_readonly_cannot_enter_config(engine):
    session = make_session(engine)
    session.role = "readonly"
    run(session, "enable", "configure terminal")
    assert session.mode == "priv"
    assert "authorization" in (session.last_error or "")


def test_cli_change_is_visible_in_store(engine):
    session = make_session(engine)
    run(session, "enable", "configure terminal", "vlan 50", "name FIFTY", "end")
    reloaded = engine.store.load_config("running")
    assert reloaded is not None
    assert 50 in reloaded.vlans
    assert reloaded.vlans[50].name == "FIFTY"


def test_interface_spellings_and_do_from_config(engine):
    session = make_session(engine)
    run(session, "en", "conf t", "int eth 1/1", "description set via pty-style input")
    assert session.last_error is None
    assert session.mode == "if"
    assert session.interface == "Ethernet1"

    run(session, "do show run int eth1/1")
    assert session.last_error is None
    assert session.mode == "if", "do must not change the configuration mode"
    assert "interface Ethernet1" in session.console.file.getvalue()


def test_abbreviated_commands(engine):
    session = make_session(engine)
    run(session, "en", "conf t", "vl 60", "na ABBREV", "ex", "int e1/2", "sw mo acc", "sw acc vl 60")
    assert session.last_error is None
    assert engine.config.vlans[60].name == "ABBREV"
    assert engine.config.interfaces["Ethernet2"].access_vlan == 60


def test_interface_token_join():
    from makemeswitch.cli.shell import _join_interface_tokens

    assert _join_interface_tokens(["interface", "ethernet", "1/1"]) == ["interface", "ethernet1/1"]
    assert _join_interface_tokens(["show", "run", "int", "eth", "1/2"]) == ["show", "run", "int", "eth1/2"]
    assert _join_interface_tokens(["interface", "Vlan10"]) == ["interface", "Vlan10"]
