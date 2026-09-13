import copy
import inspect
import io
import json
import os
from datetime import datetime
from pathlib import Path

import pytest

import app


@pytest.fixture(autouse=True)
def restore_globals():
    config_before = dict(app.CONFIG)
    state_before = copy.deepcopy(app.state)
    app.cache.invalidate()
    yield
    app.CONFIG.clear()
    app.CONFIG.update(config_before)
    app.state.clear()
    app.state.update(state_before)
    app.cache.invalidate()
    for func in (app.save_persistent_stats,):
        if hasattr(func, '_last_saved'):
            delattr(func, '_last_saved')


def _log_line(timestamp, status, method='GET', path='/v1/chat/completions', newline=True):
    line = (
        f'[{timestamp}] [--------] [info ] [gin_logger.go:92] '
        f'{status} | 12ms | 127.0.0.1 | {method} "{path}"'
    )
    return line + ('\n' if newline else '')


def _configure_log(tmp_path, monkeypatch, content):
    log_path = tmp_path / 'main.log'
    log_path.write_text(content, encoding='utf-8')
    monkeypatch.setitem(app.CONFIG, 'cliproxy_log', str(log_path))
    monkeypatch.setitem(app.CONFIG, 'log_stats_path', str(tmp_path / 'log_stats.json'))
    monkeypatch.setitem(app.CONFIG, 'log_timezone', '+08:00')
    app.state['log_stats'] = app._new_log_stats_state()
    app.state['log_stats_loaded'] = True
    app.cache.invalidate()
    return log_path


def test_bundled_quotes_are_all_loaded_and_legacy_lines_are_repaired(monkeypatch):
    monkeypatch.setitem(app.CONFIG, 'quotes_path', app.BUNDLED_QUOTES_PATH)
    quotes = app.load_quotes()
    assert len(quotes) == 181
    assert all(item['text'] and item['author'] for item in quotes)

    trump = app._parse_quote_line(
        '让美国再次伟大！(Make America Great Again!)。特朗普/Donald Trump（第45任及47任美国总统）'
    )
    assert trump['text'] == '让美国再次伟大！(Make America Great Again!)。'
    assert trump['author'].startswith('特朗普/Donald Trump')

    duplicated_marker = app._parse_quote_line('十步杀一人。出自：出自：李白（诗仙）')
    assert duplicated_marker == {'text': '十步杀一人。', 'author': '李白（诗仙）'}


def test_timezone_inference_and_conversion(monkeypatch):
    file_time = datetime(2026, 7, 21, 7, 0, tzinfo=app.UTC).timestamp()
    assert app._infer_timezone_offset_seconds('2026-07-21 15:00:00', file_time) == 8 * 3600

    monkeypatch.setitem(app.CONFIG, 'log_timezone', 'auto')
    assert app._log_time_iso('2026-07-21 15:00:00', 8 * 3600) == '2026-07-21T07:00:00Z'

    monkeypatch.setitem(app.CONFIG, 'log_timezone', 'America/New_York')
    assert app._log_time_iso('2026-07-21 12:00:00') == '2026-07-21T16:00:00Z'
    assert app._log_time_iso('2026-01-21 12:00:00') == '2026-01-21T17:00:00Z'


def test_api_base_url_does_not_double_append_ports():
    assert app._compose_api_base_url('http://127.0.0.1:9000', 8317) == 'http://127.0.0.1:9000'
    assert app._compose_api_base_url('https://example.com/api/', 8317) == 'https://example.com:8317/api'
    assert app._compose_api_base_url('http://[::1]', 8317) == 'http://[::1]:8317'
    with pytest.raises(ValueError):
        app._compose_api_base_url('file:///tmp/socket', 8317)


def test_semantic_version_ordering():
    assert app._release_version_key('v2.0.0') > app._release_version_key('v1.99.99')
    assert app._release_version_key('v1.2.3-rc.10') > app._release_version_key('v1.2.3-rc.2')
    assert app._release_version_key('v1.2.3') > app._release_version_key('v1.2.3-rc.10')
    assert app._release_version_key('v1.2.3+build.5') == app._release_version_key('v1.2.3+build.9')
    assert app._release_version_key('v1.2.3-rc.1+build.5') is not None


