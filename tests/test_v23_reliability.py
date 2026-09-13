import copy
import json
from types import SimpleNamespace
from pathlib import Path

import pytest
import requests

import app


@pytest.fixture(autouse=True)
def restore_runtime_state():
    config = dict(app.CONFIG)
    state = copy.deepcopy(app.state)
    app.cache.invalidate()
    yield
    app.CONFIG.clear()
    app.CONFIG.update(config)
    app.state.clear()
    app.state.update(state)
    app.cache.invalidate()


def test_docker_compose_passes_upstream_identity_and_persists_data():
    compose = (Path(app.BASE_DIR) / 'docker-compose.yml').read_text(encoding='utf-8')
    for name in ['MANAGEMENT_KEY', 'MODELS_API_KEY', 'CLIPROXY_API_BASE', 'SETTINGS_PATH']:
        assert 'CLIPROXY_PANEL_' + name in compose
    assert ':/app/data' in compose
    assert 'docker.sock' not in compose


def test_version_payload_is_extracted_without_inventing_release():
    assert app._find_version_value({'build': {'version': 'v7.2.102'}}) == 'v7.2.102'
    assert app._find_version_value({'version': 'development'}) is None
    assert app._find_version_value({'build': {'commit': 'a' * 40}}) == 'a' * 40
    assert app._find_version_value({'providers': [{'version': 'v9.9.9'}]}) is None


def test_remote_capabilities_never_claim_host_control(monkeypatch):
    monkeypatch.setitem(app.CONFIG, 'deployment_mode', 'docker')
    assert app.get_capabilities()['service_control'] is False
    assert app.get_capabilities()['binary_update'] is False
    assert app.get_capabilities()['resource_scope'] == 'panel_environment'


def test_default_api_port_is_used_for_models_and_proxy(monkeypatch):
    monkeypatch.setitem(app.CONFIG, 'cliproxy_api_base', 'http://127.0.0.1')
    monkeypatch.setitem(app.CONFIG, 'cliproxy_api_port', 8317)
    assert app._compose_api_base_url() == 'http://127.0.0.1:8317'
    assert app._api_host_port() == ('127.0.0.1', 8317)
    seen = []
    response = Response(payload={'data': [{'id': 'fixture'}]})
    response.raise_for_status = lambda: None
    monkeypatch.setattr(app.http_session, 'get', lambda url, **kw: (seen.append(url) or response))
    assert app.app.test_client().get('/api/models').get_json()['models'] == [{'id': 'fixture'}]
    assert seen == ['http://127.0.0.1:8317/v1/models']


def test_systemctl_installed_without_systemd_is_not_controllable(monkeypatch):
    monkeypatch.setattr(app, 'is_linux', lambda: True)
    monkeypatch.setattr(app, 'command_available', lambda _: True)
    monkeypatch.setattr(app.os.path, 'isdir', lambda _: False)
    monkeypatch.setitem(app.CONFIG, 'deployment_mode', 'systemd')
    assert not app.get_capabilities()['service_control']


def test_long_log_line_is_bounded(tmp_path):
    path = tmp_path / 'long.log'
    path.write_bytes(b'x' * (8 * 1024 * 1024) + b'\n')
    lines = app.read_log_tail(str(path), max_lines=10)
    assert len(lines) <= 10
    assert all(len(line) <= 16 * 1024 for line in lines)


def test_status_available_before_collector_starts(monkeypatch):
    monkeypatch.setitem(app.state, 'dashboard_snapshot', None)
    monkeypatch.setitem(app.state, 'collector_time', None)
    response = app.app.test_client().get('/api/status')
    assert response.status_code == 200
    payload = response.get_json()
    assert payload['collection']['stale'] is True
    assert payload['service']['status'] == 'unknown'


class Response:
    def __init__(self, code=200, headers=None, payload=None):
        self.status_code = code
        self.headers = headers or {}
        self.content = json.dumps(payload or {}).encode()
        self.closed = False
        self.encoding = 'utf-8'
        self.raw = SimpleNamespace(read=lambda size, **kw: self.content[:size])

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def iter_content(self, chunk_size):
        yield self.content

    def close(self):
        self.closed = True


@pytest.mark.parametrize('code,expected', [(200, 'running'), (401, 'auth_required'), (403, 'auth_required'), (404, 'not_found'), (502, 'upstream_error')])
def test_probe_distinguishes_connection_states(monkeypatch, code, expected):
    app._reset_management_auth_state()
    response = Response(code, {'X-CPA-VERSION': '7.2.1', 'X-CPA-COMMIT': 'a' * 40})
    monkeypatch.setattr(app.http_session, 'get', lambda *args, **kwargs: response)
    result = app.probe_upstream(force=True)
    assert result['status'] == expected
    assert result['version'] == 'v7.2.1'
    assert result['available'] is (code == 200)
    assert response.closed
    assert all(not key.startswith('_') for key in app.upstream_snapshot())


