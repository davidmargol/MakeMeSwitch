"""CLI coverage for SNMP, remote logging, banners and user accounts."""
import io

import pytest
from rich.console import Console

from makemeswitch.cli.modes import Session
from makemeswitch.cli.shell import execute_line
from makemeswitch.core.cisco9 import make_hash
from makemeswitch.core.security import SecurityService


def make_session(engine):
    session = Session(engine=engine, username="tester", role="admin", interactive=False, auto_confirm=True)
    session.console = Console(file=io.StringIO(), force_terminal=False, width=140)
    return session


def output(session) -> str:
    return session.console.file.getvalue()


def run(session, *lines):
    for line in lines:
        execute_line(session, line)


# --- SNMP -----------------------------------------------------------------

def test_snmp_community_with_acl(engine):
    session = make_session(engine)
    run(session, "enable", "configure terminal", "snmp-server community public ro 10.0.0.0/21",
        "snmp-server community monitor ro 192.168.10.0/24 192.168.20.0/24")
    assert session.last_error is None
    public = engine.config.snmp.community("public")
    assert public is not None and public.sources == ["10.0.0.0/21"]
    monitor = engine.config.snmp.community("monitor")
    assert monitor is not None and monitor.allows("192.168.20.7") and not monitor.allows("10.0.0.1")


def test_snmp_identity_commands(engine):
    session = make_session(engine)
    run(session, "enable", "configure terminal",
        "snmp-server contact noc@example.com",
        "snmp-server location rack 3 lab",
        "snmp-server object-id 1.3.6.1.4.1.99999.1")
    assert session.last_error is None
    assert engine.config.snmp.contact == "noc@example.com"
    assert engine.config.snmp.location == "rack 3 lab"
    assert engine.config.snmp.sys_object_id == "1.3.6.1.4.1.99999.1"


def test_snmp_invalid_object_id_is_rejected(engine):
    session = make_session(engine)
    run(session, "enable", "configure terminal", "snmp-server object-id not-an-oid")
    assert session.last_error and "sysObjectID" in session.last_error


def test_snmp_can_be_disabled(engine):
    session = make_session(engine)
    run(session, "enable", "configure terminal", "no snmp-server")
    assert engine.config.snmp.enabled is False


def test_show_snmp_masks_the_community(engine):
    session = make_session(engine)
    run(session, "enable", "configure terminal", "snmp-server community SuperSecretRo ro", "end")
    session.console.file = io.StringIO()  # only inspect the show output
    run(session, "show snmp")
    text = output(session)
    assert "SuperSecretRo" not in text
    assert "ro" in text and "*" in text


# --- remote syslog ---------------------------------------------------------

def test_logging_host_sets_every_field(engine):
    session = make_session(engine)
    run(session, "enable", "configure terminal",
        "logging host 10.0.0.10 transport tcp port 6514 severity warning facility local3")
    assert session.last_error is None
    cfg = engine.config.syslog
    assert (cfg.enabled, cfg.destination, cfg.transport, cfg.port, cfg.severity, cfg.facility) == (
        True, "10.0.0.10", "tcp", 6514, "warning", "local3"
    )


def test_logging_host_rejects_bad_transport(engine):
    session = make_session(engine)
    run(session, "enable", "configure terminal", "logging host 10.0.0.10 transport sctp port 514")
    assert session.last_error and "transport" in session.last_error


def test_no_logging_host_disables_remote_logging(engine):
    session = make_session(engine)
    run(session, "enable", "configure terminal", "logging host 10.0.0.10", "no logging host")
    assert engine.config.syslog.enabled is False


def test_show_logging_remote_masks_nothing_but_lists_destination(engine):
    session = make_session(engine)
    run(session, "enable", "configure terminal", "logging host 10.0.0.10", "end", "show logging remote")
    text = output(session)
    assert "10.0.0.10" in text


# --- banners ---------------------------------------------------------------

def test_banner_login_and_motd(engine):
    session = make_session(engine)
    run(session, "enable", "configure terminal",
        "banner login ^C Authorised access only ^C",
        "banner motd ^C Sunday maintenance ^C")
    assert session.last_error is None
    assert engine.config.banner.enabled is True
    assert "Authorised access only" in engine.config.banner.login
    assert "Sunday maintenance" in engine.config.banner.motd


def test_banner_is_rendered_in_running_config(engine):
    session = make_session(engine)
    run(session, "enable", "configure terminal", "banner login ^C Keep out ^C", "end",
        "show running-config | section banner")
    assert "Keep out" in output(session)


def test_no_banner_clears_it(engine):
    session = make_session(engine)
    run(session, "enable", "configure terminal", "banner login ^C Keep out ^C", "no banner login")
    assert engine.config.banner.login == ""
    assert engine.config.banner.enabled is False


