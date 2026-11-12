"""EXEC compatibility commands, exercised through the actual CLI parser."""
import io

import pytest
from rich.console import Console

from makemeswitch.cli.modes import Session
from makemeswitch.cli.shell import execute_line


def session_for(engine, **kwargs):
    return Session(engine=engine, username="tester", mode="priv", interactive=False,
                   console=Console(file=io.StringIO(), width=180), **kwargs)


@pytest.mark.parametrize("command", ["write", "wr", "write memory", "wr mem"])
def test_write_saves_running(engine, command):
    cfg = engine.config.copy()
    cfg.set_hostname("UNSAVED-SW")
    engine.apply(cfg, user="tester", confirm=True)
    session = session_for(engine)
    execute_line(session, command)
    assert session.last_error is None
    assert engine.store.load_config("startup").hostname == "UNSAVED-SW"
    assert not engine.unsaved_changes()
    assert "Configuration saved" in session.console.file.getvalue()


@pytest.mark.parametrize("answer", ["yes", "no", "", None])
def test_write_erase_only_resets_startup_after_confirmation(engine, answer):
    from copy import deepcopy
    before = engine.config.to_dict()
    persisted = engine.store.load_config("running").to_dict()
    startup = engine.store.load_config("startup").to_dict()
    ovs = deepcopy(engine.ovs.bridges)
    calls = list(engine.ovs.calls)
    session = session_for(engine)
    prompts = []
    if answer is not None:
        session.interactive = True
        session.ask = lambda prompt: prompts.append(prompt) or answer
    execute_line(session, "write erase")
    assert session.last_error is None
    assert engine.config.to_dict() == before
    assert engine.store.load_config("running").to_dict() == persisted
    assert engine.ovs.bridges == ovs
    assert engine.ovs.calls == calls
    if answer == "yes":
        saved = engine.store.load_config("startup")
        assert saved is not None  # boot must not fall back to the old running config
        assert saved.interfaces == {}
        assert saved.hostname != before["hostname"]
        assert engine.startup.to_dict() == saved.to_dict()
        assert engine.unsaved_changes()
    else:
        assert engine.store.load_config("startup").to_dict() == startup
        assert "aborted" in session.console.file.getvalue().lower()
    if answer is not None:
        assert len(prompts) == 1
        assert "startup" in prompts[0].lower()


@pytest.mark.parametrize("command", ["write", "write erase"])
def test_readonly_cannot_write(engine, command):
    before = engine.store.load_config("startup").to_dict()
    session = session_for(engine, role="readonly", auto_confirm=True)
    execute_line(session, command)
    assert "authorization failed" in session.last_error
    assert engine.store.load_config("startup").to_dict() == before


@pytest.mark.parametrize("command", ["show etherchannel summary", "sh ether sum", "show port-channel summary"])
@pytest.mark.parametrize("backend", ["present", "absent", "unavailable"])
def test_etherchannel_reports_observed_ovs_presence(engine, monkeypatch, command, backend):
    from makemeswitch.core.models import PortChannel
    engine.config.port_channels[7] = PortChannel(7, members=["Ethernet1", "Ethernet2"], mode="active")
    if backend == "present":
        engine.ovs.add_bond("bond7", ["eth9", "eth10"])
    elif backend == "unavailable":
        def unavailable():
            raise RuntimeError("OVS unavailable")
        monkeypatch.setattr(engine.ovs, "port_details", unavailable)
    session = session_for(engine, role="readonly")
    execute_line(session, command)
    assert session.last_error is None
    text = session.console.file.getvalue()
    assert "bond7" in text
    assert "Ethernet1" in text and "Ethernet2" in text
    assert {"present": "present in OVS", "absent": "not in OVS", "unavailable": "unavailable"}[backend] in text
    assert "bond active" not in text  # OVSDB existence does not prove negotiated LACP/link state


@pytest.mark.parametrize("mode", ["exec", "priv", "config"])
def test_show_terminal_monitor_reports_no_streaming_not_fake_enable(engine, mode):
    session = session_for(engine, role="readonly" if mode != "config" else "operator")
    session.mode = mode
    command = "show terminal monitor" if mode != "config" else "do show terminal monitor"
    before = engine.config.to_dict()
    execute_line(session, command)
    assert session.last_error is None
    assert session.mode == mode
    text = session.console.file.getvalue().lower()
    assert "terminal monitoring: disabled" in text
    assert "not supported" in text
    assert "show logging" in text
    assert engine.config.to_dict() == before
