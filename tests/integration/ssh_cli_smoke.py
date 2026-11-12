"""Real SSH verification: app-user auth, CLI over PTY, no escape hatches.

Runs an asyncssh server on a loopback high port with a temporary database and
a throwaway host key. No system sshd, no port 22, no host changes.
"""
import asyncio
import os
import socket
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT)]

import asyncssh  # noqa: E402

from makemeswitch.core.security import SecurityService  # noqa: E402
from makemeswitch.core.ssh_server import SwitchSSHServer, make_process_factory  # noqa: E402
from makemeswitch.core.store import Store  # noqa: E402

ADMIN_PW = "Admin" + "Pass" + "123456"
VIEWER_PW = "View" + "Pass" + "123456"


async def read_until_quiet(process, collected: list[str], idle: float = 1.5, total: float = 25) -> None:
    """Collect PTY output until no new bytes arrive for `idle` seconds."""
    deadline = asyncio.get_running_loop().time() + total
    while asyncio.get_running_loop().time() < deadline:
        try:
            chunk = await asyncio.wait_for(process.stdout.read(4096), timeout=idle)
        except asyncio.TimeoutError:
            return
        if not chunk:
            return
        collected.append(chunk if isinstance(chunk, str) else chunk.decode("utf-8", "replace"))


async def scenario(tmp: Path) -> int:
    db = tmp / "ssh.db"
    store = Store(db)
    security = SecurityService(store)
    security.set_credentials("admin", ADMIN_PW, "admin", create=True)
    security.set_credentials("viewer", VIEWER_PW, "readonly", create=True)

    host_key = tmp / "host_key"
    host_key.write_bytes(asyncssh.generate_private_key("ssh-ed25519").export_private_key())
    host_key.chmod(0o600)

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]

    logs: list[str] = []

    def log(message: str) -> None:
        logs.append(message)

    server = await asyncssh.create_server(
        lambda: SwitchSSHServer(str(db), log=log),
        "127.0.0.1",
        port,
        server_host_keys=[str(host_key)],
        encoding="utf-8",
        line_editor=False,
        line_echo=False,
        process_factory=make_process_factory(str(db), log=log),
    )
    print(f"ssh test server listening on 127.0.0.1:{port}")

    failures: list[str] = []

    def check(label: str, condition: bool, detail: str = "") -> None:
        print(f"{'OK  ' if condition else 'FAIL'} {label}")
        if not condition:
            if detail:
                print(f"--- detail ---\n{detail}\n--------------")
            failures.append(label)

    # 1. wrong password is rejected
    try:
        async with asyncssh.connect("127.0.0.1", port=port, username="admin",
                                    password="definitely-wrong", known_hosts=None):
            check("wrong password rejected", False, "connection succeeded")
    except (asyncssh.PermissionDenied, asyncssh.Error):
        check("wrong password rejected", True)

    # 2. correct password gives the CLI on a PTY and commands work
    collected: list[str] = []
    async with asyncssh.connect("127.0.0.1", port=port, username="admin",
                               password=ADMIN_PW, known_hosts=None) as conn:
        async with conn.create_process(term_type="xterm", term_size=(40, 120)) as process:
            reader = asyncio.create_task(read_until_quiet(process, collected))
            await asyncio.sleep(3)
            process.stdin.write("enable\n")
            await asyncio.sleep(0.5)
            process.stdin.write("show version\n")
            await asyncio.sleep(2)
            process.stdin.write("exit\n")
            try:
                await asyncio.wait_for(reader, timeout=25)
            except asyncio.TimeoutError:
                reader.cancel()
    output = "".join(collected)
    check("CLI prompt over SSH", "Switch" in output and (">" in output or "#" in output), output[-600:])
    check("command executed over SSH", "MakeMeSwitch CLI" in output, output[-600:])
    check("no cursor-position garbage", ";1R" not in output and "[1;1R" not in output, output[-300:])

    # 2b. Tab completion and a long idle period must both survive.
    session_out: list[str] = []
    async with asyncssh.connect("127.0.0.1", port=port, username="admin",
                               password=ADMIN_PW, known_hosts=None) as conn:
        async with conn.create_process(term_type="xterm", term_size=(120, 40)) as proc:
            async def keep_reading():
                while True:
                    chunk = await proc.stdout.read(4096)
                    if not chunk:
                        return
                    session_out.append(chunk)
            reader = asyncio.create_task(keep_reading())
            await asyncio.sleep(3)
            proc.stdin.write("ena\t")
            await asyncio.sleep(1.5)
            proc.stdin.write("\r")
            await asyncio.sleep(1)
            proc.stdin.write("sh run | incl\t")
            await asyncio.sleep(1.5)
            proc.stdin.write("\r")
            await asyncio.sleep(1)
            # Idle for 20 s: the old PTY bridge used to freeze here.
            await asyncio.sleep(20)
            proc.stdin.write("show clock\r")
            await asyncio.sleep(1.5)
            proc.stdin.write("exit\r")
            try:
                await asyncio.wait_for(reader, timeout=15)
            except asyncio.TimeoutError:
                reader.cancel()
    text = "".join(session_out)
    check("Tab completes 'ena' -> enable", "enable" in text, text[:400])
    check("Tab completes pipe filter", "include" in text, text[-500:])
    check("session survives 20 s idle", text.count("Switch") >= 2, text[-400:])

    # 3. no exec, no SFTP, no forwarding
    async with asyncssh.connect("127.0.0.1", port=port, username="admin",
                               password=ADMIN_PW, known_hosts=None) as conn:
        try:
            result = await asyncio.wait_for(conn.run("id", check=False), timeout=10)
            text = (result.stdout or "") + (result.stderr or "")
            refused = result.exit_status != 0 and "uid=" not in text
            check("exec request refused", refused, f"exit={result.exit_status} out={text[:120]!r}")
        except Exception:  # noqa: BLE001 - a hard refusal is a pass too
            check("exec request refused", True)
        try:
            await conn.start_sftp_client()
            check("SFTP refused", False, "sftp was allowed")
        except (asyncssh.Error, OSError):
            check("SFTP refused", True)
        try:
            await conn.forward_remote_port("127.0.0.1", 0, "127.0.0.1", 22)
            check("remote port forwarding refused", False, "remote forwarding was allowed")
        except Exception:  # noqa: BLE001 - any refusal is a pass here
            check("remote port forwarding refused", True)

    # 4. read-only role cannot enter configuration mode
    viewer_output: list[str] = []
    async with asyncssh.connect("127.0.0.1", port=port, username="viewer",
                               password=VIEWER_PW, known_hosts=None) as conn:
        async with conn.create_process(term_type="xterm", term_size=(40, 120)) as process:
            reader = asyncio.create_task(read_until_quiet(process, viewer_output))
            await asyncio.sleep(3)
            process.stdin.write("enable\n")
            await asyncio.sleep(0.5)
            process.stdin.write("configure terminal\n")
            await asyncio.sleep(2)
            process.stdin.write("exit\n")
            try:
                await asyncio.wait_for(reader, timeout=25)
            except asyncio.TimeoutError:
                reader.cancel()
    text = "".join(viewer_output)
    check(
        "read-only user blocked from config mode",
        ("%" in text) or ("denied" in text.lower()) or ("not allowed" in text.lower())
        or ("privilege" in text.lower()),
        text[-600:],
    )

    server.close()
    if failures:
        print(f"\nSSH VERIFY FAILED: {failures}")
        return 1
    print("\nSSH VERIFIED: app-DB auth, CLI over PTY, exec/SFTP/forwarding refused, RBAC applies")
    return 0


def main() -> int:
    os.environ.setdefault("MMS_NO_RENAME", "1")
    with tempfile.TemporaryDirectory(prefix="mms-ssh-") as tmp:
        os.environ["MMS_DB"] = str(Path(tmp) / "ssh.db")
        return asyncio.run(scenario(Path(tmp)))


if __name__ == "__main__":
    raise SystemExit(main())