# --- users -----------------------------------------------------------------

def test_username_privilege_maps_to_role(engine):
    session = make_session(engine)
    run(session, "enable", "configure terminal",
        "username operator privilege 7 secret 0 OperatorPass123",
        "username viewer privilege 1 secret 0 ViewerPass123")
    assert session.last_error is None
    users = {u["username"]: u for u in engine.store.list_users()}
    assert users["operator"]["role"] == "operator"
    assert users["viewer"]["role"] == "readonly"


def test_username_password_actually_authenticates(engine):
    session = make_session(engine)
    run(session, "enable", "configure terminal", "username operator privilege 7 secret 0 OperatorPass123")
    assert SecurityService(engine.store).authenticate("operator", "OperatorPass123") is not None
    assert SecurityService(engine.store).authenticate("operator", "wrong-password") is None


def test_username_accepts_real_cisco_type9_hash(engine):
    session = make_session(engine)
    digest = make_hash("ImportedSecret123")
    run(session, "enable", "configure terminal", f"username cisco privilege 15 secret 9 {digest}")
    assert session.last_error is None
    assert SecurityService(engine.store).authenticate("cisco", "ImportedSecret123") is not None


def test_plaintext_after_secret_9_is_rejected_with_guidance(engine):
    session = make_session(engine)
    run(session, "enable", "configure terminal", "username admin privilege 15 secret 9 MakeMeSwitch")
    assert session.last_error is not None
    assert "type 9" in session.last_error or "secret 0" in session.last_error
    assert "MakeMeSwitch" not in session.last_error  # never echo the secret


def test_show_users_hides_the_hash(engine):
    session = make_session(engine)
    run(session, "enable", "configure terminal", "username operator privilege 7 secret 0 OperatorPass123",
        "end", "show users")
    text = output(session)
    assert "operator" in text
    assert "$argon2" not in text and "$9$" not in text


def test_last_admin_cannot_be_deleted(engine):
    session = make_session(engine)
    run(session, "enable", "configure terminal",
        "username first privilege 15 secret 0 FirstAdminPass1",
        "username second privilege 15 secret 0 SecondAdminPass1",
        "no username first")
    assert session.last_error is None
    assert engine.store.get_user("first") is None
    run(session, "no username second")
    assert session.last_error is not None
    assert engine.store.get_user("second") is not None


def test_non_admin_user_can_be_deleted(engine):
    session = make_session(engine)
    run(session, "enable", "configure terminal",
        "username temp privilege 7 secret 0 TempPass12345",
        "no username temp")
    assert session.last_error is None
    assert engine.store.get_user("temp") is None


def test_secret_never_lands_in_history(engine, tmp_path):
    from makemeswitch.cli.shell import _scrub_history

    history = tmp_path / "history"
    history.write_text("enable\nconfigure terminal\nusername x privilege 15 secret 0 TopSecret123\n")
    _scrub_history(history)
    assert "TopSecret123" not in history.read_text()


def test_snmp_version_and_listen_commands(engine):
    session = make_session(engine)
    run(session, "enable", "configure terminal",
        "snmp-server version v1", "snmp-server listen 10.0.0.87 port 1161")
    assert session.last_error is None
    assert engine.config.snmp.version == "v1"
    assert engine.config.snmp.bind == "10.0.0.87" and engine.config.snmp.port == 1161


def test_snmp_v3_is_rejected_with_a_reason(engine):
    session = make_session(engine)
    run(session, "enable", "configure terminal", "snmp-server version v3")
    assert session.last_error and "v3" in session.last_error.lower()


def test_snmp_version_round_trips_through_json(engine):
    from makemeswitch.core.models import SnmpConfig

    cfg = SnmpConfig.from_dict({**SnmpConfig().to_dict(), "version": "v1"})
    assert cfg.version == "v1" and cfg.to_dict()["version"] == "v1"


def test_ssh_privilege_comes_from_the_account_role(engine):
    from makemeswitch.core.ssh_server import _privilege_for, role_for_privilege

    session = make_session(engine)
    run(session, "enable", "configure terminal",
        "username sshadmin privilege 15 secret 0 SshAdminPass123",
        "username sshops privilege 7 secret 0 SshOpsPass12345",
        "username sshview privilege 1 secret 0 SshViewPass12345")
    db = engine.settings["db_path"]
    assert _privilege_for(db, "sshadmin") == 15
    assert _privilege_for(db, "sshops") == 7
    assert _privilege_for(db, "sshview") == 1
    assert role_for_privilege(_privilege_for(db, "sshadmin")) == "admin"
    assert role_for_privilege(_privilege_for(db, "sshview")) == "readonly"
