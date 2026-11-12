"""Pipe filters wired into the interactive CLI (fake OVS backend)."""
import io

import pytest
from rich.console import Console

from makemeswitch.cli.modes import Session
from makemeswitch.cli.shell import execute_line


def make_session(engine):
    session = Session(engine=engine, username="tester", role="admin", interactive=False, auto_confirm=True)
    session.console = Console(file=io.StringIO(), force_terminal=False, width=120)
    return session


def output(session) -> str:
    return session.console.file.getvalue()


def run(session, *lines):
    for line in lines:
        execute_line(session, line)


def test_include_filter_prints_only_matching_lines(engine):
    session = make_session(engine)
    run(session, "enable", "show running-config | include hostname")
    assert session.last_error is None
    assert output(session).strip() == f"hostname {engine.config.hostname}"


def test_count_filter_prints_a_number(engine):
    session = make_session(engine)
    run(session, "enable", "show running-config | count ^vlan")
    assert output(session).strip().isdigit()
    assert int(output(session).strip()) >= 1


def test_redirect_writes_file_and_prints_nothing(engine, tmp_path):
    target = tmp_path / "run.txt"
    session = make_session(engine)
    run(session, "enable", "show running-config", )
    full = output(session)
    session.console.file = io.StringIO()
    run(session, f"show running-config | redirect {target}")
    assert session.last_error is None
    assert output(session).strip() == ""
    assert target.read_text() == full


def test_bare_pipe_reports_incomplete_command(engine):
    session = make_session(engine)
    run(session, "enable", "show running-config | ")
    assert "incomplete command" in session.last_error


def test_pipe_help_lists_every_filter(engine):
    session = make_session(engine)
    run(session, "enable", "show run | ?")
    text = output(session)
    for word in ("append", "begin", "count", "exclude", "format", "include", "redirect", "section", "tee"):
        assert word in text
    assert "Append redirected output to URL" in text


def test_pipe_help_filters_by_prefix(engine):
    session = make_session(engine)
    run(session, "enable", "show run | inc?")
    text = output(session)
    assert "include" in text
    assert "redirect" not in text


def test_invalid_regex_is_reported(engine):
    session = make_session(engine)
    run(session, "enable", "show running-config | include [unclosed")
    assert session.last_error and "invalid regular expression" in session.last_error


def test_unknown_filter_is_reported(engine):
    session = make_session(engine)
    run(session, "enable", "show running-config | grep vlan")
    assert session.last_error and "unknown pipe filter" in session.last_error


def test_do_with_pipe_keeps_config_mode(engine):
    session = make_session(engine)
    run(session, "enable", "configure terminal", "interface Ethernet1", "do show running-config | include hostname")
    assert session.last_error is None
    assert session.mode == "if"
    assert f"hostname {engine.config.hostname}" in output(session)


def test_failed_command_does_not_get_filtered_away(engine):
    session = make_session(engine)
    run(session, "enable", "show nosuchcommand | include x")
    assert session.last_error is not None


def test_chain_include_then_count(engine):
    session = make_session(engine)
    run(session, "enable", "show running-config | include vlan | count vlan")
    counted = output(session).strip()
    assert counted.isdigit()
    assert int(counted) == sum(
        1 for line in __import__("makemeswitch.core.render", fromlist=["x"]).render_running_config(engine.config).splitlines() if "vlan" in line
    )


def test_dotted_hostname_through_cli(engine):
    session = make_session(engine)
    run(session, "enable", "configure terminal", "hostname cisco.skippy.link", "end")
    assert session.last_error is None
    assert engine.config.hostname == "cisco.skippy.link"
    assert session.prompt == "cisco.skippy.link#"


def test_invalid_hostname_is_rejected(engine):
    session = make_session(engine)
    run(session, "enable", "configure terminal", "hostname bad..name")
    assert session.last_error and "invalid hostname" in session.last_error
