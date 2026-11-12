"""Installed-wheel HTTP asset smoke; lifespan off deliberately avoids host networking."""
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
import makemeswitch
from makemeswitch.api.server import WEB_ROOT

assert makemeswitch.__version__ == '0.1.6'
assert 'site-packages' in str(WEB_ROOT), WEB_ROOT
assert (WEB_ROOT / 'index.html').is_file()
with tempfile.TemporaryDirectory() as tmp:
    config = Path(tmp) / 'config.yaml'
    config.write_text(f'db_path: {tmp}/state.db\nlog_dir: {tmp}/logs\n')
    env = dict(os.environ, MMS_CONFIG=str(config))
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        port = s.getsockname()[1]
    proc = subprocess.Popen([sys.executable, '-m', 'uvicorn', 'makemeswitch.api.server:create_app', '--factory', '--lifespan', 'off', '--host', '127.0.0.1', '--port', str(port)], cwd=tmp, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    try:
        url = f'http://127.0.0.1:{port}'
        for _ in range(100):
            try:
                body = urllib.request.urlopen(url, timeout=1).read().decode()
                break
            except OSError:
                if proc.poll() is not None:
                    raise RuntimeError(proc.stderr.read().decode())
                time.sleep(.05)
        else:
            raise RuntimeError('HTTP readiness timeout')
        assert 'MakeMeSwitch' in body and '<html' in body
        assets = re.findall(r'(?:src|href)="(/assets/[^\"]+)"', body)
        assert len(assets) >= 2
        for asset in assets:
            response = urllib.request.urlopen(url + asset)
            assert response.status == 200 and len(response.read()) > 100
        print(f'INSTALLED WHEEL HTTP VERIFIED: 0.1.6, index + {len(assets)} assets, WEB_ROOT={WEB_ROOT}')
    finally:
        proc.terminate()
        proc.wait(timeout=10)
