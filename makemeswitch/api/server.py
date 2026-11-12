"""FastAPI application: REST API, WebSockets and the static web panel."""
from __future__ import annotations

import asyncio
import contextlib
import io
import json
import os
import pty
import secrets
import shutil
import signal
import subprocess
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import Body, Depends, FastAPI, HTTPException, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from makemeswitch import PROJECT_NAME, __version__
from makemeswitch.core.engine import Engine, NeedsConfirmation, load_settings
from makemeswitch.core.models import ConfigError, SwitchConfig
from makemeswitch.core.render import render_running_config
from makemeswitch.core.security import COOKIE_NAME, SecurityError, SecurityService
from makemeswitch.utils.logging import get_logger, setup_logging

log = get_logger("makemeswitch.api")


def _resolve_web_root() -> Path:
    """Locate the built panel.

    Order: explicit env override (set by the installer/systemd), then the
    app directory next to the installed code, then the package itself.
    """
    override = os.environ.get("MMS_WEB_DIST")
    if override:
        return Path(override)
    app_dir = os.environ.get("MMS_APP_DIR")
    candidates = []
    if app_dir:
        candidates.append(Path(app_dir) / "makemeswitch" / "web" / "dist")
    candidates.append(Path(__file__).resolve().parent.parent / "web" / "dist")
    candidates.append(Path.cwd() / "makemeswitch" / "web" / "dist")
    for candidate in candidates:
        if (candidate / "index.html").is_file():
            return candidate
    return candidates[0]


WEB_ROOT = _resolve_web_root()


# --------------------------------------------------------------------------
# live statistics hub
# --------------------------------------------------------------------------
class StatsHub:
    def __init__(self) -> None:
        self.clients: set[WebSocket] = set()
        self.latest: dict[str, Any] = {}
        self._lock = asyncio.Lock()

    async def add(self, websocket: WebSocket) -> None:
        async with self._lock:
            self.clients.add(websocket)
        if self.latest:
            await websocket.send_text(json.dumps(self.latest))

    async def remove(self, websocket: WebSocket) -> None:
        async with self._lock:
            self.clients.discard(websocket)

    async def broadcast(self, payload: dict[str, Any]) -> None:
        self.latest = payload
        message = json.dumps(payload)
        dead = []
        for client in list(self.clients):
            try:
                await client.send_text(message)
            except Exception:  # noqa: BLE001
                dead.append(client)
        for client in dead:
            await self.remove(client)


def _stats_payload(engine: Engine) -> dict[str, Any]:
    rates = engine.traffic_rates()
    ports = []
    totals = {"rx_bytes": 0.0, "tx_bytes": 0.0, "rx_bps": 0.0, "tx_bps": 0.0}
    for row in engine.interface_snapshot():
        linux = row.get("linux_if") or ""
        stats = row.get("stats") or {}
        port_rates = rates.get(linux, {})
        for key in ("rx_bytes", "tx_bytes"):
            totals[key] += float(stats.get(key, 0) or 0)
        totals["rx_bps"] += port_rates.get("rx_bps", 0)
        totals["tx_bps"] += port_rates.get("tx_bps", 0)
        ports.append(
            {
                "name": row["name"],
                "linux_if": linux,
                "status": row["status"],
                "present": row["present"],
                "attached": row["attached"],
                "admin_up": row["admin_up"],
                "mode": row["mode"],
                "vlans": row.get("vlans"),
                "stats": stats,
                "rates": port_rates,
            }
        )
    info = engine.system_info()
    return {
        "ts": time.time(),
        "ports": ports,
        "totals": totals,
        "system": {
            "cpu_percent": info["cpu_percent"],
            "memory_percent": info["memory_percent"],
            "memory_total": info["memory_total"],
            "memory_available": info["memory_available"],
            "temperature_c": info["temperature_c"],
            "uptime_seconds": info["uptime_seconds"],
            "ports_active": info["ports_active"],
            "ports_total": info["ports_total"],
            "vlans": info["vlans"],
            "unsaved_changes": info["unsaved_changes"],
        },
    }


