"""Post-install: put every usable physical NIC into the switch by default.

The management NIC (default route / any IP / this SSH session) is never
touched.  Ports without carrier are still attached — they start forwarding as
soon as a cable appears.
"""
from __future__ import annotations

import sys

from makemeswitch.core import interfaces as netif
from makemeswitch.core.engine import Engine


def main() -> int:
    engine = Engine()
    engine.sync_ports()
    protected = set(netif.protected_interfaces())
    cfg = engine.config
    attached, skipped = [], []
    for name, iface in sorted(cfg.interfaces.items()):
        linux = iface.linux_if
        if not linux or linux in protected:
            skipped.append(f"{name} ({linux})")
            continue
        iface.attached = True
        iface.admin_up = True
        attached.append(f"{name} ({linux})")
    if attached:
        errors = cfg.validate()
        if errors:
            print("Refusing to attach ports, config invalid: " + "; ".join(errors), file=sys.stderr)
            return 1
        engine.apply(cfg, user="installer", confirm=True)
        engine.save_startup(user="installer")
    print(f"Switch ports: {len(attached)} attached{', '.join(attached) if attached else ''}")
    if skipped:
        print("Management/absent interfaces left untouched: " + ", ".join(skipped))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