def test_probe_cache_failure_retention_and_recovery(monkeypatch):
    app._reset_management_auth_state()
    calls = []

    def fetch(*args, **kwargs):
        calls.append(kwargs)
        assert kwargs['allow_redirects'] is False
        assert kwargs['stream'] is True
        return Response(headers={'X-CPA-VERSION': 'dev'})

    monkeypatch.setattr(app.http_session, 'get', fetch)
    assert app.probe_upstream()['version'] == 'dev'
    app.probe_upstream()
    assert len(calls) == 1

    def fail(*args, **kwargs):
        raise requests.ConnectionError('unreachable')

    monkeypatch.setattr(app.http_session, 'get', fail)
    failed = app.probe_upstream(force=True)
    assert failed['version'] == 'dev'
    assert failed['version_stale'] is True
    assert failed['status'] == 'unreachable'
    monkeypatch.setattr(app.http_session, 'get', fetch)
    assert app.probe_upstream(force=True)['version_stale'] is False


def test_probe_retained_version_is_stale_if_header_disappears(monkeypatch):
    app._reset_management_auth_state()
    monkeypatch.setattr(app.http_session, 'get', lambda *a, **kw: Response(headers={'X-CPA-VERSION': 'v7.2.1'}))
    app.probe_upstream(force=True)
    monkeypatch.setattr(app.http_session, 'get', lambda *a, **kw: Response(payload={'providers': [{'version': 'v9.9.9'}]}))
    result = app.probe_upstream(force=True)
    assert result['available'] is True
    assert result['version_stale'] is True
    assert result['version'] == 'v7.2.1'


def test_probe_discards_response_after_key_change(monkeypatch):
    app._reset_management_auth_state()

    def fetch(*a, **kw):
        app.CONFIG['management_key'] = 'new-test-key'
        app._reset_management_auth_state()
        return Response(headers={'X-CPA-VERSION': 'v7.2.1'})

    monkeypatch.setattr(app.http_session, 'get', fetch)
    assert app.probe_upstream(force=True) == {}
    assert app.state['upstream'] == {}


def test_log_catchup_blocks_auto_update():
    result = app.get_idle_state({'log_available': True, 'catching_up': True, 'last_time': None})
    assert result['is_idle'] is False
    assert result['reason'] == 'log_catching_up'


def test_settings_survive_reload_and_env_precedence(monkeypatch, tmp_path):
    path = tmp_path / 'settings.json'
    monkeypatch.setitem(app.CONFIG, 'deployment_mode', 'docker')
    monkeypatch.setitem(app.CONFIG, 'settings_path', str(path))
    monkeypatch.setattr(app, '_load_dotenv', lambda: {})
    for name in list(app.os.environ):
        if name.startswith(app.ENV_PREFIX):
            monkeypatch.delenv(name)
    assert app._update_dotenv_values({'management_key': 'test-saved-key', 'idle_threshold_seconds': 2400})
    monkeypatch.setenv('CLIPROXY_PANEL_MANAGEMENT_KEY', '')
    app.load_config_overrides()
    assert app.CONFIG['management_key'] == 'test-saved-key'
    assert app.CONFIG['idle_threshold_seconds'] == 2400
    monkeypatch.setenv('CLIPROXY_PANEL_MANAGEMENT_KEY', 'test-explicit-key')
    app.load_config_overrides()
    assert app.CONFIG['management_key'] == 'test-explicit-key'


def test_version_discovery_runs_with_auto_upgrade_disabled(monkeypatch):
    import threading
    monkeypatch.setattr(app, 'shutdown_event', threading.Event())
    wakeup = threading.Event()
    wakeup.set()
    monkeypatch.setattr(app, 'auto_update_wakeup', wakeup)
    monkeypatch.setitem(app.state, 'auto_update_enabled', False)
    calls = []

    def check(**kw):
        calls.append(kw)
        app.shutdown_event.set()
        app.auto_update_wakeup.set()
        return False

    monkeypatch.setattr(app, 'check_for_updates', check)
    app.auto_update_worker()
    assert calls == [{'use_cache': False}]


def test_collector_retries_after_failure(monkeypatch):
    import threading
    monkeypatch.setattr(app, 'shutdown_event', threading.Event())
    monkeypatch.setattr(app, 'collector_wakeup', threading.Event())
    calls = []

    def collect():
        calls.append(1)
        if len(calls) == 1:
            app.collector_wakeup.set()
            raise OSError('test transient failure')
        app.shutdown_event.set()
        app.collector_wakeup.set()

    monkeypatch.setattr(app, 'collect_runtime_snapshot', collect)
    app.collector_worker()
    assert len(calls) == 2
    assert 'OSError' in app.state['collector_error']


def test_snapshot_endpoints_do_not_collect(monkeypatch):
    def forbidden(*a, **kw):
        raise AssertionError('HTTP polling must not collect')
    for name in ['probe_upstream', 'get_system_resources', 'perform_health_check', 'parse_log_file']:
        monkeypatch.setattr(app, name, forbidden)
    client = app.app.test_client()
    for path in ['/api/status', '/api/resources', '/api/cliproxy-logs', '/api/health']:
        assert client.get(path).status_code == 200
