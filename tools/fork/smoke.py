"""Boot the built container, probe the whole stack, and check restart/persistence."""
# Docker is a trusted local CLI, invoked without a shell. URLs are loopback HTTP only.
# ruff: noqa: S404, S603, S607, S310
import json
import subprocess
import sys
import tempfile
import time
import urllib.request
import uuid
from pathlib import Path


def docker(*args):
    return subprocess.check_output(['docker', *args], text=True).strip()


def healthy(name):
    deadline = time.monotonic() + 300
    while time.monotonic() < deadline:
        state = json.loads(docker('inspect', name))[0]['State']
        if not state['Running']:
            raise RuntimeError('Container exited during startup')
        if state['Health']['Status'] == 'healthy':
            return
        time.sleep(5)
    raise TimeoutError('Container did not become healthy within five minutes')


def main():
    name = f'rotkibv-smoke-{uuid.uuid4().hex[:12]}'
    docker('run', '-d', '--name', name, '-p', '127.0.0.1::80', sys.argv[1])
    try:
        healthy(name)
        ports = json.loads(docker('inspect', name))[0]['NetworkSettings']['Ports']
        port = ports['80/tcp'][0]['HostPort']
        base = f'http://127.0.0.1:{port}'
        with urllib.request.urlopen(base, timeout=15) as response:
            assert b'<html' in response.read().lower(), 'Frontend is not HTML'
        with urllib.request.urlopen(f'{base}/api/1/ping', timeout=15) as response:
            assert json.load(response)['result'] is True, 'Backend ping failed'
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / 'marker'
            marker.write_text(name)
            docker('cp', str(marker), f'{name}:/data/.fork-smoke')
            docker('restart', '--time', '60', name)
            healthy(name)
            marker.unlink()
            docker('cp', f'{name}:/data/.fork-smoke', str(marker))
            assert marker.read_text() == name, 'Volume data lost after restart'
        print('PASS: container health, frontend, API, restart and persistent volume')
    finally:
        print(docker('logs', name))
        docker('rm', '-f', '-v', name)


if __name__ == '__main__':
    main()
