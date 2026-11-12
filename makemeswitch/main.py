"""Entry point: CLI, API server, users and one-shot commands."""
from __future__ import annotations

import argparse
import getpass
import os
import sys

from makemeswitch import PROJECT_NAME, __version__


def _engine():
    from makemeswitch.core.engine import Engine

    return Engine()


def cmd_cli(args: argparse.Namespace) -> int:
    from makemeswitch.cli.shell import run_cli

    return run_cli(command=args.command, auto_confirm=args.yes, banner=not args.no_banner)


def cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    from makemeswitch.core.engine import load_settings

    settings = load_settings()
    api = settings.get("api", {})
    host = args.host or api.get("bind", "0.0.0.0")
    port = args.port or int(api.get("port", 8000))
    ssl_kwargs = {}
    if api.get("https"):
        ssl_kwargs = {"ssl_certfile": api.get("certfile"), "ssl_keyfile": api.get("keyfile")}
    uvicorn.run(
        "makemeswitch.api.server:app",
        host=host,
        port=port,
        log_level=os.environ.get("MMS_LOG_LEVEL", "info").lower(),
        proxy_headers=False,
        **ssl_kwargs,
    )
    return 0


def cmd_init(args: argparse.Namespace) -> int:
    engine = _engine()
    engine.bootstrap(apply=True)
    print(f"Ports registered: {len(engine.ports)}")
    for row in engine.port_table():
        print(f"  {row['name']:14s} {row['linux_if'] or '-':12s} {row['identity'] or '-'}")
    print(f"Bridge {engine.bridge} reconciled.")
    return 0


def cmd_users(args: argparse.Namespace) -> int:
    from makemeswitch.core.security import SecurityService

    engine = _engine()
    service = SecurityService(engine.store)
    if args.action == "list":
        users = service.list_users()
        if not users:
            print("No web users configured. Create one with: makemeswitch user add <name> --role admin")
        for user in users:
            print(f"{user['username']:20s} {user['role']:10s} disabled={bool(user['disabled'])} created={user['created_at']:.0f}")
        return 0
    if args.action in ("add", "passwd"):
        username = args.username
        if not username:
            print("username required", file=sys.stderr)
            return 2
        role = args.role or "admin"
        if args.action == "add" and service.get_user(username):
            print(f"user {username} already exists; use 'makemeswitch user passwd {username}'", file=sys.stderr)
            return 2
        password = args.password
        if not password:
            password = getpass.getpass("Password: ")
            confirm = getpass.getpass("Confirm password: ")
            if password != confirm:
                print("passwords do not match", file=sys.stderr)
                return 2
        try:
            service.set_credentials(username, password, role, create=args.action == "add")
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        print(f"user {username} {'created' if args.action == 'add' else 'updated'} with role {role}")
        return 0
    if args.action == "delete":
        service.delete_user(args.username)
        print(f"user {args.username} deleted")
        return 0
    return 2


def cmd_doctor(args: argparse.Namespace) -> int:
    from makemeswitch.core.engine import Engine

    engine = Engine()
    print(f"{PROJECT_NAME} {__version__}")
    print(f"root privileges: {'yes' if os.geteuid() == 0 else 'NO (configuration will fail)'}")
    ovs = engine.ovs.health()
    print(f"Open vSwitch: installed={ovs['installed']} running={ovs['running']} version={ovs.get('version')}")
    print(f"Bridge {engine.bridge}: {'present' if ovs.get('bridge_exists') else 'missing'}")
    print(f"Ports registered: {len(engine.ports)}")
    for row in engine.port_table():
        print(f"  {row['name']:14s} {str(row['linux_if']):12s} present={row['present']} attached={row['attached']} protected={row['protected']}")
    print(f"Database: {engine.store.path}")
    print(f"Users: {len(engine.store.list_users())}")
    return 0


def _normalise_argv(argv: list[str] | None) -> list[str]:
    """`makemeswitch`, `makemeswitch -c '...'` and friends all mean the CLI."""
    raw = list(sys.argv[1:] if argv is None else argv)
    subcommands = {"cli", "serve", "init", "doctor", "user"}
    flags = {"-h", "--help", "--version"}
    if raw and raw[0] not in subcommands and raw[0] not in flags:
        return ["cli", *raw]
    return raw


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="makemeswitch", description=f"{PROJECT_NAME} - manage a Linux host as an Ethernet switch")
    parser.add_argument("--version", action="version", version=f"{PROJECT_NAME} {__version__}")
    sub = parser.add_subparsers(dest="subcommand")

    cli_parser = sub.add_parser("cli", help="interactive switch CLI")
    cli_parser.add_argument("-c", "--command", action="append", help="run command(s) and exit")
    cli_parser.add_argument("--yes", action="store_true")
    cli_parser.add_argument("--no-banner", action="store_true")
    cli_parser.set_defaults(func=cmd_cli)

    serve = sub.add_parser("serve", help="run the REST API and web panel")
    serve.add_argument("--host")
    serve.add_argument("--port", type=int)
    serve.set_defaults(func=cmd_serve)

    init = sub.add_parser("init", help="detect NICs and reconcile OVS")
    init.set_defaults(func=cmd_init)

    doctor = sub.add_parser("doctor", help="diagnose the installation")
    doctor.set_defaults(func=cmd_doctor)

    users = sub.add_parser("user", help="manage web panel users")
    users.add_argument("action", choices=("add", "delete", "passwd", "list"))
    users.add_argument("username", nargs="?")
    users.add_argument("--password")
    users.add_argument("--role", choices=("admin", "operator", "readonly"), default="admin")
    users.set_defaults(func=cmd_users)

    args = parser.parse_args(_normalise_argv(argv))
    if not getattr(args, "func", None):
        from makemeswitch.cli.shell import run_cli

        return run_cli()
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