def test_anonymous_release_check_prefers_latest_redirect(monkeypatch):
    monkeypatch.delenv('CLIPROXY_PANEL_GITHUB_TOKEN', raising=False)
    monkeypatch.delenv('GITHUB_TOKEN', raising=False)
    urls = []

    class RedirectResponse:
        def __init__(self):
            self.headers = {'Location': '/router-for-me/CLIProxyAPI/releases/tag/v7.2.102'}
            self.url = 'https://github.com/router-for-me/CLIProxyAPI/releases/latest'

        def close(self):
            pass

    def fake_get(url, **_kwargs):
        urls.append(url)
        if 'api.github.com' in url:
            raise AssertionError('anonymous release check consumed the GitHub API')
        return RedirectResponse()

    monkeypatch.setattr(app.http_session, 'get', fake_get)
    assert app.get_github_release_version(use_cache=False) == 'v7.2.102'
    assert urls == ['https://github.com/router-for-me/CLIProxyAPI/releases/latest']


def test_auto_update_failure_backoff_persists_and_resets_for_new_release(tmp_path, monkeypatch):
    retry_path = tmp_path / 'auto_update_state.json'
    monkeypatch.setattr(app, 'AUTO_UPDATE_STATE_PATH', str(retry_path))
    monkeypatch.setitem(app.CONFIG, 'auto_update_failure_backoff_seconds', 6 * 3600)
    monkeypatch.setitem(app.CONFIG, 'auto_update_failure_backoff_max_seconds', 24 * 3600)
    now = datetime(2026, 7, 27, 12, 0, tzinfo=app.UTC)
    monkeypatch.setattr(app, '_utc_now', lambda: now)

    first = app._record_auto_update_failure('v7.2.102')
    second = app._record_auto_update_failure('v7.2.102')
    third = app._record_auto_update_failure('v7.2.102')
    assert first['retry_in_seconds'] == 6 * 3600
    assert second['retry_in_seconds'] == 12 * 3600
    assert third['retry_in_seconds'] == 24 * 3600
    assert retry_path.exists()

    app.state['auto_update_failure_count'] = 0
    app.state['auto_update_retry_not_before'] = None
    app.state['auto_update_failed_version'] = None
    assert app.load_auto_update_failure_state() is True
    assert app.state['auto_update_failure_count'] == 3
    assert app.state['auto_update_failed_version'] == 'v7.2.102'

    assert app._clear_failure_for_new_release('v7.2.103') is True
    assert app._auto_update_failure_snapshot()['failure_count'] == 0


def test_incremental_log_parser_handles_methods_exclusions_partial_lines_and_rotation(tmp_path, monkeypatch):
    content = ''.join([
        _log_line('2026-07-21 15:00:00', 302, 'GET'),
        _log_line('2026-07-21 15:00:30', 200, 'GET', '/v0/management/usage'),
        _log_line('2026-07-21 15:01:00', 500, 'PUT'),
        _log_line('2026-07-21 15:02:00', 204, 'PATCH', newline=False),
    ])
    log_path = _configure_log(tmp_path, monkeypatch, content)

    first = app.get_request_count_from_logs()
    assert (first['count'], first['success'], first['failed']) == (2, 1, 1)
    assert first['last_time'] == '2026-07-21T07:01:00Z'

    with log_path.open('a', encoding='utf-8') as handle:
        handle.write('\n')
    app.cache.invalidate('request_count_logs')
    second = app.get_request_count_from_logs()
    assert (second['count'], second['success'], second['failed']) == (3, 2, 1)
    assert second['last_time'] == '2026-07-21T07:02:00Z'

    app.cache.invalidate('request_count_logs')
    assert app.get_request_count_from_logs()['count'] == 3

    rotated = tmp_path / 'main.log.old'
    os.replace(log_path, rotated)
    log_path.write_text(_log_line('2026-07-21 15:03:00', 201, 'DELETE'), encoding='utf-8')
    app.cache.invalidate('request_count_logs')
    after_rotation = app.get_request_count_from_logs()
    assert (after_rotation['count'], after_rotation['success'], after_rotation['failed']) == (4, 3, 1)


