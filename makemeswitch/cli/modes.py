"""CLI session modes and privilege handling."""
from __future__ import annotations

import getpass
import os
from dataclasses import dataclass, field
from typing import Any, Callable

from rich.console import Console

from makemeswitch.core.engine import Engine, NeedsConfirmation
from makemeswitch.core.models import ConfigError, SwitchConfig

ROLE_LEVEL = {"readonly": 0, "operator": 1, "admin": 2}

MODE_PROMPT = {
    "exec": ">",
    "priv": "#",
    "config": "(config)#",
    "if": "(config-if)#",
    "vlan": "(config-vlan)#",
    "pc": "(config-if)#",
    "svi": "(config-if)#",
    "ifrange": "(config-if-range)#",
}

MODE_BANNER = {
    "exec": "User EXEC",
    "priv": "Privileged EXEC",
    "config": "Global configuration",
    "if": "Interface configuration",
    "vlan": "VLAN configuration",
    "pc": "Port-channel configuration",
    "svi": "SVI configuration",
}

CONFIG_MODES = {"config", "if", "vlan", "pc", "svi"}


@dataclass
class Session:
    engine: Engine
    username: str = field(default_factory=lambda: os.environ.get("SUDO_USER") or getpass.getuser())
    role: str = field(default_factory=lambda: os.environ.get("MMS_ROLE", "admin"))
    interactive: bool = True
    auto_confirm: bool = False
    console: Console = field(default_factory=lambda: Console(highlight=False))
    mode: str = "exec"
    interface: str | None = None
    vlan_id: int | None = None
    channel_id: int | None = None
    svi_vlan: int | None = None
    range_members: list[str] = field(default_factory=list)
    ask: Callable[[str], str] | None = None
    running: bool = True
    exit_code: int = 0
    last_error: str | None = None
    last_command_tokens: list[str] = field(default_factory=list)
    pending_banner: tuple[str, str] | None = None

    def __post_init__(self) -> None:
        self.role = self.role if self.role in ROLE_LEVEL else "readonly"

    # --- helpers ---------------------------------------------------------
    @property
    def config(self) -> SwitchConfig:
        return self.engine.config

    @property
    def hostname(self) -> str:
        return self.engine.config.hostname

    @property
    def prompt(self) -> str:
        suffix = MODE_PROMPT.get(self.mode, ">")
        if self.mode in ("if", "pc", "svi"):
            return f"{self.hostname}{suffix}"
        return f"{self.hostname}{suffix}"

    def can(self, level: str) -> bool:
        return ROLE_LEVEL.get(self.role, 0) >= ROLE_LEVEL.get(level, 0)

    def error(self, message: str) -> None:
        self.last_error = message
        self.console.print(f"[bold red]% {message}[/bold red]" if self.console.is_terminal else f"% {message}")

    def info(self, message: str) -> None:
        self.console.print(message)

    def show(self, table: Any, **kwargs: Any) -> None:
        """Print a Rich table without ellipsis-truncating switch data."""
        for column in table.columns:
            if not column.no_wrap:
                column.overflow = "fold"
        self.console.print(table, **kwargs)

    def ask_confirm(self, message: str) -> bool:
        if self.auto_confirm:
            return True
        if not self.interactive or self.ask is None:
            return False
        try:
            answer = self.ask(message)
        except (EOFError, KeyboardInterrupt):
            return False
        return answer.strip().lower() in ("y", "yes")

    # --- configuration ---------------------------------------------------
    def commit(self, new_config: SwitchConfig, action: str = "configure", quiet: bool = False) -> bool:
        """Validate, preview and apply a configuration change."""
        try:
            result = self.engine.apply(new_config, user=self.username, confirm=False)
        except NeedsConfirmation as pending:
            if not self.can("operator"):
                self.error("authorization failed: configuration privilege required")
                return False
            if not quiet:
                self.console.print("[yellow]This change enslaves a physical NIC into the switch:[/yellow]" if self.console.is_terminal else "This change enslaves a physical NIC into the switch:")
                for op in pending.preview.get("ops", []):
                    self.console.print(f"  - {op['action']} {op['target']}: {op.get('detail', '')}")
            if not self.ask_confirm("Proceed with interface reassignment? [yes/no]: "):
                self.error("configuration aborted (no changes applied)")
                return False
            try:
                result = self.engine.apply(new_config, user=self.username, confirm=True)
            except (ConfigError, Exception) as exc:  # noqa: BLE001
                self.error(str(exc))
                return False
        except ConfigError as exc:
            self.error(exc.message)
            return False
        except Exception as exc:  # noqa: BLE001
            self.error(f"apply failed: {exc}")
            return False
        if result.changed and not quiet:
            self.console.print(f"[green]{len(result.ops)} change(s) applied to Open vSwitch.[/green]" if self.console.is_terminal else f"{len(result.ops)} change(s) applied to Open vSwitch.")
            for op in result.ops:
                self.console.print(f"  * {op['action']} {op['target']} {op.get('detail', '')}")
        return True

    def reset_mode(self) -> None:
        self.mode = "exec"
        self.interface = None
        self.vlan_id = None
        self.channel_id = None
        self.svi_vlan = None