# --------------------------------------------------------------------------
# app
# --------------------------------------------------------------------------
def create_app(engine: Engine | None = None, settings: dict[str, Any] | None = None, security: SecurityService | None = None) -> FastAPI:
    settings = settings or load_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        setup_logging("makemeswitch.api", settings.get("log_dir"))
        app.state.engine = engine or Engine(settings=settings)
        app.state.security = security or SecurityService(
            app.state.engine.store,
            session_ttl=int(settings.get("api", {}).get("session_ttl_seconds", 28800)),
            login_limit=int(settings.get("api", {}).get("login_rate_limit", 10)),
        )
        app.state.hub = StatsHub()
        app.state.settings = settings
        if not engine and os.environ.get("MMS_SKIP_BOOTSTRAP", "").lower() not in ("1", "true"):
            try:
                # Run blocking netlink/OVS discovery off the event loop.
                await asyncio.to_thread(app.state.engine.boot, True)
            except Exception as exc:  # noqa: BLE001
                app.state.engine.store.add_event("error", "api", f"Startup bootstrap failed: {exc}")
        created = app.state.security.ensure_bootstrap_admin()
        if created:
            # Never persist the generated password: it is written to the service
            # log once so the administrator can retrieve it from journalctl.
            app.state.engine.store.add_event(
                "warning", "security", f"Initial administrator '{created[0]}' created; credentials shown once in the service log"
            )
            print(
                f"MakeMeSwitch initial administrator: {created[0]} / {created[1]} — change the password after first login",
                file=sys.stderr,
                flush=True,
            )

        async def sampler() -> None:
            counter = 0
            while True:
                try:
                    payload = await asyncio.to_thread(_stats_payload, app.state.engine)
                    await app.state.hub.broadcast(payload)
                    if counter % 5 == 0:
                        await asyncio.to_thread(
                            app.state.engine.store.add_stats,
                            [
                                {"port": p["name"], **{k: p["stats"].get(k, 0) for k in ("rx_bytes", "tx_bytes", "rx_packets", "tx_packets", "rx_errors", "tx_errors")}}
                                for p in payload["ports"]
                                if p["attached"]
                            ],
                        )
                    counter += 1
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001
                    log.warning("stats sampler error", extra={"data": {"error": str(exc)}})
                await asyncio.sleep(2)
                app.state.engine.refresh()

        task = asyncio.create_task(sampler())
        try:
            yield
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    app = FastAPI(title=f"{PROJECT_NAME} API", version=__version__, docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.state.settings = settings

    # ---- dependencies ---------------------------------------------------
    async def current_session(request: Request) -> dict[str, Any]:
        if not hasattr(request.app.state, "security"):
            raise HTTPException(503, "service starting")
        session = request.app.state.security.validate_session(request.cookies.get(COOKIE_NAME))
        if not session:
            raise HTTPException(401, "authentication required")
        if request.method in ("POST", "PUT", "PATCH", "DELETE"):
            header = request.headers.get("x-csrf-token", "")
            if not header or not secrets.compare_digest(header, session["csrf"]):
                raise HTTPException(403, "invalid CSRF token")
        request.app.state.engine.refresh()
        return session

    def require(role: str):
        async def dependency(session: dict[str, Any] = Depends(current_session)) -> dict[str, Any]:
            if not SecurityService.can(session["role"], role):
                raise HTTPException(403, f"{role} role required")
            return session

        return dependency

    def client_ip(request: Request) -> str:
        forwarded = request.headers.get("x-forwarded-for", "")
        if forwarded:
            return forwarded.split(",")[0].strip()
        return request.client.host if request.client else "unknown"

    # ---- auth -----------------------------------------------------------
    @app.post("/api/v1/auth/login")
    async def login(request: Request, payload: dict[str, Any] = Body(...)) -> JSONResponse:
        username = str(payload.get("username", ""))[:64]
        password = str(payload.get("password", ""))[:256]
        ip = client_ip(request)
        try:
            user = request.app.state.security.authenticate(username, password, ip)
        except SecurityError as exc:
            raise HTTPException(429, str(exc)) from exc
        if not user:
            raise HTTPException(401, "invalid credentials")
        session = request.app.state.security.create_session(username, ip, request.headers.get("user-agent", ""))
        secure = bool(request.app.state.settings.get("api", {}).get("https"))
        response = JSONResponse(
            {
                "username": username,
                "role": user["role"],
                "csrf": session["csrf"],
                "expires_in": session["expires_in"],
                "must_change_password": bool(user["must_change_password"]),
            }
        )
        response.set_cookie(
            COOKIE_NAME,
            session["token"],
            max_age=session["expires_in"],
            httponly=True,
            samesite="strict",
            secure=secure,
            path="/",
        )
        return response

    @app.post("/api/v1/auth/logout")
    async def logout(request: Request, session: dict[str, Any] = Depends(current_session)) -> dict[str, Any]:
        request.app.state.security.revoke(request.cookies.get(COOKIE_NAME))
        response = JSONResponse({"ok": True})
        response.delete_cookie(COOKIE_NAME, path="/")
        return response

    @app.get("/api/v1/auth/session")
    async def session_info(session: dict[str, Any] = Depends(current_session)) -> dict[str, Any]:
        return {"username": session["username"], "role": session["role"], "csrf": session["csrf"], "expires_at": session["expires_at"]}

    @app.post("/api/v1/auth/password")
    async def change_password(request: Request, payload: dict[str, Any] = Body(...), session: dict[str, Any] = Depends(current_session)) -> dict[str, Any]:
        current = str(payload.get("current_password", ""))
        new = str(payload.get("new_password", ""))
        user = request.app.state.engine.store.get_user(session["username"])
        if not user or not request.app.state.security.verify_password(user["password_hash"], current):
            raise HTTPException(403, "current password is incorrect")
        try:
            request.app.state.security.set_credentials(session["username"], new, user["role"])
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        request.app.state.engine.store.audit(session["username"], client_ip(request), "password-change", session["username"], "success")
        return {"ok": True}

    # ---- system ---------------------------------------------------------
    @app.get("/api/v1/system")
    async def system(request: Request, session: dict[str, Any] = Depends(current_session)) -> dict[str, Any]:
        engine = request.app.state.engine
        info = engine.system_info()
        info["user"] = {"username": session["username"], "role": session["role"]}
        info["events"] = engine.store.list_events(limit=8)
        info["summary"] = engine.config.summary()
        info["features"] = _features(engine)
        info["startup_saved"] = not engine.unsaved_changes()
        return info

    @app.get("/api/v1/features")
    async def features(request: Request, session: dict[str, Any] = Depends(current_session)) -> dict[str, Any]:
        return _features(request.app.state.engine)

    # ---- interfaces -----------------------------------------------------
    @app.get("/api/v1/interfaces")
    async def interfaces(request: Request, session: dict[str, Any] = Depends(current_session)) -> dict[str, Any]:
        engine = request.app.state.engine
        return {"interfaces": engine.interface_snapshot(), "mapping": engine.port_table(), "bridge": engine.bridge}

    @app.post("/api/v1/interfaces/scan")
    async def scan_interfaces(request: Request, session: dict[str, Any] = Depends(require("operator"))) -> dict[str, Any]:
        engine = request.app.state.engine
        engine.sync_ports()
        engine.store.audit(session["username"], client_ip(request), "interface-scan", None, "success")
        return {"mapping": engine.port_table()}

    @app.post("/api/v1/interfaces/{name:path}/attach")
    async def attach_interface(name: str, request: Request, payload: dict[str, Any] = Body(default={}), session: dict[str, Any] = Depends(require("operator"))) -> dict[str, Any]:
        engine = request.app.state.engine
        iface = next((item for key, item in engine.config.interfaces.items() if key.lower() == name.lower()), None)
        if not iface:
            raise HTTPException(404, f"interface {name} not found")
        cfg = engine.config.copy()
        target = cfg.interfaces[iface.name]
        linux = payload.get("linux_if") or target.linux_if
        record = next((row for row in engine.port_table() if row["linux_if"] == linux), None)
        if not record:
            raise HTTPException(400, f"{linux} is not a detected switch port")
        if record["protected"]:
            raise HTTPException(409, f"{linux} carries host management connectivity")
        if record["name"].lower() != target.name.lower():
            raise HTTPException(409, f"{linux} is mapped to {record['name']}")
        target.linux_if = linux
        target.attached = True
        return _apply_and_respond(request, cfg, session, bool(payload.get("confirm", False)), f"attach {linux}")

    @app.delete("/api/v1/interfaces/{name:path}/attach")
    async def detach_interface(name: str, request: Request, session: dict[str, Any] = Depends(require("operator"))) -> dict[str, Any]:
        engine = request.app.state.engine
        cfg = engine.config.copy()
        target = next((key for key in cfg.interfaces if key.lower() == name.lower()), None)
        if target is None:
            raise HTTPException(404, f"interface {name} not found")
        cfg.interfaces[target].attached = False
        return _apply_and_respond(request, cfg, session, True, f"detach {target}")

    @app.get("/api/v1/interfaces/{name:path}")
    async def interface_detail(name: str, request: Request, session: dict[str, Any] = Depends(current_session)) -> dict[str, Any]:
        try:
            detail = request.app.state.engine.interface_detail(name)
        except ConfigError as exc:
            raise HTTPException(404, str(exc)) from exc
        detail["drift"] = [d for d in request.app.state.engine.diff() if d.get("port") in (name, detail.get("linux_if"))]
        return detail

    @app.patch("/api/v1/interfaces/{name:path}")
    async def patch_interface(
        name: str,
        request: Request,
        payload: dict[str, Any] = Body(...),
        session: dict[str, Any] = Depends(require("operator")),
    ) -> dict[str, Any]:
        engine = request.app.state.engine
        cfg = engine.config.copy()
        target = None
        for key in cfg.interfaces:
            if key.lower() == name.lower():
                target = key
                break
        if target is None:
            raise HTTPException(404, f"interface {name} not found")
        iface = cfg.interfaces[target]
        if "description" in payload:
            iface.description = str(payload["description"])[:255]
        if "admin_up" in payload:
            iface.admin_up = bool(payload["admin_up"])
        if "mode" in payload:
            if payload["mode"] not in ("access", "trunk"):
                raise HTTPException(400, "mode must be access or trunk")
            iface.mode = payload["mode"]
            iface.attached = True
        if "access_vlan" in payload:
            iface.access_vlan = int(payload["access_vlan"])
            iface.attached = True
        if "trunk_allowed" in payload:
            iface.trunk_allowed = [int(v) for v in payload["trunk_allowed"]]
            iface.attached = True
        if "native_vlan" in payload:
            value = payload["native_vlan"]
            iface.native_vlan = None if value in (None, "", 0, "0") else int(value)
        if "mtu" in payload:
            iface.mtu = int(payload["mtu"])
        if "attached" in payload:
            iface.attached = bool(payload["attached"])
        if "channel_group" in payload:
            value = payload["channel_group"]
            iface.channel_group = None if value in (None, "", 0, "0") else int(value)
        return _apply_and_respond(request, cfg, session, bool(payload.get("confirm", False)), f"interface {target}")

    # ---- VLANs ----------------------------------------------------------
    @app.get("/api/v1/vlans")
    async def vlans(request: Request, session: dict[str, Any] = Depends(current_session)) -> dict[str, Any]:
        engine = request.app.state.engine
        payload = []
        for vlan in sorted(engine.config.vlans.values(), key=lambda v: v.vid):
            untagged, tagged = [], []
            for iface in engine.config.interfaces.values():
                if not iface.attached or not iface.admin_up:
                    continue
                if iface.mode == "access" and iface.access_vlan == vlan.vid:
                    untagged.append(iface.name)
                elif iface.mode == "trunk":
                    if iface.native_vlan == vlan.vid:
                        untagged.append(iface.name)
                    elif vlan.vid in iface.trunk_allowed:
                        tagged.append(iface.name)
            payload.append(
                {
                    "id": vlan.vid,
                    "name": vlan.name,
                    "description": vlan.description,
                    "untagged": untagged,
                    "tagged": tagged,
                }
            )
        return {"vlans": payload, "interfaces": engine.interface_snapshot()}

    @app.post("/api/v1/vlans")
    async def create_vlan(request: Request, payload: dict[str, Any] = Body(...), session: dict[str, Any] = Depends(require("operator"))) -> dict[str, Any]:
        engine = request.app.state.engine
        cfg = engine.config.copy()
        try:
            vid = int(payload.get("id"))
            cfg.add_vlan(vid, payload.get("name"))
        except (TypeError, ValueError, ConfigError) as exc:
            raise HTTPException(400, str(exc)) from exc
        return _apply_and_respond(request, cfg, session, True, f"vlan {vid}", quiet=True)

    @app.patch("/api/v1/vlans/{vid}")
    async def update_vlan(vid: int, request: Request, payload: dict[str, Any] = Body(...), session: dict[str, Any] = Depends(require("operator"))) -> dict[str, Any]:
        engine = request.app.state.engine
        cfg = engine.config.copy()
        vlan = cfg.vlans.get(vid)
        if not vlan:
            raise HTTPException(404, f"VLAN {vid} not found")
        if "name" in payload:
            vlan.name = str(payload["name"])[:32]
        if "description" in payload:
            vlan.description = str(payload["description"])[:255]
        try:
            from makemeswitch.core.models import Vlan

            cfg.vlans[vid] = Vlan(vid, vlan.name, vlan.description)
        except ConfigError as exc:
            raise HTTPException(400, str(exc)) from exc
        return _apply_and_respond(request, cfg, session, True, f"vlan {vid}", quiet=True)

    @app.delete("/api/v1/vlans/{vid}")
    async def delete_vlan(vid: int, request: Request, session: dict[str, Any] = Depends(require("operator"))) -> dict[str, Any]:
        engine = request.app.state.engine
        cfg = engine.config.copy()
        try:
            cfg.delete_vlan(vid)
        except ConfigError as exc:
            raise HTTPException(409, str(exc)) from exc
        return _apply_and_respond(request, cfg, session, True, f"vlan {vid}", quiet=True)

    # ---- forwarding tables / topology ----------------------------------
    @app.get("/api/v1/mac-table")
    async def mac_table(request: Request, session: dict[str, Any] = Depends(current_session)) -> dict[str, Any]:
        engine = request.app.state.engine
        return {"entries": engine.mac_table(), "bridge": engine.bridge}

    @app.get("/api/v1/topology")
    async def topology(request: Request, session: dict[str, Any] = Depends(current_session)) -> dict[str, Any]:
        engine = request.app.state.engine
        lldp = engine.lldp_status()
        neighbors = lldp.get("neighbors", [])
        ports = []
        for row in engine.interface_snapshot():
            neighbor = next((n for n in neighbors if n.get("local_interface") == row.get("linux_if")), None)
            ports.append(
                {
                    "name": row["name"],
                    "linux_if": row.get("linux_if"),
                    "status": row["status"],
                    "speed": (row.get("link") or {}).get("speed"),
                    "neighbor": neighbor,
                    "lldp_identified": bool(neighbor),
                }
            )
        return {"local": {"hostname": engine.config.hostname, "bridge": engine.bridge}, "lldp": lldp, "ports": ports}

    @app.get("/api/v1/stats")
    async def stats(request: Request, session: dict[str, Any] = Depends(current_session)) -> dict[str, Any]:
        return await asyncio.to_thread(_stats_payload, request.app.state.engine)

    @app.get("/api/v1/stats/history")
    async def stats_history(request: Request, port: str | None = None, range: int = 900, session: dict[str, Any] = Depends(current_session)) -> dict[str, Any]:
        seconds = max(60, min(int(range), 86400))
        engine = request.app.state.engine
        logical = {i.name: i.name for i in engine.config.interfaces.values()}
        target = logical.get(port, port) if port else None
        return {"port": target, "range": seconds, "samples": engine.store.stats_history(target, seconds)}

    # ---- config ---------------------------------------------------------
    @app.get("/api/v1/config")
    async def get_config(request: Request, session: dict[str, Any] = Depends(current_session)) -> dict[str, Any]:
        engine = request.app.state.engine
        startup = engine.store.load_config("startup")
        return {
            "running": engine.config.to_dict(),
            "startup": startup.to_dict() if startup else None,
            "running_text": render_running_config(
                engine.config, include_defaults=True,
                users=engine.store.list_users(include_secrets=False),
            ),
            "startup_text": render_running_config(
                startup, include_defaults=True,
                users=engine.store.list_users(include_secrets=False),
            ) if startup else "",
            "unsaved_changes": engine.unsaved_changes(),
            "drift": engine.diff(),
            "history": engine.store.history(limit=10),
        }

    @app.get("/api/v1/config/drift")
    async def get_drift(request: Request, session: dict[str, Any] = Depends(current_session)) -> dict[str, Any]:
        """Compare the desired configuration with live OVS state."""
        engine = request.app.state.engine
        drift = engine.diff()
        return {
            "drift": drift,
            "unsaved_changes": engine.unsaved_changes(),
            "in_sync": not drift,
        }

    @app.post("/api/v1/config/validate")
    async def validate_config(request: Request, payload: dict[str, Any] = Body(...), session: dict[str, Any] = Depends(require("readonly"))) -> dict[str, Any]:
        try:
            cfg = SwitchConfig.from_dict(payload.get("config") or payload)
        except ConfigError as exc:
            return {"valid": False, "errors": [exc.message]}
        errors = cfg.validate()
        return {"valid": not errors, "errors": errors}

    @app.post("/api/v1/config/preview")
    async def preview_config(request: Request, payload: dict[str, Any] = Body(...), session: dict[str, Any] = Depends(require("operator"))) -> dict[str, Any]:
        engine = request.app.state.engine
        try:
            cfg = SwitchConfig.from_dict(payload.get("config") or payload)
        except ConfigError as exc:
            raise HTTPException(400, exc.message) from exc
        errors = cfg.validate()
        if errors:
            raise HTTPException(400, "; ".join(errors))
        result = engine.apply(cfg, user=session["username"], dry_run=True)
        newly = [
            name
            for name, iface in cfg.interfaces.items()
            if iface.attached and (name not in engine.config.interfaces or not engine.config.interfaces[name].attached)
        ]
        return {"ops": result.ops, "newly_attached": newly, "requires_confirmation": bool(newly)}

    @app.post("/api/v1/config/apply")
    async def apply_config(request: Request, payload: dict[str, Any] = Body(...), session: dict[str, Any] = Depends(require("operator"))) -> dict[str, Any]:
        try:
            cfg = SwitchConfig.from_dict(payload.get("config") or payload)
        except ConfigError as exc:
            raise HTTPException(400, exc.message) from exc
        return _apply_and_respond(request, cfg, session, bool(payload.get("confirm", False)), "configuration")

    @app.post("/api/v1/config/save")
    async def save_config(request: Request, session: dict[str, Any] = Depends(require("operator"))) -> dict[str, Any]:
        engine = request.app.state.engine
        engine.refresh()
        engine.save_startup(session["username"])
        engine.store.audit(session["username"], client_ip(request), "config-save", None, "success")
        return {"ok": True, "saved_at": time.time()}

    @app.post("/api/v1/config/rollback")
    async def rollback_config(request: Request, payload: dict[str, Any] = Body(default={}), session: dict[str, Any] = Depends(require("admin"))) -> dict[str, Any]:
        engine = request.app.state.engine
        history_id = payload.get("history_id")
        try:
            result = engine.rollback(int(history_id) if history_id else None, session["username"])
        except (ConfigError, Exception) as exc:  # noqa: BLE001
            raise HTTPException(400, str(exc)) from exc
        engine.store.audit(session["username"], client_ip(request), "config-rollback", str(history_id), "success")
        return {"ok": True, "changed": result.changed, "ops": result.ops}

    @app.get("/api/v1/config/export")
    async def export_config(request: Request, session: dict[str, Any] = Depends(current_session)) -> Response:
        engine = request.app.state.engine
        body = json.dumps({"format": "makemeswitch-config-v1", "config": engine.config.to_dict()}, indent=2)
        return Response(
            content=body,
            media_type="application/json",
            headers={"Content-Disposition": 'attachment; filename="makemeswitch-config.json"'},
        )

    @app.post("/api/v1/config/import")
    async def import_config(request: Request, payload: dict[str, Any] = Body(...), session: dict[str, Any] = Depends(require("admin"))) -> dict[str, Any]:
        engine = request.app.state.engine
        raw = payload.get("config") or payload.get("document") or payload
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise HTTPException(400, "imported document is not valid JSON") from exc
        cfg = SwitchConfig.from_dict(raw.get("config", raw) if isinstance(raw, dict) else raw)
        return _apply_and_respond(request, cfg, session, bool(payload.get("confirm", False)), "imported configuration")

    @app.get("/api/v1/config/history")
    async def config_history(request: Request, session: dict[str, Any] = Depends(require("operator"))) -> dict[str, Any]:
        return {"history": request.app.state.engine.store.history(limit=25)}

    # ---- settings / advanced -------------------------------------------
    @app.get("/api/v1/settings")
    async def get_settings(request: Request, session: dict[str, Any] = Depends(current_session)) -> dict[str, Any]:
        engine = request.app.state.engine
        cfg = engine.config
        return {
            "stp": cfg.stp.to_dict(),
            "lldp_enabled": cfg.lldp_enabled,
            "ip_routing": cfg.ip_routing,
            "mirrors": [m.to_dict() for m in cfg.mirrors.values()],
            "port_channels": [p.to_dict() for p in cfg.port_channels.values()],
            "svis": [s.to_dict() for s in cfg.svis.values()],
            "routes": [r.to_dict() for r in cfg.routes],
            "banner": cfg.banner.to_dict(),
            "snmp": cfg.snmp.to_dict(),
            "syslog": cfg.syslog.to_dict(),
            "features": _features(engine),
            "frr": engine.frr.status(),
        }

    @app.patch("/api/v1/settings")
    async def patch_settings(request: Request, payload: dict[str, Any] = Body(...), session: dict[str, Any] = Depends(require("operator"))) -> dict[str, Any]:
        engine = request.app.state.engine
        cfg = engine.config.copy()
        if "stp" in payload:
            from makemeswitch.core.models import StpConfig

            cfg.stp = StpConfig.from_dict({**cfg.stp.to_dict(), **payload["stp"]})
        if "lldp_enabled" in payload:
            cfg.lldp_enabled = bool(payload["lldp_enabled"])
        if "banner" in payload:
            from makemeswitch.core.models import BannerConfig

            cfg.banner = BannerConfig.from_dict({**cfg.banner.to_dict(), **payload["banner"]})
        if "snmp" in payload:
            from makemeswitch.core.models import SnmpConfig

            cfg.snmp = SnmpConfig.from_dict({**cfg.snmp.to_dict(), **payload["snmp"]})
        if "syslog" in payload:
            from makemeswitch.core.models import SyslogConfig

            cfg.syslog = SyslogConfig.from_dict({**cfg.syslog.to_dict(), **payload["syslog"]})
        if "ip_routing" in payload:
            cfg.ip_routing = bool(payload["ip_routing"])
        return _apply_and_respond(request, cfg, session, True, "settings", quiet=True)

    @app.get("/api/v1/events")
    async def events(request: Request, limit: int = 50, session: dict[str, Any] = Depends(current_session)) -> dict[str, Any]:
        return {"events": request.app.state.engine.store.list_events(limit=min(limit, 200))}

    @app.get("/api/v1/audit")
    async def audit_log(request: Request, limit: int = 100, session: dict[str, Any] = Depends(require("admin"))) -> dict[str, Any]:
        return {"entries": request.app.state.engine.store.list_audit(limit=min(limit, 500))}

    @app.get("/api/v1/services")
    async def services(request: Request, session: dict[str, Any] = Depends(current_session)) -> dict[str, Any]:
        engine = request.app.state.engine
        return {
            "ovs": engine.ovs.health(),
            "frr": engine.frr.status(),
            "lldp": engine.lldp_status(),
            "bridge": engine.bridge,
            "web": {"version": __version__, "uptime": engine.system_info()["uptime_seconds"]},
        }

    # ---- users ----------------------------------------------------------
    @app.get("/api/v1/users")
    async def list_users(request: Request, session: dict[str, Any] = Depends(require("admin"))) -> dict[str, Any]:
        return {"users": request.app.state.engine.store.list_users()}

    @app.post("/api/v1/users")
    async def create_user(request: Request, payload: dict[str, Any] = Body(...), session: dict[str, Any] = Depends(require("admin"))) -> dict[str, Any]:
        security = request.app.state.security
        username = str(payload.get("username", "")).strip()
        password = str(payload.get("password", ""))
        role = str(payload.get("role", "readonly"))
        if not username or len(username) > 32:
            raise HTTPException(400, "invalid username")
        try:
            security.set_credentials(username, password, role, create=True)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        request.app.state.engine.store.audit(session["username"], client_ip(request), "user-create", username, "success")
        return {"ok": True}

    @app.delete("/api/v1/users/{username}")
    async def delete_user(username: str, request: Request, session: dict[str, Any] = Depends(require("admin"))) -> dict[str, Any]:
        if username == session["username"]:
            raise HTTPException(400, "cannot delete the account you are logged in with")
        request.app.state.engine.store.delete_user(username)
        request.app.state.engine.store.audit(session["username"], client_ip(request), "user-delete", username, "success")
        return {"ok": True}

    # ---- websockets -----------------------------------------------------
    def _ws_session(websocket: WebSocket) -> dict[str, Any] | None:
        return websocket.app.state.security.validate_session(websocket.cookies.get(COOKIE_NAME))

    def _origin_ok(websocket: WebSocket) -> bool:
        origin = websocket.headers.get("origin")
        if not origin:
            return True
        allowed = websocket.app.state.settings.get("api", {}).get("allowed_origins") or []
        host = websocket.headers.get("host", "")
        if allowed:
            return any(origin.startswith(a) for a in allowed)
        return host in origin

    @app.websocket("/api/v1/ws/stats")
    async def ws_stats(websocket: WebSocket) -> None:
        if not _origin_ok(websocket):
            await websocket.close(code=4403)
            return
        session = _ws_session(websocket)
        if not session:
            await websocket.close(code=4401)
            return
        await websocket.accept()
        hub: StatsHub = websocket.app.state.hub
        await hub.add(websocket)
        try:
            while True:
                message = await websocket.receive_text()
                if message == "ping":
                    await websocket.send_text(json.dumps({"type": "pong", "ts": time.time()}))
        except WebSocketDisconnect:
            pass
        except Exception:  # noqa: BLE001
            pass
        finally:
            await hub.remove(websocket)

    @app.websocket("/api/v1/ws/terminal")
    async def ws_terminal(websocket: WebSocket) -> None:
        if not _origin_ok(websocket):
            await websocket.close(code=4403)
            return
        session = _ws_session(websocket)
        if not session:
            await websocket.close(code=4401)
            return
        if not SecurityService.can(session["role"], "readonly"):
            await websocket.close(code=4403)
            return
        await websocket.accept()
        master, slave = pty.openpty()
        env = {
            **os.environ,
            "TERM": "xterm-256color",
            "MMS_USER": session["username"],
            "MMS_ROLE": session["role"],
            "COLUMNS": "120",
            "LINES": "40",
        }
        env.pop("MMS_ADMIN_PASSWORD", None)
        proc = subprocess.Popen(
            [sys.executable, "-m", "makemeswitch.cli.shell", "--user", session["username"], "--role", session["role"], "--no-banner"],
            stdin=slave,
            stdout=slave,
            stderr=slave,
            preexec_fn=os.setsid,
            env=env,
            close_fds=True,
        )
        os.close(slave)
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[bytes] = asyncio.Queue()

        def reader() -> None:
            try:
                data = os.read(master, 65536)
            except OSError:
                data = b""
            loop.call_soon_threadsafe(queue.put_nowait, data)

        loop.add_reader(master, reader)
        websocket.app.state.engine.store.audit(session["username"], None, "terminal-open", None, "success")

        async def pump_output() -> None:
            while True:
                data = await queue.get()
                if not data:
                    break
                await websocket.send_text(json.dumps({"type": "data", "data": data.decode("utf-8", "replace")}))

        output_task = asyncio.create_task(pump_output())
        try:
            while True:
                raw = await websocket.receive_text()
                try:
                    message = json.loads(raw)
                except json.JSONDecodeError:
                    os.write(master, raw.encode())
                    continue
                kind = message.get("type")
                if kind == "data":
                    os.write(master, str(message.get("data", "")).encode())
                elif kind == "resize":
                    try:
                        import fcntl
                        import struct
                        import termios

                        cols = max(20, min(int(message.get("cols", 120)), 400))
                        rows = max(10, min(int(message.get("rows", 40)), 200))
                        fcntl.ioctl(master, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
                    except Exception:  # noqa: BLE001
                        pass
        except WebSocketDisconnect:
            pass
        finally:
            output_task.cancel()
            with contextlib.suppress(Exception):
                loop.remove_reader(master)
            with contextlib.suppress(Exception):
                os.close(master)
            with contextlib.suppress(Exception):
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            with contextlib.suppress(Exception):
                proc.wait(timeout=3)
            websocket.app.state.engine.store.audit(session["username"], None, "terminal-close", None, "success")

    # ---- error handlers / static ---------------------------------------
    @app.exception_handler(NeedsConfirmation)
    async def needs_confirmation_handler(request: Request, exc: NeedsConfirmation) -> JSONResponse:
        return JSONResponse(status_code=409, content={"detail": exc.message, "preview": exc.preview, "requires_confirmation": True})

    def _apply_and_respond(request: Request, cfg: SwitchConfig, session: dict[str, Any], confirm: bool, action: str, quiet: bool = False) -> dict[str, Any]:
        engine = request.app.state.engine
        try:
            result = engine.apply(cfg, user=session["username"], confirm=confirm)
        except NeedsConfirmation as exc:
            # Return the preview with 409 so the UI can show a confirmation dialog.
            raise
        except ConfigError as exc:
            engine.store.audit(session["username"], client_ip(request), "config-apply", action, "failure", exc.message)
            raise HTTPException(400, exc.message) from exc
        engine.store.audit(
            session["username"],
            client_ip(request),
            "config-apply",
            action,
            "success",
            f"{len(result.ops)} change(s)",
        )
        return {"changed": result.changed, "ops": result.ops, "config": result.config.to_dict(), "unsaved_changes": engine.unsaved_changes()}

    index_file = WEB_ROOT / "index.html"
    if WEB_ROOT.is_dir():
        assets = WEB_ROOT / "assets"
        if assets.is_dir():
            app.mount("/assets", StaticFiles(directory=assets), name="assets")
        app.mount("/static", StaticFiles(directory=WEB_ROOT), name="static")

        @app.get("/{full_path:path}", include_in_schema=False)
        async def spa(full_path: str) -> Response:
            if full_path.startswith("api/"):
                raise HTTPException(404, "not found")
            candidate = WEB_ROOT / full_path
            if full_path and candidate.is_file():
                return FileResponse(candidate)
            return FileResponse(index_file)
    else:
        @app.get("/", include_in_schema=False)
        async def no_ui() -> dict[str, Any]:
            return {"detail": "Web panel is not built. Run 'npm run build' in frontend/.", "api": "/api/v1/system"}

    return app


def _features(engine: Engine) -> dict[str, Any]:
    lldp = engine.lldp_status()
    version = ""
    try:
        version = engine.ovs.version()
    except Exception:  # noqa: BLE001
        version = ""
    return {
        "ovs": {"supported": engine.ovs.installed, "version": version},
        "vlan": {"supported": True, "detail": "802.1Q access/trunk via OVS tag/trunks"},
        "stp": {"supported": True, "detail": "STP and RSTP provided by Open vSwitch"},
        "lacp": {"supported": True, "detail": "OVS bond with LACP active/passive"},
        "lldp": {"supported": bool(lldp.get("installed")), "detail": lldp.get("detail") or ("lldpd available" if lldp.get("installed") else "install lldpd")},
        "mirror": {"supported": True, "detail": "SPAN via OVS mirror"},
        "l3_svi": {"supported": True, "detail": "SVIs and static routes via Linux routing"},
        "frr": {"supported": bool(engine.frr.installed), "detail": "FRRouting for dynamic routing protocols" if engine.frr.installed else "vtysh not installed (optional)"},
        "storm_control": {"supported": False, "detail": "OVS has no per-port broadcast storm control; ingress policing is not equivalent"},
        "acl": {"supported": False, "detail": "ACL/QoS are not implemented in this release"},
        "mac_aging": {"supported": False, "detail": "OVS uses a fixed 300s MAC aging time"},
    }


app = create_app()
