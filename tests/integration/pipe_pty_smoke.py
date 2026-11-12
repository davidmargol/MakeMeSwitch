"""Real PTY smoke for the ``|`` pipe: Tab menu, help, include and count.

Run with the dev venv (pexpect required). Uses the in-memory OVS backend, so
no host networking or NIC discovery is involved.
"""
import json
import os
from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / 'tests')]
os.environ['MMS_NO_RENAME'] = '1'


def child(tmp):
    import conftest
    from makemeswitch.core.store import Store
    from makemeswitch.cli import shell

    engine = conftest.engine.__wrapped__(Store(Path(tmp) / 'state.db'), conftest.FakeOvs())
    engine.settings['cli'] = {'history_file': str(Path(tmp) / 'history')}
    recorded = []
    original = shell.execute_line

    def record(session, line):
        recorded.append(line)
        original(session, line)

    shell.execute_line = record
    shell.run_cli(engine=engine, auto_confirm=True)
    (Path(tmp) / 'commands.json').write_text(json.dumps(recorded))


def parent():
    import pexpect

    with tempfile.TemporaryDirectory(prefix='mms-pipe-') as tmp:
        proc = pexpect.spawn(
            sys.executable,
            [str(Path(__file__).resolve()), '--child', tmp],
            encoding='utf-8',
            timeout=20,
            dimensions=(40, 120),
            env=dict(os.environ, TERM='xterm', PROMPT_TOOLKIT_NO_CPR='1'),
        )
        proc.logfile = sys.stdout
        proc.expect('TEST-SW>')
        proc.sendline('enable')
        proc.expect_exact('TEST-SW#')

        # Bare pipe + Tab lists every IOS filter with its description.
        proc.send('show run | \t\t')
        proc.expect(r'append')
        proc.expect(r'redirect')
        proc.expect(r'tee')
        proc.sendcontrol('c')
        proc.expect_exact('^C')
        proc.expect_exact('TEST-SW#')

        # ``?`` after a pipe prints the same menu without pressing Enter.
        proc.send('show run | ?')
        proc.expect(r'begin')
        proc.sendcontrol('c')
        proc.expect_exact('^C')
        proc.expect_exact('TEST-SW#')

        # Prefix completion of a filter name, then a working include filter.
        proc.send('show run | inc\t')
        proc.sendline(' hostname')
        proc.expect(r'hostname TEST-SW')
        proc.expect_exact('TEST-SW#')

        # redirect writes a file and prints nothing.
        target = Path(tmp) / 'snapshot.txt'
        proc.sendline(f'show run | redirect {target}')
        proc.expect_exact('TEST-SW#')

        proc.sendcontrol('d')
        proc.expect(pexpect.EOF)
        proc.close()
        assert proc.exitstatus == 0, proc.exitstatus
        assert target.is_file() and 'hostname TEST-SW' in target.read_text()
        commands = json.loads((Path(tmp) / 'commands.json').read_text())
        assert any('| include' in line for line in commands), commands
        print('\nPTY PIPE VERIFIED: Tab menu, ? help, include and redirect')
        print('Accepted commands:', commands)


if __name__ == '__main__':
    if len(sys.argv) > 1 and sys.argv[1] == '--child':
        child(sys.argv[2])
    else:
        parent()
