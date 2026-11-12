"""Port-channel lifecycle: create first, add members later (Cisco behaviour).

The old behaviour was a deadlock: ``interface port-channel 1`` refused to open a
channel that did not exist, and the channel was only created by adding a member
- while adding the first member tried to build an OVS bond with one interface,
which OVS rejects.
"""
import io

from rich.console import Console

from makemeswitch.cli.modes import Session
from makemeswitch.cli.shell import execute_line


def make_session(engine):
    session = Session(engine=engine, username="tester", role="admin", interactive=False, auto_confirm=True)
    session.console = Console(file=io.StringIO(), force_terminal=False, width=120)
    return session


def run(session, *lines):
    for line in lines:
        execute_line(session, line)
    return session


def bond_calls(ovs):
    return [call for call in ovs.calls if call[0] == "add_bond"]


def test_empty_port_channel_can_be_created_first(engine):
    session = make_session(engine)
    run(session, "enable", "configure terminal", "interface port-channel 1")

    assert session.last_error is None
    assert session.mode == "pc"
    assert session.channel_id == 1
    assert 1 in engine.config.port_channels
    assert engine.config.port_channels[1].members == []
    assert bond_calls(engine.ovs) == []  # nothing to bond yet


def test_interface_po_abbreviation_creates_channel(engine):
    session = make_session(engine)
    run(session, "enable", "configure terminal", "int po2")
    assert session.last_error is None
    assert session.mode == "pc" and session.channel_id == 2
    assert 2 in engine.config.port_channels


def test_single_member_does_not_call_add_bond(engine):
    session = make_session(engine)
    run(
        session,
        "enable",
        "configure terminal",
        "interface port-channel 1",
        "exit",
        "interface ethernet 1",
        "channel-group 1 mode active",
    )

    assert session.last_error is None
    assert engine.config.port_channels[1].members == ["Ethernet1"]
    assert bond_calls(engine.ovs) == []
    # the lone member must not be pulled out of the bridge: it keeps switching
    # as a plain port until a second member turns it into a bond
    assert engine.ovs.bridges.get("mms0") is not None


def test_second_member_builds_the_bond(engine):
    session = make_session(engine)
    run(
        session,
        "enable",
        "configure terminal",
        "interface port-channel 3",
        "exit",
        "interface ethernet 1",
        "channel-group 3 mode active",
        "exit",
        "interface ethernet 2",
        "channel-group 3 mode active",
    )

    assert session.last_error is None
    assert engine.config.port_channels[3].members == ["Ethernet1", "Ethernet2"]
    calls = bond_calls(engine.ovs)
    assert calls, "expected a bond to be created for two members"
    assert "bond3" in calls[-1][1][0]


def test_no_interface_port_channel_removes_it(engine):
    session = make_session(engine)
    run(
        session,
        "enable",
        "configure terminal",
        "interface port-channel 4",
        "exit",
        "interface ethernet 1",
        "channel-group 4 mode active",
        "exit",
        "no interface port-channel 4",
    )

    assert session.last_error is None
    assert 4 not in engine.config.port_channels
    assert engine.config.interfaces["Ethernet1"].channel_group is None


def test_removing_last_member_keeps_the_channel_object(engine):
    session = make_session(engine)
    run(
        session,
        "enable",
        "configure terminal",
        "interface ethernet 1",
        "channel-group 5 mode active",
        "no channel-group",
    )

    assert session.last_error is None
    assert 5 in engine.config.port_channels
    assert engine.config.port_channels[5].members == []
    assert engine.config.interfaces["Ethernet1"].channel_group is None
