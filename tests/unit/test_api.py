"""REST API tests: authentication, CSRF, RBAC, VLAN operations and apply."""
import pytest
from fastapi.testclient import TestClient

from makemeswitch.api.server import create_app
from makemeswitch.core.security import SecurityService


def make_client(engine):
    security = SecurityService(engine.store, session_ttl=600, login_limit=5)
    security.set_credentials("admin", "AdminPass123", "admin", create=True)
    security.set_credentials("viewer", "ViewerPass123", "readonly", create=True)
    app = create_app(engine=engine, settings=engine.settings, security=security)
    return TestClient(app)


def login(client, username="admin", password="AdminPass123"):
    response = client.post("/api/v1/auth/login", json={"username": username, "password": password})
    assert response.status_code == 200, response.text
    return response.json()["csrf"]


def test_unauthenticated_system_is_rejected(engine):
    with make_client(engine) as client:
        assert client.get("/api/v1/system").status_code == 401


def test_login_and_session(engine):
    with make_client(engine) as client:
        csrf = login(client)
        session = client.get("/api/v1/auth/session")
        assert session.status_code == 200
        assert session.json()["username"] == "admin"
        assert session.json()["csrf"] == csrf


def test_bad_password(engine):
    with make_client(engine) as client:
        response = client.post("/api/v1/auth/login", json={"username": "admin", "password": "wrong-password"})
        assert response.status_code == 401


def test_csrf_is_required(engine):
    with make_client(engine) as client:
        login(client)
        response = client.post("/api/v1/vlans", json={"id": 30, "name": "NOCSRF"})
        assert response.status_code == 403


def test_create_and_delete_vlan(engine):
    with make_client(engine) as client:
        csrf = login(client)
        response = client.post("/api/v1/vlans", json={"id": 30, "name": "STORAGE"}, headers={"X-CSRF-Token": csrf})
        assert response.status_code == 200, response.text
        vlans = client.get("/api/v1/vlans").json()["vlans"]
        assert any(v["id"] == 30 for v in vlans)
        response = client.delete("/api/v1/vlans/30", headers={"X-CSRF-Token": csrf})
        assert response.status_code == 200


def test_readonly_role_cannot_configure(engine):
    with make_client(engine) as client:
        csrf = login(client, "viewer", "ViewerPass123")
        assert client.get("/api/v1/system").status_code == 200
        response = client.post("/api/v1/config/save", headers={"X-CSRF-Token": csrf})
        assert response.status_code == 403


def test_patch_interface_updates_ovs(engine, fake_ovs):
    with make_client(engine) as client:
        csrf = login(client)
        response = client.patch(
            "/api/v1/interfaces/Ethernet1",
            json={"description": "uplink", "access_vlan": 20},
            headers={"X-CSRF-Token": csrf},
        )
        assert response.status_code == 200, response.text
        assert fake_ovs.port_details()["eth9"]["tag"] == 20
        assert response.json()["unsaved_changes"] is True


def test_apply_config_preview_and_confirm(engine):
    with make_client(engine) as client:
        csrf = login(client)
        config = client.get("/api/v1/config").json()["running"]
        config["interfaces"]["Ethernet5"] = {
            "name": "Ethernet5",
            "linux_if": "eth13",
            "attached": True,
            "admin_up": True,
            "mode": "access",
            "access_vlan": 10,
            "trunk_allowed": [1],
            "native_vlan": None,
            "mtu": 1500,
        }
        preview = client.post("/api/v1/config/preview", json={"config": config}, headers={"X-CSRF-Token": csrf})
        assert preview.status_code == 200
        assert preview.json()["requires_confirmation"] is True

        response = client.post("/api/v1/config/apply", json={"config": config}, headers={"X-CSRF-Token": csrf})
        assert response.status_code == 409
        body = response.json()
        assert body["requires_confirmation"] is True

        response = client.post("/api/v1/config/apply", json={"config": config, "confirm": True}, headers={"X-CSRF-Token": csrf})
        assert response.status_code == 200
        assert any(op["action"] == "add-port" for op in response.json()["ops"])


def test_invalid_config_is_rejected(engine):
    with make_client(engine) as client:
        csrf = login(client)
        config = client.get("/api/v1/config").json()["running"]
        config["interfaces"]["Ethernet1"]["access_vlan"] = 999
        response = client.post("/api/v1/config/apply", json={"config": config}, headers={"X-CSRF-Token": csrf})
        assert response.status_code == 400


def test_stats_and_mac_endpoints(engine):
    with make_client(engine) as client:
        login(client)
        stats = client.get("/api/v1/stats")
        assert stats.status_code == 200
        assert "ports" in stats.json()
        mac = client.get("/api/v1/mac-table")
        assert mac.status_code == 200
        assert mac.json()["entries"] == []


def test_audit_log_records_actions(engine):
    with make_client(engine) as client:
        csrf = login(client)
        client.post("/api/v1/config/save", headers={"X-CSRF-Token": csrf})
        entries = client.get("/api/v1/audit").json()["entries"]
        assert any(entry["action"] == "config-save" for entry in entries)


def test_drift_endpoint_reports_in_sync(engine, fake_ovs):
    with make_client(engine) as client:
        login(client)
        body = client.get("/api/v1/config/drift").json()
        assert body["drift"] == []
        assert body["in_sync"] is True
        assert body["unsaved_changes"] is False

        fake_ovs.bridges["mms0"]["eth9"]["tag"] = 99
        body = client.get("/api/v1/config/drift").json()
        assert body["in_sync"] is False
        assert body["drift"]