def test_initial_log_scan_is_bounded(tmp_path, monkeypatch):
    filler = ('x' * (1024 * 1024 + 128)) + '\n'
    log_path = _configure_log(
        tmp_path,
        monkeypatch,
        filler + _log_line('2026-07-21 15:00:00', 200),
    )
    monkeypatch.setitem(app.CONFIG, 'log_initial_scan_max_mb', 1)
    app.state['log_stats'] = app._new_log_stats_state()
    app.cache.invalidate()

    result = app.get_request_count_from_logs()
    assert result['count'] == 1
    assert result['partial'] is True
    assert result['skipped_bytes'] > 0
    assert app.state['log_stats']['offset'] == log_path.stat().st_size


def test_idle_detection_is_timezone_safe_and_missing_logs_are_not_idle(monkeypatch):
    missing = app.get_idle_state({'last_time': None, 'log_available': False})
    assert missing['is_idle'] is False
    assert missing['reason'] == 'log_unavailable'

    monkeypatch.setitem(app.CONFIG, 'idle_threshold_seconds', 1800)
    monkeypatch.setattr(app, '_utc_now', lambda: datetime(2026, 7, 21, 8, 0, tzinfo=app.UTC))
    recent = app.get_idle_state({
        'last_time': '2026-07-21T07:50:00Z',
        'log_available': True,
        'timezone': {'offset_seconds': 8 * 3600},
    })
    assert recent['is_idle'] is False
    assert recent['idle_wait_seconds'] == 1200

    old = app.get_idle_state({
        'last_time': '2026-07-21T07:00:00Z',
        'log_available': True,
        'timezone': {'offset_seconds': 8 * 3600},
    })
    assert old['is_idle'] is True

    invalid = app.get_idle_state({'last_time': 'not-a-time', 'log_available': True})
    assert invalid['is_idle'] is False
    assert invalid['reason'] == 'invalid_timestamp'
    assert invalid['idle_wait_seconds'] is None


def test_usage_accumulator_ignores_disk_fallback_and_handles_resets():
    app.state['accumulated_stats'] = {
        'input_tokens': 10, 'output_tokens': 5, 'reasoning_tokens': 0, 'cached_tokens': 1,
        'total_requests': 10, 'success': 9, 'failure': 1,
    }
    app.state['last_snapshot'] = {
        'input_tokens': 100, 'output_tokens': 50, 'reasoning_tokens': 0, 'cached_tokens': 10,
        'total_requests': 100, 'success': 90, 'failure': 10,
    }
    tokens = {'input_tokens': 120, 'output_tokens': 55, 'cached_tokens': 12}
    requests = {'total_requests': 120, 'success': 108, 'failure': 12}

    first = app.update_accumulated_usage(tokens, requests, live=True)
    assert first == {
        'input_tokens': 30, 'output_tokens': 10, 'reasoning_tokens': 0, 'cached_tokens': 3,
        'total_requests': 30, 'success': 27, 'failure': 3,
    }
    assert app.update_accumulated_usage(tokens, requests, live=True) == first
    assert app.update_accumulated_usage({}, {}, live=False) == first

    reset = app.update_accumulated_usage(
        {'input_tokens': 5, 'output_tokens': 2, 'cached_tokens': 1},
        {'total_requests': 5, 'success': 4, 'failure': 1},
        live=True,
    )
    assert reset['input_tokens'] == 35
    assert reset['total_requests'] == 35

    app.state['usage_reset_pending'] = True
    baseline = app.update_accumulated_usage(
        {'input_tokens': 999, 'output_tokens': 999, 'cached_tokens': 999},
        {'total_requests': 999, 'success': 900, 'failure': 99},
        live=True,
    )
    assert baseline == reset
    assert app.state['usage_reset_pending'] is False


