"""Local-only browser fixture. Uses isolated temporary data and a fake upstream.

Run: python tests/preview_server.py
Never use this simulated upstream as a deployment / live service test.
"""
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
from datetime import datetime, timezone

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ['CLIPROXY_PANEL_BIND_HOST'] = '127.0.0.1'
os.environ['CLIPROXY_PANEL_PANEL_ACCESS_KEY'] = ''

import app  # noqa: E402
from flask import Flask, jsonify  # noqa: E402
from waitress import serve  # noqa: E402

root = Path(tempfile.mkdtemp(prefix='cpax-browser-fixture-'))
(root / 'auths').mkdir()
(root / 'config.yaml').write_text('port: 8766\nremote-management:\n  allow-remote: true\n', encoding='utf-8')
now = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
(root / 'main.log').write_text('\n'.join(
    f'[{now}] [gin_logger.go:100] {500 if i % 19 == 0 else 200} | 231ms | 10.0.0.2 | POST "/v1/chat/completions" fixture-request-{i}'
    for i in range(160)
) + '\n', encoding='utf-8')
app.DATA_DIR = str(root)
app.UPDATE_HISTORY_PATH = str(root / 'updates.json')
app.CONFIG.update(
    deployment_mode='docker', cliproxy_api_base='http://127.0.0.1', cliproxy_api_port=8766,
    management_key='local-fixture-key', models_api_key='local-fixture-key',
    cliproxy_config=str(root / 'config.yaml'), cliproxy_log=str(root / 'main.log'),
    cliproxy_stderr=str(root / 'stderr.log'), cliproxy_binary='', auth_dir=str(root / 'auths'),
    config_write_enabled=False, panel_access_key='', bind_host='127.0.0.1',
    pricing_auto_enabled=False, auto_update_enabled=False,
    log_timezone='UTC', disk_path=str(root.anchor),
)
for field in ['usage_snapshot_path', 'log_stats_path', 'persistent_stats_path', 'settings_path']:
    app.CONFIG[field] = str(root / (field + '.json'))
app.get_github_release_version = lambda **kw: 'v7.2.2'
app.state['auto_update_enabled'] = False
upstream = Flask('fixture')


@upstream.route('/v0/management/config')
def config():
    response = jsonify({'port': 8766})
    response.headers['X-CPA-VERSION'] = 'v7.2.1'
    response.headers['X-CPA-COMMIT'] = 'a' * 40
    response.headers['X-CPA-BUILD-DATE'] = '2026-09-11T00:00:00Z'
    return response


@upstream.route('/v1/models')
def models():
    return jsonify({'data': [{'id': 'fixture-model', 'owned_by': 'local-test', 'object': 'model'}]})


if __name__ == '__main__':
    threading.Thread(target=lambda: serve(upstream, host='127.0.0.1', port=8766), daemon=True).start()
    app.initialize_runtime()
    print(json.dumps({'fixture_root': str(root), 'url': 'http://127.0.0.1:8765'}, ensure_ascii=True), flush=True)
    serve(app.app, host='127.0.0.1', port=8765, threads=4)
