"""Exercise the built image via real Docker networking and volume recreation.

Run after `docker build -t cpax-panel:ci .`: python tests/container_smoke.py
Creates only randomly named test containers/network/volume and cleans them up.
"""
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import secrets
import subprocess
import time
import urllib.error
import urllib.request
import uuid


def docker(*args):
    return subprocess.check_output(['docker', *args], text=True, timeout=120).strip()


def main():
    prefix = 'cpax-test-' + uuid.uuid4().hex[:10]
    network, upstream, panel, volume = [prefix + suffix for suffix in ['-net', '-up', '-panel', '-data']]
    panel_key, management_key = secrets.token_hex(24), secrets.token_hex(24)
    image = os.environ.get('CPAX_TEST_IMAGE', 'cpax-panel:ci')
    fixture = str(Path(__file__).with_name('fake_upstream.py').resolve())
    env = [
        '-e', 'CLIPROXY_PANEL_PANEL_ACCESS_KEY=' + panel_key,
        '-e', 'CLIPROXY_PANEL_MANAGEMENT_KEY=' + management_key,
        '-e', 'CLIPROXY_PANEL_MODELS_API_KEY=' + management_key,
        '-e', 'CLIPROXY_PANEL_DEPLOYMENT_MODE=docker',
        '-e', 'CLIPROXY_PANEL_AUTO_UPDATE_ENABLED=false',
        '-e', 'CLIPROXY_PANEL_PRICING_AUTO_ENABLED=false',
        '-e', 'CLIPROXY_PANEL_CLIPROXY_API_BASE=http://' + upstream,
        '-e', 'CLIPROXY_PANEL_CLIPROXY_API_PORT=8317',
    ]
    base = ''

    def start_panel():
        nonlocal base
        docker('run', '-d', '--name', panel, '--network', network,
               '-p', '127.0.0.1::8080', '-v', volume + ':/app/data', *env, image)
        port = docker('port', panel, '8080/tcp').split(':')[-1]
        base = 'http://127.0.0.1:' + port

    def request(path, data=None, authenticated=True):
        headers = {'X-Panel-Key': panel_key} if authenticated else {}
        if data is not None:
            headers.update({'Content-Type': 'application/json', 'X-Panel-CSRF': '1'})
        req = urllib.request.Request(base + path, headers=headers,
                                     data=json.dumps(data).encode() if data is not None else None)
        with urllib.request.urlopen(req, timeout=5) as response:
            raw = response.read()
        return json.loads(raw) if path.startswith('/api/') else raw

    def wait_for(predicate, seconds=75):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            try:
                if predicate():
                    return
            except (OSError, ValueError):
                pass
            time.sleep(1)
        raise AssertionError('Container state did not reach expected condition')

    try:
        docker('network', 'create', network)
        docker('volume', 'create', volume)
        docker('run', '-d', '--name', upstream, '--network', network,
               '-e', 'FIXTURE_KEY=' + management_key, '-v', fixture + ':/fixture.py:ro', image,
               'python', '/fixture.py')
        start_panel()
        wait_for(lambda: request('/api/status')['upstream']['status'] == 'running')
        data = request('/api/status')
        assert data['version']['current'] == 'v7.2.1'
        assert data['capabilities']['service_control'] is False
        assert data['capabilities']['binary_update'] is False
        assert data['collection']['stale'] is False
        assert request('/api/models')['models'][0]['id'] == 'fixture-model'
        assert request('/api/healthz', authenticated=False)['ok']
        for asset in ['/', '/dashboard.js', '/dashboard.css']:
            assert len(request(asset, authenticated=False)) > 100
        try:
            request('/api/status', authenticated=False)
            raise AssertionError('Protected endpoint accepted unauthenticated request')
        except urllib.error.HTTPError as exc:
            assert exc.code == 401
        for endpoint in ['/api/service/restart', '/api/update']:
            try:
                request(endpoint, {})
                raise AssertionError('Container accepted host-control operation')
            except urllib.error.HTTPError as exc:
                assert exc.code == 409
        assert request('/api/config/idle-threshold', {'threshold': 2400})['success']
        docker('stop', '--time', '15', panel)
        docker('rm', panel)
        start_panel()
        wait_for(lambda: request('/api/status').get('config', {}).get('idle_threshold') == 2400)
        # Real upstream outage: status requests must remain responsive and not report stopped systemd.
        docker('stop', '--time', '3', upstream)
        wait_for(lambda: request('/api/status')['upstream']['status'] == 'unreachable')
        durations = []

        def sample(_):
            start = time.monotonic()
            request('/api/status')
            return time.monotonic() - start

        with ThreadPoolExecutor(max_workers=8) as pool:
            durations = list(pool.map(sample, range(200)))
        assert max(durations) < 5
        docker('start', upstream)
        wait_for(lambda: request('/api/status')['upstream']['status'] == 'running')
        print(json.dumps({'result': 'pass', 'requests_during_outage': 200,
                          'max_status_seconds': round(max(durations), 3),
                          'volume_recreation': 'pass', 'version': 'v7.2.1'}))
    finally:
        for name in [panel, upstream]:
            subprocess.run(['docker', 'rm', '-f', name], capture_output=True, timeout=30)
        subprocess.run(['docker', 'volume', 'rm', volume], capture_output=True, timeout=30)
        subprocess.run(['docker', 'network', 'rm', network], capture_output=True, timeout=30)


if __name__ == '__main__':
    main()