def test_dashboard_usage_read_never_performs_network_io(tmp_path, monkeypatch):
    snapshot_path = tmp_path / 'usage.json'
    snapshot_path.write_text('{"usage": {"total_requests": 7}}', encoding='utf-8')
    monkeypatch.setitem(app.CONFIG, 'usage_snapshot_path', str(snapshot_path))

    def unexpected_network(*_args, **_kwargs):
        raise AssertionError('dashboard read attempted network I/O')

    monkeypatch.setattr(app.http_session, 'get', unexpected_network)
    snapshot, meta = app.fetch_usage_snapshot(with_meta=True, allow_network=False)
    assert snapshot['usage']['total_requests'] == 7
    assert meta['source'] == 'disk'


def test_v7_usage_queue_fallback_is_persisted_without_double_count(tmp_path, monkeypatch):
    monkeypatch.setitem(app.CONFIG, 'persistent_stats_path', str(tmp_path / 'stats.json'))
    monkeypatch.setitem(app.CONFIG, 'usage_snapshot_path', str(tmp_path / 'usage.json'))
    records = [
        {
            'model': 'gpt-5', 'alias': 'gpt-5-high', 'failed': False,
            'tokens': {
                'input_tokens': 100, 'output_tokens': 20,
                'reasoning_tokens': 5, 'cached_tokens': 30, 'total_tokens': 120,
            },
        },
        {
            'model': 'gemini', 'failed': True,
            'tokens': {
                'input_tokens': 10, 'output_tokens': 2,
                'reasoning_tokens': 3, 'cached_tokens': 1, 'total_tokens': 15,
            },
        },
    ]

    class RawBody(io.BytesIO):
        def read(self, size=-1, decode_content=False):
            return super().read(size)

    class FakeResponse:
        def __init__(self, status, payload):
            self.status_code = status
            self.headers = {}
            self.encoding = 'utf-8'
            self.raw = RawBody(json.dumps(payload).encode())

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def raise_for_status(self):
            if self.status_code >= 400:
                raise app.requests.HTTPError(str(self.status_code))

    urls = []

    def fake_get(url, **_kwargs):
        urls.append(url)
        return FakeResponse(404, {}) if url.endswith('/usage') else FakeResponse(200, records)

    monkeypatch.setattr(app.http_session, 'get', fake_get)
    payload, meta = app.fetch_usage_snapshot(use_cache=False, with_meta=True)
    assert payload == {'queue_records': 2}
    assert meta['source'] == 'queue'
    assert any('/usage-queue?count=500' in url for url in urls)
    assert app.state['accumulated_stats']['total_requests'] == 2
    assert app.state['accumulated_stats']['reasoning_tokens'] == 3
    assert app.state['stats']['successful_requests'] == 1
    assert app.state['stats']['failed_requests'] == 1
    assert app.state['stats']['model_usage']['gpt-5-high'] == 1
    assert app.state['usage_counter_mode'] == 'queue'
    assert (tmp_path / 'stats.json').exists()

    before = dict(app.state['accumulated_stats'])
    app.update_accumulated_usage(
        {'input_tokens': 999, 'output_tokens': 999, 'reasoning_tokens': 10, 'cached_tokens': 999},
        {'total_requests': 999, 'success': 900, 'failure': 99},
        live=True,
    )
    assert app.state['accumulated_stats'] == before
    assert app.state['usage_counter_mode'] == 'cumulative'


def test_backup_cleanup_uses_filename_timestamp_not_preserved_mtime(tmp_path):
    binary = tmp_path / 'cliproxyapi'
    binary.write_bytes(b'binary')
    names = [
        'cliproxyapi.bak.20260721-010000-000001',
        'cliproxyapi.bak.20260721-020000-000001',
        'cliproxyapi.bak.20260721-030000-000001',
    ]
    paths = []
    for index, name in enumerate(names):
        path = tmp_path / name
        path.write_bytes(bytes([index]) * 16)
        # Deliberately reverse mtimes; cleanup must still use the name timestamp.
        os.utime(path, (1000 - index, 1000 - index))
        paths.append(path)

    deleted = app.cleanup_binary_backups(
        str(binary), keep=2, max_total_bytes=0, max_age_seconds=0,
    )
    assert str(paths[0]) in deleted
    assert not paths[0].exists()
    assert paths[1].exists() and paths[2].exists()


def test_update_prepares_before_stopping_and_rolls_binary_back(tmp_path, monkeypatch):
    binary = tmp_path / 'cliproxyapi'
    old_bytes = b'old-binary'
    binary.write_bytes(old_bytes)
    monkeypatch.setitem(app.CONFIG, 'cliproxy_binary', str(binary))
    monkeypatch.setitem(app.CONFIG, 'cliproxy_dir', str(tmp_path / 'not-a-repo'))
    monkeypatch.setitem(app.CONFIG, 'cliproxy_service', 'cliproxy')
    monkeypatch.setitem(app.CONFIG, 'backup_retention_count', 2)
    monkeypatch.setattr(app, 'is_linux', lambda: True)
    monkeypatch.setattr(app, 'command_available', lambda _name: True)
    monkeypatch.setattr(app, 'get_capabilities', lambda: {'service_control': True, 'binary_update': True, 'config_write': True, 'mode': 'systemd', 'resource_scope': 'host', 'reason': ''})

    calls = []
    starts = 0

    def fake_run(args, **_kwargs):
        nonlocal starts
        calls.append(tuple(args))
        if args[:2] == ['systemctl', 'start']:
            starts += 1
            if starts == 1:
                return False, '', 'new binary failed'
        return True, '', ''

    def fake_release(binary_path=''):
        Path(binary_path).write_bytes(b'n' * (128 * 1024))
        return True, 'verified', 'v9.9.9'

    monkeypatch.setattr(app, 'run_cmd', fake_run)
    monkeypatch.setattr(app, 'update_from_github_release', fake_release)
    monkeypatch.setattr(app, '_wait_for_service_healthy', lambda *_args, **_kwargs: (True, {'running': True}))

    success, result = app.perform_update()
    assert success is False
    assert 'rolled back' in result['message']
    assert binary.read_bytes() == old_bytes
    assert ('systemctl', 'stop', 'cliproxy') in calls
    assert starts == 2

    calls.clear()
    monkeypatch.setattr(app, 'update_from_github_release', lambda **_kwargs: (False, 'prepare failed', None))
    success, _ = app.perform_update()
    assert success is False
    assert not any(call[:2] == ('systemctl', 'stop') for call in calls)


def test_update_health_requires_management_config_http_200(monkeypatch):
    monkeypatch.setitem(app.CONFIG, 'management_key', 'management-key')
    monkeypatch.setattr(app, 'get_service_status', lambda **_kwargs: {'running': True})
    monkeypatch.setattr(app.time, 'sleep', lambda _seconds: None)
    statuses = iter([503, 200, 200])
    requests_seen = []

    class FakeResponse:
        def __init__(self, status):
            self.status_code = status

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    def fake_get(url, **kwargs):
        requests_seen.append((url, kwargs.get('headers', {})))
        return FakeResponse(next(statuses))

    monkeypatch.setattr(app.http_session, 'get', fake_get)
    healthy, details = app._wait_for_service_healthy('cliproxy', timeout=5)
    assert healthy is True
    assert details['management_config_status'] == 200
    assert len(requests_seen) == 3
    assert all(url.endswith('/v0/management/config') for url, _headers in requests_seen)
    assert all(headers['X-Management-Key'] == 'management-key' for _url, headers in requests_seen)


def test_update_rolls_back_when_management_endpoint_never_becomes_healthy(tmp_path, monkeypatch):
    binary = tmp_path / 'cliproxyapi'
    old_bytes = b'old-binary'
    binary.write_bytes(old_bytes)
    monkeypatch.setitem(app.CONFIG, 'cliproxy_binary', str(binary))
    monkeypatch.setitem(app.CONFIG, 'cliproxy_dir', str(tmp_path / 'not-a-repo'))
    monkeypatch.setitem(app.CONFIG, 'cliproxy_service', 'cliproxy')
    monkeypatch.setattr(app, 'is_linux', lambda: True)
    monkeypatch.setattr(app, 'command_available', lambda _name: True)
    monkeypatch.setattr(app, 'get_capabilities', lambda: {'service_control': True, 'binary_update': True, 'config_write': True, 'mode': 'systemd', 'resource_scope': 'host', 'reason': ''})
    monkeypatch.setattr(app, 'run_cmd', lambda *_args, **_kwargs: (True, '', ''))

    def fake_release(binary_path=''):
        Path(binary_path).write_bytes(b'n' * (128 * 1024))
        return True, 'verified', 'v9.9.9'

    health_results = iter([
        (False, {'running': True, 'management_config_status': 503}),
        (True, {'running': True, 'management_config_status': 200}),
    ])
    monkeypatch.setattr(app, 'update_from_github_release', fake_release)
    monkeypatch.setattr(app, '_wait_for_service_healthy', lambda *_args, **_kwargs: next(health_results))

    success, result = app.perform_update()
    assert success is False
    assert 'rolled back' in result['message']
    assert binary.read_bytes() == old_bytes


def test_runtime_does_not_start_deprecated_usage_polling():
    source = inspect.getsource(app.initialize_runtime)
    assert 'usage_snapshot_worker' not in source


def test_clear_stats_never_requests_deprecated_usage_endpoint(tmp_path, monkeypatch):
    snapshot_path = tmp_path / 'usage.json'
    snapshot_path.write_text('{"usage": {"total_requests": 7}}', encoding='utf-8')
    monkeypatch.setitem(app.CONFIG, 'usage_snapshot_path', str(snapshot_path))
    monkeypatch.setitem(app.CONFIG, 'persistent_stats_path', str(tmp_path / 'stats.json'))
    _configure_log(tmp_path, monkeypatch, _log_line('2026-07-21 15:00:00', 200))

    def unexpected_network(*_args, **_kwargs):
        raise AssertionError('stats clear requested a deprecated usage endpoint')

    monkeypatch.setattr(app.http_session, 'get', unexpected_network)
    response = app.app.test_client().post('/api/stats/clear', json={})
    assert response.status_code == 200


def test_stats_clear_never_copies_or_truncates_service_log(tmp_path, monkeypatch):
    log_path = _configure_log(tmp_path, monkeypatch, _log_line('2026-07-21 15:00:00', 200))
    original = log_path.read_bytes()
    monkeypatch.setitem(app.CONFIG, 'persistent_stats_path', str(tmp_path / 'stats.json'))
    monkeypatch.setattr(
        app,
        'fetch_usage_snapshot',
        lambda **_kwargs: ({'usage': {'total_requests': 50}}, {
            'source': 'disk', 'live': False, 'fetched_at': '2026-07-21T07:00:00Z',
        }),
    )

    response = app.app.test_client().post('/api/stats/clear', json={})
    assert response.status_code == 200
    assert log_path.read_bytes() == original
    assert not list(tmp_path.glob('main.log.bak*'))
    assert app.state['usage_reset_pending'] is True
    assert app.state['log_stats']['offset'] == log_path.stat().st_size


def test_panel_access_key_healthz_and_security_headers(monkeypatch):
    monkeypatch.setitem(app.CONFIG, 'panel_access_key', 'secret')
    client = app.app.test_client()

    assert client.get('/api/paths').status_code == 401
    assert client.get('/api/paths?panel_key=secret').status_code == 401
    assert client.get('/api/paths', headers={'X-Panel-Key': 'secret'}).status_code == 200
    assert client.get('/api/healthz').status_code == 200

    root = client.get('/')
    assert root.status_code == 200
    assert root.headers['X-Frame-Options'] == 'DENY'
    assert "default-src 'self'" in root.headers['Content-Security-Policy']


def test_loopback_binding_rejects_dns_rebinding_and_cross_site_posts(monkeypatch):
    monkeypatch.setitem(app.CONFIG, 'bind_host', '127.0.0.1')
    monkeypatch.setitem(app.CONFIG, 'panel_access_key', '')
    client = app.app.test_client()
    assert client.get('/api/healthz', headers={'Host': 'attacker.example'}).status_code == 400
    blocked = client.post(
        '/api/stats/clear',
        headers={
            'Host': '127.0.0.1',
            'Origin': 'https://attacker.example',
            'Sec-Fetch-Site': 'cross-site',
        },
        json={},
    )
    assert blocked.status_code == 403
    allowed = client.post(
        '/api/stats/clear',
        headers={
            'Host': '127.0.0.1',
            'Origin': 'http://127.0.0.1',
            'Sec-Fetch-Site': 'same-origin',
            'X-Panel-CSRF': '1',
        },
        json={},
    )
    assert allowed.status_code == 200


def test_non_loopback_binding_requires_a_strong_panel_key(monkeypatch):
    monkeypatch.setitem(app.CONFIG, 'bind_host', '127.0.0.1')
    monkeypatch.setitem(app.CONFIG, 'panel_access_key', '')
    app._validate_panel_security()

    monkeypatch.setitem(app.CONFIG, 'bind_host', '0.0.0.0')
    with pytest.raises(RuntimeError, match='at least 32 characters'):
        app._validate_panel_security()

    monkeypatch.setitem(app.CONFIG, 'panel_access_key', 'short-key')
    with pytest.raises(RuntimeError, match='at least 32 characters'):
        app._validate_panel_security()

    monkeypatch.setitem(app.CONFIG, 'panel_access_key', 'x' * 32)
    app._validate_panel_security()


def test_force_update_requires_a_real_boolean():
    response = app.app.test_client().post('/api/update', json={'force': 'false'})
    assert response.status_code == 400
    assert 'boolean' in response.get_json()['message']


def test_json_routes_reject_missing_or_non_object_payloads():
    client = app.app.test_client()
    for path in ('/api/config/auto-update', '/api/config/idle-threshold', '/api/test/connection'):
        assert client.post(path, data='not-json', content_type='text/plain').status_code == 400
        assert client.post(path, json=[]).status_code == 400


def test_record_request_normalizes_unhashable_models(tmp_path, monkeypatch):
    monkeypatch.setitem(app.CONFIG, 'persistent_stats_path', str(tmp_path / 'stats.json'))
    response = app.app.test_client().post('/api/record-request', json={
        'model': ['unexpected', 'list'],
        'status': 'success',
        'response_time': -1,
    })
    assert response.status_code == 200
    assert "['unexpected', 'list']" in app.state['stats']['model_usage']


def test_run_cmd_rejects_shell_strings():
    success, _, error = app.run_cmd('echo unsafe')
    assert success is False
    assert 'argument sequence' in error


def test_frontend_is_self_contained_accessible_and_responsive():
    from bs4 import BeautifulSoup

    html = (Path(app.BASE_DIR) / 'static' / 'index.html').read_text(encoding='utf-8')
    soup = BeautifulSoup(html, 'html.parser')
    assert 'fonts.googleapis.com' not in html
    css = (Path(app.BASE_DIR) / 'static' / 'dashboard.css').read_text(encoding='utf-8')
    js = (Path(app.BASE_DIR) / 'static' / 'dashboard.js').read_text(encoding='utf-8')
    assert '@media (max-width: 820px)' in css
    assert 'prefers-reduced-motion' in css
    assert 'backdrop-filter' not in css
    assert 'fonts.googleapis.com' not in css
    assert all(button.get('type') for button in soup.find_all('button'))
    for control in soup.find_all(['input', 'select', 'textarea']):
        if control.get('type') == 'checkbox':
            continue
        assert control.get('aria-label') or (
            control.get('id') and soup.find('label', attrs={'for': control.get('id')})
        )
    ids = [node.get('id') for node in soup.find_all(attrs={'id': True})]
    assert len(ids) == len(set(ids))
    assert "url.searchParams.get('panel_key')" not in js
    assert "new URLSearchParams(url.hash" in js
    client = app.app.test_client()
    assert client.get('/dashboard.css').status_code == 200
    assert client.get('/dashboard.js').status_code == 200
    assert client.get('/favicon.svg').status_code == 200
    assert client.get('/favicon.ico').status_code == 200
    assert client.get('/apple-touch-icon.png').status_code == 200
    from urllib.parse import urljoin
    for prefix in ['https://example.test/', 'https://example.test/cpax/']:
        for node in soup.select('link[href], script[src]'):
            url = node.get('href') or node.get('src')
            assert url.startswith('./')
            assert urljoin(prefix, url).startswith(prefix)
    assert "new URL('.', document.currentScript.src)" in js
