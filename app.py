#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
CPA-X 管理面板后端 v2.3.0
功能: 为 CLIProxyAPI 提供监控统计、健康检查、资源监控、配置管理、API测试、模型管理
优化: 缓存机制、预编译正则、非阻塞监控、减少shell调用
"""

import os
import atexit
import json
import time
import math
import subprocess
import threading
import re
import platform
import shutil
import stat as stat_module
import tempfile
import tarfile
import hashlib
import hmac
import random
import signal
import socket
import ipaddress
from datetime import datetime, timedelta, timezone
from collections import deque
from urllib.parse import urlsplit, urlunsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from flask import Flask, jsonify, request, send_from_directory, Response
from flask_cors import CORS
import requests
from requests.adapters import HTTPAdapter

# 面板自身版本（与 GitHub Release/README 同步）
PANEL_NAME = "CPA-X"
PANEL_VERSION = "2.3.0"
PRICING_BASIS_TOKENS = 1_000_000
PRICING_BASIS_LABEL = '百万Tokens'
PRICING_BASIS_TEXT = f'美元/{PRICING_BASIS_LABEL}'

# ==================== 预编译正则表达式 ====================
# 日志格式: [2026-01-17 05:21:09] [--------] [info ] [gin_logger.go:92] 200 |            0s |       127.0.0.1 | GET     "/v1/models"
REQUEST_LOG_PATTERN = re.compile(
    r'\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\].*\[gin_logger\.go:\d+\]\s+(\d+)\s+\|\s+(\S+)\s+\|\s*([^|]+?)\s*\|\s+([A-Z]+)\s+"([^"]+)"'
)
REQUEST_METHOD_PATTERN = re.compile(r'\|\s*(GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS)\s+"')
REQUEST_STATUS_PATTERN = re.compile(r'\[gin_logger\.go:\d+\]\s+(\d{3})\s+\|')
HASH_VERSION_PATTERN = re.compile(r'^[0-9a-f]{7,40}$', re.IGNORECASE)
EXCLUDED_LOG_PATHS = (
    '"/v0/management/',
    '"/v1/models',
)
MANAGEMENT_AUTH_MAX_FAILURES = 10
MANAGEMENT_AUTH_FAILURE_STATUSES = {401, 403}
MAX_MODEL_USAGE_ENTRIES = 500
BACKUP_RETENTION_COUNT = 2
BACKUP_TS_PATTERN = re.compile(r'\.bak\.(\d{8}-\d{6}(?:-\d{6})?)$')
PANEL_ACCESS_KEY_MIN_LENGTH = 32
LOG_TIME_PATTERN = re.compile(r'\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\]')
VERSION_PATTERN = re.compile(
    r'^[vV]?(?P<release>\d+(?:\.\d+){1,3})'
    r'(?P<suffix>(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?)$'
)
TIMEZONE_OFFSET_PATTERN = re.compile(r'^(?:UTC|GMT)?(?P<sign>[+-])(?P<hours>\d{1,2})(?::?(?P<minutes>\d{2}))?$', re.IGNORECASE)
UTC = timezone.utc
BUNDLED_QUOTES_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'X.txt')

# 可选依赖
try:
    import psutil
    HAS_PSUTIL = True
except ImportError:
    HAS_PSUTIL = False
    print("Warning: psutil not installed. Resource monitoring will be limited.")

try:
    import yaml
    HAS_YAML = True
except ImportError:
    HAS_YAML = False
    print("Warning: pyyaml not installed. Config validation will be limited.")

try:
    from waitress import serve as waitress_serve
    HAS_WAITRESS = True
except ImportError:
    waitress_serve = None
    HAS_WAITRESS = False

app = Flask(__name__, static_folder='static', static_url_path='')
app.config['MAX_CONTENT_LENGTH'] = 3 * 1024 * 1024

# 配置
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, 'data')

CONFIG = {
    'cliproxy_dir': '/opt/CLIProxyAPI',
    'cliproxy_config': '/opt/CLIProxyAPI/config.yaml',
    'cliproxy_binary': '/opt/CLIProxyAPI/cliproxy',
    'cliproxy_log': '/opt/CLIProxyAPI/logs/main.log',  # CLIProxy 主日志
    'cliproxy_stderr': '/var/log/cliproxy/stderr.log',
    'auth_dir': '/opt/CLIProxyAPI/data',
    'cliproxy_service': 'cliproxy',
    'panel_port': 8080,
    'idle_threshold_seconds': 1800,  # 30分钟
    'auto_update_check_interval': 300,
    'auto_update_enabled': True,
    'auto_update_failure_backoff_seconds': 21600,
    'auto_update_failure_backoff_max_seconds': 86400,
    'service_health_timeout_seconds': 45,
    # CLIProxyAPI 使用进程本地时区写入无偏移日志。auto 会从日志时间与文件 mtime 推断，
    # 也可显式填写 local、UTC、+08:00 或 IANA 时区（如 Asia/Shanghai）。
    'log_timezone': 'auto',
    'cliproxy_api_port': 8317,  # CLIProxy API端口
    'cliproxy_api_base': 'http://127.0.0.1',
    'models_api_key': '',
    'management_key': '',
    'config_write_enabled': False,
    'usage_snapshot_path': os.path.join(DATA_DIR, 'usage_snapshot.json'),
    'log_stats_path': os.path.join(DATA_DIR, 'log_stats.json'),
    'persistent_stats_path': os.path.join(DATA_DIR, 'persistent_stats.json'),
    'pricing_input': 0.0,
    'pricing_output': 0.0,
    'pricing_cache': 0.0,
    # Token 价格自动同步（默认启用；当手动价格为 0 时会尝试从权威来源补齐）
    'pricing_auto_enabled': True,
    'pricing_auto_source': 'openrouter',  # 目前仅实现 openrouter
    'pricing_auto_model': '',  # 为空时会从 config.yaml 里挑一个模型，最后回退到 openai/gpt-4o-mini
    'quotes_path': os.path.join(BASE_DIR, 'X.txt'),
    'disk_path': '/',
    # 二进制备份同时受数量、保留天数和总大小限制，避免长期自动更新挤满磁盘。
    'backup_retention_count': BACKUP_RETENTION_COUNT,
    'backup_max_age_days': 14,
    'backup_max_total_mb': 512,
    'log_initial_scan_max_mb': 64,
    'log_clear_enabled': False,
    'update_require_checksum': True,
    # 安全默认值：本机直接运行只监听回环地址；非回环监听必须同时配置强访问密钥。
    'bind_host': '127.0.0.1',
    'panel_access_key': '',
    # 逗号分隔的跨域来源；留空时仅允许浏览器同源访问。
    'cors_origins': '',
    # auto detects Docker/remote/systemd; monitoring never requires host privileges.
    'deployment_mode': 'auto',
    'settings_path': os.path.join(DATA_DIR, 'panel_settings.json'),
    'log_scan_budget_mb': 4,
}

ENV_PREFIX = 'CLIPROXY_PANEL_'
dotenv_lock = threading.Lock()

CONFIG_TYPES = {
    'panel_port': int,
    'idle_threshold_seconds': int,
    'auto_update_check_interval': int,
    'auto_update_enabled': bool,
    'auto_update_failure_backoff_seconds': int,
    'auto_update_failure_backoff_max_seconds': int,
    'service_health_timeout_seconds': int,
    'backup_retention_count': int,
    'backup_max_age_days': int,
    'backup_max_total_mb': int,
    'log_initial_scan_max_mb': int,
    'log_scan_budget_mb': int,
    'log_clear_enabled': bool,
    'update_require_checksum': bool,
    'config_write_enabled': bool,
    'cliproxy_api_port': int,
    'pricing_input': float,
    'pricing_output': float,
    'pricing_cache': float,
    'pricing_auto_enabled': bool,
}


def _panel_access_key_expected() -> str:
    return str(CONFIG.get('panel_access_key', '') or '').strip()


def _bind_host_is_loopback(value=None) -> bool:
    host = str(value if value is not None else CONFIG.get('bind_host', '') or '').strip()
    if not host:
        return True
    normalized = host.strip('[]').lower()
    if normalized == 'localhost':
        return True
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        # Unknown hostnames may resolve outside the machine, so fail closed.
        return False


def _validate_panel_security() -> None:
    if _bind_host_is_loopback():
        return
    if len(_panel_access_key_expected()) < PANEL_ACCESS_KEY_MIN_LENGTH:
        raise RuntimeError(
            'Non-loopback panel binding requires '
            f'CLIPROXY_PANEL_PANEL_ACCESS_KEY with at least {PANEL_ACCESS_KEY_MIN_LENGTH} characters'
        )


def _panel_access_key_provided() -> str:
    # Mutating APIs deliberately accept the secret only through a custom
    # header. Cookie/query authentication would make CSRF and access-log leaks
    # much easier; the SPA converts its one-time URL parameter into this header.
    return str(request.headers.get('X-Panel-Key') or '').strip()


def _request_host_is_allowed() -> bool:
    """Prevent DNS rebinding when the panel is bound to loopback."""
    if not _bind_host_is_loopback():
        return True
    raw_host = str(request.host or '').strip()
    hostname = raw_host
    if raw_host.startswith('['):
        hostname = raw_host[1:].split(']', 1)[0]
    elif raw_host.count(':') == 1:
        hostname = raw_host.rsplit(':', 1)[0]
    normalized = hostname.strip('[]').lower()
    if normalized == 'localhost':
        return True
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return False


def _browser_mutation_is_allowed() -> bool:
    if request.method not in {'POST', 'PUT', 'PATCH', 'DELETE'}:
        return True
    fetch_site = str(request.headers.get('Sec-Fetch-Site') or '').lower()
    origin = str(request.headers.get('Origin') or '').strip()
    browser_context = bool(fetch_site or origin or request.headers.get('Sec-Fetch-Mode'))
    if not browser_context:
        # Authenticated command-line clients cannot be driven by a cross-site form.
        return True
    if fetch_site not in {'', 'same-origin', 'same-site', 'none'}:
        return False
    if request.headers.get('X-Panel-CSRF') != '1':
        return False
    if not origin:
        return fetch_site in {'same-origin', 'same-site'}
    try:
        parsed = urlsplit(origin)
        expected = urlsplit(request.host_url)
        return (
            parsed.scheme in {'http', 'https'}
            and parsed.scheme == expected.scheme
            and parsed.hostname == expected.hostname
            and parsed.port == expected.port
        )
    except ValueError:
        return False


@app.before_request
def _enforce_panel_access_key():
    if not _request_host_is_allowed():
        return jsonify({'success': False, 'error': 'Invalid Host'}), 400
    if request.path == '/api/healthz':
        return None
    if request.path.startswith('/api') and not _browser_mutation_is_allowed():
        return jsonify({'success': False, 'error': 'Cross-site mutation rejected'}), 403
    expected = _panel_access_key_expected()
    if not expected:
        return None

    # 允许 CORS 预检请求通过
    if request.method == 'OPTIONS':
        return None

    # 只保护 API（静态页面可访问，但无法读取/操作数据）
    if not request.path.startswith('/api'):
        return None

    if not hmac.compare_digest(_panel_access_key_provided(), expected):
        return jsonify({'success': False, 'error': 'Unauthorized'}), 401

    return None


def _parse_bool(value):
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    value_str = str(value).strip().lower()
    if value_str in {'1', 'true', 'yes', 'on'}:
        return True
    if value_str in {'0', 'false', 'no', 'off'}:
        return False
    return False


def _error_kind(exc):
    """Return a non-sensitive diagnostic label without exposing exception text."""
    return type(exc).__name__


def _parse_float(value, default=0.0):
    if value is None:
        return default
    try:
        return float(value)
    except Exception:
        return default


def _load_dotenv():
    env_path = os.path.join(BASE_DIR, '.env')
    if not os.path.exists(env_path):
        return {}
    values = {}
    try:
        with open(env_path, 'r', encoding='utf-8') as f:
            for raw_line in f:
                line = raw_line.strip()
                if not line or line.startswith('#'):
                    continue
                if '=' not in line:
                    continue
                key, value = line.split('=', 1)
                key = key.strip()
                value = value.strip()
                if len(value) >= 2 and value[0] == value[-1] == '"':
                    try:
                        value = json.loads(value)
                    except (TypeError, ValueError):
                        value = value[1:-1]
                elif len(value) >= 2 and value[0] == value[-1] == "'":
                    value = value[1:-1]
                values[key] = value
    except Exception as e:
        print(f"Warning: failed to load .env ({_error_kind(e)})")
    return values


def _format_env_value(value):
    if isinstance(value, bool):
        return 'true' if value else 'false'
    value_text = str(value)
    if '\n' in value_text or '\r' in value_text:
        raise ValueError('Environment values cannot contain newlines')
    if value_text != value_text.strip() or value_text.startswith(('"', "'")) or value_text.endswith(('"', "'")):
        return json.dumps(value_text, ensure_ascii=False)
    return value_text


def _fsync_parent_directory(path):
    """Best-effort directory sync so an atomic rename survives power loss."""
    if os.name == 'nt':
        return
    directory = os.path.dirname(os.path.abspath(path))
    flags = os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0)
    try:
        descriptor = os.open(directory, flags)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except OSError:
        pass


def _atomic_write_text(path, content, *, encoding='utf-8', mode=None):
    """Write a file in the same directory and atomically replace the destination."""
    if not path:
        raise ValueError('Path is required')
    target = os.path.abspath(os.fspath(path))
    parent = os.path.dirname(target)
    os.makedirs(parent, exist_ok=True)

    existing_mode = None
    try:
        existing_mode = os.stat(target).st_mode & 0o777
    except OSError:
        pass

    fd, temp_path = tempfile.mkstemp(prefix=f'.{os.path.basename(target)}.', suffix='.tmp', dir=parent)
    try:
        with os.fdopen(fd, 'w', encoding=encoding, newline='') as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        if mode is not None:
            desired_mode = (existing_mode & mode) if existing_mode is not None else mode
        else:
            desired_mode = existing_mode
        if desired_mode is not None:
            try:
                os.chmod(temp_path, desired_mode)
            except OSError:
                pass
        os.replace(temp_path, target)
        _fsync_parent_directory(target)
    except Exception:
        try:
            os.remove(temp_path)
        except OSError:
            pass
        raise


def _atomic_write_json(path, payload, *, mode=None):
    content = json.dumps(payload, ensure_ascii=False, indent=2) + '\n'
    _atomic_write_text(path, content, mode=mode)


def _load_json_file_limited(path, max_bytes):
    if os.path.getsize(path) > max_bytes:
        raise ValueError(f'JSON file exceeds the {max_bytes}-byte limit')
    with open(path, 'r', encoding='utf-8') as handle:
        return json.load(handle)


def _update_dotenv_values(updates):
    # Container writable layer is ephemeral. Persist UI settings on the data
    # volume; explicit nonempty environment variables remain authoritative.
    if str(CONFIG.get('deployment_mode')) == 'docker' or os.path.exists('/.dockerenv'):
        try:
            path = _resolve_panel_path(CONFIG['settings_path'])
            with dotenv_lock:
                saved = _load_json_file_limited(path, 64 * 1024) if os.path.isfile(path) else {}
                if not isinstance(saved, dict):
                    saved = {}
                saved.update(updates)
                _atomic_write_json(path, saved, mode=0o600)
            return True
        except (OSError, ValueError, TypeError):
            return False
    env_path = os.path.join(BASE_DIR, '.env')
    try:
        env_updates = {f'{ENV_PREFIX}{key.upper()}': _format_env_value(val) for key, val in updates.items()}
        with dotenv_lock:
            lines = []
            if os.path.exists(env_path):
                with open(env_path, 'r', encoding='utf-8') as f:
                    lines = f.read().splitlines()

            updated = set()
            new_lines = []
            for line in lines:
                stripped = line.strip()
                if not stripped or stripped.startswith('#') or '=' not in line:
                    new_lines.append(line)
                    continue
                key, _ = line.split('=', 1)
                key = key.strip()
                if key in env_updates:
                    new_lines.append(f"{key}={env_updates[key]}")
                    updated.add(key)
                else:
                    new_lines.append(line)

            for key, value in env_updates.items():
                if key not in updated:
                    new_lines.append(f"{key}={value}")

            _atomic_write_text(env_path, '\n'.join(new_lines) + '\n', mode=0o600)
        return True
    except Exception as e:
        print(f"Warning: failed to save .env ({_error_kind(e)})")
        return False


def _apply_overrides(overrides):
    for key, value in overrides.items():
        if key not in CONFIG:
            continue
        caster = CONFIG_TYPES.get(key)
        if caster is None:
            CONFIG[key] = value
            continue
        if caster is bool:
            CONFIG[key] = _parse_bool(value)
        else:
            try:
                CONFIG[key] = caster(value)
            except Exception:
                pass


def load_config_overrides():
    env_overrides = {}
    for key in CONFIG.keys():
        env_key = f'{ENV_PREFIX}{key.upper()}'
        if env_key in os.environ:
            env_overrides[key] = os.environ[env_key]

    dotenv_raw = _load_dotenv()
    dotenv_overrides = {}
    for key in CONFIG.keys():
        env_key = f'{ENV_PREFIX}{key.upper()}'
        if env_key in dotenv_raw:
            dotenv_overrides[key] = dotenv_raw[env_key]

    _apply_overrides(dotenv_overrides)
    settings_path = env_overrides.get('settings_path') or CONFIG['settings_path']
    try:
        with open(settings_path, 'rb') as handle:
            raw = handle.read(64 * 1024 + 1)
        if len(raw) <= 64 * 1024:
            saved = json.loads(raw)
            allowed = {'management_key', 'auto_update_enabled', 'idle_threshold_seconds',
                       'auto_update_check_interval', 'pricing_input', 'pricing_output',
                       'pricing_cache', 'pricing_auto_enabled'}
            if isinstance(saved, dict):
                _apply_overrides({key: value for key, value in saved.items() if key in allowed})
    except (OSError, ValueError, TypeError):
        pass
    # Empty Compose placeholders must not erase persisted settings.
    _apply_overrides({key: value for key, value in env_overrides.items() if value != ''})


load_config_overrides()

cors_origins = [origin.strip() for origin in str(CONFIG.get('cors_origins') or '').split(',') if origin.strip()]
if cors_origins:
    CORS(app, resources={r'/api/*': {'origins': cors_origins}}, supports_credentials=False)


@app.after_request
def _set_security_headers(response):
    response.headers.setdefault('X-Content-Type-Options', 'nosniff')
    response.headers.setdefault('X-Frame-Options', 'DENY')
    response.headers.setdefault('Referrer-Policy', 'no-referrer')
    response.headers.setdefault('Permissions-Policy', 'camera=(), microphone=(), geolocation=()')
    response.headers.setdefault(
        'Content-Security-Policy',
        "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; font-src 'self'; connect-src 'self'; object-src 'none'; "
        "frame-ancestors 'none'; base-uri 'self'; form-action 'self'",
    )
    if request.is_secure:
        response.headers.setdefault('Strict-Transport-Security', 'max-age=31536000')
    if request.path.startswith('/api/'):
        response.headers['Cache-Control'] = 'no-store'
    elif request.path == '/' or request.path.endswith(('.js', '.css')):
        response.headers['Cache-Control'] = 'no-cache'
    return response


def _utc_now():
    return datetime.now(UTC)


def _utc_iso(value=None):
    dt = value or _utc_now()
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).isoformat(timespec='seconds').replace('+00:00', 'Z')


def _parse_iso_datetime(value, *, assume_timezone=UTC):
    if isinstance(value, datetime):
        dt = value
    else:
        raw = str(value or '').strip()
        if not raw:
            return None
        if raw.endswith(('Z', 'z')):
            raw = raw[:-1] + '+00:00'
        try:
            dt = datetime.fromisoformat(raw)
        except ValueError:
            try:
                dt = datetime.strptime(raw, '%Y-%m-%d %H:%M:%S')
            except ValueError:
                return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=assume_timezone)
    return dt


def _timezone_from_setting(setting):
    value = str(setting or 'auto').strip()
    lowered = value.lower()
    if lowered == 'auto':
        return None, 'auto'
    if lowered in {'local', 'system'}:
        return datetime.now().astimezone().tzinfo or UTC, 'local'
    if lowered in {'utc', 'z', 'gmt'}:
        return UTC, 'UTC'

    offset_match = TIMEZONE_OFFSET_PATTERN.match(value)
    if offset_match:
        hours = int(offset_match.group('hours'))
        minutes = int(offset_match.group('minutes') or 0)
        if hours > 14 or minutes > 59 or (hours == 14 and minutes != 0):
            raise ValueError(f'Invalid timezone offset: {value}')
        offset = timedelta(hours=hours, minutes=minutes)
        if offset_match.group('sign') == '-':
            offset = -offset
        return timezone(offset), value

    try:
        return ZoneInfo(value), value
    except ZoneInfoNotFoundError as exc:
        raise ValueError(f'Unknown timezone: {value}') from exc


def _infer_timezone_offset_seconds(log_time, file_mtime):
    """Infer CLIProxy's UTC offset by comparing its latest local log time to file mtime."""
    if not log_time or file_mtime is None:
        return None
    try:
        naive = datetime.strptime(str(log_time), '%Y-%m-%d %H:%M:%S')
        raw_offset = naive.replace(tzinfo=UTC).timestamp() - float(file_mtime)
    except (TypeError, ValueError, OSError):
        return None

    # Real-world UTC offsets use 15-minute increments. Allow a small write/flush delay.
    step = 15 * 60
    rounded = int(round(raw_offset / step) * step)
    if abs(raw_offset - rounded) > 5 * 60 or not (-12 * 3600 <= rounded <= 14 * 3600):
        return None
    return rounded


def _log_timezone(offset_seconds=None):
    setting = CONFIG.get('log_timezone', 'auto')
    try:
        configured, source = _timezone_from_setting(setting)
    except ValueError as exc:
        print(f'Warning: timezone detection failed ({_error_kind(exc)}); falling back to system local timezone')
        configured, source = datetime.now().astimezone().tzinfo or UTC, 'local-fallback'
    if configured is not None:
        return configured, source
    if offset_seconds is not None:
        offset_seconds = max(-12 * 3600, min(14 * 3600, int(offset_seconds)))
        return timezone(timedelta(seconds=offset_seconds)), 'auto-inferred'
    return datetime.now().astimezone().tzinfo or UTC, 'auto-local-fallback'


def _log_time_to_utc(value, offset_seconds=None):
    if not value:
        return None
    raw = str(value).strip()
    parsed = _parse_iso_datetime(raw)
    if parsed is None:
        return None
    # A plain CLIProxy timestamp has no offset and must use the log source timezone.
    if not re.search(r'(?:Z|[+-]\d{2}:?\d{2})$', raw, re.IGNORECASE):
        setting = str(CONFIG.get('log_timezone', 'auto') or 'auto').strip().lower()
        if setting in {'local', 'system'} or (setting == 'auto' and offset_seconds is None):
            # astimezone() on a naive datetime asks the operating system for the
            # offset at that historical instant, including DST transitions.
            return parsed.replace(tzinfo=None).astimezone(UTC)
        tz, _ = _log_timezone(offset_seconds)
        parsed = parsed.replace(tzinfo=tz)
    return parsed.astimezone(UTC)


def _log_time_iso(value, offset_seconds=None):
    parsed = _log_time_to_utc(value, offset_seconds)
    return _utc_iso(parsed) if parsed else None


def _compose_api_base_url(base_url=None, port=None):
    raw = str(base_url if base_url is not None else CONFIG.get('cliproxy_api_base', '') or '').strip().rstrip('/')
    if not raw:
        raw = 'http://127.0.0.1'
    if '://' not in raw:
        raw = f'http://{raw}'
    parsed = urlsplit(raw)
    if parsed.scheme not in {'http', 'https'} or not parsed.hostname:
        raise ValueError(f'Invalid CLIProxy API base URL: {raw}')
    if parsed.username or parsed.password:
        raise ValueError('CLIProxy API base URL must not contain credentials')

    if port is None and base_url is None:
        port = CONFIG.get('cliproxy_api_port')
    selected_port = parsed.port
    if selected_port is None and port:
        selected_port = int(port)
    host = parsed.hostname
    if ':' in host and not host.startswith('['):
        host = f'[{host}]'
    netloc = f'{host}:{selected_port}' if selected_port else host
    return urlunsplit((parsed.scheme, netloc, parsed.path.rstrip('/'), '', ''))


def _api_host_port():
    base_url = _compose_api_base_url()
    parsed = urlsplit(base_url)
    port = parsed.port or (443 if parsed.scheme == 'https' else 80)
    return parsed.hostname, port


def _normalize_runtime_config():
    """Keep malformed environment overrides from breaking the whole panel."""
    def as_int(value, fallback):
        try:
            return int(value)
        except (TypeError, ValueError, OverflowError):
            return fallback

    def as_float(value, fallback):
        try:
            return float(value)
        except (TypeError, ValueError, OverflowError):
            return fallback

    bounded_ints = {
        'panel_port': (1, 65535, 8080),
        'cliproxy_api_port': (1, 65535, 8317),
        'idle_threshold_seconds': (10, 7 * 86400, 1800),
        'auto_update_check_interval': (60, 86400, 300),
        'auto_update_failure_backoff_seconds': (60, 7 * 86400, 21600),
        'auto_update_failure_backoff_max_seconds': (60, 30 * 86400, 86400),
        'service_health_timeout_seconds': (5, 300, 45),
        'backup_retention_count': (1, 20, BACKUP_RETENTION_COUNT),
        'backup_max_age_days': (1, 3650, 14),
        'backup_max_total_mb': (16, 10240, 512),
        'log_initial_scan_max_mb': (1, 1024, 64),
    }
    for key, (minimum, maximum, fallback) in bounded_ints.items():
        value = as_int(CONFIG.get(key), fallback)
        if not minimum <= value <= maximum:
            print(f'Warning: invalid {key}={CONFIG.get(key)!r}; using {fallback}')
            value = fallback
        CONFIG[key] = value

    CONFIG['auto_update_failure_backoff_max_seconds'] = max(
        CONFIG['auto_update_failure_backoff_seconds'],
        CONFIG['auto_update_failure_backoff_max_seconds'],
    )

    for key in ('pricing_input', 'pricing_output', 'pricing_cache'):
        value = as_float(CONFIG.get(key), 0.0)
        CONFIG[key] = value if math.isfinite(value) and 0 <= value <= 1_000_000 else 0.0

    bind_host = str(CONFIG.get('bind_host') or '').strip()
    CONFIG['bind_host'] = bind_host or '127.0.0.1'
    try:
        _timezone_from_setting(CONFIG.get('log_timezone', 'auto'))
    except ValueError as exc:
        print(f'Warning: log timezone detection failed ({_error_kind(exc)}); using auto detection')
        CONFIG['log_timezone'] = 'auto'


_normalize_runtime_config()
_validate_panel_security()


def is_config_write_enabled():
    return _parse_bool(CONFIG.get('config_write_enabled', False))


def _request_json_object():
    data = request.get_json(silent=True)
    return data if isinstance(data, dict) else None


def config_write_blocked_response():
    message = '当前面板已禁用配置写入，只保留自动更新和查看能力'
    return jsonify({'success': False, 'error': message, 'message': message}), 403

UPDATE_HISTORY_PATH = os.path.join(DATA_DIR, 'update_history.json')
AUTO_UPDATE_STATE_PATH = os.path.join(DATA_DIR, 'auto_update_state.json')

# 全局状态
state = {
    'last_request_time': None,
    'request_count': 0,
    'update_in_progress': False,
    'last_update_time': None,
    'last_update_result': None,
    'last_auto_update_check_time': None,
    'next_auto_update_check_time': None,
    'next_auto_update_check_monotonic': None,
    'auto_update_failure_count': 0,
    'auto_update_retry_not_before': None,
    'auto_update_failed_version': None,
    'current_version': 'unknown',
    'latest_version': 'unknown',
    'has_update': False,
    'auto_update_enabled': CONFIG['auto_update_enabled'],
    'request_log': [],
    # 统计数据
    'stats': {
        'total_requests': 0,
        'successful_requests': 0,
        'failed_requests': 0,
        'input_tokens': 0,
        'output_tokens': 0,
        'reasoning_tokens': 0,
        'cached_tokens': 0,
        'total_response_time': 0,
        'requests_per_minute': deque(maxlen=60),
        'requests_per_hour': deque(maxlen=24),
        'model_usage': {},
        'error_types': {},
        'hourly_stats': deque(maxlen=24),
    },
    # 上次从 CLIProxyAPI 读取的快照值（用于计算增量）
    'last_snapshot': {
        'input_tokens': 0,
        'output_tokens': 0,
        'reasoning_tokens': 0,
        'cached_tokens': 0,
        'total_requests': 0,
        'success': 0,
        'failure': 0,
    },
    # 面板独立累加的统计数据（持久化保存，不受 CLIProxyAPI 重启影响）
    'accumulated_stats': {
        'input_tokens': 0,
        'output_tokens': 0,
        'reasoning_tokens': 0,
        'cached_tokens': 0,
        'total_requests': 0,
        'success': 0,
        'failure': 0,
    },
    # Optional counters submitted through /api/record-request. Keep them
    # separate so refreshing a live upstream snapshot cannot erase them.
    'recorded_stats': {
        'total_requests': 0,
        'success': 0,
        'failure': 0,
    },
    'last_health_check': None,
    'health_status': 'unknown',
    'management_auth': {
        'consecutive_failures': 0,
        'locked': False,
        'last_status': None,
        'last_error': None,
        'last_failure_time': None,
    },
    'usage_snapshot_source': 'none',
    'usage_snapshot_time': None,
    'usage_api_kind': 'unknown',
    # v6 exposes cumulative snapshots; v7 exposes a destructive usage queue.
    # Persist the active accounting mode so switching formats cannot double-count.
    'usage_counter_mode': 'cumulative',
    # When stats are cleared while the upstream usage endpoint is unavailable,
    # the first recovered live snapshot becomes the new baseline.
    'usage_reset_pending': False,
    'log_stats': {
        'initialized': False,
        'offset': 0,
        'last_size': 0,
        'last_mtime': None,
        'file_identity': None,
        'total': 0,
        'success': 0,
        'failed': 0,
        'last_time': None,
        'latest_log_time': None,
        'timezone_offset_seconds': None,
        'timezone_source': 'unknown',
        'partial': False,
        'skipped_bytes': 0,
        'buffer': '',
        'base_total': 0,
        'base_success': 0,
        'base_failed': 0,
        'last_saved_ts': 0
    },
    'log_stats_loaded': False,
    'upstream': {},
    'service_snapshot': None,
    'resources_snapshot': None,
    'logs_snapshot': {'logs': [], 'count': 0},
    'collector_time': None,
    'collector_error': None,
    'version_source': 'unknown',
    'version_checked_at': None,
    'version_error': None,
}

log_lock = threading.Lock()
quotes_lock = threading.Lock()
log_stats_lock = threading.Lock()
stats_lock = threading.Lock()
persistent_stats_lock = threading.Lock()
management_auth_lock = threading.Lock()
usage_snapshot_lock = threading.Lock()
usage_fetch_lock = threading.Lock()
update_history_lock = threading.Lock()
update_lock = threading.Lock()
auto_update_wakeup = threading.Event()
auto_update_failure_lock = threading.Lock()
upstream_probe_lock = threading.Lock()
version_check_lock = threading.Lock()
shutdown_event = threading.Event()
collector_wakeup = threading.Event()

http_session = requests.Session()
http_session.headers.update({'User-Agent': f'{PANEL_NAME}/{PANEL_VERSION}'})
http_adapter = HTTPAdapter(pool_connections=10, pool_maxsize=20, max_retries=0)
http_session.mount('http://', http_adapter)
http_session.mount('https://', http_adapter)


def _response_json_limited(response, max_bytes):
    content_length = _safe_int(response.headers.get('Content-Length'), 0)
    if content_length and content_length > max_bytes:
        raise ValueError(f'JSON response exceeds the {max_bytes}-byte limit')
    raw = response.raw.read(max_bytes + 1, decode_content=True)
    if len(raw) > max_bytes:
        raise ValueError(f'JSON response exceeds the {max_bytes}-byte limit')
    encoding = response.encoding or 'utf-8'
    return json.loads(raw.decode(encoding, errors='strict'))

# ==================== 持久化统计系统 ====================
PERSISTENT_STATS_FIELDS = (
    'total_requests',
    'successful_requests',
    'failed_requests',
    'input_tokens',
    'output_tokens',
    'reasoning_tokens',
    'cached_tokens',
    'model_usage',
)


def load_persistent_stats():
    """从磁盘加载持久化统计数据"""
    def safe_int(v, default=0):
        try:
            return int(v)
        except (TypeError, ValueError):
            return default
    
    path = _resolve_panel_path(CONFIG.get('persistent_stats_path'))
    if not path or not os.path.exists(path):
        return False
    try:
        data = _load_json_file_limited(path, 8 * 1024 * 1024)
        if not isinstance(data, dict):
            return False
        with stats_lock:
            for key in PERSISTENT_STATS_FIELDS:
                if key in data:
                    if key == 'model_usage':
                        loaded_models = data[key] if isinstance(data[key], dict) else {}
                        state['stats'][key] = {
                            str(model)[:200]: max(0, safe_int(count))
                            for model, count in list(loaded_models.items())[:MAX_MODEL_USAGE_ENTRIES]
                        }
                    else:
                        state['stats'][key] = safe_int(data[key])
            # 加载累计统计值
            if 'accumulated_stats' in data and isinstance(data['accumulated_stats'], dict):
                for key in state['accumulated_stats']:
                    if key in data['accumulated_stats']:
                        state['accumulated_stats'][key] = safe_int(data['accumulated_stats'][key])
            # 加载上次快照值
            if 'last_snapshot' in data and isinstance(data['last_snapshot'], dict):
                for key in state['last_snapshot']:
                    if key in data['last_snapshot']:
                        state['last_snapshot'][key] = safe_int(data['last_snapshot'][key])
            if 'recorded_stats' in data and isinstance(data['recorded_stats'], dict):
                for key in state['recorded_stats']:
                    if key in data['recorded_stats']:
                        state['recorded_stats'][key] = max(0, safe_int(data['recorded_stats'][key]))
            mode = str(data.get('usage_counter_mode', 'cumulative') or 'cumulative').strip().lower()
            state['usage_counter_mode'] = mode if mode in {'cumulative', 'queue'} else 'cumulative'
            state['usage_reset_pending'] = bool(data.get('usage_reset_pending', False))
            # 同步 request_count
            state['request_count'] = state['stats']['total_requests']
        print(f"Loaded persistent stats: accumulated={state['accumulated_stats']}, last_snapshot={state['last_snapshot']}")
        return True
    except Exception as e:
        print(f"Warning: failed to load persistent stats ({_error_kind(e)})")
        return False


def save_persistent_stats(force=False):
    """保存统计数据到磁盘"""
    path = _resolve_panel_path(CONFIG.get('persistent_stats_path'))
    if not path:
        return False
    with persistent_stats_lock:
        now = time.monotonic()
        # 限制保存频率，除非强制保存
        last_saved = getattr(save_persistent_stats, '_last_saved', 0)
        if not force and now - last_saved < 10:
            return False
        try:
            with stats_lock:
                payload = {
                    'total_requests': state['stats'].get('total_requests', 0),
                    'successful_requests': state['stats'].get('successful_requests', 0),
                    'failed_requests': state['stats'].get('failed_requests', 0),
                    'input_tokens': state['stats'].get('input_tokens', 0),
                    'output_tokens': state['stats'].get('output_tokens', 0),
                    'reasoning_tokens': state['stats'].get('reasoning_tokens', 0),
                    'cached_tokens': state['stats'].get('cached_tokens', 0),
                    'model_usage': dict(state['stats'].get('model_usage', {})),
                    'accumulated_stats': dict(state.get('accumulated_stats', {})),
                    'last_snapshot': dict(state.get('last_snapshot', {})),
                    'recorded_stats': dict(state.get('recorded_stats', {})),
                    'usage_counter_mode': str(state.get('usage_counter_mode', 'cumulative')),
                    'usage_reset_pending': bool(state.get('usage_reset_pending', False)),
                    'saved_at': _utc_iso(),
                }
            _atomic_write_json(path, payload, mode=0o600)
            save_persistent_stats._last_saved = now
            return True
        except Exception as e:
            print(f"Warning: failed to save persistent stats ({_error_kind(e)})")
            return False


def _persistent_stats_worker():
    """后台线程：定期保存统计数据"""
    while not shutdown_event.wait(30):  # 每30秒保存一次
        try:
            save_persistent_stats()
            save_log_stats_state()
        except Exception as e:
            print(f"Warning: persistent stats worker error ({_error_kind(e)})")


def start_persistent_stats_worker():
    """启动持久化统计后台线程"""
    thread = threading.Thread(target=_persistent_stats_worker, daemon=True)
    thread.start()


# ==================== 缓存系统 ====================
class CacheManager:
    """轻量级缓存管理器"""
    def __init__(self):
        self._cache = {}
        self._lock = threading.Lock()

    def get(self, key, max_age=5):
        """获取缓存值，max_age为秒数"""
        with self._lock:
            if key in self._cache:
                value, timestamp = self._cache[key]
                if time.monotonic() - timestamp < max_age:
                    return value
                self._cache.pop(key, None)
        return None

    def set(self, key, value):
        """设置缓存值"""
        with self._lock:
            self._cache[key] = (value, time.monotonic())

    def invalidate(self, key=None):
        """使缓存失效（key=None 表示清空全部）"""
        with self._lock:
            if key is None:
                self._cache.clear()
            else:
                self._cache.pop(key, None)


def _safe_int(value, default=0):
    try:
        return int(value)
    except Exception:
        return default


def _safe_float(value, default=0.0):
    try:
        return float(value)
    except Exception:
        return default


def _build_management_base_url():
    return _compose_api_base_url(
        CONFIG.get('cliproxy_api_base', 'http://127.0.0.1'),
        CONFIG.get('cliproxy_api_port'),
    )


def _management_headers():
    key = str(CONFIG.get('management_key', '') or '').strip()
    # AI 友好兜底：很多部署把管理密钥与 API Key 设为同一个值
    if not key:
        key = str(CONFIG.get('models_api_key', '') or '').strip()
    headers = {'Content-Type': 'application/json'}
    if key:
        headers['X-Management-Key'] = key
    return headers


def _resolve_panel_path(path):
    if not path:
        return ''
    path = os.path.expandvars(os.path.expanduser(str(path)))
    if os.path.isabs(path):
        return os.path.abspath(path)
    return os.path.abspath(os.path.join(BASE_DIR, path))


def _management_key_configured():
    return bool(
        str(CONFIG.get('management_key', '') or '').strip()
        or str(CONFIG.get('models_api_key', '') or '').strip()
    )


def _management_auth_message(auth_state=None):
    auth_state = auth_state or state.get('management_auth', {})
    failures = int(auth_state.get('consecutive_failures') or 0)
    if auth_state.get('locked'):
        return 'CPA 管理密钥连续错误，已暂停管理接口请求。请手动配置正确密钥。'
    if failures > 0:
        return f'CPA 管理密钥可能错误，已连续失败 {failures}/{MANAGEMENT_AUTH_MAX_FAILURES} 次。'
    if not _management_key_configured():
        return '未配置 CPA 管理密钥，部分管理功能可能不可用。'
    return 'CPA 管理密钥状态正常。'


def _management_auth_snapshot():
    with management_auth_lock:
        auth_state = dict(state.get('management_auth', {}))
    auth_state.update({
        'configured': _management_key_configured(),
        'using_models_key_fallback': bool(
            not str(CONFIG.get('management_key', '') or '').strip()
            and str(CONFIG.get('models_api_key', '') or '').strip()
        ),
        'max_failures': MANAGEMENT_AUTH_MAX_FAILURES,
        'needs_attention': bool(auth_state.get('locked') or int(auth_state.get('consecutive_failures') or 0) > 0),
    })
    auth_state['message'] = _management_auth_message(auth_state)
    return auth_state


def _management_auth_locked():
    with management_auth_lock:
        return bool(state.get('management_auth', {}).get('locked'))


def _reset_management_auth_state():
    with management_auth_lock:
        state['management_auth'] = {
            'consecutive_failures': 0,
            'locked': False,
            'last_status': None,
            'last_error': None,
            'last_failure_time': None,
        }
    cache.invalidate('usage_snapshot_envelope')
    cache.invalidate('local_version_mgmt')
    cache.invalidate('local_version')
    cache.invalidate('health_check')
    state['upstream'] = {}
    collector_wakeup.set()


def _record_management_auth_success():
    with management_auth_lock:
        auth_state = state.get('management_auth', {})
        if not auth_state.get('consecutive_failures') and not auth_state.get('locked'):
            return
        auth_state.update({
            'consecutive_failures': 0,
            'locked': False,
            'last_status': None,
            'last_error': None,
            'last_failure_time': None,
        })
        state['management_auth'] = auth_state
    cache.invalidate('health_check')


def _record_management_auth_failure(status_code, error=None):
    if status_code not in MANAGEMENT_AUTH_FAILURE_STATUSES:
        return
    with management_auth_lock:
        auth_state = state.get('management_auth', {})
        failures = int(auth_state.get('consecutive_failures') or 0) + 1
        auth_state.update({
            'consecutive_failures': failures,
            'locked': failures >= MANAGEMENT_AUTH_MAX_FAILURES,
            'last_status': status_code,
            'last_error': str(error or f'HTTP {status_code}'),
            'last_failure_time': _utc_iso(),
        })
        state['management_auth'] = auth_state
    cache.invalidate('health_check')


def _observe_management_response(resp):
    if resp is None:
        return
    status_code = getattr(resp, 'status_code', None)
    if status_code in MANAGEMENT_AUTH_FAILURE_STATUSES:
        _record_management_auth_failure(status_code)
    elif status_code is not None and 200 <= int(status_code) < 300:
        _record_management_auth_success()


def load_usage_snapshot_from_disk():
    path = _resolve_panel_path(CONFIG.get('usage_snapshot_path'))
    if not path:
        return None
    try:
        if os.path.exists(path):
            loaded = _load_json_file_limited(path, 32 * 1024 * 1024)
            return loaded if isinstance(loaded, dict) else None
    except Exception as e:
        print(f"Warning: failed to load usage snapshot ({_error_kind(e)})")
    return None


def save_usage_snapshot(snapshot):
    path = _resolve_panel_path(CONFIG.get('usage_snapshot_path'))
    if not path or snapshot is None:
        return False
    with usage_snapshot_lock:
        try:
            _atomic_write_json(path, snapshot, mode=0o600)
            return True
        except Exception as e:
            print(f"Warning: failed to save usage snapshot ({_error_kind(e)})")
            return False


LOG_STATS_PERSIST_FIELDS = (
    'initialized',
    'offset',
    'last_size',
    'last_mtime',
    'file_identity',
    'total',
    'success',
    'failed',
    'last_time',
    'latest_log_time',
    'timezone_offset_seconds',
    'timezone_source',
    'partial',
    'skipped_bytes',
    'base_total',
    'base_success',
    'base_failed',
)


def _ensure_parent_dir(path):
    if not path:
        return False
    try:
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        return True
    except Exception as e:
        print(f"Warning: failed to create required directory ({_error_kind(e)})")
        return False


def load_log_stats_state():
    path = _resolve_panel_path(CONFIG.get('log_stats_path'))
    if not path or not os.path.exists(path):
        return False
    try:
        data = _load_json_file_limited(path, 1024 * 1024)
        if not isinstance(data, dict):
            return False
        with log_stats_lock:
            log_state = state.get('log_stats', {}).copy()
            for key in LOG_STATS_PERSIST_FIELDS:
                if key in data:
                    log_state[key] = data[key]
            log_state['buffer'] = ''
            state['log_stats'] = log_state
            state['log_stats_loaded'] = True
        return True
    except Exception as e:
        print(f"Warning: failed to load log stats ({_error_kind(e)})")
        return False


def save_log_stats_state(force=False):
    path = _resolve_panel_path(CONFIG.get('log_stats_path'))
    if not path:
        return False
    with log_stats_lock:
        log_state = state.get('log_stats', {})
        now = time.monotonic()
        last_saved = _safe_float(log_state.get('last_saved_ts', 0), 0.0)
        if not force and now - last_saved < 5:
            return False
        payload = {key: log_state.get(key) for key in LOG_STATS_PERSIST_FIELDS}
        log_state['last_saved_ts'] = now
        state['log_stats'] = log_state
    try:
        _atomic_write_json(path, payload, mode=0o600)
        return True
    except Exception as e:
        print(f"Warning: failed to save log stats ({_error_kind(e)})")
        return False


def _apply_usage_queue_records(records):
    """Persist v7 usage-queue events as deltas; returns accepted record count."""
    if not isinstance(records, list):
        return 0

    delta = {
        'input_tokens': 0,
        'output_tokens': 0,
        'reasoning_tokens': 0,
        'cached_tokens': 0,
        'total_requests': 0,
        'success': 0,
        'failure': 0,
    }
    models = {}
    for record in records:
        if not isinstance(record, dict) or not any(
            key in record for key in ('tokens', 'failed', 'model', 'timestamp')
        ):
            continue
        tokens = record.get('tokens') if isinstance(record.get('tokens'), dict) else {}
        input_tokens = max(0, _safe_int(tokens.get('input_tokens', 0)))
        output_tokens = max(0, _safe_int(tokens.get('output_tokens', 0)))
        raw_reasoning = max(0, _safe_int(tokens.get('reasoning_tokens', 0)))
        total_tokens = max(0, _safe_int(tokens.get('total_tokens', 0)))
        reasoning_extra = max(0, min(raw_reasoning, total_tokens - input_tokens - output_tokens))

        delta['input_tokens'] += input_tokens
        delta['output_tokens'] += output_tokens
        delta['reasoning_tokens'] += reasoning_extra
        delta['cached_tokens'] += max(0, _safe_int(tokens.get('cached_tokens', 0)))
        delta['total_requests'] += 1
        if record.get('failed') is True:
            delta['failure'] += 1
        else:
            delta['success'] += 1
        model = str(record.get('alias') or record.get('model') or 'unknown')[:200]
        models[model] = models.get(model, 0) + 1

    if not delta['total_requests']:
        return 0

    with stats_lock:
        accumulated = state.setdefault('accumulated_stats', {})
        for key, value in delta.items():
            accumulated[key] = max(0, _safe_int(accumulated.get(key, 0))) + value
        state['usage_counter_mode'] = 'queue'
        state['usage_reset_pending'] = False

        state['stats']['input_tokens'] = accumulated['input_tokens']
        state['stats']['output_tokens'] = accumulated['output_tokens']
        state['stats']['reasoning_tokens'] = accumulated['reasoning_tokens']
        state['stats']['cached_tokens'] = accumulated['cached_tokens']
        recorded = state.setdefault('recorded_stats', {})
        state['stats']['total_requests'] = accumulated['total_requests'] + max(
            0, _safe_int(recorded.get('total_requests', 0))
        )
        state['stats']['successful_requests'] = accumulated['success'] + max(
            0, _safe_int(recorded.get('success', 0))
        )
        state['stats']['failed_requests'] = accumulated['failure'] + max(
            0, _safe_int(recorded.get('failure', 0))
        )
        state['request_count'] = state['stats']['total_requests']
        for model, count in models.items():
            _increment_model_usage_locked(model, count)

    # Queue reads remove upstream records, so durability cannot wait for the
    # regular 30-second persistence timer.
    save_persistent_stats(force=True)
    return delta['total_requests']


def _fetch_usage_queue(base_url, headers):
    """Return (supported, accepted_count) for the CLIProxyAPI v7 queue."""
    accepted_total = 0
    batch_size = 500
    for _ in range(4):
        with http_session.get(
            f'{base_url}/v0/management/usage-queue?count={batch_size}',
            headers=headers,
            timeout=(2, 4),
            stream=True,
        ) as resp:
            _observe_management_response(resp)
            if resp.status_code in {404, 405}:
                return False, 0
            resp.raise_for_status()
            records = _response_json_limited(resp, 32 * 1024 * 1024)
        if not isinstance(records, list):
            raise ValueError('Usage queue endpoint did not return a JSON array')
        accepted_total += _apply_usage_queue_records(records)
        if len(records) < batch_size:
            break
    return True, accepted_total


def fetch_usage_snapshot(use_cache=True, with_meta=False, *, allow_network=True):
    cache_key = 'usage_snapshot_envelope'
    if use_cache:
        cached = cache.get(cache_key, max_age=5)
        if cached is not None:
            return (cached.get('data'), dict(cached)) if with_meta else cached.get('data')
    def finish(snapshot, source):
        envelope = {
            'data': snapshot,
            'source': source,
            'live': source == 'live',
            'fetched_at': _utc_iso(),
        }
        cache.set(cache_key, envelope)
        with usage_snapshot_lock:
            state['usage_snapshot_source'] = source
            state['usage_snapshot_time'] = envelope['fetched_at']
        return (snapshot, dict(envelope)) if with_meta else snapshot

    if not allow_network:
        snapshot = load_usage_snapshot_from_disk()
        return finish(snapshot, 'disk' if snapshot is not None else 'none')

    # Avoid making every dashboard refresh wait on an unreachable upstream.
    if use_cache and cache.get('usage_snapshot_failure', max_age=15) is not None:
        snapshot = load_usage_snapshot_from_disk()
        return finish(snapshot, 'disk' if snapshot is not None else 'none')

    if _management_auth_locked():
        snapshot = load_usage_snapshot_from_disk()
        return finish(snapshot, 'disk' if snapshot is not None else 'none')

    # Serialize refreshes so several dashboard clients cannot fan out identical
    # management requests while the upstream is slow or unavailable.
    with usage_fetch_lock:
        try:
            base_url = _build_management_base_url()
            headers = _management_headers()
            if state.get('usage_api_kind') == 'queue':
                supported, accepted = _fetch_usage_queue(base_url, headers)
                if supported:
                    cache.invalidate('usage_snapshot_failure')
                    return finish({'queue_records': accepted}, 'queue')
                state['usage_api_kind'] = 'unknown'

            url = f'{base_url}/v0/management/usage'
            with http_session.get(url, headers=headers, timeout=(2, 4), stream=True) as resp:
                _observe_management_response(resp)
                if resp.status_code in {404, 405}:
                    snapshot = None
                else:
                    resp.raise_for_status()
                    snapshot = _response_json_limited(resp, 32 * 1024 * 1024)
            if snapshot is None:
                supported, accepted = _fetch_usage_queue(base_url, headers)
                if supported:
                    state['usage_api_kind'] = 'queue'
                    cache.invalidate('usage_snapshot_failure')
                    envelope_data = {'queue_records': accepted}
                    return finish(envelope_data, 'queue')
                raise ValueError('No supported usage endpoint is available')
            if not isinstance(snapshot, dict):
                raise ValueError('Usage endpoint did not return a JSON object')
            state['usage_api_kind'] = 'cumulative'
            save_usage_snapshot(snapshot)
            cache.invalidate('usage_snapshot_failure')
            return finish(snapshot, 'live')
        except Exception:
            cache.set('usage_snapshot_failure', True)
            snapshot = load_usage_snapshot_from_disk()
            return finish(snapshot, 'disk' if snapshot is not None else 'none')


def aggregate_usage_snapshot(snapshot):
    totals = {
        'input_tokens': 0,
        'output_tokens': 0,
        'reasoning_tokens': 0,
        'cached_tokens': 0,
        'total_tokens': 0,
    }
    reqs = {
        'total_requests': 0,
        'success': 0,
        'failure': 0,
    }
    if not snapshot:
        return totals, reqs

    usage = snapshot.get('usage') if isinstance(snapshot, dict) else None
    if not isinstance(usage, dict):
        usage = snapshot if isinstance(snapshot, dict) else {}

    top_total = _safe_int(usage.get('total_requests', usage.get('total', 0)))
    top_success = _safe_int(usage.get('success', usage.get('successful_requests', usage.get('success_count', 0))))
    top_failure = _safe_int(usage.get('failure', usage.get('failed_requests', usage.get('failure_count', 0))))

    def extract_tokens(obj):
        if not isinstance(obj, dict):
            return 0, 0, 0, 0, 0
        tokens = obj.get('tokens') or obj.get('usage') or obj
        input_tokens = _safe_int(tokens.get('input_tokens', tokens.get('input', tokens.get('prompt_tokens', 0))))
        output_tokens = _safe_int(tokens.get('output_tokens', tokens.get('output', tokens.get('completion_tokens', 0))))
        cached_tokens = _safe_int(tokens.get('cached_tokens', tokens.get('cache', 0)))
        reasoning_tokens = _safe_int(tokens.get('reasoning_tokens', tokens.get('reasoning', 0)))
        total_tokens = _safe_int(tokens.get('total_tokens', tokens.get('total', obj.get('total_tokens', 0))))
        if total_tokens == 0:
            total_tokens = input_tokens + output_tokens + reasoning_tokens
        # Some providers include reasoning inside output_tokens while others
        # expose it in addition. Count only the portion proven to sit outside
        # input+output so totals and output billing cannot double-count it.
        reasoning_extra = max(0, min(reasoning_tokens, total_tokens - input_tokens - output_tokens))
        return input_tokens, output_tokens, reasoning_extra, cached_tokens, total_tokens

    apis = usage.get('apis', [])
    if isinstance(apis, dict):
        apis = list(apis.values())
    if not isinstance(apis, list):
        apis = []

    sum_total = 0
    sum_success = 0
    sum_failure = 0

    for api in apis:
        if not isinstance(api, dict):
            continue
        sum_total += _safe_int(api.get('total_requests', api.get('total', api.get('requests', 0))))
        sum_success += _safe_int(api.get('success', api.get('successful_requests', api.get('success_count', 0))))
        sum_failure += _safe_int(api.get('failure', api.get('failed_requests', api.get('failure_count', 0))))

        models = api.get('models', [])
        if isinstance(models, dict):
            models = list(models.values())
        if not isinstance(models, list):
            continue
        for model in models:
            if not isinstance(model, dict):
                continue
            details = model.get('details')
            if isinstance(details, list) and details:
                for detail in details:
                    input_tokens, output_tokens, reasoning_tokens, cached_tokens, total_tokens = extract_tokens(detail)
                    totals['input_tokens'] += input_tokens
                    totals['output_tokens'] += output_tokens
                    totals['reasoning_tokens'] += reasoning_tokens
                    totals['cached_tokens'] += cached_tokens
                    totals['total_tokens'] += total_tokens
            else:
                input_tokens, output_tokens, reasoning_tokens, cached_tokens, total_tokens = extract_tokens(model)
                totals['input_tokens'] += input_tokens
                totals['output_tokens'] += output_tokens
                totals['reasoning_tokens'] += reasoning_tokens
                totals['cached_tokens'] += cached_tokens
                totals['total_tokens'] += total_tokens

    if totals['total_tokens'] == 0:
        totals['total_tokens'] = _safe_int(usage.get('total_tokens', 0))

    # 请求数/成功/失败：优先使用 usage 顶层汇总，避免与 apis breakdown 叠加导致双计数
    if top_total > 0:
        reqs['total_requests'] = top_total
        reqs['success'] = top_success
        reqs['failure'] = top_failure
    else:
        reqs['total_requests'] = sum_total
        reqs['success'] = sum_success
        reqs['failure'] = sum_failure

    # Older/newer upstream builds may omit one side of the success/failure
    # breakdown. Infer the missing side without ever exceeding the total.
    reqs['total_requests'] = max(0, reqs['total_requests'])
    reqs['success'] = max(0, min(reqs['success'], reqs['total_requests']))
    reqs['failure'] = max(0, min(reqs['failure'], reqs['total_requests']))
    if reqs['total_requests'] and reqs['success'] == 0 and reqs['failure'] > 0:
        reqs['success'] = reqs['total_requests'] - reqs['failure']
    elif reqs['total_requests'] and reqs['failure'] == 0 and reqs['success'] > 0:
        reqs['failure'] = reqs['total_requests'] - reqs['success']

    return totals, reqs


def compute_usage_costs(tokens, pricing):
    input_price = _safe_float(pricing.get('input', 0.0))
    output_price = _safe_float(pricing.get('output', 0.0))
    cache_price = _safe_float(pricing.get('cache', 0.0))

    billable_input_tokens = get_billable_input_tokens(tokens)
    cached_tokens = _safe_int(tokens.get('cached_tokens', 0))

    input_cost = billable_input_tokens / PRICING_BASIS_TOKENS * input_price
    output_tokens = _safe_int(tokens.get('output_tokens', 0)) + _safe_int(tokens.get('reasoning_tokens', 0))
    output_cost = output_tokens / PRICING_BASIS_TOKENS * output_price
    cache_cost = cached_tokens / PRICING_BASIS_TOKENS * cache_price
    total_cost = input_cost + output_cost + cache_cost

    return {
        'input': input_cost,
        'output': output_cost,
        'cache': cache_cost,
        'total': total_cost,
    }


def get_billable_input_tokens(tokens):
    input_tokens = _safe_int(tokens.get('input_tokens', 0))
    cached_tokens = _safe_int(tokens.get('cached_tokens', 0))
    return max(input_tokens - cached_tokens, 0)


def get_pricing_basis_info():
    return {
        'tokens': PRICING_BASIS_TOKENS,
        'label': PRICING_BASIS_LABEL,
        'text': PRICING_BASIS_TEXT,
    }


def _parse_float_or_none(value):
    if value is None:
        return None
    try:
        parsed = float(value)
        return parsed if math.isfinite(parsed) and parsed >= 0 else None
    except Exception:
        return None


def _fetch_openrouter_models(*, allow_network=True):
    """
    从 OpenRouter 获取模型列表（带长缓存）。
    OpenRouter pricing 字段为“美元/Token”，本面板内部价格口径为“美元/百万Tokens”。
    """
    cache_key = 'openrouter_models_v1'
    cached = cache.get(cache_key, max_age=6 * 3600)
    if cached is not None:
        return cached
    if cache.get('openrouter_models_error', max_age=300) is not None:
        return []
    if not allow_network:
        return None

    url = 'https://openrouter.ai/api/v1/models'
    try:
        with http_session.get(url, timeout=8, stream=True) as resp:
            resp.raise_for_status()
            payload = _response_json_limited(resp, 16 * 1024 * 1024)
        models = payload.get('data', []) if isinstance(payload, dict) else []
        if not isinstance(models, list):
            models = []
        cache.set(cache_key, models)
        return models
    except Exception as e:
        print(f'Warning: failed to fetch OpenRouter models ({_error_kind(e)})')
        cache.set('openrouter_models_error', True)
        return []


def _openrouter_pricing_per_million(model_id: str, *, allow_network=True):
    if not model_id:
        return None

    models = _fetch_openrouter_models(allow_network=allow_network)
    if models is None:
        return None
    for m in models:
        if not isinstance(m, dict):
            continue
        if (m.get('id') or '') != model_id:
            continue
        pricing = m.get('pricing') if isinstance(m.get('pricing'), dict) else {}
        prompt = _parse_float_or_none(pricing.get('prompt'))
        completion = _parse_float_or_none(pricing.get('completion'))
        cache_read = _parse_float_or_none(pricing.get('input_cache_read'))

        if prompt is None or completion is None:
            return None

        # OpenRouter pricing is USD/token; panel uses USD / 1M tokens
        per_million = {
            'input': prompt * 1_000_000,
            'output': completion * 1_000_000,
        }
        # cached tokens price is optional
        if cache_read is not None:
            per_million['cache'] = cache_read * 1_000_000
        else:
            # 兜底：如果来源不提供 cache 价格，先按 input 计（用户可手动改）
            per_million['cache'] = per_million['input']

        return {
            'pricing': per_million,
            'model': model_id,
            'source': 'openrouter',
        }
    return None


def _pick_pricing_auto_model_id():
    configured = (str(CONFIG.get('pricing_auto_model', '') or '').strip())
    if configured:
        return configured
    # 尝试从 config.yaml 拿一个模型 id（不依赖上游接口）
    try:
        models, _ = get_models_from_config()
        if isinstance(models, list) and models:
            mid = (models[0].get('id') if isinstance(models[0], dict) else None) or ''
            mid = str(mid).strip()
            if mid:
                return mid
    except Exception:
        pass
    # 最终回退
    return 'openai/gpt-4o-mini'


def get_effective_pricing(*, allow_remote=True):
    """
    返回本次用于展示/计算的价格（USD / 1M tokens）。
    规则：
    - 手动价格 > 0：优先使用手动
    - 手动价格为 0：且开启自动同步时，尝试从来源补齐（目前为 OpenRouter）
    """
    manual = {
        'input': _safe_float(CONFIG.get('pricing_input', 0.0)),
        'output': _safe_float(CONFIG.get('pricing_output', 0.0)),
        'cache': _safe_float(CONFIG.get('pricing_cache', 0.0)),
    }

    meta = {
        'mode': 'manual',
        'source': 'manual',
        'model': None,
        'fields': {'input': 'manual', 'output': 'manual', 'cache': 'manual'},
        'auto_enabled': _parse_bool(CONFIG.get('pricing_auto_enabled', True)),
        'auto_source': str(CONFIG.get('pricing_auto_source', 'openrouter') or 'openrouter').strip().lower(),
        'auto_model': (str(CONFIG.get('pricing_auto_model', '') or '').strip() or None),
    }

    if not _parse_bool(CONFIG.get('pricing_auto_enabled', True)):
        return manual, meta

    need_auto = any(manual.get(k, 0.0) <= 0 for k in ('input', 'output', 'cache'))
    if not need_auto:
        return manual, meta

    source = str(CONFIG.get('pricing_auto_source', 'openrouter') or 'openrouter').strip().lower()
    if source != 'openrouter':
        return manual, meta

    model_id = _pick_pricing_auto_model_id()
    suggested = _openrouter_pricing_per_million(model_id, allow_network=allow_remote)
    if not suggested:
        # 如果 config.yaml 挑的模型在 OpenRouter 找不到，尝试回退到固定模型
        if model_id != 'openai/gpt-4o-mini':
            suggested = _openrouter_pricing_per_million('openai/gpt-4o-mini', allow_network=allow_remote)
    if not suggested:
        if not allow_remote:
            meta['pending'] = True
        return manual, meta

    eff = dict(manual)
    fields = dict(meta['fields'])
    for k in ('input', 'output', 'cache'):
        if eff.get(k, 0.0) <= 0:
            eff[k] = _safe_float(suggested['pricing'].get(k, eff[k]))
            fields[k] = 'openrouter'

    meta = {
        'mode': 'mixed' if any(v == 'openrouter' for v in fields.values()) and any(v == 'manual' for v in fields.values()) else 'auto',
        'source': suggested.get('source', 'openrouter'),
        'model': suggested.get('model'),
        'fields': fields,
        'auto_enabled': True,
        'auto_source': source,
        'auto_model': (str(CONFIG.get('pricing_auto_model', '') or '').strip() or None),
    }
    return eff, meta


def _read_file_first_line(path):
    try:
        if os.path.exists(path):
            with open(path, 'r', encoding='utf-8', errors='ignore') as f:
                return f.readline().strip()
    except Exception:
        pass
    return None


def get_system_info():
    cached = cache.get('system_info', max_age=3600)
    if cached is not None:
        return cached
    info = {
        'cpu_model': None,
        'os_version': None,
        'cloud_vendor': None,
    }
    if is_linux():
        try:
            with open('/proc/cpuinfo', 'r', encoding='utf-8', errors='ignore') as f:
                for line in f:
                    if 'model name' in line:
                        info['cpu_model'] = line.split(':', 1)[-1].strip()
                        break
        except Exception:
            pass

        try:
            with open('/etc/os-release', 'r', encoding='utf-8', errors='ignore') as f:
                for line in f:
                    if line.startswith('PRETTY_NAME='):
                        info['os_version'] = line.split('=', 1)[-1].strip().strip('"')
                        break
        except Exception:
            pass

        vendor = _read_file_first_line('/sys/class/dmi/id/sys_vendor')
        product = _read_file_first_line('/sys/class/dmi/id/product_name')
        if vendor or product:
            info['cloud_vendor'] = ' '.join([v for v in [vendor, product] if v])

    info['cpu_model'] = info['cpu_model'] or platform.processor() or 'unknown'
    info['os_version'] = info['os_version'] or platform.platform()
    info['cloud_vendor'] = info['cloud_vendor'] or 'unknown'
    cache.set('system_info', info)
    return info


def get_cliproxy_process_usage():
    if not get_capabilities()['service_control']:
        return {'available': False, 'cpu_percent': None, 'memory_bytes': None, 'memory_percent': None}
    if not HAS_PSUTIL:
        return {'cpu_percent': 0.0, 'memory_bytes': 0, 'memory_percent': 0.0}
    monitor = globals().get('resource_monitor')
    if monitor is not None:
        return monitor.get_cliproxy_usage()
    target = str(CONFIG.get('cliproxy_service', 'cliproxy') or 'cliproxy').lower()
    cpu_percent = 0.0
    memory_bytes = 0
    memory_percent = 0.0
    try:
        for proc in psutil.process_iter(['name', 'cmdline', 'memory_info', 'memory_percent']):
            name = (proc.info.get('name') or '').lower()
            cmdline = ' '.join(proc.info.get('cmdline') or []).lower()
            if target in name or target in cmdline:
                try:
                    cpu_percent = proc.cpu_percent(interval=0.0)
                    mem_info = proc.info.get('memory_info')
                    if mem_info:
                        memory_bytes = getattr(mem_info, 'rss', 0)
                    memory_percent = _safe_float(proc.info.get('memory_percent', 0.0))
                    break
                except Exception:
                    continue
    except Exception:
        pass
    return {
        'cpu_percent': cpu_percent,
        'memory_bytes': memory_bytes,
        'memory_percent': memory_percent,
    }


def _normalize_quote_text(text):
    # Preserve the supplied wording and punctuation. Earlier versions reordered
    # Chinese/English parentheticals, which silently changed the quote itself.
    return str(text or '').strip()


def _parse_quote_line(raw_line):
    line = str(raw_line or '').strip().lstrip('\ufeff')
    if not line:
        return None

    if '出自：' in line:
        quote, author = line.rsplit('出自：', 1)
        quote = re.sub(r'(?:出自：\s*)+$', '', quote).strip()
        author = author.strip()
    else:
        # The provided X.txt contains a few legacy lines without the marker but
        # with a bilingual author suffix, e.g. “……。特朗普/Donald Trump（…）”.
        author_match = re.search(
            r'(?P<author>[\u4e00-\u9fff·]{2,}\s*/\s*[A-Za-z][A-Za-z .\'-]*(?:（[^）]+）)?)\s*$',
            line,
        )
        if author_match:
            quote = line[:author_match.start()].strip()
            author = author_match.group('author').strip()
        else:
            quote = line
            author = '未标注出处'

    quote = _normalize_quote_text(quote)
    if not quote or not author:
        return None
    return {'text': quote, 'author': author}


def load_quotes():
    configured_path = _resolve_panel_path(CONFIG.get('quotes_path'))
    # X.txt supplied with the project is always loaded. A configured path can
    # add quotes, but can no longer accidentally replace the required library.
    candidate_paths = [BUNDLED_QUOTES_PATH]
    if configured_path and os.path.normcase(os.path.realpath(configured_path)) != os.path.normcase(os.path.realpath(BUNDLED_QUOTES_PATH)):
        candidate_paths.append(configured_path)

    quotes = []
    seen = set()
    for path in candidate_paths:
        if not path or not os.path.isfile(path):
            continue
        try:
            with open(path, 'r', encoding='utf-8-sig', errors='replace') as handle:
                for raw_line in handle:
                    item = _parse_quote_line(raw_line)
                    if not item:
                        continue
                    key = (item['text'], item['author'])
                    if key in seen:
                        continue
                    seen.add(key)
                    quotes.append(item)
        except Exception as e:
            print(f"Warning: failed to load quotes ({_error_kind(e)})")
    return quotes


def get_random_quote():
    cached = cache.get('quotes_cache', max_age=300)
    if cached is None:
        cached = load_quotes()
        cache.set('quotes_cache', cached)
    if not cached:
        return {'text': '欢迎回来，祝你今天高效完成任务。', 'author': '系统'}
    return random.choice(cached)

cache = CacheManager()

# ==================== 后台资源监控 ====================
class ResourceMonitor:
    """非阻塞资源监控器"""
    def __init__(self):
        self._cpu_percent = 0.0
        self._cliproxy_usage = {'cpu_percent': 0.0, 'memory_bytes': 0, 'memory_percent': 0.0, 'pid': None}
        self._cliproxy_process = None
        self._lock = threading.Lock()
        self._running = False

    def start(self):
        """启动后台监控线程"""
        if self._running:
            return
        self._running = True
        thread = threading.Thread(target=self._monitor_loop, daemon=True)
        thread.start()

    def _monitor_loop(self):
        """后台监控循环"""
        while self._running:
            try:
                if HAS_PSUTIL:
                    cpu = psutil.cpu_percent(interval=1)  # 1秒采样
                    process_usage = self._sample_cliproxy_process()
                    with self._lock:
                        self._cpu_percent = cpu
                        self._cliproxy_usage = process_usage
            except Exception as e:
                print(f'Warning: resource monitor sample failed ({_error_kind(e)})')
            time.sleep(2)  # 每3秒更新一次(1秒采样+2秒等待)

    def _sample_cliproxy_process(self):
        empty = {'cpu_percent': 0.0, 'memory_bytes': 0, 'memory_percent': 0.0, 'pid': None}
        if not HAS_PSUTIL or not get_capabilities()['service_control']:
            return empty
        target = str(CONFIG.get('cliproxy_service', 'cliproxy') or 'cliproxy').lower()
        proc = self._cliproxy_process
        try:
            if proc is not None and not proc.is_running():
                proc = None
        except (psutil.Error, OSError):
            proc = None

        if proc is None:
            try:
                for candidate in psutil.process_iter(['name', 'cmdline']):
                    name = str(candidate.info.get('name') or '').lower()
                    cmdline = ' '.join(candidate.info.get('cmdline') or []).lower()
                    if target in name or target in cmdline or 'cli-proxy-api' in name or 'cliproxyapi' in name:
                        proc = candidate
                        proc.cpu_percent(interval=None)
                        break
            except (psutil.Error, OSError):
                proc = None
            self._cliproxy_process = proc

        if proc is None:
            return empty
        try:
            memory = proc.memory_info()
            return {
                'cpu_percent': round(max(0.0, proc.cpu_percent(interval=None)), 1),
                'memory_bytes': max(0, int(memory.rss)),
                'memory_percent': round(max(0.0, proc.memory_percent()), 2),
                'pid': proc.pid,
            }
        except (psutil.Error, OSError):
            self._cliproxy_process = None
            return empty

    def get_cpu_percent(self):
        """获取CPU使用率（非阻塞）"""
        with self._lock:
            return self._cpu_percent

    def get_cliproxy_usage(self):
        with self._lock:
            return dict(self._cliproxy_usage)

resource_monitor = ResourceMonitor()

def run_cmd(args, timeout=60, cwd=None):
    """Run a command without a shell so environment-supplied paths stay data."""
    try:
        if isinstance(args, (str, bytes)):
            raise TypeError('run_cmd requires an argument sequence, not a shell command string')
        result = subprocess.run(
            [os.fspath(arg) for arg in args],
            cwd=os.fspath(cwd) if cwd else None,
            shell=False,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        return result.returncode == 0, result.stdout.strip(), result.stderr.strip()
    except subprocess.TimeoutExpired:
        return False, '', 'Command timed out'
    except TypeError:
        return False, '', 'run_cmd requires an argument sequence'
    except Exception as e:
        return False, '', f'Command failed ({_error_kind(e)})'


def is_linux():
    return platform.system().lower() == 'linux'


def command_available(command):
    return shutil.which(command) is not None


def _systemd_service_name(value=None):
    name = str(value if value is not None else CONFIG.get('cliproxy_service') or '').strip()
    if (
        not name
        or len(name) > 255
        or name.startswith('-')
        or not re.fullmatch(r'(?:[A-Za-z0-9_.@:-]|\\x[0-9A-Fa-f]{2})+', name)
    ):
        return ''
    return name


def cleanup_binary_backups(binary_path, keep=None, max_total_bytes=None, max_age_seconds=None):
    if not binary_path:
        return []
    if keep is None:
        keep = CONFIG.get('backup_retention_count', BACKUP_RETENTION_COUNT)
    keep = max(1, _safe_int(keep, BACKUP_RETENTION_COUNT))
    if max_total_bytes is None:
        max_total_bytes = max(0, _safe_int(CONFIG.get('backup_max_total_mb', 512), 512)) * 1024 * 1024
    if max_age_seconds is None:
        max_age_seconds = max(0, _safe_int(CONFIG.get('backup_max_age_days', 14), 14)) * 86400
    backup_dir = os.path.dirname(os.path.abspath(binary_path))
    binary_name = os.path.basename(binary_path)
    if not backup_dir or not binary_name or not os.path.isdir(backup_dir):
        return []

    backups = []
    prefix = f'{binary_name}.bak.'
    try:
        for entry in os.scandir(backup_dir):
            if not entry.is_file(follow_symlinks=False):
                continue
            if not entry.name.startswith(prefix):
                continue
            timestamp_match = BACKUP_TS_PATTERN.search(entry.name)
            if not timestamp_match:
                continue
            stat_result = entry.stat(follow_symlinks=False)
            timestamp_text = timestamp_match.group(1)
            is_utc_format = timestamp_text.count('-') == 2
            timestamp_format = '%Y%m%d-%H%M%S-%f' if is_utc_format else '%Y%m%d-%H%M%S'
            try:
                parsed_timestamp = datetime.strptime(timestamp_text, timestamp_format)
                # v2.2 microsecond names are UTC. Legacy second-only names were
                # generated with datetime.now(), so interpret them in the host's
                # historical local timezone (including DST).
                created_at = (
                    parsed_timestamp.replace(tzinfo=UTC).timestamp()
                    if is_utc_format
                    else parsed_timestamp.astimezone(UTC).timestamp()
                )
            except ValueError:
                created_at = stat_result.st_mtime
            backups.append({
                'mtime': stat_result.st_mtime,
                'created_at': created_at,
                'size': max(0, stat_result.st_size),
                'path': entry.path,
            })
    except Exception as e:
        print(f"Warning: failed to scan binary backups ({_error_kind(e)})")
        return []

    backups.sort(key=lambda item: item['created_at'], reverse=True)
    deleted = []
    kept_size = 0
    now = time.time()
    for index, backup in enumerate(backups):
        too_many = index >= keep
        too_old = bool(max_age_seconds and now - backup['created_at'] > max_age_seconds)
        too_large = bool(max_total_bytes and kept_size + backup['size'] > max_total_bytes)
        # Always retain the newest valid rollback point even if it exceeds a size/age cap.
        should_delete = index > 0 and (too_many or too_old or too_large)
        if not should_delete:
            kept_size += backup['size']
            continue
        try:
            os.remove(backup['path'])
            deleted.append(backup['path'])
        except Exception as e:
            print(f"Warning: failed to remove old backup ({_error_kind(e)})")
    return deleted


def create_binary_backup(binary_path):
    if not binary_path or not os.path.isfile(binary_path):
        return None
    timestamp = _utc_now().strftime('%Y%m%d-%H%M%S-%f')
    backup_path = f'{binary_path}.bak.{timestamp}'
    try:
        # A hard link avoids a temporary second copy before os.replace swaps in
        # the new binary. Fall back to copy when the filesystem disallows links.
        os.link(binary_path, backup_path)
    except OSError:
        shutil.copy2(binary_path, backup_path)
    return backup_path


def get_capabilities():
    mode = str(CONFIG.get('deployment_mode', 'auto')).lower()
    try:
        host, _ = _api_host_port()
        local = host.lower() == 'localhost' or ipaddress.ip_address(host).is_loopback
    except ValueError:
        local = False
    container = os.path.exists('/.dockerenv') or os.path.exists('/run/.containerenv')
    systemd = is_linux() and command_available('systemctl') and os.path.isdir('/run/systemd/system')
    if mode == 'auto':
        mode = 'docker' if container else ('systemd' if local and systemd else 'remote')
    controllable = mode == 'systemd' and local and systemd and not container
    return {
        'mode': mode,
        'service_control': controllable,
        'binary_update': controllable,
        'config_write': is_config_write_enabled(),
        'reason': '' if controllable else '当前为远程 / 容器监控模式。服务启停与升级请在上游部署环境中执行。',
        'resource_scope': 'panel_environment' if not controllable else 'host',
    }


def _upstream_fingerprint():
    # Used only in memory; never expose secret-bearing identity in API payloads.
    raw = _build_management_base_url() + '\0' + _management_headers().get('X-Management-Key', '')
    return hashlib.sha256(raw.encode()).hexdigest()


def _find_version_value(value, depth=0):
    if depth > 3:
        return None
    if isinstance(value, dict):
        for key, item in value.items():
            key_text = str(key).lower().replace('_', '-').replace(' ', '-')
            if key_text in {'version', 'cpa-version', 'cli-proxy-api-version', 'build-version', 'release', 'commit', 'commit-sha', 'git-commit'} and isinstance(item, (str, int, float)):
                candidate = str(item).strip()
                if _is_semver_like(candidate) or HASH_VERSION_PATTERN.fullmatch(candidate):
                    return candidate
            # Do not accidentally use a configured provider/client version.
            if key_text in {'build', 'build-info', 'server', 'metadata'}:
                found = _find_version_value(item, depth + 1)
                if found:
                    return found
    return None


def probe_upstream(force=False):
    """Single-flight, bounded probe; version is read from headers or config JSON."""
    identity = _upstream_fingerprint()
    previous = state.get('upstream', {})
    now = time.monotonic()
    if previous.get('_identity') == identity and now < previous.get('_next_probe', 0) and not force:
        return previous
    if not upstream_probe_lock.acquire(blocking=False):
        return previous
    try:
        previous = state.get('upstream', {})
        if previous.get('_identity') != identity:
            previous = {}
        elif not force and time.monotonic() < previous.get('_next_probe', 0):
            return previous
        result = {
            'available': False, 'reachable': False, 'status': 'unreachable',
            'http_status': None, 'version': previous.get('version', 'unknown'),
            'version_source': previous.get('version_source', 'unknown'),
            'version_stale': True, 'checked_at': _utc_iso(),
            'commit': previous.get('commit'), 'build_date': previous.get('build_date'),
            'last_success_at': previous.get('last_success_at'),
            'message': '无法连接上游，请检查容器网络、地址和端口。',
            '_identity': identity,
        }
        if _management_auth_locked():
            result.update(status='auth_required', message='管理接口认证已暂停，请重新保存正确的管理密钥。')
        else:
            response = None
            try:
                response = http_session.get(
                    _build_management_base_url() + '/v0/management/config',
                    headers=_management_headers(), timeout=(2, 3),
                    stream=True, allow_redirects=False,
                )
                if _upstream_fingerprint() != identity:
                    return state.get('upstream', {})
                _observe_management_response(response)
                code = response.status_code
                result.update(reachable=True, http_status=code)
                # Header names are case insensitive even for simple test adapters.
                headers = {str(k).lower(): str(v).strip() for k, v in response.headers.items()}
                version = (headers.get('x-cpa-version') or headers.get('x-cliproxy-version')
                           or headers.get('x-cli-proxy-api-version') or headers.get('x-version') or '')
                result['commit'] = headers.get('x-cpa-commit', '')[:80] or None
                result['build_date'] = headers.get('x-cpa-build-date', '')[:80] or None
                payload = None
                if code == 200 and not version:
                    try:
                        payload = _response_json_limited(response, 256 * 1024)
                    except (OSError, ValueError, TypeError):
                        payload = None
                    version = _find_version_value(payload) or ''
                if _is_semver_like(version) or version.lower() == 'dev' or HASH_VERSION_PATTERN.fullmatch(version):
                    source = 'management_header' if headers.get('x-cpa-version') or headers.get('x-cliproxy-version') or headers.get('x-cli-proxy-api-version') or headers.get('x-version') else 'management_payload'
                    result.update(version=_decorate_version_tag(version), version_source=source, version_stale=False)
                if code == 200:
                    result.update(available=True, status='running', last_success_at=_utc_iso(),
                                  message='管理接口可用' if not result['version_stale'] else '管理接口可用，但未提供可识别的版本响应头。')
                elif code in (401, 403):
                    result.update(status='auth_required', message='上游可连接，但拒绝管理密钥；检查密钥及 allow-remote 配置。')
                elif code == 404:
                    result.update(status='not_found', message='上游未开放管理接口；检查管理密钥、API 基址与反向代理路径。')
                else:
                    result.update(status='upstream_error', message=f'管理接口返回 HTTP {code}，请检查上游服务。')
            except requests.RequestException:
                pass
            finally:
                if response is not None:
                    response.close()
        failures = 0 if result['available'] else min(6, int(previous.get('_failures', 0)) + 1)
        result['_failures'] = failures
        result['_next_probe'] = time.monotonic() + (10 if not failures else min(120, 5 * 2 ** failures))
        # Discard a response if the operator changed credentials during the request.
        if _upstream_fingerprint() != identity:
            return state.get('upstream', {})
        state['upstream'] = result
        return result
    finally:
        upstream_probe_lock.release()


def upstream_snapshot():
    snapshot = {key: value for key, value in state.get('upstream', {}).items() if not key.startswith('_')}
    snapshot.setdefault('status', 'checking')
    snapshot.setdefault('message', '正在等待首次连接检查')
    checked = _parse_iso_datetime(snapshot.get('checked_at'))
    snapshot['stale'] = not checked or (_utc_now() - checked).total_seconds() > 150
    return snapshot


def get_service_status(use_cache=True):
    """Local systemd status, or explicit remote management reachability."""
    if not get_capabilities()['service_control']:
        remote = upstream_snapshot()
        stale = remote.get('stale', True)
        return {
            'running': bool(remote.get('available')) and not stale,
            'status': 'unknown' if stale else remote.get('status', 'checking'),
            'pid': None, 'memory': 'N/A', 'cpu': 'N/A', 'uptime': 'N/A',
            'details': remote.get('message'), 'source': 'management_api',
            'checked_at': remote.get('checked_at'), 'stale': stale,
        }
    cache_key = 'service_status'
    if use_cache:
        cached = cache.get(cache_key, max_age=1)
        if cached:
            return cached

    status_out = ''
    pid_out = ''
    is_running = False

    if is_linux() and command_available('systemctl'):
        service_name = _systemd_service_name()
        if service_name:
            success, stdout, _ = run_cmd(['systemctl', 'is-active', service_name], timeout=3)
            is_running = success and stdout == 'active'
            _, full_status, _ = run_cmd(['systemctl', 'status', service_name, '--no-pager', '-l'], timeout=3)
            status_out = '\n'.join(full_status.splitlines()[:20])
            # 尽量用 systemd 的 MainPID（比 pgrep 更准确，且不依赖进程名）
            ok_pid, pid_value, _ = run_cmd(['systemctl', 'show', service_name, '-p', 'MainPID', '--value'], timeout=3)
            if ok_pid:
                pid_value = (pid_value or '').strip()
                if pid_value and pid_value != '0':
                    pid_out = pid_value
        else:
            status_out = 'Invalid or missing systemd service name'
    else:
        status_out = 'Not supported on this platform'

    # fallback：没有 systemd 或无法获取 MainPID 时再尝试 pgrep
    if is_running and not pid_out and command_available('pgrep'):
        _, all_pids, _ = run_cmd(['pgrep', '-f', 'cli-proxy-api|cliproxyapi|cliproxy'])
        pid_out = next((line.strip() for line in all_pids.splitlines() if line.strip()), '')

    memory = 'N/A'
    cpu = 'N/A'
    uptime = 'N/A'

    if pid_out:
        if HAS_PSUTIL:
            try:
                proc = psutil.Process(int(pid_out))
                memory = f'{proc.memory_info().rss / 1024 / 1024:.1f} MB'
                # 使用后台进程采样，避免把整机 CPU 错标成 CLIProxy CPU。
                cpu = f'{get_cliproxy_process_usage().get("cpu_percent", 0.0):.1f}%'
                uptime_seconds = time.time() - proc.create_time()
                uptime = format_uptime(uptime_seconds)
            except (ValueError, psutil.Error, OSError):
                pass
        elif command_available('ps'):
            _, mem_out, _ = run_cmd(['ps', '-o', 'rss=', '-p', str(pid_out)])
            if mem_out:
                try:
                    memory = f'{int(mem_out) / 1024:.1f} MB'
                except (TypeError, ValueError):
                    pass

    result = {
        'running': is_running,
        'status': 'running' if is_running else 'stopped',
        'pid': pid_out if pid_out else None,
        'memory': memory,
        'cpu': cpu,
        'uptime': uptime,
        'details': status_out,
        'source': 'systemd',
        'checked_at': _utc_iso(),
        'stale': False,
    }

    cache.set(cache_key, result)
    return result

def format_uptime(seconds):
    if seconds < 60:
        return f'{int(seconds)}秒'
    elif seconds < 3600:
        return f'{int(seconds/60)}分钟'
    elif seconds < 86400:
        hours = int(seconds / 3600)
        mins = int((seconds % 3600) / 60)
        return f'{hours}小时{mins}分'
    else:
        days = int(seconds / 86400)
        hours = int((seconds % 86400) / 3600)
        return f'{days}天{hours}小时'


def _configured_github_token():
    return (os.environ.get('CLIPROXY_PANEL_GITHUB_TOKEN') or os.environ.get('GITHUB_TOKEN') or '').strip()


def _latest_release_redirect_version(repo):
    """Resolve releases/latest without consuming the anonymous API quota."""
    html_latest_url = f'https://github.com/{repo}/releases/latest'
    resp = http_session.get(
        html_latest_url,
        headers={'User-Agent': 'CLIProxyPanel'},
        timeout=10,
        allow_redirects=False,
        stream=True,
    )
    try:
        location = resp.headers.get('Location', '')
        match = re.search(r'/tag/(v[^/?#]+)', location)
    finally:
        resp.close()
    if match:
        return match.group(1)

    resp = http_session.get(
        html_latest_url,
        headers={'User-Agent': 'CLIProxyPanel'},
        timeout=10,
        allow_redirects=True,
        stream=True,
    )
    try:
        match = re.search(r'/tag/(v[^/?#]+)', str(getattr(resp, 'url', '') or ''))
    finally:
        resp.close()
    return match.group(1) if match else 'unknown'


def _latest_release_api_version(repo, token=''):
    headers = {
        'User-Agent': 'CLIProxyPanel',
        'Accept': 'application/vnd.github+json',
    }
    if token:
        headers['Authorization'] = 'Bearer ' + token
    with http_session.get(
        f'https://api.github.com/repos/{repo}/releases/latest',
        headers=headers,
        timeout=8,
        stream=True,
    ) as resp:
        if resp.status_code != 200:
            return 'unknown'
        data = _response_json_limited(resp, 2 * 1024 * 1024)
        return (data.get('tag_name') if isinstance(data, dict) else None) or 'unknown'


def get_github_release_version(use_cache=True):
    """从 GitHub releases 获取最新版本；匿名场景优先使用稳定跳转地址。"""
    cache_key = 'github_release'
    if use_cache:
        cached = cache.get(cache_key, max_age=300)
        if cached is not None:
            return cached

    repo = 'router-for-me/CLIProxyAPI'
    token = _configured_github_token()
    attempts = (
        (lambda: _latest_release_api_version(repo, token), 'api'),
        (lambda: _latest_release_redirect_version(repo), 'redirect'),
    ) if token else (
        (lambda: _latest_release_redirect_version(repo), 'redirect'),
        (lambda: _latest_release_api_version(repo), 'api fallback'),
    )

    for resolver, label in attempts:
        try:
            version = resolver()
            if _release_version_key(version) is not None:
                cache.set(cache_key, version)
                return version
        except Exception as exc:
            print(f'get_github_release_version {label} error ({_error_kind(exc)})')

    cache.set(cache_key, 'unknown')
    return 'unknown'


def _normalize_release_version(version):
    if version is None:
        return ''
    v = str(version).strip()
    if not v:
        return ''
    if v.lower() == 'unknown':
        return 'unknown'
    if v.lower() == 'dev':
        return 'dev'
    if v.startswith(('v', 'V')) and len(v) > 1:
        return v[1:]
    return v


def _decorate_version_tag(version):
    """统一显示为 vX.Y.Z（如果看起来像语义版本）"""
    raw = str(version).strip() if version is not None else ''
    if not raw:
        return raw
    if raw.lower() in {'unknown', 'dev'}:
        return raw.lower()
    normalized = _normalize_release_version(raw)
    # 只对语义版本样式做装饰，保留预发布/构建信息。
    if VERSION_PATTERN.match(normalized):
        return f'v{normalized}'
    return raw


def _release_version_key(version):
    raw = str(version or '').strip()
    match = VERSION_PATTERN.match(raw)
    if not match:
        return None
    release = tuple(int(part) for part in match.group('release').split('.'))
    release = release + (0,) * (4 - len(release))
    suffix = match.group('suffix') or ''
    prerelease = ''
    if suffix.startswith('-'):
        prerelease = suffix[1:].split('+', 1)[0]
    # A final release sorts after a prerelease of the same numeric version.
    stability = 1 if not prerelease else 0
    identifiers = []
    for identifier in prerelease.lower().split('.') if prerelease else []:
        if identifier.isdigit():
            identifiers.append((0, int(identifier)))
        else:
            identifiers.append((1, identifier))
    return release, stability, tuple(identifiers)


def _cliproxy_management_get(path, timeout=6):
    if _management_auth_locked():
        return None
    try:
        base_url = _build_management_base_url()
        url = f'{base_url}{path}'
        headers = _management_headers()
        resp = http_session.get(url, headers=headers, timeout=timeout, stream=True)
        _observe_management_response(resp)
        return resp
    except Exception:
        return None


def _get_local_version_from_management():
    snapshot = probe_upstream()
    version = snapshot.get('version')
    if version and version != 'unknown':
        state['version_source'] = snapshot.get('version_source', 'management_header')
        return version
    return None


def _is_git_repo(path):
    try:
        return bool(path) and os.path.isdir(path) and os.path.isdir(os.path.join(path, '.git'))
    except Exception:
        return False


def _is_semver_like(version) -> bool:
    """是否看起来像 release 版本号（支持 v 前缀）"""
    normalized = _normalize_release_version(version)
    if not normalized or normalized in {'unknown', 'dev'}:
        return False
    return bool(VERSION_PATTERN.match(str(normalized)))


def _get_last_successful_release_version_from_history():
    """从 update_history.json 中取最近一次成功更新的 release 版本号（用于兜底显示）"""
    try:
        path = UPDATE_HISTORY_PATH
        if not path or not os.path.exists(path):
            return None
        history = _load_json_file_limited(path, 1024 * 1024)
        if not isinstance(history, list):
            return None
        for entry in reversed(history):
            if not isinstance(entry, dict):
                continue
            if entry.get('success') is not True:
                continue
            v = entry.get('version')
            if _is_semver_like(v):
                return _decorate_version_tag(v)
    except Exception:
        return None
    return None


def get_local_version():
    """获取本地版本号"""
    cache_key = 'local_version'
    cached = cache.get(cache_key, max_age=15)
    if cached is not None:
        return cached

    mgmt_candidate = None

    # 1) 优先：从管理接口读取（适配 release 二进制安装场景）
    mgmt_version = _get_local_version_from_management()
    if mgmt_version:
        # 如果是规范的 release 版本号，直接返回
        if _is_semver_like(mgmt_version):
            cache.set(cache_key, mgmt_version)
            return mgmt_version
        # A dev build is honest information, not a reason to invent a release.
        cache.set(cache_key, mgmt_version)
        return mgmt_version

    if not get_capabilities()['binary_update']:
        state['version_source'] = 'unknown'
        cache.set(cache_key, 'unknown')
        return 'unknown'

    # 2) 其次：本地 git 仓库
    cliproxy_dir = _resolve_panel_path(CONFIG.get('cliproxy_dir'))
    if _is_git_repo(cliproxy_dir) and command_available('git'):
        version_file = os.path.join(cliproxy_dir, 'VERSION')
        if os.path.exists(version_file):
            try:
                with open(version_file, 'r') as f:
                    version = f.read().strip()
                    if version and _is_semver_like(version):
                        decorated = _decorate_version_tag(version)
                        cache.set(cache_key, decorated)
                        return decorated
            except Exception:
                pass

        ok, stdout, _ = run_cmd(['git', 'describe', '--tags', '--exact-match'], cwd=cliproxy_dir, timeout=3)
        if ok and stdout and _is_semver_like(stdout):
            decorated = _decorate_version_tag(stdout)
            cache.set(cache_key, decorated)
            return decorated

        ok, stdout, _ = run_cmd(['git', 'rev-parse', '--short', 'HEAD'], cwd=cliproxy_dir)
        if ok and stdout:
            mgmt_candidate = mgmt_candidate or stdout

    # Update history is not proof of the binary currently deployed.
    # Never replace an unknown current version with a previous successful release.

    # 4) 再兜底：如果管理接口返回了 hash 等信息，至少返回它；否则 unknown
    if mgmt_candidate:
        cache.set(cache_key, mgmt_candidate)
        return mgmt_candidate

    cache.set(cache_key, 'unknown')
    return 'unknown'

def _stat_file_identity(stat_result):
    return f'{getattr(stat_result, "st_dev", 0)}:{getattr(stat_result, "st_ino", 0)}'


def _new_log_stats_state(*, start_at_end=False):
    log_file = _resolve_panel_path(CONFIG.get('cliproxy_log'))
    initialized = False
    offset = 0
    last_size = 0
    last_mtime = None
    file_identity = None
    if start_at_end and log_file:
        try:
            stat_result = os.stat(log_file)
            initialized = True
            offset = stat_result.st_size
            last_size = stat_result.st_size
            last_mtime = stat_result.st_mtime
            file_identity = _stat_file_identity(stat_result)
        except OSError:
            pass
    return {
        'initialized': initialized,
        'offset': offset,
        'last_size': last_size,
        'last_mtime': last_mtime,
        'file_identity': file_identity,
        'total': 0,
        'success': 0,
        'failed': 0,
        'last_time': None,
        'latest_log_time': None,
        'timezone_offset_seconds': None,
        'timezone_source': 'unknown',
        'partial': False,
        'skipped_bytes': 0,
        'buffer': '',
        'base_total': 0,
        'base_success': 0,
        'base_failed': 0,
        'last_saved_ts': 0,
    }


def _reset_log_stats_state(*, start_at_end=False):
    with log_stats_lock:
        state['log_stats'] = _new_log_stats_state(start_at_end=start_at_end)
        state['log_stats_loaded'] = True
    cache.invalidate('request_count_logs')
    save_log_stats_state(force=True)


def read_log_tail(log_file, max_lines=100, chunk_size=4096):
    """Bounded O(n) tail: even a file with no newline cannot consume unbounded RAM."""
    if max_lines <= 0:
        return []
    try:
        with open(log_file, 'rb') as handle:
            handle.seek(0, os.SEEK_END)
            offset = max(0, handle.tell() - 1024 * 1024)
            handle.seek(offset)
            data = handle.read(1024 * 1024)
        lines = data.splitlines()
        if offset and lines:
            lines = lines[1:]  # first line may have been cut at the byte boundary
        return [line[:16 * 1024].decode('utf-8', errors='replace') for line in lines[-min(max_lines, 1000):]]
    except (OSError, ValueError):
        return []


def get_request_count_from_logs():
    """从日志获取请求统计（增量解析）"""
    cache_key = 'request_count_logs'
    cached = cache.get(cache_key, max_age=2)
    if cached is not None:
        return cached

    if not state.get('log_stats_loaded'):
        load_log_stats_state()

    log_file = _resolve_panel_path(CONFIG.get('cliproxy_log'))
    if not os.path.exists(log_file):
        with log_stats_lock:
            log_state = state.get('log_stats', {})
            result = {
                'count': _safe_int(log_state.get('base_total', 0)) + _safe_int(log_state.get('total', 0)),
                'last_time': _log_time_iso(log_state.get('last_time'), log_state.get('timezone_offset_seconds')),
                'last_time_raw': log_state.get('last_time'),
                'success': _safe_int(log_state.get('base_success', 0)) + _safe_int(log_state.get('success', 0)),
                'failed': _safe_int(log_state.get('base_failed', 0)) + _safe_int(log_state.get('failed', 0)),
                'log_available': False,
                'partial': bool(log_state.get('partial', False)),
                'skipped_bytes': _safe_int(log_state.get('skipped_bytes', 0)),
                'timezone': {
                    'configured': str(CONFIG.get('log_timezone', 'auto')),
                    'source': log_state.get('timezone_source', 'unknown'),
                    'offset_seconds': log_state.get('timezone_offset_seconds'),
                },
            }
        cache.set(cache_key, result)
        return result

    try:
        stat_result = os.stat(log_file)
        file_size = stat_result.st_size
        mtime = stat_result.st_mtime
        file_identity = _stat_file_identity(stat_result)
    except OSError:
        result = {
            'count': 0,
            'last_time': None,
            'last_time_raw': None,
            'success': 0,
            'failed': 0,
            'log_available': False,
        }
        cache.set(cache_key, result)
        return result

    needs_save = False
    with log_stats_lock:
        log_state = state.get('log_stats', {})
        initialized = log_state.get('initialized')
        last_size = log_state.get('last_size', 0)
        last_mtime = log_state.get('last_mtime')
        offset = max(0, _safe_int(log_state.get('offset', 0)))
        previous_identity = log_state.get('file_identity')

        rotated = False
        if not initialized:
            rotated = True
        elif previous_identity and previous_identity != file_identity:
            rotated = True
        elif file_size < offset or file_size < last_size:
            rotated = True
        elif last_mtime and mtime < last_mtime:
            rotated = True

        if rotated:
            if log_state.get('initialized'):
                log_state['base_total'] = _safe_int(log_state.get('base_total', 0)) + _safe_int(log_state.get('total', 0))
                log_state['base_success'] = _safe_int(log_state.get('base_success', 0)) + _safe_int(log_state.get('success', 0))
                log_state['base_failed'] = _safe_int(log_state.get('base_failed', 0)) + _safe_int(log_state.get('failed', 0))
            offset = 0
            log_state['buffer'] = ''
            log_state['total'] = 0
            log_state['success'] = 0
            log_state['failed'] = 0
            log_state['last_time'] = None
            log_state['latest_log_time'] = None
            log_state['partial'] = False
            log_state['skipped_bytes'] = 0
            initial_scan_limit = max(1, _safe_int(CONFIG.get('log_initial_scan_max_mb', 64), 64)) * 1024 * 1024
            if file_size > initial_scan_limit:
                offset = file_size - initial_scan_limit
                log_state['partial'] = True
                log_state['skipped_bytes'] = offset
        changed = rotated

        try:
            with open(log_file, 'rb') as f:
                f.seek(offset)
                if rotated:
                    log_state['discarding_line'] = bool(offset)
                new_offset = f.tell()
                budget = max(1, min(16, _safe_int(CONFIG.get('log_scan_budget_mb'), 4))) * 1024 * 1024
                deadline = time.monotonic() + 0.05
                while f.tell() - offset < budget and time.monotonic() < deadline:
                    line_start = f.tell()
                    raw_line = f.readline(64 * 1024)
                    if not raw_line:
                        break
                    new_offset = f.tell()
                    if log_state.get('discarding_line'):
                        log_state['discarding_line'] = not raw_line.endswith(b'\n')
                        continue
                    if not raw_line.endswith(b'\n'):
                        if len(raw_line) == 64 * 1024:
                            log_state['discarding_line'] = True
                            log_state['partial'] = True
                            log_state['skipped_bytes'] += len(raw_line)
                            continue
                        # Preserve a normal incomplete trailing line for the next scan.
                        new_offset = line_start
                        break
                    line = raw_line.decode('utf-8', errors='replace')
                    time_match = LOG_TIME_PATTERN.search(line)
                    if time_match:
                        log_state['latest_log_time'] = time_match.group(1)

                    if '[gin_logger.go:' not in line or not REQUEST_METHOD_PATTERN.search(line):
                        continue
                    if any(path in line for path in EXCLUDED_LOG_PATHS):
                        continue
                    log_state['total'] += 1
                    if time_match:
                        log_state['last_time'] = time_match.group(1)
                    status_match = REQUEST_STATUS_PATTERN.search(line)
                    if status_match:
                        code = int(status_match.group(1))
                        if 200 <= code < 400:
                            log_state['success'] += 1
                        elif code >= 400:
                            log_state['failed'] += 1
                    changed = True
        except (OSError, ValueError):
            result = {
                'count': _safe_int(log_state.get('base_total', 0)) + _safe_int(log_state.get('total', 0)),
                'last_time': _log_time_iso(log_state.get('last_time'), log_state.get('timezone_offset_seconds')),
                'last_time_raw': log_state.get('last_time'),
                'success': _safe_int(log_state.get('base_success', 0)) + _safe_int(log_state.get('success', 0)),
                'failed': _safe_int(log_state.get('base_failed', 0)) + _safe_int(log_state.get('failed', 0)),
                # An existing path is not enough: permission/I/O failures mean
                # activity cannot be observed reliably, so automatic updates
                # must stay on the safe side and wait.
                'log_available': False,
                'partial': bool(log_state.get('partial', False)),
                'skipped_bytes': _safe_int(log_state.get('skipped_bytes', 0)),
                'timezone': {
                    'configured': str(CONFIG.get('log_timezone', 'auto')),
                    'source': log_state.get('timezone_source', 'unknown'),
                    'offset_seconds': log_state.get('timezone_offset_seconds'),
                },
            }
            cache.set(cache_key, result)
            return result

        configured_tz, configured_source = _log_timezone(log_state.get('timezone_offset_seconds'))
        if str(CONFIG.get('log_timezone', 'auto')).strip().lower() == 'auto':
            inferred_offset = _infer_timezone_offset_seconds(log_state.get('latest_log_time'), mtime)
            if inferred_offset is not None:
                log_state['timezone_offset_seconds'] = inferred_offset
                configured_source = 'auto-inferred'
        else:
            utc_offset = _utc_now().astimezone(configured_tz).utcoffset() if configured_tz else None
            log_state['timezone_offset_seconds'] = int(utc_offset.total_seconds()) if utc_offset is not None else None
        log_state['timezone_source'] = configured_source

        log_state['initialized'] = True
        log_state['offset'] = new_offset
        log_state['catching_up'] = new_offset < file_size
        log_state['last_size'] = file_size
        log_state['last_mtime'] = mtime
        log_state['file_identity'] = file_identity
        log_state['buffer'] = ''
        state['log_stats'] = log_state

        needs_save = changed or new_offset != offset

        result = {
            'count': _safe_int(log_state.get('base_total', 0)) + _safe_int(log_state.get('total', 0)),
            'last_time': _log_time_iso(log_state.get('last_time'), log_state.get('timezone_offset_seconds')),
            'last_time_raw': log_state.get('last_time'),
            'success': _safe_int(log_state.get('base_success', 0)) + _safe_int(log_state.get('success', 0)),
            'failed': _safe_int(log_state.get('base_failed', 0)) + _safe_int(log_state.get('failed', 0)),
            'log_available': True,
            'catching_up': bool(log_state.get('catching_up', False)),
            'partial': bool(log_state.get('partial', False)),
            'skipped_bytes': _safe_int(log_state.get('skipped_bytes', 0)),
            'timezone': {
                'configured': str(CONFIG.get('log_timezone', 'auto')),
                'source': log_state.get('timezone_source', 'unknown'),
                'offset_seconds': log_state.get('timezone_offset_seconds'),
            },
        }
        cache.set(cache_key, result)

    if needs_save:
        save_log_stats_state()
    log_stats_path = _resolve_panel_path(CONFIG.get('log_stats_path'))
    if log_stats_path and not os.path.exists(log_stats_path):
        save_log_stats_state(force=True)
    return result


def resolve_version_label(version):
    if not version:
        return version
    version_str = str(version).strip()
    if not HASH_VERSION_PATTERN.match(version_str):
        return version_str
    if not command_available('git'):
        return version_str
    _, tags_out, _ = run_cmd(
        ['git', 'tag', '--contains', version_str],
        cwd=_resolve_panel_path(CONFIG.get('cliproxy_dir')),
        timeout=10,
    )
    if not tags_out:
        return version_str
    tags = [t.strip() for t in tags_out.splitlines() if t.strip()]
    if not tags:
        return version_str
    def parse_version_key(tag):
        cleaned = tag.lstrip('vV')
        parts = re.split(r'[^0-9]+', cleaned)
        nums = [int(p) for p in parts if p.isdigit()]
        return nums or [0]
    tags.sort(key=parse_version_key)
    return tags[-1]


def get_current_commit():
    """获取当前commit（带缓存）"""
    cache_key = 'current_commit'
    cached = cache.get(cache_key, max_age=30)
    if cached:
        return cached
    if not command_available('git'):
        cache.set(cache_key, 'unknown')
        return 'unknown'
    cliproxy_dir = _resolve_panel_path(CONFIG.get('cliproxy_dir'))
    _, stdout, _ = run_cmd(['git', 'rev-parse', '--short', 'HEAD'], cwd=cliproxy_dir)
    result = stdout if stdout else 'unknown'
    cache.set(cache_key, result)
    return result

def get_latest_commit():
    """获取最新commit（带缓存，减少网络请求）"""
    cache_key = 'latest_commit'
    cached = cache.get(cache_key, max_age=120)  # 2分钟缓存
    if cached:
        return cached
    if not command_available('git'):
        cache.set(cache_key, 'unknown')
        return 'unknown'
    cliproxy_dir = _resolve_panel_path(CONFIG.get('cliproxy_dir'))
    run_cmd(['git', 'fetch', 'origin', 'main', '--quiet'], cwd=cliproxy_dir, timeout=10)
    _, stdout, _ = run_cmd(['git', 'rev-parse', '--short', 'origin/main'], cwd=cliproxy_dir)
    result = stdout if stdout else 'unknown'
    cache.set(cache_key, result)
    return result


def _auto_update_failure_payload():
    return {
        'failure_count': max(0, _safe_int(state.get('auto_update_failure_count'), 0)),
        'retry_not_before': state.get('auto_update_retry_not_before'),
        'failed_version': state.get('auto_update_failed_version'),
        'saved_at': _utc_iso(),
    }


def _save_auto_update_failure_state_locked():
    try:
        _atomic_write_json(AUTO_UPDATE_STATE_PATH, _auto_update_failure_payload(), mode=0o600)
        return True
    except Exception as exc:
        print(f'Warning: failed to save auto-update retry state ({_error_kind(exc)})')
        return False


def load_auto_update_failure_state():
    if not os.path.exists(AUTO_UPDATE_STATE_PATH):
        return False
    try:
        data = _load_json_file_limited(AUTO_UPDATE_STATE_PATH, 64 * 1024)
        if not isinstance(data, dict):
            return False
        failed_version = _decorate_version_tag(data.get('failed_version'))
        if _release_version_key(failed_version) is None:
            failed_version = None
        retry_not_before = data.get('retry_not_before')
        if retry_not_before and _parse_iso_datetime(retry_not_before) is None:
            retry_not_before = None
        with auto_update_failure_lock:
            state['auto_update_failure_count'] = max(0, _safe_int(data.get('failure_count'), 0))
            state['auto_update_retry_not_before'] = retry_not_before
            state['auto_update_failed_version'] = failed_version
        return True
    except Exception as exc:
        print(f'Warning: failed to load auto-update retry state ({_error_kind(exc)})')
        return False


def _clear_auto_update_failure_state():
    with auto_update_failure_lock:
        changed = bool(
            state.get('auto_update_failure_count')
            or state.get('auto_update_retry_not_before')
            or state.get('auto_update_failed_version')
        )
        state['auto_update_failure_count'] = 0
        state['auto_update_retry_not_before'] = None
        state['auto_update_failed_version'] = None
        if changed:
            _save_auto_update_failure_state_locked()
    return changed


def _record_auto_update_failure(version):
    failed_version = _decorate_version_tag(version)
    if _release_version_key(failed_version) is None:
        return None
    with auto_update_failure_lock:
        previous_version = _decorate_version_tag(state.get('auto_update_failed_version'))
        if previous_version.lower() == failed_version.lower():
            failure_count = max(0, _safe_int(state.get('auto_update_failure_count'), 0)) + 1
        else:
            failure_count = 1
        base_delay = max(60, _safe_int(CONFIG.get('auto_update_failure_backoff_seconds'), 21600))
        max_delay = max(base_delay, _safe_int(CONFIG.get('auto_update_failure_backoff_max_seconds'), 86400))
        delay = min(max_delay, base_delay * (2 ** min(failure_count - 1, 20)))
        retry_not_before = _utc_iso(_utc_now() + timedelta(seconds=delay))
        state['auto_update_failure_count'] = failure_count
        state['auto_update_retry_not_before'] = retry_not_before
        state['auto_update_failed_version'] = failed_version
        _save_auto_update_failure_state_locked()
        return {
            'failure_count': failure_count,
            'retry_not_before': retry_not_before,
            'failed_version': failed_version,
            'retry_in_seconds': delay,
        }


def _auto_update_failure_snapshot():
    with auto_update_failure_lock:
        failure_count = max(0, _safe_int(state.get('auto_update_failure_count'), 0))
        retry_not_before = state.get('auto_update_retry_not_before')
        failed_version = state.get('auto_update_failed_version')
    retry_in_seconds = 0
    retry_at = _parse_iso_datetime(retry_not_before)
    if retry_at is not None:
        retry_in_seconds = max(0, int((retry_at - _utc_now()).total_seconds()))
    return {
        'failure_count': failure_count,
        'retry_not_before': retry_not_before,
        'failed_version': failed_version,
        'retry_in_seconds': retry_in_seconds,
    }


def _clear_failure_for_new_release(latest_version):
    latest = _decorate_version_tag(latest_version)
    if _release_version_key(latest) is None:
        return False
    with auto_update_failure_lock:
        failed = _decorate_version_tag(state.get('auto_update_failed_version'))
    if failed and failed.lower() != latest.lower():
        return _clear_auto_update_failure_state()
    return False


def check_for_updates(use_cache=True, *, allow_network=True):
    """检查更新（使用GitHub releases）"""
    cache_key = 'update_check_details'
    if not allow_network:
        return bool(state.get('has_update', False))
    if use_cache:
        cached = cache.get(cache_key, max_age=60)
        if cached is not None:
            state['current_version'] = cached['current']
            state['latest_version'] = cached['latest']
            state['has_update'] = bool(cached['has_update'])
            return bool(cached['has_update'])
        if not allow_network:
            return bool(state.get('has_update', False))

    current = get_local_version()
    latest = get_github_release_version(use_cache=use_cache)

    # 统一展示版本格式
    current_display = _decorate_version_tag(current)
    latest_display = _decorate_version_tag(latest)
    state['current_version'] = current_display
    state['latest_version'] = latest_display
    _clear_failure_for_new_release(latest_display)

    # 语义比较避免把“本地版本更高”或前缀差异误判为可更新。
    current_key = _release_version_key(current_display)
    latest_key = _release_version_key(latest_display)
    if current_key is not None and latest_key is not None:
        result = latest_key > current_key and not upstream_snapshot().get('version_stale', False)
    elif get_capabilities()['binary_update'] and _is_git_repo(_resolve_panel_path(CONFIG.get('cliproxy_dir'))) and command_available('git'):
        current_commit = get_current_commit()
        latest_commit = get_latest_commit()
        result = (
            current_commit not in {'', 'unknown'}
            and latest_commit not in {'', 'unknown'}
            and current_commit != latest_commit
        )
    else:
        # 无法可靠判断时宁可不执行破坏性的自动更新，并在界面保留 unknown。
        result = False

    cache.set(cache_key, {
        'current': current_display,
        'latest': latest_display,
        'has_update': result,
    })
    state['has_update'] = result
    return result

def is_idle():
    """检查系统是否空闲（基于日志中的最后请求时间）"""
    return get_idle_state().get('is_idle', True)


def get_idle_state(stats=None):
    """返回当前空闲状态及剩余等待时间。"""
    if stats is None:
        stats = get_request_count_from_logs()

    last_time_str = stats.get('last_time')
    idle_threshold = max(0, int(CONFIG.get('idle_threshold_seconds', 0) or 0))
    result = {
        'is_idle': True,
        'reason': 'no_requests',
        'log_available': bool(stats.get('log_available', True)) if isinstance(stats, dict) else True,
        'last_request_time': last_time_str,
        'idle_threshold_seconds': idle_threshold,
        'idle_for_seconds': None,
        'idle_wait_seconds': 0,
        'clock_skew_seconds': 0,
        'timezone': stats.get('timezone') if isinstance(stats, dict) else None,
    }

    if not result['log_available'] or stats.get('catching_up'):
        result.update(is_idle=False, reason='log_catching_up' if stats.get('catching_up') else 'log_unavailable', idle_wait_seconds=None)
        return result

    if not last_time_str:
        if not result['log_available']:
            # Missing activity data is not proof of idleness. Staying busy here
            # prevents an automatic update from interrupting live traffic when
            # a path/mount is wrong or a log is temporarily unavailable.
            result['is_idle'] = False
            result['reason'] = 'log_unavailable'
            result['idle_wait_seconds'] = None
        return result

    try:
        offset_seconds = None
        timezone_meta = stats.get('timezone') if isinstance(stats, dict) else None
        if isinstance(timezone_meta, dict):
            offset_seconds = timezone_meta.get('offset_seconds')
        last_time = _log_time_to_utc(last_time_str, offset_seconds)
        if last_time is None:
            result['is_idle'] = False
            result['reason'] = 'invalid_timestamp'
            result['idle_wait_seconds'] = None
            return result
        elapsed = int((_utc_now() - last_time).total_seconds())
        idle_seconds = max(0, elapsed)
        idle_wait_seconds = max(0, idle_threshold - idle_seconds)
        result['last_request_time'] = _utc_iso(last_time)
        result['idle_for_seconds'] = idle_seconds
        result['idle_wait_seconds'] = idle_wait_seconds
        result['clock_skew_seconds'] = max(0, -elapsed)
        result['is_idle'] = idle_wait_seconds == 0 and elapsed >= 0
        result['reason'] = 'threshold_reached' if result['is_idle'] else ('clock_skew' if elapsed < 0 else 'recent_request')
        return result
    except (TypeError, ValueError, OverflowError):
        result['is_idle'] = False
        result['reason'] = 'invalid_timestamp'
        result['idle_wait_seconds'] = None
        return result


def get_auto_update_state(has_update=None, stats=None):
    """返回自动更新当前所处阶段，供前端直接展示。"""
    if stats is None:
        stats = get_request_count_from_logs()
    if has_update is None:
        has_update = check_for_updates()

    idle_state = get_idle_state(stats)
    failure_state = _auto_update_failure_snapshot()
    next_check_time = state.get('next_auto_update_check_time')
    next_check_in_seconds = None
    next_check_monotonic = state.get('next_auto_update_check_monotonic')
    if next_check_monotonic is not None:
        next_check_in_seconds = max(0, int(float(next_check_monotonic) - time.monotonic()))
    elif next_check_time:
        try:
            next_check_dt = _parse_iso_datetime(next_check_time)
            next_check_in_seconds = max(0, int((next_check_dt - _utc_now()).total_seconds())) if next_check_dt else None
        except (TypeError, ValueError, OverflowError):
            next_check_in_seconds = None

    summary = '等待状态更新'
    phase = 'unknown'
    if not get_capabilities()['binary_update']:
        phase = 'unsupported'
        summary = '监控模式：升级由部署环境管理'
    elif not state.get('auto_update_enabled', False):
        phase = 'disabled'
        summary = '自动更新已关闭'
    elif state.get('update_in_progress'):
        phase = 'updating'
        summary = '正在执行自动更新'
    elif _normalize_release_version(state.get('latest_version')) in {'', 'unknown'}:
        phase = 'checking'
        summary = '正在检查最新版本'
    elif _release_version_key(state.get('current_version')) is None or upstream_snapshot().get('version_stale'):
        phase = 'unknown_version'
        summary = '当前版本无法可靠比较'
    elif not has_update:
        phase = 'no_update'
        summary = '未发现更高版本'
    elif failure_state['retry_in_seconds'] > 0:
        phase = 'backoff'
        summary = f'上次更新失败，{format_uptime(failure_state["retry_in_seconds"])}后自动重试'
    elif not idle_state.get('is_idle'):
        phase = 'wait_idle'
        if idle_state.get('reason') == 'log_unavailable':
            summary = '等待可用的请求日志'
        elif idle_state.get('reason') == 'invalid_timestamp':
            summary = '等待有效的请求时间戳'
        elif idle_state.get('reason') == 'clock_skew':
            summary = '等待服务器时钟恢复一致'
        else:
            summary = f'还需空闲 {idle_state.get("idle_wait_seconds", 0)} 秒'
    elif next_check_in_seconds is not None and next_check_in_seconds > 0:
        phase = 'wait_check'
        summary = f'{next_check_in_seconds} 秒后进行下一次检查'
    else:
        phase = 'ready'
        summary = '已满足自动更新条件'

    return {
        'phase': phase,
        'summary': summary,
        'can_update_now': phase == 'ready',
        'has_update': has_update,
        'last_check_time': state.get('last_auto_update_check_time'),
        'next_check_time': next_check_time,
        'next_check_in_seconds': next_check_in_seconds,
        'idle': idle_state,
        'failure_count': failure_state['failure_count'],
        'retry_not_before': failure_state['retry_not_before'],
        'retry_in_seconds': failure_state['retry_in_seconds'],
        'failed_version': failure_state['failed_version'],
    }

def _management_config_status():
    """Return the real management API status used to validate an updated service."""
    if not _management_key_configured():
        return None
    try:
        with http_session.get(
            f'{_build_management_base_url()}/v0/management/config',
            headers=_management_headers(),
            timeout=(2, 4),
            stream=True,
        ) as resp:
            _observe_management_response(resp)
            return int(resp.status_code)
    except Exception:
        return None


def _wait_for_service_healthy(service_name, timeout=None):
    """Require both systemd active and two consecutive management API 200s."""
    timeout = timeout or CONFIG.get('service_health_timeout_seconds', 45)
    deadline = time.monotonic() + max(1, timeout)
    consecutive = 0
    last_status = None
    while time.monotonic() < deadline:
        cache.invalidate('service_status')
        last_status = get_service_status(use_cache=False)
        management_status = _management_config_status() if last_status.get('running') else None
        last_status = dict(last_status)
        last_status['management_config_status'] = management_status
        if last_status.get('running') and management_status == 200:
            consecutive += 1
            if consecutive >= 2:
                return True, last_status
        else:
            consecutive = 0
        time.sleep(1)
    return False, last_status or get_service_status(use_cache=False)


def perform_update(*, lock_acquired=False):
    if not lock_acquired and not update_lock.acquire(blocking=False):
        return False, 'Update already in progress'

    state['update_in_progress'] = True
    result = {'success': False, 'message': '', 'details': []}
    service_stopped = False
    service_name = _systemd_service_name()
    backup_path = None
    staged_target = None
    replaced_binary = False
    updated_release_version = None
    source_target_commit = None

    configured_binary = _resolve_panel_path(CONFIG.get('cliproxy_binary'))
    cliproxy_bin = os.path.realpath(configured_binary) if configured_binary and os.path.lexists(configured_binary) else configured_binary
    cliproxy_dir = _resolve_panel_path(CONFIG.get('cliproxy_dir'))

    try:
        if not get_capabilities()['binary_update']:
            result['message'] = 'Update only supported for a local Linux systemd deployment'
            return False, result
        if not service_name:
            result['message'] = 'Service name is missing or invalid'
            return False, result
        if not cliproxy_bin:
            result['message'] = 'Binary path not set (CLIPROXY_PANEL_CLIPROXY_BINARY)'
            return False, result

        target_parent = os.path.dirname(os.path.abspath(cliproxy_bin))
        os.makedirs(target_parent, exist_ok=True)
        old_binary_existed = os.path.isfile(cliproxy_bin)
        old_mode = stat_module.S_IMODE(os.stat(cliproxy_bin).st_mode) if old_binary_existed else 0o755

        fd, staged_target = tempfile.mkstemp(
            prefix=f'.{os.path.basename(cliproxy_bin)}.',
            suffix='.new',
            dir=target_parent,
        )
        os.close(fd)
        os.remove(staged_target)

        # Prepare and verify the new binary while the current service remains
        # online. The actual downtime is limited to stop/replace/start.
        use_source_update = _is_git_repo(cliproxy_dir) and command_available('git') and command_available('go')
        if use_source_update:
            result['details'].append('Fetching and building the new source revision...')
            success, _, stderr = run_cmd(
                ['git', 'fetch', '--tags', 'origin', 'main'],
                cwd=cliproxy_dir,
                timeout=90,
            )
            if not success:
                result['message'] = f'Fetch failed: {stderr}'
                return False, result

            success, commit_out, stderr = run_cmd(
                ['git', 'rev-parse', 'origin/main'],
                cwd=cliproxy_dir,
                timeout=15,
            )
            if not success or not re.fullmatch(r'[0-9a-fA-F]{40}', commit_out):
                result['message'] = f'Failed to resolve fetched revision: {stderr or commit_out}'
                return False, result
            source_target_commit = commit_out.lower()

            with tempfile.TemporaryDirectory(prefix='cpa-update-build-') as build_root:
                worktree_path = os.path.join(build_root, 'source')
                worktree_added = False
                try:
                    success, _, stderr = run_cmd(
                        ['git', 'worktree', 'add', '--detach', worktree_path, source_target_commit],
                        cwd=cliproxy_dir,
                        timeout=60,
                    )
                    if not success:
                        result['message'] = f'Failed to create isolated build tree: {stderr}'
                        return False, result
                    worktree_added = True
                    success, _, stderr = run_cmd(
                        ['go', 'build', '-trimpath', '-o', staged_target, './cmd/server'],
                        cwd=worktree_path,
                        timeout=600,
                    )
                    if not success:
                        result['message'] = f'Build failed: {stderr}'
                        return False, result
                finally:
                    if worktree_added:
                        run_cmd(
                            ['git', 'worktree', 'remove', '--force', worktree_path],
                            cwd=cliproxy_dir,
                            timeout=30,
                        )
                    run_cmd(['git', 'worktree', 'prune'], cwd=cliproxy_dir, timeout=15)

            _, tag_out, _ = run_cmd(
                ['git', 'describe', '--tags', '--abbrev=0', source_target_commit],
                cwd=cliproxy_dir,
                timeout=15,
            )
            if _release_version_key(tag_out) is not None:
                updated_release_version = tag_out
            result['details'].append('Isolated build completed successfully')
        else:
            result['details'].append('Downloading and verifying the latest release...')
            ok, msg, updated_release_version = update_from_github_release(binary_path=staged_target)
            if not ok:
                result['message'] = msg or 'Release preparation failed'
                return False, result
            result['details'].append(msg or 'Release verified')

        if not os.path.isfile(staged_target) or os.path.getsize(staged_target) < 128 * 1024:
            result['message'] = 'Prepared binary is missing or unexpectedly small'
            return False, result
        # Preserve the already deployed executable's access mode.
        os.chmod(staged_target, old_mode)

        if old_binary_existed:
            cleanup_binary_backups(cliproxy_bin)
            backup_path = create_binary_backup(cliproxy_bin)
            if not backup_path:
                result['message'] = 'Failed to create a rollback point'
                return False, result
            result['details'].append(f'Rollback point created: {backup_path}')

        result['details'].append('Stopping service for atomic replacement...')
        stopped, _, stop_error = run_cmd(['systemctl', 'stop', service_name])
        if not stopped:
            result['message'] = f'Failed to stop service: {stop_error or "unknown error"}'
            return False, result
        service_stopped = True
        cache.invalidate('service_status')

        os.replace(staged_target, cliproxy_bin)
        _fsync_parent_directory(cliproxy_bin)
        staged_target = None
        replaced_binary = True

        def rollback_binary(reason):
            nonlocal service_stopped
            result['details'].append(f'Rollback: {reason}')
            try:
                run_cmd(['systemctl', 'stop', service_name])
                if backup_path and os.path.isfile(backup_path):
                    os.replace(backup_path, cliproxy_bin)
                    _fsync_parent_directory(cliproxy_bin)
                elif replaced_binary and not old_binary_existed and os.path.isfile(cliproxy_bin):
                    os.remove(cliproxy_bin)
                restarted, _, restart_error = run_cmd(['systemctl', 'start', service_name])
                cache.invalidate('service_status')
                healthy, _ = _wait_for_service_healthy(service_name) if restarted else (False, None)
                service_stopped = not healthy
                if healthy:
                    result['details'].append('Rollback successful')
                    return True
                result['details'].append(
                    f'Rollback health check failed: {restart_error or "management endpoint unavailable"}'
                )
            except Exception as exc:
                result['details'].append(f'Rollback failed ({_error_kind(exc)})')
            return False

        result['details'].append('Starting service...')
        success, _, stderr = run_cmd(['systemctl', 'start', service_name])
        cache.invalidate('service_status')
        if not success:
            rolled_back = rollback_binary('start command failed after update')
            result['message'] = f'Start failed: {stderr}' + (' (rolled back)' if rolled_back else '')
            return False, result

        healthy, _ = _wait_for_service_healthy(service_name)
        if not healthy:
            rolled_back = rollback_binary('service or management endpoint did not become healthy after update')
            result['message'] = 'Service failed its post-update health check' + (' (rolled back)' if rolled_back else '')
            return False, result
        service_stopped = False

        # Only advance the source checkout after the new binary has proved it
        # can stay active. A failed deployment can then roll back without
        # leaving git metadata that falsely reports the newer version.
        if source_target_commit:
            merged, merge_out, merge_error = run_cmd(
                ['git', 'merge', '--ff-only', source_target_commit],
                cwd=cliproxy_dir,
                timeout=90,
            )
            if merged:
                if merge_out:
                    result['details'].append(merge_out[-1000:])
            else:
                result['details'].append(
                    'Warning: binary updated but source checkout could not fast-forward: '
                    f'{merge_error or merge_out or "unknown git error"}'
                )

        result['success'] = True
        result['message'] = 'Update successful'
        result['details'].append('Service is active and management endpoint returned HTTP 200')
        state['last_update_time'] = _utc_iso()
        _clear_auto_update_failure_state()

        for cache_key in ('local_version', 'local_version_mgmt', 'current_commit', 'github_release', 'update_check_details'):
            cache.invalidate(cache_key)
        state['current_version'] = get_local_version()
        updated_key = _release_version_key(updated_release_version)
        detected_key = _release_version_key(state.get('current_version'))
        if updated_key is not None and (detected_key is None or detected_key < updated_key):
            state['current_version'] = _decorate_version_tag(updated_release_version)
        record_update_history(state['current_version'])

        deleted_backups = cleanup_binary_backups(cliproxy_bin)
        if deleted_backups:
            result['details'].append(f'Old backups removed: {len(deleted_backups)}')
        return True, result

    except Exception as exc:
        result['message'] = f'Update failed ({_error_kind(exc)})'
        return False, result
    finally:
        if staged_target:
            try:
                os.remove(staged_target)
            except OSError:
                pass
        if service_stopped and service_name and is_linux() and command_available('systemctl'):
            run_cmd(['systemctl', 'start', service_name])
            cache.invalidate('service_status')
        if not result.get('success'):
            _record_auto_update_failure(updated_release_version or state.get('latest_version'))
        state['last_update_result'] = result
        state['update_in_progress'] = False
        update_lock.release()


def _guess_goarch():
    machine = (platform.machine() or '').lower()
    if machine in {'aarch64', 'arm64'}:
        return 'arm64'
    if machine in {'x86_64', 'amd64'}:
        return 'amd64'
    if machine.startswith('armv7') or machine == 'armv7l':
        return 'armv7'
    if machine in {'i386', 'i686', 'x86'}:
        return '386'
    return None


def update_from_github_release(binary_path=''):
    """
    下载、校验并写入 CLIProxyAPI 最新 release 到指定目标。
    调用方可传入暂存路径，再自行原子替换正式二进制。

    返回：(ok, message, release_tag)
    """
    try:
        if not binary_path:
            return False, 'Binary path not set', None

        goarch = _guess_goarch()
        if not goarch:
            return False, f'Unsupported CPU architecture: {platform.machine() or "unknown"}', None

        repo = 'router-for-me/CLIProxyAPI'
        api_error = None
        data = {}
        token = _configured_github_token()

        # 有认证时使用 assets 元数据；匿名场景直接走 releases/latest
        # 跳转和稳定资产命名，避免消耗容易见底的 GitHub API 限额。
        if token:
            try:
                headers = {
                    'User-Agent': 'CLIProxyPanel',
                    'Accept': 'application/vnd.github+json',
                    'Authorization': 'Bearer ' + token,
                }
                with http_session.get(
                    f'https://api.github.com/repos/{repo}/releases/latest',
                    headers=headers,
                    timeout=10,
                    stream=True,
                ) as resp:
                    resp.raise_for_status()
                    data = _response_json_limited(resp, 4 * 1024 * 1024)
            except Exception as e:
                api_error = e
                data = {}
                print(f'Warning: authenticated GitHub release lookup failed ({_error_kind(e)})')

        resolved_tag = _decorate_version_tag((data.get('tag_name') or '') if isinstance(data, dict) else '')
        if _release_version_key(resolved_tag) is None:
            resolved_tag = ''

        assets = data.get('assets', []) if isinstance(data, dict) else []

        asset_url = ''
        checksum_url = ''
        asset_name = ''
        for a in assets:
            name = (a.get('name') or '')
            url = (a.get('browser_download_url') or '')
            if not url:
                continue
            if name.endswith(f'linux_{goarch}.tar.gz'):
                asset_url = url
                asset_name = name
            elif name == 'checksums.txt':
                checksum_url = url

        # 2) 回退：如果 API 拿不到资产列表（被限流/网络问题），用 tag + 固定命名规则拼装下载链接
        if not asset_url:
            if not resolved_tag:
                resolved_tag = _decorate_version_tag(get_github_release_version())
            tag_display = resolved_tag
            tag_number = _normalize_release_version(tag_display)
            if not tag_number or _release_version_key(tag_display) is None:
                if api_error:
                    return False, f'Failed to fetch latest release info ({_error_kind(api_error)})', None
                return False, 'Failed to resolve latest release tag', None

            asset_name = f'CLIProxyAPI_{tag_number}_linux_{goarch}.tar.gz'
            asset_url = f'https://github.com/{repo}/releases/download/{tag_display}/{asset_name}'
            checksum_url = f'https://github.com/{repo}/releases/download/{tag_display}/checksums.txt'
        elif not resolved_tag:
            # 极端兜底：有 asset_url 但拿不到 tag（理论上不应发生）
            resolved_tag = _decorate_version_tag(get_github_release_version())

        with tempfile.TemporaryDirectory() as tmpdir:
            tar_path = os.path.join(tmpdir, 'cliproxyapi.tar.gz')
            # 下载 tarball
            max_download_bytes = 512 * 1024 * 1024
            with http_session.get(asset_url, timeout=(10, 60), stream=True) as r:
                r.raise_for_status()
                content_length = _safe_int(r.headers.get('Content-Length'), 0)
                if content_length and content_length > max_download_bytes:
                    return False, 'Release package is unexpectedly large', resolved_tag or None
                downloaded = 0
                with open(tar_path, 'wb') as f:
                    for chunk in r.iter_content(chunk_size=1024 * 1024):
                        if chunk:
                            downloaded += len(chunk)
                            if downloaded > max_download_bytes:
                                return False, 'Release package exceeded the download size limit', resolved_tag or None
                            f.write(chunk)
                    f.flush()
                    os.fsync(f.fileno())

            # 校验 sha256。默认严格校验，避免安装截断或被篡改的二进制。
            require_checksum = _parse_bool(CONFIG.get('update_require_checksum', True))
            if checksum_url:
                try:
                    with http_session.get(checksum_url, timeout=15, stream=True) as c:
                        if c.status_code != 200:
                            raise RuntimeError(f'checksums status: {c.status_code}')
                        checksum_raw = c.raw.read(2 * 1024 * 1024 + 1, decode_content=True)
                    if len(checksum_raw) > 2 * 1024 * 1024:
                        raise RuntimeError('checksums file is unexpectedly large')
                    checksum_text = checksum_raw.decode('utf-8', errors='strict')
                    expected = None
                    for line in checksum_text.splitlines():
                        parts = line.strip().split()
                        if len(parts) >= 2 and os.path.basename(parts[-1].lstrip('*')) == asset_name:
                            expected = parts[0]
                            break
                    if not expected or not re.fullmatch(r'[0-9a-fA-F]{64}', expected):
                        raise RuntimeError(f'checksum entry missing for {asset_name}')
                    digest = hashlib.sha256()
                    with open(tar_path, 'rb') as f:
                        for chunk in iter(lambda: f.read(1024 * 1024), b''):
                            digest.update(chunk)
                    actual = digest.hexdigest()
                    if not hmac.compare_digest(actual.lower(), expected.lower()):
                        return False, 'Checksum mismatch (download may be corrupted)', resolved_tag or None
                except Exception as e:
                    if require_checksum:
                        return False, f'Checksum verification failed ({_error_kind(e)})', resolved_tag or None
                    print(f'Warning: checksum verification skipped ({_error_kind(e)})')
            elif require_checksum:
                return False, 'Release checksum is unavailable', resolved_tag or None

            # 解压并找到二进制
            try:
                def _safe_extract(tar, target_dir):
                    target_dir_abs = os.path.abspath(target_dir)
                    total_size = 0
                    for member in tar.getmembers():
                        # 防御更严格：拒绝符号链接/硬链接，避免“先解出链接再写文件”绕过路径校验
                        if getattr(member, 'issym', lambda: False)() or getattr(member, 'islnk', lambda: False)():
                            raise RuntimeError(f'Unsafe link in tar: {member.name}')
                        if not (member.isfile() or member.isdir()):
                            raise RuntimeError(f'Unsupported archive entry: {member.name}')
                        total_size += max(0, int(member.size or 0))
                        if total_size > 1024 * 1024 * 1024:
                            raise RuntimeError('Archive expands beyond the safety limit')
                        member_path = os.path.abspath(os.path.join(target_dir_abs, member.name))
                        if not member_path.startswith(target_dir_abs + os.sep) and member_path != target_dir_abs:
                            raise RuntimeError(f'Unsafe path in tar: {member.name}')
                    for member in tar.getmembers():
                        tar.extract(member, target_dir_abs)

                with tarfile.open(tar_path, 'r:gz') as tf:
                    _safe_extract(tf, tmpdir)
            except Exception as e:
                return False, f'Extract failed ({_error_kind(e)})', resolved_tag or None

            def _looks_like_elf(path: str) -> bool:
                try:
                    if not os.path.isfile(path):
                        return False
                    with open(path, 'rb') as f:
                        head = f.read(4)
                    return head == b'\x7fELF'
                except Exception:
                    return False

            def _find_extracted_binary(extract_root: str) -> str:
                preferred_names = [
                    'cli-proxy-api',
                    'cliproxyapi',
                    'cliproxy',
                    'CLIProxyAPI',
                    'cli_proxy_api',
                ]

                # 1) 精确名字优先
                by_name = []
                for root, _, files in os.walk(extract_root):
                    for name in files:
                        if name in preferred_names:
                            by_name.append((preferred_names.index(name), os.path.join(root, name)))
                by_name.sort(key=lambda x: x[0])
                for _, p in by_name:
                    try:
                        if _looks_like_elf(p) or os.path.getsize(p) > 128 * 1024:
                            return p
                    except Exception:
                        continue

                # 2) 兜底：找 ELF 可执行文件
                elf_paths = []
                for root, _, files in os.walk(extract_root):
                    for name in files:
                        p = os.path.join(root, name)
                        if _looks_like_elf(p):
                            elf_paths.append(p)

                if len(elf_paths) == 1:
                    return elf_paths[0]

                if elf_paths:
                    def score(p: str) -> tuple:
                        base = os.path.basename(p).lower()
                        s = 0
                        if 'cliproxy' in base:
                            s += 3
                        if 'proxy' in base:
                            s += 2
                        if 'api' in base:
                            s += 2
                        try:
                            s_size = os.path.getsize(p)
                        except Exception:
                            s_size = 0
                        return (s, s_size)

                    elf_paths.sort(key=score, reverse=True)
                    return elf_paths[0]

                return ''

            extracted_bin = _find_extracted_binary(tmpdir)
            if not extracted_bin:
                return False, 'No binary found in release package', resolved_tag or None

            # 原子替换
            target_parent = os.path.dirname(os.path.abspath(binary_path))
            os.makedirs(target_parent, exist_ok=True)
            fd, tmp_target = tempfile.mkstemp(prefix=f'.{os.path.basename(binary_path)}.', suffix='.new', dir=target_parent)
            os.close(fd)
            try:
                shutil.copyfile(extracted_bin, tmp_target)
                with open(tmp_target, 'r+b') as handle:
                    handle.flush()
                    os.fsync(handle.fileno())
                # Release payload is the executable launched by systemd.
                os.chmod(tmp_target, 0o755)  # nosec B103
                os.replace(tmp_target, binary_path)
            finally:
                try:
                    os.remove(tmp_target)
                except OSError:
                    pass

        tag_for_message = resolved_tag or 'unknown'
        return True, f'Release {tag_for_message} verified for linux_{goarch}', resolved_tag or None
    except Exception as e:
        return False, f'Release update failed ({_error_kind(e)})', None

def auto_update_worker():
    first_check = True
    while True:
        interval = max(60, int(CONFIG.get('auto_update_check_interval', 300) or 300))
        wait_seconds = min(5, interval) if first_check else interval
        first_check = False
        state['next_auto_update_check_monotonic'] = time.monotonic() + wait_seconds
        state['next_auto_update_check_time'] = _utc_iso(_utc_now() + timedelta(seconds=wait_seconds))
        auto_update_wakeup.wait(wait_seconds)
        auto_update_wakeup.clear()
        state['next_auto_update_check_monotonic'] = None
        state['last_auto_update_check_time'] = _utc_iso()

        # Version discovery must remain active even when upgrades are disabled.
        if shutdown_event.is_set():
            return
        try:
            with version_check_lock:
                check_for_updates(use_cache=False)
                state['version_checked_at'] = _utc_iso()
                state['version_error'] = None if _is_semver_like(state['latest_version']) else '暂时无法访问 GitHub 更新源'
        except Exception as exc:
            state['version_error'] = f'版本检查失败 ({_error_kind(exc)})'
        if not state['auto_update_enabled'] or not get_capabilities()['binary_update']:
            continue

        if state['update_in_progress']:
            print(f'[{_utc_iso()}] Auto-update skipped: update already in progress')
            continue

        try:
            has_update = bool(state.get('has_update'))
            if not has_update:
                print(f'[{_utc_iso()}] Auto-update check: no new release')
                continue

            failure_state = _auto_update_failure_snapshot()
            if failure_state['retry_in_seconds'] > 0:
                print(
                    f'[{_utc_iso()}] Auto-update skipped: retry backoff for '
                    f'{failure_state.get("failed_version") or "current release"}, '
                    f'{failure_state["retry_in_seconds"]}s remaining'
                )
                continue

            idle_state = get_idle_state()
            if idle_state.get('is_idle'):
                print(f'[{_utc_iso()}] Update detected and system idle, starting auto-update...')
                perform_update()
            else:
                print(
                    f'[{_utc_iso()}] Auto-update skipped: busy, '
                    f'last request at {idle_state.get("last_request_time")}, '
                    f'threshold={CONFIG["idle_threshold_seconds"]}s'
                )
        except Exception as e:
            print(f'[{_utc_iso()}] Auto-update check failed ({_error_kind(e)})')

def parse_log_file(log_file, max_lines=100, limit=None):
    """解析日志文件（优化：Python原生读取，提取实际时间戳）"""
    log_file = _resolve_panel_path(log_file)
    if not os.path.exists(log_file):
        return []
    if limit is None:
        limit = max_lines if max_lines and max_lines > 0 else 50

    try:
        lines = read_log_tail(log_file, max_lines=max_lines)
        timestamp_values = [match.group(1) for line in lines if (match := LOG_TIME_PATTERN.search(line))]
        try:
            file_mtime = os.path.getmtime(log_file)
        except OSError:
            file_mtime = None
        inferred_offset = _infer_timezone_offset_seconds(timestamp_values[-1], file_mtime) if timestamp_values else None
        if inferred_offset is None:
            with log_stats_lock:
                inferred_offset = state.get('log_stats', {}).get('timezone_offset_seconds')
        fallback_time = _utc_iso(datetime.fromtimestamp(file_mtime, tz=UTC)) if file_mtime is not None else _utc_iso()

        logs = []
        for line in lines:
            line = line.strip()
            if line:
                # 尝试从日志中提取时间
                time_match = LOG_TIME_PATTERN.search(line)
                if time_match:
                    log_time_str = time_match.group(1)
                    time_iso = _log_time_iso(log_time_str, inferred_offset) or fallback_time
                else:
                    time_iso = fallback_time

                logs.append({
                    'time': time_iso,
                    'message': line[:500],
                    'source': 'file'
                })

        return logs[-limit:]
    except (OSError, ValueError, TypeError):
        return []


def parse_journal_logs(service_name, max_lines=100):
    """读取 systemd journal，补齐 CLIProxyAPI 后台日志"""
    service_name = _systemd_service_name(service_name)
    if not service_name or not get_capabilities()['service_control'] or not command_available('journalctl'):
        return []

    ok, stdout, _ = run_cmd(
        ['journalctl', '-u', str(service_name), '-n', str(int(max_lines)), '--no-pager', '-o', 'json'],
        timeout=3,
    )
    if not ok or not stdout:
        return []

    logs = []
    for raw_line in stdout.splitlines():
        raw_line = raw_line.strip()
        if not raw_line:
            continue
        try:
            item = json.loads(raw_line)
        except Exception:
            continue

        message = str(item.get('MESSAGE') or '').strip()
        if not message:
            continue

        time_iso = _utc_iso()
        ts_raw = item.get('_SOURCE_REALTIME_TIMESTAMP') or item.get('__REALTIME_TIMESTAMP')
        if ts_raw:
            try:
                ts_value = int(str(ts_raw)) / 1_000_000
                time_iso = _utc_iso(datetime.fromtimestamp(ts_value, tz=UTC))
            except (TypeError, ValueError, OSError, OverflowError):
                pass

        logs.append({
            'time': time_iso,
            'message': message[:500],
            'source': 'journal'
        })

    return logs[-max_lines:]


def merge_log_entries(*groups, limit=200):
    """合并多个日志来源并按时间排序去重"""
    merged = []
    seen = set()

    for group in groups:
        for entry in group or []:
            if not isinstance(entry, dict):
                continue
            message = str(entry.get('message') or '').strip()
            if not message:
                continue
            time_value = str(entry.get('time') or '').strip()
            source = str(entry.get('source') or '').strip()
            dedupe_key = (time_value, message)
            if dedupe_key in seen:
                continue
            seen.add(dedupe_key)
            merged.append({
                'time': time_value,
                'message': message,
                'source': source
            })

    merged.sort(key=lambda item: item.get('time') or '')
    if limit and limit > 0:
        return merged[-limit:]
    return merged

def parse_request_logs(max_lines=200, use_cache=True):
    """解析 CLIProxy 请求日志（优化：预编译正则+缓存+原生读取）"""
    cache_key = 'request_logs'
    empty_stats = {'total': 0, 'success': 0, 'failed': 0}

    if use_cache:
        cached = cache.get(cache_key, max_age=2)
        if cached:
            return cached

    log_file = _resolve_panel_path(CONFIG.get('cliproxy_log'))

    if not os.path.exists(log_file):
        return [], empty_stats

    try:
        lines = read_log_tail(log_file, max_lines=max_lines)
        timestamp_values = [match.group(1) for line in lines if (match := LOG_TIME_PATTERN.search(line))]
        try:
            file_mtime = os.path.getmtime(log_file)
        except OSError:
            file_mtime = None
        inferred_offset = _infer_timezone_offset_seconds(timestamp_values[-1], file_mtime) if timestamp_values else None
        if inferred_offset is None:
            with log_stats_lock:
                inferred_offset = state.get('log_stats', {}).get('timezone_offset_seconds')

        logs = []
        # 使用预编译的正则表达式
        for line in lines:
            match = REQUEST_LOG_PATTERN.search(line)
            if match:
                timestamp, status, duration, client_ip, method, path = match.groups()
                client_ip = client_ip.strip()
                logs.append({
                    'time': _log_time_iso(timestamp, inferred_offset),
                    'status': int(status),
                    'duration': duration,
                    'client': client_ip,
                    'method': method,
                    'path': path,
                    'message': f'{method} {path} - {status} ({duration})'
                })

        # 统计
        total = len(logs)
        success = sum(1 for entry in logs if entry['status'] < 400)
        failed = total - success

        result = (logs[-50:], {'total': total, 'success': success, 'failed': failed})
        cache.set(cache_key, result)
        return result
    except Exception as e:
        print(f'parse_request_logs error ({_error_kind(e)})')
        return [], empty_stats

def get_paths_info():
    return {
        'config': _resolve_panel_path(CONFIG.get('cliproxy_config')),
        'auth_dir': _resolve_panel_path(CONFIG.get('auth_dir')),
        'binary': _resolve_panel_path(CONFIG.get('cliproxy_binary')),
        'logs': os.path.dirname(_resolve_panel_path(CONFIG.get('cliproxy_log'))),
        'project_dir': _resolve_panel_path(CONFIG.get('cliproxy_dir')),
    }

def load_cliproxy_config(use_cache=True):
    """加载CLIProxy配置文件（优化：带缓存）"""
    cache_key = 'cliproxy_config'
    if use_cache:
        cached = cache.get(cache_key, max_age=30)
        if cached:
            return cached

    config_path = _resolve_panel_path(CONFIG.get('cliproxy_config'))
    if not os.path.exists(config_path):
        return None, 'Config file not found'
    try:
        if os.path.getsize(config_path) > 2 * 1024 * 1024:
            return None, 'Config file exceeds the 2 MiB limit'
    except OSError:
        return None, 'Config file metadata is unavailable'

    if not HAS_YAML:
        # 没有yaml模块时返回原始内容
        try:
            with open(config_path, 'r', encoding='utf-8') as f:
                result = ({'_raw': f.read()}, None)
                cache.set(cache_key, result)
                return result
        except Exception:
            return None, 'Config file read failed'

    try:
        with open(config_path, 'r', encoding='utf-8') as f:
            config = yaml.safe_load(f)
            if not isinstance(config, dict):
                return None, 'Config root must be a mapping/object'
            result = (config, None)
            cache.set(cache_key, result)
            return result
    except Exception:
        return None, 'Config file read failed'

def validate_yaml_config(content):
    """验证YAML配置格式"""
    if not HAS_YAML:
        return {
            'valid': True,
            'errors': [],
            'warnings': ['pyyaml未安装，无法进行深度验证'],
            'config': None
        }

    try:
        config = yaml.safe_load(content)
        errors = []
        warnings = []

        # 基本结构检查
        if not isinstance(config, dict):
            errors.append('配置必须是一个字典/对象')
            return {'valid': False, 'errors': errors, 'warnings': warnings}

        # 检查必需字段
        required_fields = ['port']
        for field in required_fields:
            if field not in config:
                errors.append(f'缺少必需字段: {field}')

        # 检查端口
        if 'port' in config:
            port = config['port']
            if not isinstance(port, int) or port < 1 or port > 65535:
                errors.append('端口必须是1-65535之间的整数')

        # 检查providers
        if 'providers' in config:
            if not isinstance(config['providers'], list):
                errors.append('providers必须是一个数组')
            else:
                for i, provider in enumerate(config['providers']):
                    if not isinstance(provider, dict):
                        errors.append(f'provider[{i}] 必须是一个对象')
                        continue
                    if 'name' not in provider:
                        warnings.append(f'provider[{i}] 缺少name字段')
                    if 'type' not in provider:
                        warnings.append(f'provider[{i}] 缺少type字段')

        # 检查路由策略
        if 'routing' in config:
            valid_strategies = ['round-robin', 'fill-first']
            routing = config['routing']
            if not isinstance(routing, dict):
                errors.append('routing 必须是一个对象')
            else:
                strategy = routing.get('strategy', '')
                if strategy and strategy not in valid_strategies:
                    warnings.append(f'未知的路由策略: {strategy}，有效值: {", ".join(valid_strategies)}')

        auth_dir = config.get('auth-dir')
        if auth_dir is not None and (not isinstance(auth_dir, str) or not auth_dir.strip()):
            errors.append('auth-dir 必须是非空字符串')

        return {
            'valid': len(errors) == 0,
            'errors': errors,
            'warnings': warnings,
            'config': config if len(errors) == 0 else None
        }
    except yaml.YAMLError:
        return {
            'valid': False,
            'errors': ['YAML解析错误'],
            'warnings': []
        }

def get_system_resources(use_cache=True):
    """获取系统资源（优化：非阻塞CPU+缓存）"""
    cache_key = 'system_resources'
    if use_cache:
        cached = cache.get(cache_key, max_age=2)
        if cached:
            return cached

    disk_path = _resolve_panel_path(CONFIG.get('disk_path') or '/')
    system_info = get_system_info()
    cliproxy_usage = get_cliproxy_process_usage()

    if not HAS_PSUTIL:
        # 没有psutil时使用命令行获取基本信息
        resources = {
            'cpu': {'percent': 0, 'cores': 1},
            'memory': {'total': 0, 'used': 0, 'percent': 0, 'available': 0},
            'disk': {'total': 0, 'used': 0, 'percent': 0, 'free': 0, 'path': disk_path},
            'network': {'bytes_sent': 0, 'bytes_recv': 0},
            'system': system_info,
            'cliproxy': cliproxy_usage,
            'timestamp': _utc_iso(),
            'limited': True
        }

        # 尝试获取内存信息（Linux）
        if is_linux() and command_available('free'):
            _, free_out, _ = run_cmd(['free', '-b'])
            mem_out = next((line for line in free_out.splitlines() if line.lstrip().startswith('Mem:')), '')
            if mem_out:
                parts = mem_out.split()
                if len(parts) >= 4:
                    try:
                        total = int(parts[1])
                        used = int(parts[2])
                        resources['memory']['total'] = total
                        resources['memory']['used'] = used
                        resources['memory']['available'] = total - used
                        resources['memory']['percent'] = round(used / total * 100, 1) if total > 0 else 0
                    except (TypeError, ValueError):
                        pass

        # 尝试获取磁盘信息（Linux）
        try:
            usage = shutil.disk_usage(disk_path)
            total = usage.total
            used = usage.used
            resources['disk']['total'] = total
            resources['disk']['used'] = used
            resources['disk']['free'] = usage.free
            resources['disk']['percent'] = round(used / total * 100, 1) if total > 0 else 0
        except Exception:
            if is_linux() and command_available('df'):
                _, df_out, _ = run_cmd(['df', disk_path])
                disk_out = next((line for line in reversed(df_out.splitlines()) if line.strip()), '')
                if disk_out:
                    parts = disk_out.split()
                    if len(parts) >= 5:
                        try:
                            total = int(parts[1]) * 1024
                            used = int(parts[2]) * 1024
                            resources['disk']['total'] = total
                            resources['disk']['used'] = used
                            resources['disk']['free'] = total - used
                            resources['disk']['percent'] = round(used / total * 100, 1) if total > 0 else 0
                        except Exception:
                            pass

        cache.set(cache_key, resources)
        return resources

    try:
        # 使用后台监控的CPU数据，避免阻塞
        cpu_percent = resource_monitor.get_cpu_percent()
        memory = psutil.virtual_memory()
        disk = psutil.disk_usage(disk_path)

        # 网络IO
        net_io = psutil.net_io_counters()

        # 获取更详细的CPU信息
        cpu_freq = psutil.cpu_freq()
        cpu_times = psutil.cpu_times_percent(interval=0)
        per_cpu = psutil.cpu_percent(percpu=True)

        # 获取更详细的内存信息
        swap = psutil.swap_memory()

        # 获取系统负载（Linux）
        try:
            load_avg = psutil.getloadavg()
        except (AttributeError, OSError):
            load_avg = (0, 0, 0)

        # 获取进程数
        try:
            process_count = len(psutil.pids())
        except (psutil.Error, OSError):
            process_count = 0

        result = {
            'cpu': {
                'percent': cpu_percent,
                'cores': psutil.cpu_count(),
                'cores_logical': psutil.cpu_count(logical=True),
                'cores_physical': psutil.cpu_count(logical=False) or psutil.cpu_count(),
                'freq_current': cpu_freq.current if cpu_freq else 0,
                'freq_max': cpu_freq.max if cpu_freq and cpu_freq.max else 0,
                'per_cpu': per_cpu,
                'user': cpu_times.user if cpu_times else 0,
                'system': cpu_times.system if cpu_times else 0,
                'idle': cpu_times.idle if cpu_times else 0,
                'iowait': getattr(cpu_times, 'iowait', 0),
                'load_1m': round(load_avg[0], 2),
                'load_5m': round(load_avg[1], 2),
                'load_15m': round(load_avg[2], 2),
                'process_count': process_count,
            },
            'memory': {
                'total': memory.total,
                'used': memory.used,
                'percent': memory.percent,
                'available': memory.available,
                'free': memory.free,
                'cached': getattr(memory, 'cached', 0),
                'buffers': getattr(memory, 'buffers', 0),
                'shared': getattr(memory, 'shared', 0),
                'swap_total': swap.total,
                'swap_used': swap.used,
                'swap_percent': swap.percent,
                'swap_free': swap.free,
            },
            'disk': {
                'total': disk.total,
                'used': disk.used,
                'percent': round(disk.used / disk.total * 100, 1) if disk.total > 0 else 0,
                'free': disk.free,
                'path': disk_path,
            },
            'network': {
                'bytes_sent': net_io.bytes_sent,
                'bytes_recv': net_io.bytes_recv,
            },
            'system': system_info,
            'cliproxy': cliproxy_usage,
            'timestamp': _utc_iso()
        }
        cache.set(cache_key, result)
        return result
    except Exception as e:
        return {'error': f'系统资源读取失败 ({_error_kind(e)})'}

def perform_health_check(use_cache=True):
    """执行健康检查（优化：带缓存）"""
    cache_key = 'health_check'
    if use_cache:
        cached = cache.get(cache_key, max_age=10)
        if cached:
            return cached

    results = {
        'timestamp': _utc_iso(),
        'checks': [],
        'checks_map': {},
        'overall': 'healthy'
    }

    # 1. 服务状态检查
    service = get_service_status()
    service_check = {
        'name': '服务状态',
        'status': 'pass' if service['running'] else ('warn' if service.get('status') in {'unknown', 'checking'} else 'fail'),
        'message': ('服务运行中' if service['running'] else '服务未运行') if service.get('source') != 'management_api' else service.get('details', '管理接口状态未知'),
        'details': service
    }
    results['checks'].append(service_check)
    results['checks_map']['service'] = service_check

    # 2. 配置文件检查
    config, error = load_cliproxy_config()
    remote_mode = not get_capabilities()['service_control']
    config_check = {
        'name': '配置文件（可选挂载）' if remote_mode else '配置文件',
        'status': 'pass' if config is not None else ('warn' if remote_mode else 'fail'),
        'message': '配置文件有效' if config is not None else f'配置错误: {error}'
    }
    results['checks'].append(config_check)
    results['checks_map']['config'] = config_check

    if isinstance(config, dict) and config.get('usage-statistics-enabled') is False:
        usage_check = {
            'name': '用量统计',
            'status': 'warn',
            'message': '上游用量统计已关闭；本面板仅展示已保存的 Token 历史，实时请求来自日志。',
        }
        results['checks'].append(usage_check)
        results['checks_map']['usage_statistics'] = usage_check

    log_path = _resolve_panel_path(CONFIG.get('cliproxy_log'))
    log_ok = bool(log_path and os.path.isfile(log_path) and os.access(log_path, os.R_OK))
    log_check = {
        'name': '请求日志',
        'status': 'pass' if log_ok else 'warn',
        'message': f'日志可读: {log_path}' if log_ok else f'日志不可用，自动更新将保持等待: {log_path or "未配置"}',
    }
    results['checks'].append(log_check)
    results['checks_map']['log'] = log_check

    # 3. 磁盘空间检查
    disk_path = _resolve_panel_path(CONFIG.get('disk_path') or '/')
    if HAS_PSUTIL:
        try:
            disk = psutil.disk_usage(disk_path)
            disk_ok = disk.percent < 90
            disk_check = {
                'name': '磁盘空间',
                'status': 'pass' if disk_ok else 'warn',
                'message': f'已使用 {disk.percent}%',
                'details': {'percent': disk.percent}
            }
            results['checks'].append(disk_check)
            results['checks_map']['disk'] = disk_check
        except (OSError, ValueError):
            disk_check = {
                'name': '磁盘空间',
                'status': 'unknown',
                'message': '无法获取磁盘信息'
            }
            results['checks'].append(disk_check)
            results['checks_map']['disk'] = disk_check
    else:
        # 使用df命令获取磁盘信息（Linux）
        if is_linux() and command_available('df'):
            _, df_out, _ = run_cmd(['df', disk_path])
            disk_out = next((line for line in reversed(df_out.splitlines()) if line.strip()), '')
            if disk_out:
                parts = disk_out.split()
                if len(parts) >= 5:
                    try:
                        percent = int(parts[4].replace('%', ''))
                        disk_ok = percent < 90
                        disk_check = {
                            'name': '磁盘空间',
                            'status': 'pass' if disk_ok else 'warn',
                            'message': f'已使用 {percent}%',
                            'details': {'percent': percent}
                        }
                        results['checks'].append(disk_check)
                        results['checks_map']['disk'] = disk_check
                    except (TypeError, ValueError):
                        disk_check = {
                            'name': '磁盘空间',
                            'status': 'unknown',
                            'message': '无法获取磁盘信息'
                        }
                        results['checks'].append(disk_check)
                        results['checks_map']['disk'] = disk_check
                else:
                    disk_check = {
                        'name': '磁盘空间',
                        'status': 'unknown',
                        'message': '无法获取磁盘信息'
                    }
                    results['checks'].append(disk_check)
                    results['checks_map']['disk'] = disk_check
            else:
                disk_check = {
                    'name': '磁盘空间',
                    'status': 'unknown',
                    'message': '无法获取磁盘信息'
                }
                results['checks'].append(disk_check)
                results['checks_map']['disk'] = disk_check
        else:
            disk_check = {
                'name': '磁盘空间',
                'status': 'unknown',
                'message': '无法获取磁盘信息'
            }
            results['checks'].append(disk_check)
            results['checks_map']['disk'] = disk_check

    # 4. 内存检查
    if HAS_PSUTIL:
        try:
            memory = psutil.virtual_memory()
            mem_ok = memory.percent < 90
            memory_check = {
                'name': '内存使用',
                'status': 'pass' if mem_ok else 'warn',
                'message': f'已使用 {memory.percent}%',
                'details': {'percent': memory.percent}
            }
            results['checks'].append(memory_check)
            results['checks_map']['memory'] = memory_check
        except Exception:
            memory_check = {
                'name': '内存使用',
                'status': 'unknown',
                'message': '无法获取内存信息'
            }
            results['checks'].append(memory_check)
            results['checks_map']['memory'] = memory_check
    else:
        # 使用free命令获取内存信息（Linux）
        if is_linux() and command_available('free'):
            _, free_out, _ = run_cmd(['free'])
            mem_out = next((line for line in free_out.splitlines() if line.lstrip().startswith('Mem:')), '')
            if mem_out:
                parts = mem_out.split()
                if len(parts) >= 3:
                    try:
                        total = int(parts[1])
                        used = int(parts[2])
                        percent = round(used / total * 100, 1) if total > 0 else 0
                        mem_ok = percent < 90
                        memory_check = {
                            'name': '内存使用',
                            'status': 'pass' if mem_ok else 'warn',
                            'message': f'已使用 {percent}%',
                            'details': {'percent': percent}
                        }
                        results['checks'].append(memory_check)
                        results['checks_map']['memory'] = memory_check
                    except (TypeError, ValueError):
                        memory_check = {
                            'name': '内存使用',
                            'status': 'unknown',
                            'message': '无法获取内存信息'
                        }
                        results['checks'].append(memory_check)
                        results['checks_map']['memory'] = memory_check
                else:
                    memory_check = {
                        'name': '内存使用',
                        'status': 'unknown',
                        'message': '无法获取内存信息'
                    }
                    results['checks'].append(memory_check)
                    results['checks_map']['memory'] = memory_check
            else:
                memory_check = {
                    'name': '内存使用',
                    'status': 'unknown',
                    'message': '无法获取内存信息'
                }
                results['checks'].append(memory_check)
                results['checks_map']['memory'] = memory_check
        else:
            memory_check = {
                'name': '内存使用',
                'status': 'unknown',
                'message': '无法获取内存信息'
            }
            results['checks'].append(memory_check)
            results['checks_map']['memory'] = memory_check

    # 5. 认证文件检查
    auth_dir = _resolve_panel_path(CONFIG.get('auth_dir'))
    if os.path.exists(auth_dir):
        try:
            auth_count = sum(1 for entry in os.scandir(auth_dir) if entry.is_file(follow_symlinks=False))
        except OSError:
            auth_count = 0
        auth_check = {
            'name': '认证文件',
            'status': 'pass' if auth_count > 0 else 'warn',
            'message': f'找到 {auth_count} 个凭证文件',
            'details': {'count': auth_count}
        }
        results['checks'].append(auth_check)
        results['checks_map']['auth'] = auth_check
    else:
        auth_check = {
            'name': '认证文件',
            'status': 'warn' if remote_mode else 'fail',
            'message': '未挂载认证目录（可选能力）' if remote_mode else '认证目录不存在'
        }
        results['checks'].append(auth_check)
        results['checks_map']['auth'] = auth_check

    # 6. API端口检查
    try:
        api_host, api_port = _api_host_port()
        with socket.create_connection((api_host, api_port), timeout=2):
            port_open = True
        port_check = {
            'name': 'API端口',
            'status': 'pass' if port_open else 'fail',
            'message': f'{api_host}:{api_port} 可连接'
        }
        results['checks'].append(port_check)
        results['checks_map']['api_port'] = port_check
    except (OSError, ValueError):
        port_check = {
            'name': 'API端口',
            'status': 'unknown',
            'message': '无法检测端口状态'
        }
        results['checks'].append(port_check)
        results['checks_map']['api_port'] = port_check

    # 7. CPA 管理密钥状态检查
    management_auth = _management_auth_snapshot()
    if management_auth.get('locked'):
        management_status = 'fail'
    elif management_auth.get('consecutive_failures') or not management_auth.get('configured'):
        management_status = 'warn'
    else:
        management_status = 'pass'
    management_check = {
        'name': '管理密钥',
        'status': management_status,
        'message': management_auth.get('message'),
        'details': {
            'configured': management_auth.get('configured'),
            'locked': management_auth.get('locked'),
            'consecutive_failures': management_auth.get('consecutive_failures'),
            'max_failures': management_auth.get('max_failures'),
        }
    }
    results['checks'].append(management_check)
    results['checks_map']['management_key'] = management_check

    exposed_without_key = (
        not _bind_host_is_loopback()
        and len(_panel_access_key_expected()) < PANEL_ACCESS_KEY_MIN_LENGTH
    )
    security_check = {
        'name': '面板访问保护',
        'status': 'warn' if exposed_without_key else 'pass',
        'message': '面板对外监听但未设置访问密钥' if exposed_without_key else '面板访问范围与密钥配置正常',
    }
    results['checks'].append(security_check)
    results['checks_map']['panel_security'] = security_check

    # 计算整体状态
    statuses = [c['status'] for c in results['checks']]
    if 'fail' in statuses:
        results['overall'] = 'unhealthy'
    elif 'warn' in statuses or 'unknown' in statuses:
        results['overall'] = 'degraded'
    else:
        results['overall'] = 'healthy'

    state['last_health_check'] = results
    state['health_status'] = results['overall']

    cache.set(cache_key, results)
    return results

def get_models_from_config():
    """从配置中获取模型列表"""
    config, error = load_cliproxy_config()
    if config is None:
        return [], error

    # 如果没有yaml，无法解析模型
    if '_raw' in config:
        return [], 'pyyaml未安装，无法解析模型列表'

    models = []
    providers = config.get('providers', [])
    if not isinstance(providers, list):
        return [], 'providers 必须是列表'

    for provider in providers:
        if not isinstance(provider, dict):
            continue
        provider_name = provider.get('name', 'unknown')
        provider_models = provider.get('models', [])
        if not isinstance(provider_models, list):
            continue

        for model in provider_models:
            if isinstance(model, str):
                models.append({
                    'id': model,
                    'provider': provider_name,
                    'name': model
                })
            elif isinstance(model, dict):
                models.append({
                    'id': model.get('id', model.get('name', 'unknown')),
                    'provider': provider_name,
                    'name': model.get('name', model.get('id', 'unknown')),
                    'aliases': model.get('aliases', [])
                })

    return models, None


def _increment_model_usage_locked(model, amount=1):
    """Increment a model counter without allowing unbounded key growth."""
    counters = state['stats'].setdefault('model_usage', {})
    key = str(model or 'unknown')[:200]
    if key not in counters and len(counters) >= MAX_MODEL_USAGE_ENTRIES:
        key = '__other__'
    counters[key] = max(0, _safe_int(counters.get(key, 0))) + max(0, _safe_int(amount, 0))


def update_accumulated_usage(token_totals, usage_reqs, *, live):
    """Apply one live upstream snapshot exactly once under a single lock."""
    current = {
        'input_tokens': max(0, _safe_int(token_totals.get('input_tokens', 0))),
        'output_tokens': max(0, _safe_int(token_totals.get('output_tokens', 0))),
        'reasoning_tokens': max(0, _safe_int(token_totals.get('reasoning_tokens', 0))),
        'cached_tokens': max(0, _safe_int(token_totals.get('cached_tokens', 0))),
        'total_requests': max(0, _safe_int(usage_reqs.get('total_requests', 0))),
        'success': max(0, _safe_int(usage_reqs.get('success', 0))),
        'failure': max(0, _safe_int(usage_reqs.get('failure', 0))),
    }
    with stats_lock:
        accumulated = state.setdefault('accumulated_stats', {}).copy()
        previous = state.setdefault('last_snapshot', {}).copy()
        for key in current:
            accumulated.setdefault(key, 0)
            previous.setdefault(key, 0)

        if live:
            reset_pending = bool(state.get('usage_reset_pending', False))
            if state.get('usage_counter_mode') == 'queue':
                # The first cumulative payload after a v7 queue period is a
                # baseline. Adding it would recount queue records already saved.
                reset_pending = True
            if not reset_pending:
                for key, value in current.items():
                    old_value = max(0, _safe_int(previous.get(key, 0)))
                    # A lower upstream counter means CLIProxy restarted/reset.
                    delta = value - old_value if value >= old_value else value
                    accumulated[key] = max(0, _safe_int(accumulated.get(key, 0))) + max(0, delta)
            state['last_snapshot'] = current
            state['accumulated_stats'] = accumulated
            state['usage_reset_pending'] = False
            state['usage_counter_mode'] = 'cumulative'

        state['stats']['input_tokens'] = accumulated['input_tokens']
        state['stats']['output_tokens'] = accumulated['output_tokens']
        state['stats']['reasoning_tokens'] = accumulated['reasoning_tokens']
        state['stats']['cached_tokens'] = accumulated['cached_tokens']
        recorded = state.setdefault('recorded_stats', {})
        combined = dict(accumulated)
        combined['total_requests'] += max(0, _safe_int(recorded.get('total_requests', 0)))
        combined['success'] += max(0, _safe_int(recorded.get('success', 0)))
        combined['failure'] += max(0, _safe_int(recorded.get('failure', 0)))
        state['stats']['total_requests'] = combined['total_requests']
        state['stats']['successful_requests'] = combined['success']
        state['stats']['failed_requests'] = combined['failure']
        state['request_count'] = combined['total_requests']
        return combined

# ==================== API 路由 ====================

@app.route('/')
def index():
    return send_from_directory(app.static_folder, 'index.html')


@app.route('/api/healthz')
def api_healthz():
    """Minimal unauthenticated liveness endpoint for Docker/systemd probes."""
    return jsonify({'ok': True, 'panel': PANEL_NAME, 'version': PANEL_VERSION, 'time': _utc_iso()})


@app.errorhandler(413)
def request_too_large(_error):
    if request.path.startswith('/api/'):
        return jsonify({'success': False, 'error': 'Request body exceeds the 3 MiB limit'}), 413
    return 'Request body too large', 413

def build_status_payload():
    service = get_service_status()
    has_update = check_for_updates(allow_network=False)
    log_requests = get_request_count_from_logs()
    # The background collector performs network I/O. Dashboard refreshes only
    # consume the latest memory/disk snapshot, keeping the UI responsive during
    # upstream outages.
    snapshot, snapshot_meta = fetch_usage_snapshot(with_meta=True, allow_network=False)
    token_totals, usage_reqs = aggregate_usage_snapshot(snapshot)
    pricing, pricing_meta = get_effective_pricing(allow_remote=False)
    acc = update_accumulated_usage(token_totals, usage_reqs, live=bool(snapshot_meta.get('live')))

    # 使用累计值作为显示值
    display_input_tokens = acc['input_tokens']
    display_output_tokens = acc['output_tokens']
    display_reasoning_tokens = acc['reasoning_tokens']
    display_cached_tokens = acc['cached_tokens']
    display_total_tokens = display_input_tokens + display_output_tokens + display_reasoning_tokens
    display_total_requests = acc['total_requests']
    display_success = acc['success']
    display_failure = acc['failure']

    # 使用显示值计算费用
    display_token_totals = {
        'input_tokens': display_input_tokens,
        'output_tokens': display_output_tokens,
        'reasoning_tokens': display_reasoning_tokens,
        'cached_tokens': display_cached_tokens,
    }
    billable_input_tokens = get_billable_input_tokens(display_token_totals)
    usage_costs = compute_usage_costs(display_token_totals, pricing)

    # Persistence is performed by the background worker, never by dashboard polling.

    # 如果没有从 API 获取到请求数，使用日志统计
    has_usage_requests = display_total_requests > 0
    final_count = display_total_requests if has_usage_requests else log_requests.get('count', 0)
    final_success = display_success if has_usage_requests else log_requests.get('success', 0)
    final_failed = display_failure if has_usage_requests else log_requests.get('failed', 0)
    idle_state = get_idle_state(log_requests)
    auto_update_state = get_auto_update_state(has_update=has_update, stats=log_requests)

    return {
        'panel': {
            'name': PANEL_NAME,
            'version': f'v{PANEL_VERSION}',
        },
        'service': service,
        'version': {
            'current': state['current_version'],
            'latest': state['latest_version'],
            'has_update': has_update,
            'source': state.get('version_source', 'unknown'),
            'stale': upstream_snapshot().get('version_stale', False),
            'checked_at': state.get('version_checked_at'),
            'error': state.get('version_error'),
        },
        'capabilities': get_capabilities(),
        'upstream': upstream_snapshot(),
        'requests': {
            'count': final_count,
            'last_time': log_requests.get('last_time'),
            'success': final_success,
            'failed': final_failed,
            'is_idle': idle_state.get('is_idle', True),
            'input_tokens': display_input_tokens,
            'billable_input_tokens': billable_input_tokens,
            'output_tokens': display_output_tokens,
            'reasoning_tokens': display_reasoning_tokens,
            'cached_tokens': display_cached_tokens,
            'total_tokens': display_total_tokens,
            'timezone': log_requests.get('timezone'),
            'log_available': bool(log_requests.get('log_available', False)),
            'log_partial': bool(log_requests.get('partial', False)),
            'log_catching_up': bool(log_requests.get('catching_up', False)),
            'log_skipped_bytes': _safe_int(log_requests.get('skipped_bytes', 0)),
            'idle_reason': idle_state.get('reason'),
        },
        'update': {
            'in_progress': state['update_in_progress'],
            'last_time': state['last_update_time'],
            'last_result': state['last_update_result'],
            'auto_enabled': state['auto_update_enabled'],
            'status': auto_update_state,
        },
        'config': {
            'idle_threshold': CONFIG['idle_threshold_seconds'],
            'check_interval': CONFIG['auto_update_check_interval'],
            'write_enabled': is_config_write_enabled(),
        },
        'pricing': pricing,
        'pricing_basis': get_pricing_basis_info(),
        'pricing_meta': pricing_meta,
        'usage_costs': usage_costs,
        'paths': get_paths_info(),
        'health': state['health_status'],
        'management_auth': _management_auth_snapshot(),
        'usage_snapshot': {
            'source': snapshot_meta.get('source'),
            'live': bool(snapshot_meta.get('live')),
            'fetched_at': snapshot_meta.get('fetched_at'),
        },
    }


@app.route('/api/status')
def api_status():
    payload = dict(state.get('dashboard_snapshot') or {
        'panel': {'name': PANEL_NAME, 'version': f'v{PANEL_VERSION}'},
        'service': {'running': False, 'status': 'checking', 'source': 'unknown'},
        'version': {'current': 'unknown', 'latest': 'unknown', 'has_update': False},
        'requests': {}, 'update': {}, 'config': {'write_enabled': is_config_write_enabled()},
        'health': 'unknown', 'capabilities': get_capabilities(),
        'upstream': {'status': 'checking', 'message': '正在收集首次状态'},
    })
    collected = _parse_iso_datetime(state.get('collector_time'))
    age = max(0, (_utc_now() - collected).total_seconds()) if collected else None
    payload['collection'] = {'at': state.get('collector_time'), 'age_seconds': age,
                             'stale': age is None or age > 30, 'error': state.get('collector_error')}
    if payload['collection']['stale']:
        payload['service'] = {**payload['service'], 'running': False, 'status': 'unknown', 'stale': True}
    return jsonify(payload)


@app.route('/api/logs')
def api_logs():
    logs = parse_log_file(CONFIG['cliproxy_log'])
    return jsonify({'logs': logs, 'count': len(logs)})

@app.route('/api/cliproxy-logs')
def api_cliproxy_logs():
    """获取 CLIProxy 完整日志"""
    return jsonify(state.get('logs_snapshot', {'logs': [], 'count': 0}))

@app.route('/api/cliproxy-logs/clear', methods=['POST'])
def api_clear_cliproxy_logs():
    """清空 CLIProxy 日志"""
    if not _parse_bool(CONFIG.get('log_clear_enabled', False)):
        return jsonify({
            'success': False,
            'message': '服务日志清空默认禁用；界面的“清空”只清除当前浏览器显示',
        }), 403
    log_files = [CONFIG.get('cliproxy_log'), CONFIG.get('cliproxy_stderr')]
    cleared = False
    errors = []

    for log_file in log_files:
        log_file = _resolve_panel_path(log_file)
        if not log_file or not os.path.exists(log_file):
            continue
        try:
            with open(log_file, 'w', encoding='utf-8') as f:
                f.flush()
                os.fsync(f.fileno())
            cleared = True
        except Exception:
            errors.append("日志文件无法清空")

    _reset_log_stats_state()
    cache.invalidate('request_count_logs')

    if errors:
        return jsonify({'success': False, 'message': '清空失败', 'errors': errors}), 500
    if not cleared:
        return jsonify({'success': True, 'message': '暂无日志可清空'})
    return jsonify({'success': True, 'message': '日志已清空'})

@app.route('/api/request-logs')
def api_request_logs():
    """获取解析后的 HTTP 请求日志"""
    logs, stats = parse_request_logs(max_lines=300)
    return jsonify({
        'logs': logs,
        'count': len(logs),
        'stats': stats
    })

@app.route('/api/paths')
def api_paths():
    return jsonify(get_paths_info())


@app.route('/api/update-history')
def api_update_history():
    """获取更新历史"""
    history_file = UPDATE_HISTORY_PATH
    try:
        with update_history_lock:
            if os.path.exists(history_file):
                loaded = _load_json_file_limited(history_file, 1024 * 1024)
                history = loaded if isinstance(loaded, list) else []
            else:
                history = []

        # 计算每次更新距今多少小时
        now = _utc_now()
        response_history = []
        for original in history:
            if not isinstance(original, dict):
                continue
            entry = dict(original)
            try:
                # Legacy history values were naive UTC strings.
                update_time = _parse_iso_datetime(entry.get('time'), assume_timezone=UTC)
                hours_ago = max(0.0, (now - update_time.astimezone(UTC)).total_seconds() / 3600) if update_time else None
                entry['hours_ago'] = round(hours_ago, 1) if hours_ago is not None else None
                if update_time:
                    entry['time'] = _utc_iso(update_time)
            except (TypeError, ValueError, OverflowError):
                entry['hours_ago'] = None
            entry['version'] = resolve_version_label(entry.get('version'))
            response_history.append(entry)

        return jsonify({
            'success': True,
            'history': response_history[-10:]  # 返回最近10条
        })
    except Exception as exc:
        print(f"Warning: update history read failed ({_error_kind(exc)})")
        return jsonify({'success': False, 'error': '更新历史读取失败'}), 500

def record_update_history(version, success=True):
    """记录更新历史"""
    history_file = UPDATE_HISTORY_PATH
    with update_history_lock:
        try:
            if os.path.exists(history_file):
                loaded = _load_json_file_limited(history_file, 1024 * 1024)
                history = loaded if isinstance(loaded, list) else []
            else:
                history = []

            history.append({
                'version': version,
                'time': _utc_iso(),
                'success': bool(success),
            })
            _atomic_write_json(history_file, history[-50:], mode=0o600)
            return True
        except Exception as exc:
            print(f"Error recording update history ({_error_kind(exc)})")
            return False

@app.route('/api/update', methods=['POST'])
def api_update():
    data = _request_json_object()
    if data is None:
        return jsonify({'success': False, 'message': 'JSON object required'}), 400
    force = data.get('force', False)
    if not isinstance(force, bool):
        return jsonify({'success': False, 'message': 'force must be a boolean'}), 400

    if not get_capabilities()['binary_update']:
        return jsonify({'success': False, 'message': get_capabilities()['reason']}), 409
    if not force and not is_idle():
        return jsonify({
            'success': False,
            'message': 'System has active requests. Wait for idle or use force update.'
        }), 400

    if not update_lock.acquire(blocking=False):
        return jsonify({'success': False, 'message': 'Update already in progress'}), 400

    def do_update():
        perform_update(lock_acquired=True)

    state['update_in_progress'] = True
    try:
        thread = threading.Thread(target=do_update, daemon=True)
        thread.start()
    except Exception:
        state['update_in_progress'] = False
        update_lock.release()
        raise

    return jsonify({'success': True, 'message': 'Update started, please refresh to check status'})

@app.route('/api/service/<action>', methods=['POST'])
def api_service(action):
    if action not in ['start', 'stop', 'restart']:
        return jsonify({'success': False, 'message': 'Invalid action'}), 400

    if not get_capabilities()['service_control']:
        return jsonify({'success': False, 'message': get_capabilities()['reason']}), 409

    service_name = _systemd_service_name()
    if not service_name:
        return jsonify({'success': False, 'message': 'Invalid or missing service name'}), 400
    if not update_lock.acquire(blocking=False):
        return jsonify({'success': False, 'message': '升级或服务操作正在进行，请稍后重试'}), 409
    try:
        success, stdout, stderr = run_cmd(['systemctl', action, service_name], timeout=10)
    finally:
        update_lock.release()
    collector_wakeup.set()
    cache.invalidate('service_status')
    time.sleep(2)

    status = get_service_status(use_cache=False)
    return jsonify({'success': success, 'message': stdout or stderr, 'status': status})

@app.route('/api/config/auto-update', methods=['POST'])
def api_toggle_auto_update():
    data = _request_json_object()
    if data is None:
        return jsonify({'success': False, 'error': 'JSON object required'}), 400
    enabled_raw = data.get('enabled', not state['auto_update_enabled'])
    enabled = enabled_raw if isinstance(enabled_raw, bool) else _parse_bool(enabled_raw)
    if enabled and not get_capabilities()['binary_update']:
        return jsonify({'success': False, 'error': get_capabilities()['reason']}), 409
    if not _update_dotenv_values({'auto_update_enabled': enabled}):
        return jsonify({'success': False, 'error': '保存 .env 失败'}), 500
    state['auto_update_enabled'] = enabled
    CONFIG['auto_update_enabled'] = enabled
    auto_update_wakeup.set()
    return jsonify({'success': True, 'auto_update_enabled': state['auto_update_enabled']})

@app.route('/api/config/idle-threshold', methods=['POST'])
def api_set_idle_threshold():
    data = _request_json_object()
    if data is None:
        return jsonify({'success': False, 'error': 'JSON object required'}), 400
    threshold = data.get('threshold', 60)

    if isinstance(threshold, bool) or not isinstance(threshold, int) or not 10 <= threshold <= 7 * 86400:
        return jsonify({'success': False, 'message': 'Threshold must be an integer between 10 and 604800 seconds'}), 400

    if not _update_dotenv_values({'idle_threshold_seconds': threshold}):
        return jsonify({'success': False, 'error': '保存 .env 失败'}), 500
    CONFIG['idle_threshold_seconds'] = threshold
    return jsonify({'success': True, 'idle_threshold': CONFIG['idle_threshold_seconds']})

@app.route('/api/config/check-interval', methods=['POST'])
def api_set_check_interval():
    """设置自动更新检查间隔"""
    data = _request_json_object()
    if data is None:
        return jsonify({'success': False, 'error': 'JSON object required'}), 400
    interval = data.get('interval', 300)
    if isinstance(interval, bool) or not isinstance(interval, (int, float)) or not 60 <= interval <= 86400:
        return jsonify({'success': False, 'error': 'Invalid interval (60-86400 seconds)'}), 400
    interval = int(interval)
    if not _update_dotenv_values({'auto_update_check_interval': interval}):
        return jsonify({'success': False, 'error': '保存 .env 失败'}), 500
    CONFIG['auto_update_check_interval'] = interval
    auto_update_wakeup.set()
    return jsonify({'success': True, 'check_interval': CONFIG['auto_update_check_interval']})


@app.route('/api/config/pricing-auto', methods=['POST'])
def api_set_pricing_auto():
    """开启/关闭 Token 价格自动同步（默认开启；关闭后严格使用手动价格）"""
    data = _request_json_object()
    if data is None:
        return jsonify({'success': False, 'error': 'JSON object required'}), 400
    enabled_raw = data.get('enabled', CONFIG.get('pricing_auto_enabled', True))
    enabled = enabled_raw if isinstance(enabled_raw, bool) else _parse_bool(enabled_raw)
    if not _update_dotenv_values({'pricing_auto_enabled': enabled}):
        return jsonify({'success': False, 'error': '保存 .env 失败'}), 500
    CONFIG['pricing_auto_enabled'] = enabled
    # 返回当前 effective 价格，方便前端立即刷新显示
    effective, pricing_meta = get_effective_pricing()
    return jsonify({
        'success': True,
        'pricing_auto_enabled': enabled,
        'effective_pricing': effective,
        'pricing_basis': get_pricing_basis_info(),
        'pricing_meta': pricing_meta,
    })


@app.route('/api/pricing', methods=['GET', 'POST'])
def api_pricing():
    if request.method == 'POST':
        data = _request_json_object()
        if data is None:
            return jsonify({'success': False, 'error': 'JSON object required'}), 400
        input_price = _parse_float(data.get('input', CONFIG.get('pricing_input', 0.0)))
        output_price = _parse_float(data.get('output', CONFIG.get('pricing_output', 0.0)))
        cache_price = _parse_float(data.get('cache', CONFIG.get('pricing_cache', 0.0)))
        prices = (input_price, output_price, cache_price)
        if any(not math.isfinite(value) or value < 0 or value > 1_000_000 for value in prices):
            return jsonify({'success': False, 'error': '价格必须是 0 到 1000000 之间的有限数字'}), 400
        if not _update_dotenv_values({
            'pricing_input': input_price,
            'pricing_output': output_price,
            'pricing_cache': cache_price,
        }):
            return jsonify({'success': False, 'error': '保存 .env 失败'}), 500
        CONFIG['pricing_input'] = input_price
        CONFIG['pricing_output'] = output_price
        CONFIG['pricing_cache'] = cache_price
        # 手动保存后，effective 价格也会随之变化（除非仍为 0 且开启自动同步）
        effective, pricing_meta = get_effective_pricing()
        return jsonify({
            'success': True,
            'pricing': {'input': input_price, 'output': output_price, 'cache': cache_price},
            'effective_pricing': effective,
            'pricing_basis': get_pricing_basis_info(),
            'pricing_meta': pricing_meta,
        })

    manual = {
        'input': _safe_float(CONFIG.get('pricing_input', 0.0)),
        'output': _safe_float(CONFIG.get('pricing_output', 0.0)),
        'cache': _safe_float(CONFIG.get('pricing_cache', 0.0)),
    }
    effective, pricing_meta = get_effective_pricing()
    return jsonify({
        'success': True,
        'pricing': manual,
        'effective_pricing': effective,
        'pricing_basis': get_pricing_basis_info(),
        'pricing_meta': pricing_meta,
    })


@app.route('/api/config/management-key', methods=['GET', 'POST'])
def api_management_key():
    if request.method == 'GET':
        return jsonify({'success': True, 'management_auth': _management_auth_snapshot()})

    data = _request_json_object()
    if data is None:
        return jsonify({'success': False, 'error': 'JSON object required'}), 400
    key_value = data.get('management_key')
    if not isinstance(key_value, str):
        return jsonify({'success': False, 'error': 'CPA 管理密钥必须是文本'}), 400
    key = key_value.strip()
    if not key:
        return jsonify({'success': False, 'error': 'CPA 管理密钥不能为空'}), 400
    if len(key) > 4096 or any(char in key for char in ('\r', '\n', '\x00')):
        return jsonify({'success': False, 'error': 'CPA 管理密钥格式无效'}), 400
    env_key = os.environ.get('CLIPROXY_PANEL_MANAGEMENT_KEY', '').strip()
    if get_capabilities()['mode'] == 'docker' and env_key and env_key != key:
        return jsonify({'success': False, 'error': '密钥由容器环境变量管理，请修改部署配置并重新创建容器。'}), 409

    if not _update_dotenv_values({'management_key': key}):
        return jsonify({'success': False, 'error': '保存 .env 失败'}), 500

    CONFIG['management_key'] = key
    _reset_management_auth_state()
    return jsonify({
        'success': True,
        'message': 'CPA 管理密钥已保存',
        'management_auth': _management_auth_snapshot(),
    })


@app.route('/api/quote', methods=['GET', 'POST'])
def api_quote():
    if request.method == 'POST':
        data = _request_json_object()
        if data is None or not isinstance(data.get('line'), str):
            return jsonify({'success': False, 'error': 'line 必须是文本'}), 400
        line = data['line'].strip()
        if not line or '出自：' not in line:
            return jsonify({'success': False, 'error': '格式错误，请使用“内容 出自：作者”'}), 400
        if '\n' in line or '\r' in line or '\x00' in line or len(line) > 10_000:
            return jsonify({'success': False, 'error': '语录必须是长度不超过 10000 字的单行文本'}), 400
        path = _resolve_panel_path(CONFIG.get('quotes_path')) or BUNDLED_QUOTES_PATH
        try:
            parent_dir = os.path.dirname(path)
            if parent_dir:
                os.makedirs(parent_dir, exist_ok=True)
            with quotes_lock:
                with open(path, 'a', encoding='utf-8') as f:
                    f.write(line + '\n')
                    f.flush()
                    os.fsync(f.fileno())
                loaded_quotes = load_quotes()
                cache.set('quotes_cache', loaded_quotes)
            return jsonify({'success': True, 'count': len(loaded_quotes)})
        except Exception as exc:
            print(f"Warning: quote save failed ({_error_kind(exc)})")
            return jsonify({'success': False, 'error': '语录保存失败'}), 500

    quote = get_random_quote()
    return jsonify({'text': quote.get('text', ''), 'author': quote.get('author', '')})

@app.route('/api/record-request', methods=['POST'])
def api_record_request():
    data = _request_json_object()
    if data is None:
        return jsonify({'success': False, 'error': 'JSON object required'}), 400
    model = str(data.get('model') or 'unknown')[:200]
    status_value = str(data.get('status') or 'unknown')[:40]
    response_time = _safe_float(data.get('response_time'), 0.0)
    if not math.isfinite(response_time) or response_time < 0:
        response_time = 0.0

    with log_lock:
        state['last_request_time'] = time.time()
        state['request_count'] += 1

        state['request_log'].append({
            'time': _utc_iso(),
            'model': model,
            'client': request.remote_addr,
            'status': status_value,
            'response_time': response_time,
        })

        if len(state['request_log']) > 100:
            state['request_log'] = state['request_log'][-100:]

        # 更新统计
        with stats_lock:
            state['stats']['total_requests'] += 1
            recorded = state.setdefault('recorded_stats', {
                'total_requests': 0, 'success': 0, 'failure': 0,
            })
            recorded['total_requests'] = max(0, _safe_int(recorded.get('total_requests', 0))) + 1
            if status_value == 'success':
                state['stats']['successful_requests'] += 1
                recorded['success'] = max(0, _safe_int(recorded.get('success', 0))) + 1
            else:
                state['stats']['failed_requests'] += 1
                recorded['failure'] = max(0, _safe_int(recorded.get('failure', 0))) + 1

            _increment_model_usage_locked(model)

    # 触发持久化保存（后台线程会定期保存，这里只是触发检查）
    save_persistent_stats()

    return jsonify({'success': True})

@app.route('/api/request-history')
def api_request_history():
    return jsonify({
        'history': state['request_log'][-50:],
        'total_count': state['request_count'],
        'last_time': state['last_request_time']
    })

@app.route('/api/check-update')
def api_check_update():
    # Enqueue at most one check; a slow GitHub response must not occupy HTTP workers.
    if cache.get('manual_version_check', max_age=15) is None:
        cache.set('manual_version_check', True)
        cache.invalidate('local_version')
        auto_update_wakeup.set()
        collector_wakeup.set()
    return jsonify({
        'checking': True, 'has_update': state['has_update'],
        'current': state['current_version'], 'latest': state['latest_version'],
    }), 202

@app.route('/api/auth-files')
def api_auth_files():
    auth_dir = _resolve_panel_path(CONFIG.get('auth_dir'))
    if not os.path.exists(auth_dir):
        return jsonify({'files': [], 'error': 'Auth directory not found'})

    try:
        files = []
        for f in sorted(os.listdir(auth_dir))[:1000]:
            filepath = os.path.join(auth_dir, f)
            if os.path.isfile(filepath):
                stat = os.stat(filepath)
                files.append({
                    'name': f,
                    'size': stat.st_size,
                    'modified': _utc_iso(datetime.fromtimestamp(stat.st_mtime, tz=UTC))
                })
        return jsonify({'files': files, 'path': auth_dir})
    except Exception as exc:
        print(f"Warning: auth file listing failed ({_error_kind(exc)})")
        return jsonify({'files': [], 'error': '认证文件列表读取失败'}), 500


def _save_cliproxy_config_content(content):
    if not isinstance(content, str):
        return False, 'Config content must be text', None
    if len(content.encode('utf-8')) > 2 * 1024 * 1024:
        return False, 'Config file exceeds the 2 MiB limit', None
    validation = validate_yaml_config(content)
    if not validation.get('valid'):
        errors = '; '.join(validation.get('errors') or ['invalid YAML'])
        return False, errors, None

    config_path = _resolve_panel_path(CONFIG.get('cliproxy_config'))
    if not config_path:
        return False, 'Config path is not set', None
    backup_path = config_path + '.bak'
    try:
        if os.path.isfile(config_path):
            shutil.copy2(config_path, backup_path)
        _atomic_write_text(config_path, content)
        cache.invalidate('cliproxy_config')
        cache.invalidate('health_check')
        return True, None, backup_path if os.path.exists(backup_path) else None
    except Exception as exc:
        print(f"Warning: configuration save failed ({_error_kind(exc)})")
        return False, '配置保存失败', None

@app.route('/api/config', methods=['GET'])
def api_get_config():
    config_path = _resolve_panel_path(CONFIG.get('cliproxy_config'))
    if not os.path.exists(config_path):
        return jsonify({'success': False, 'error': 'Config file not found', 'path': config_path}), 404

    try:
        if os.path.getsize(config_path) > 2 * 1024 * 1024:
            return jsonify({'success': False, 'error': 'Config file exceeds the 2 MiB limit'}), 413
        with open(config_path, 'r', encoding='utf-8') as f:
            content = f.read()
        return jsonify({'success': True, 'content': content, 'path': config_path})
    except Exception as exc:
        print(f"Warning: configuration read failed ({_error_kind(exc)})")
        return jsonify({'success': False, 'error': '配置读取失败'}), 500

@app.route('/api/config', methods=['POST'])
def api_upload_config():
    if not is_config_write_enabled():
        return config_write_blocked_response()

    config_path = _resolve_panel_path(CONFIG.get('cliproxy_config'))

    if 'file' in request.files:
        file = request.files['file']
        if file.filename == '':
            return jsonify({'success': False, 'error': 'No file selected'}), 400

        try:
            raw = file.stream.read(2 * 1024 * 1024 + 1)
            if len(raw) > 2 * 1024 * 1024:
                return jsonify({'success': False, 'error': 'Config file exceeds the 2 MiB limit'}), 413
            content = raw.decode('utf-8')
            success, error, backup_path = _save_cliproxy_config_content(content)
            if not success:
                return jsonify({'success': False, 'error': error}), 400
            return jsonify({
                'success': True,
                'message': 'Config uploaded successfully',
                'path': config_path,
                'backup': backup_path
            })
        except (OSError, UnicodeDecodeError) as exc:
            print(f"Warning: configuration upload failed ({_error_kind(exc)})")
            return jsonify({'success': False, 'error': '配置上传失败'}), 500

    data = _request_json_object()
    if data and 'content' in data:
        try:
            success, error, backup_path = _save_cliproxy_config_content(data['content'])
            if not success:
                return jsonify({'success': False, 'error': error}), 400
            return jsonify({
                'success': True,
                'message': 'Config saved successfully',
                'path': config_path,
                'backup': backup_path
            })
        except Exception as exc:
            print(f"Warning: configuration update failed ({_error_kind(exc)})")
            return jsonify({'success': False, 'error': '配置保存失败'}), 500

    return jsonify({'success': False, 'error': 'No file or content provided'}), 400

@app.route('/api/config/restore', methods=['POST'])
def api_restore_config():
    if not is_config_write_enabled():
        return config_write_blocked_response()

    config_path = _resolve_panel_path(CONFIG.get('cliproxy_config'))
    backup_path = config_path + '.bak'

    if not os.path.exists(backup_path):
        return jsonify({'success': False, 'error': 'No backup file found'}), 404

    try:
        with open(backup_path, 'r', encoding='utf-8') as handle:
            content = handle.read()
        _atomic_write_text(config_path, content)
        cache.invalidate('cliproxy_config')
        cache.invalidate('health_check')
        return jsonify({
            'success': True,
            'message': 'Config restored from backup',
            'path': config_path
        })
    except Exception as exc:
        print(f"Warning: configuration restore failed ({_error_kind(exc)})")
        return jsonify({'success': False, 'error': '配置恢复失败'}), 500

# ==================== 新增API ====================

@app.route('/api/config/validate', methods=['POST'])
def api_validate_config():
    """验证配置文件格式"""
    data = _request_json_object()
    if data is None:
        return jsonify({'success': False, 'error': 'JSON object required'}), 400
    content = data.get('content', '')

    if not content:
        # 验证当前配置文件
        config_path = _resolve_panel_path(CONFIG.get('cliproxy_config'))
        if not os.path.exists(config_path):
            return jsonify({'success': False, 'error': 'Config file not found'}), 404
        with open(config_path, 'r', encoding='utf-8') as f:
            content = f.read()

    result = validate_yaml_config(content)
    return jsonify(result)

@app.route('/api/config/reload', methods=['POST'])
def api_reload_config():
    """重新加载配置（发送SIGHUP信号）"""
    service_status = get_service_status(use_cache=False)
    pid_out = service_status.get('pid')

    if not pid_out:
        return jsonify({'success': False, 'message': '服务未运行'}), 400

    try:
        success = False
        if hasattr(signal, 'SIGHUP'):
            try:
                os.kill(int(pid_out), signal.SIGHUP)
                success = True
            except (OSError, TypeError, ValueError):
                success = False

        if success:
            return jsonify({'success': True, 'message': '配置重载信号已发送'})
        else:
            # 如果SIGHUP不支持，尝试重启服务（Linux/systemd）
            if is_linux() and command_available('systemctl'):
                service_name = _systemd_service_name()
                if not service_name:
                    return jsonify({'success': False, 'message': 'Invalid or missing service name'}), 400
                run_cmd(['systemctl', 'restart', service_name])
                cache.invalidate('service_status')
                time.sleep(2)
                status = get_service_status(use_cache=False)
                return jsonify({
                    'success': status['running'],
                    'message': '已重启服务以应用配置',
                    'status': status
                })

            return jsonify({'success': False, 'message': 'Reload not supported on this platform'}), 400
    except Exception as exc:
        print(f"Warning: configuration reload failed ({_error_kind(exc)})")
        return jsonify({'success': False, 'error': '配置重载失败'}), 500

@app.route('/api/config/routing', methods=['GET'])
def api_get_routing():
    """获取当前路由策略"""
    config, error = load_cliproxy_config()
    if config is None:
        return jsonify({'success': False, 'error': error}), 500

    # 如果没有yaml，返回默认值
    if '_raw' in config:
        return jsonify({
            'success': True,
            'strategy': 'round-robin',
            'available': ['round-robin', 'fill-first'],
            'note': 'pyyaml未安装，无法解析配置'
        })

    routing = config.get('routing', {})
    return jsonify({
        'success': True,
        'strategy': routing.get('strategy', 'round-robin'),
        'available': ['round-robin', 'fill-first']
    })

@app.route('/api/config/routing', methods=['POST'])
def api_set_routing():
    """设置路由策略"""
    if not is_config_write_enabled():
        return config_write_blocked_response()

    if not HAS_YAML:
        return jsonify({'success': False, 'error': 'pyyaml未安装，无法修改配置'}), 400

    data = _request_json_object()
    if data is None:
        return jsonify({'success': False, 'error': 'JSON object required'}), 400
    strategy = data.get('strategy')

    valid_strategies = ['round-robin', 'fill-first']
    if strategy not in valid_strategies:
        return jsonify({'success': False, 'error': f'无效的策略，可选: {", ".join(valid_strategies)}'}), 400

    config_path = _resolve_panel_path(CONFIG.get('cliproxy_config'))
    config, error = load_cliproxy_config()
    if config is None:
        return jsonify({'success': False, 'error': error}), 500

    # 更新路由策略
    if 'routing' not in config:
        config['routing'] = {}
    config['routing']['strategy'] = strategy

    try:
        # 备份
        backup_path = config_path + '.bak'
        if os.path.exists(config_path):
            shutil.copy2(config_path, backup_path)

        # 写入新配置
        content = yaml.safe_dump(config, default_flow_style=False, allow_unicode=True, sort_keys=False)
        _atomic_write_text(config_path, content)
        cache.invalidate('cliproxy_config')
        cache.invalidate('health_check')

        return jsonify({'success': True, 'message': f'路由策略已设置为 {strategy}'})
    except Exception as exc:
        print(f"Warning: routing update failed ({_error_kind(exc)})")
        return jsonify({'success': False, 'error': '路由策略保存失败'}), 500

@app.route('/api/health')
def api_health():
    """健康检查"""
    payload = dict(state.get('health_snapshot') or {
        'overall': 'unknown', 'checks': [], 'checks_map': {}, 'pending': True,
    })
    checked = _parse_iso_datetime(payload.get('timestamp'))
    payload['stale'] = not checked or (_utc_now() - checked).total_seconds() > 150
    if payload['stale']:
        payload['overall'] = 'unknown'
    return jsonify(payload)

@app.route('/api/resources')
def api_resources():
    """Latest background sample; never sample process / host resources in a request."""
    payload = dict(state.get('resources_snapshot') or {'pending': True})
    collected = _parse_iso_datetime(payload.get('collected_at'))
    payload['stale'] = not collected or (_utc_now() - collected).total_seconds() > 30
    return jsonify(payload)

@app.route('/api/stats')
def api_stats():
    """获取统计数据"""
    with stats_lock:
        stats = {
            'total_requests': state['stats']['total_requests'],
            'successful_requests': state['stats']['successful_requests'],
            'failed_requests': state['stats']['failed_requests'],
            'success_rate': (state['stats']['successful_requests'] / max(state['stats']['total_requests'], 1)) * 100,
            'input_tokens': state['stats']['input_tokens'],
            'output_tokens': state['stats']['output_tokens'],
            'reasoning_tokens': state['stats']['reasoning_tokens'],
            'cached_tokens': state['stats']['cached_tokens'],
            'usage_counter_mode': state.get('usage_counter_mode', 'cumulative'),
            'model_usage': dict(state['stats']['model_usage']),
            'error_types': dict(state['stats']['error_types']),
            'request_log': state['request_log'][-20:],
        }

    return jsonify(stats)

@app.route('/api/stats/clear', methods=['POST'])
def api_clear_stats():
    """清空请求统计（清空累计值，更新快照为当前值）"""
    # 新版上游不再提供旧 usage 接口。这里只读取本地兼容快照，
    # 清空统计绝不能为了建立基线再次请求已废弃的管理端点。
    snapshot, snapshot_meta = fetch_usage_snapshot(
        use_cache=False,
        with_meta=True,
        allow_network=False,
    )
    token_totals, usage_reqs = aggregate_usage_snapshot(snapshot)

    with log_lock:
        with stats_lock:
            if snapshot is not None and snapshot_meta.get('live'):
                state['last_snapshot'] = {
                    'input_tokens': token_totals.get('input_tokens', 0),
                    'output_tokens': token_totals.get('output_tokens', 0),
                    'reasoning_tokens': token_totals.get('reasoning_tokens', 0),
                    'cached_tokens': token_totals.get('cached_tokens', 0),
                    'total_requests': usage_reqs.get('total_requests', 0) or 0,
                    'success': usage_reqs.get('success', 0) or 0,
                    'failure': usage_reqs.get('failure', 0) or 0,
                }
                state['usage_reset_pending'] = False
                state['usage_counter_mode'] = 'cumulative'
            elif snapshot_meta.get('source') == 'queue':
                state['last_snapshot'] = {
                    'input_tokens': 0,
                    'output_tokens': 0,
                    'reasoning_tokens': 0,
                    'cached_tokens': 0,
                    'total_requests': 0,
                    'success': 0,
                    'failure': 0,
                }
                state['usage_reset_pending'] = False
                state['usage_counter_mode'] = 'queue'
            else:
                state['usage_reset_pending'] = True
            state['accumulated_stats'] = {
                'input_tokens': 0,
                'output_tokens': 0,
                'reasoning_tokens': 0,
                'cached_tokens': 0,
                'total_requests': 0,
                'success': 0,
                'failure': 0,
            }
            state['recorded_stats'] = {
                'total_requests': 0,
                'success': 0,
                'failure': 0,
            }
            state['stats']['total_requests'] = 0
            state['stats']['successful_requests'] = 0
            state['stats']['failed_requests'] = 0
            state['stats']['input_tokens'] = 0
            state['stats']['output_tokens'] = 0
            state['stats']['reasoning_tokens'] = 0
            state['stats']['cached_tokens'] = 0
            state['stats']['model_usage'].clear()
            state['stats']['error_types'].clear()
            state['request_log'].clear()
            state['request_count'] = 0

    # 保存清空后的状态到持久化文件
    save_persistent_stats(force=True)

    # 清除所有缓存
    cache.invalidate()

    # 统计归零不再复制/截断可能很大的服务日志；从当前 EOF 重新计数即可。
    _reset_log_stats_state(start_at_end=True)

    return jsonify({'success': True, 'message': '统计数据已清空'})

@app.route('/api/models')
def api_models():
    """获取模型列表"""
    api_key = CONFIG.get('models_api_key', '')
    headers = {'Content-Type': 'application/json'}
    if api_key:
        headers['Authorization'] = f'Bearer {api_key}'

    try:
        base_url = _compose_api_base_url(port=CONFIG.get('cliproxy_api_port'))
        models_url = f'{base_url}/v1/models'
        with http_session.get(models_url, headers=headers, timeout=10, stream=True) as resp:
            resp.raise_for_status()
            payload = _response_json_limited(resp, 8 * 1024 * 1024)
        models = payload.get('data', []) if isinstance(payload, dict) else []
        return jsonify({'success': True, 'models': models, 'count': len(models)})
    except Exception as exc:
        print(f"Warning: model listing failed ({_error_kind(exc)})")
        return jsonify({'success': False, 'error': '模型列表读取失败', 'models': []}), 502

@app.route('/api/test/connection', methods=['POST'])
def api_test_connection():
    """测试连接"""
    data = _request_json_object()
    if data is None:
        return jsonify({'success': False, 'error': 'JSON object required'}), 400
    target = data.get('target', 'api')

    results = {'success': True, 'tests': []}

    if target in ['api', 'all']:
        # 测试API端口
        try:
            api_host, api_port = _api_host_port()
            start = time.time()
            with socket.create_connection((api_host, api_port), timeout=5):
                pass
            latency = (time.time() - start) * 1000

            results['tests'].append({
                'name': 'API端口',
                'success': True,
                'latency': f'{latency:.1f}ms',
                'message': f'{api_host}:{api_port} 正常',
            })
        except Exception:
            results['tests'].append({
                'name': 'API端口',
                'success': False,
                'message': 'API 端口连接失败'
            })

    if target in ['internet', 'all']:
        # 直接测试自动更新依赖的 GitHub，而不是容易被网络策略屏蔽的公共 DNS 端口。
        try:
            start = time.time()
            response = http_session.get(
                'https://github.com/router-for-me/CLIProxyAPI/releases/latest',
                timeout=8,
                allow_redirects=False,
                stream=True,
            )
            latency = (time.time() - start) * 1000
            reachable = response.status_code < 500
            response.close()

            results['tests'].append({
                'name': 'GitHub 更新源',
                'success': reachable,
                'latency': f'{latency:.1f}ms' if reachable else None,
                'message': '更新源可访问' if reachable else f'更新源返回 HTTP {response.status_code}',
            })
        except Exception:
            results['tests'].append({
                'name': '外网连接',
                'success': False,
                'message': '更新源连接失败'
            })

    # 整体结果
    results['success'] = all(t['success'] for t in results['tests'])

    return jsonify(results)

@app.route('/api/test/api', methods=['POST'])
def api_test_api():
    """API测试器"""
    data = _request_json_object()
    if data is None:
        return jsonify({'success': False, 'error': 'JSON object required'}), 400
    endpoint = str(data.get('endpoint', '/v1/models') or '')
    method = str(data.get('method', 'GET') or 'GET').upper()
    body = data.get('body')
    headers = data.get('headers', {})

    if method not in {'GET', 'POST', 'PUT', 'PATCH', 'DELETE', 'HEAD'}:
        return jsonify({'success': False, 'error': '不支持的 HTTP 方法'}), 400
    if not endpoint.startswith('/') or endpoint.startswith('//') or '://' in endpoint or len(endpoint) > 2048:
        return jsonify({'success': False, 'error': 'endpoint 必须是长度不超过 2048 的相对路径'}), 400
    if not isinstance(headers, dict) or len(headers) > 50:
        return jsonify({'success': False, 'error': 'headers 必须是最多包含 50 项的对象'}), 400
    safe_headers = {}
    for key, value in headers.items():
        key_text = str(key)
        value_text = str(value)
        if any(char in key_text + value_text for char in ('\r', '\n', '\x00')):
            return jsonify({'success': False, 'error': '请求头包含非法字符'}), 400
        safe_headers[key_text] = value_text

    try:
        base_url = _compose_api_base_url(port=CONFIG.get('cliproxy_api_port'))
        url = base_url + endpoint
        start_time = time.time()
        response = http_session.request(
            method,
            url,
            headers=safe_headers,
            json=body if body is not None else None,
            timeout=30,
            allow_redirects=False,
            stream=True,
        )
        response_time = (time.time() - start_time) * 1000
        raw_body = response.raw.read(2 * 1024 * 1024 + 1, decode_content=True)
        truncated = len(raw_body) > 2 * 1024 * 1024
        raw_body = raw_body[:2 * 1024 * 1024]
        response_body = raw_body.decode(response.encoding or 'utf-8', errors='replace')
        response.close()
        try:
            response_json = json.loads(response_body)
        except (TypeError, ValueError):
            response_json = None
        return jsonify({
            'success': response.ok,
            'status': response.status_code,
            'response_time': f'{response_time:.1f}ms',
            'headers': dict(response.headers),
            'body': response_json if response_json is not None else response_body[:2000],
            'truncated': truncated,
        })
    except requests.RequestException:
        return jsonify({
            'success': False,
            'error': '连接失败'
        })

@app.route('/api/export/<data_type>')
def api_export(data_type):
    """数据导出"""
    if data_type == 'logs':
        logs = state['request_log']
        content = json.dumps(logs, indent=2, ensure_ascii=False)
        return Response(
            content,
            mimetype='application/json',
            headers={'Content-Disposition': f'attachment; filename=logs_{_utc_now().strftime("%Y%m%d_%H%M%S")}_UTC.json'}
        )

    elif data_type == 'stats':
        with stats_lock:
            stats = {
                'exported_at': _utc_iso(),
                'total_requests': state['stats']['total_requests'],
                'successful_requests': state['stats']['successful_requests'],
                'failed_requests': state['stats']['failed_requests'],
                'model_usage': dict(state['stats']['model_usage']),
            }
        content = json.dumps(stats, indent=2, ensure_ascii=False)
        return Response(
            content,
            mimetype='application/json',
            headers={'Content-Disposition': f'attachment; filename=stats_{_utc_now().strftime("%Y%m%d_%H%M%S")}_UTC.json'}
        )

    elif data_type == 'config':
        config_path = _resolve_panel_path(CONFIG.get('cliproxy_config'))
        if os.path.exists(config_path):
            with open(config_path, 'r', encoding='utf-8') as f:
                content = f.read()
            return Response(
                content,
                mimetype='application/x-yaml',
                headers={'Content-Disposition': f'attachment; filename=config_{_utc_now().strftime("%Y%m%d_%H%M%S")}_UTC.yaml'}
            )
        return jsonify({'error': 'Config not found'}), 404

    elif data_type == 'health':
        health = perform_health_check()
        content = json.dumps(health, indent=2, ensure_ascii=False)
        return Response(
            content,
            mimetype='application/json',
            headers={'Content-Disposition': f'attachment; filename=health_{_utc_now().strftime("%Y%m%d_%H%M%S")}_UTC.json'}
        )

    return jsonify({'error': 'Unknown data type'}), 400

# 启动后台任务
def redact_log_message(message):
    text = str(message)[:16 * 1024]
    for key in (_panel_access_key_expected(), str(CONFIG.get('management_key') or ''),
                str(CONFIG.get('models_api_key') or '')):
        if key:
            text = text.replace(key, '[REDACTED]')
    text = re.sub(r'(?i)(bearer\s+)[^\s,;"\']+', r'\1[REDACTED]', text)
    text = re.sub(r'(?i)((?:api[_-]?key|token|password|secret)=)[^\s&"\']+', r'\1[REDACTED]', text)
    return text


def collect_runtime_snapshot():
    """Only this bounded collector does work for automatic browser polling."""
    remote = probe_upstream()
    cache.invalidate('local_version')
    state['current_version'] = get_local_version()
    current_key = _release_version_key(state['current_version'])
    latest_key = _release_version_key(state['latest_version'])
    state['has_update'] = bool(current_key and latest_key and latest_key > current_key and not remote.get('version_stale'))
    resources = get_system_resources()
    resources['scope'] = get_capabilities()['resource_scope']
    if not resources.get('error'):
        state['resources_snapshot'] = {**resources, 'collected_at': _utc_iso()}
    logs = merge_log_entries(
        parse_log_file(CONFIG['cliproxy_log'], max_lines=400, limit=400),
        parse_log_file(CONFIG['cliproxy_stderr'], max_lines=120, limit=120),
        parse_journal_logs(CONFIG.get('cliproxy_service'), max_lines=120), limit=200,
    )
    for entry in logs:
        entry['message'] = redact_log_message(entry['message'])
    state['logs_snapshot'] = {'logs': logs, 'count': len(logs), 'collected_at': _utc_iso()}
    state['dashboard_snapshot'] = build_status_payload()
    state['collector_time'] = _utc_iso()
    state['collector_error'] = None


def collector_worker():
    while not shutdown_event.is_set():
        collector_wakeup.clear()
        try:
            collect_runtime_snapshot()
        except Exception as exc:
            # Keep the previous sample, visibly mark it stale, and retry next cycle.
            state['collector_error'] = f'状态采集失败 ({_error_kind(exc)})'
        collector_wakeup.wait(5)


def background_tasks():
    """Slow diagnostics/pricing are isolated from the frequent state collector."""
    next_pricing_refresh = 0.0
    while not shutdown_event.is_set():
        try:
            state['health_snapshot'] = perform_health_check(use_cache=False)
        except Exception as exc:
            print(f'[{_utc_iso()}] Health check failed ({_error_kind(exc)})')
        try:
            if time.monotonic() >= next_pricing_refresh:
                get_effective_pricing(allow_remote=True)
                next_pricing_refresh = time.monotonic() + 6 * 3600
        except Exception as exc:
            print(f'[{_utc_iso()}] Pricing refresh failed ({_error_kind(exc)})')
        shutdown_event.wait(60)


def shutdown_runtime():
    shutdown_event.set()
    auto_update_wakeup.set()
    collector_wakeup.set()
    resource_monitor._running = False
    save_persistent_stats(force=True)
    save_log_stats_state(force=True)


runtime_lock = threading.Lock()
runtime_started = False


def initialize_runtime():
    global runtime_started
    with runtime_lock:
        if runtime_started:
            return
        runtime_started = True

    os.makedirs(DATA_DIR, exist_ok=True)
    load_persistent_stats()
    load_auto_update_failure_state()
    save_persistent_stats(force=True)
    load_log_stats_state()
    resource_monitor.start()
    atexit.register(shutdown_runtime)

    cliproxy_binary = _resolve_panel_path(CONFIG.get('cliproxy_binary'))
    if cliproxy_binary and os.path.lexists(cliproxy_binary):
        cliproxy_binary = os.path.realpath(cliproxy_binary)
    if cliproxy_binary and get_capabilities()['binary_update']:
        cleanup_binary_backups(cliproxy_binary)

    threads = [
        threading.Thread(target=collector_worker, daemon=True, name='cpa-collector'),
        threading.Thread(target=auto_update_worker, daemon=True, name='cpa-auto-update'),
        threading.Thread(target=background_tasks, daemon=True, name='cpa-background'),
        threading.Thread(target=_persistent_stats_worker, daemon=True, name='cpa-stats-persist'),
    ]
    for thread in threads:
        thread.start()

    quotes = load_quotes()
    if quotes:
        cache.set('quotes_cache', quotes)
        if len(quotes) < 181:
            print(f'Warning: only {len(quotes)} bundled quotes were loaded')

if __name__ == '__main__':
    def handle_shutdown(_signum, _frame):
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, handle_shutdown)
    signal.signal(signal.SIGINT, handle_shutdown)
    initialize_runtime()
    bind_host = str(CONFIG.get('bind_host') or '127.0.0.1')
    panel_port = int(CONFIG['panel_port'])
    print(f'{PANEL_NAME} Panel v{PANEL_VERSION} started on {bind_host}:{panel_port}')
    if HAS_WAITRESS:
        waitress_serve(
            app,
            host=bind_host,
            port=panel_port,
            threads=max(4, min(16, (os.cpu_count() or 2) * 2)),
            channel_timeout=30,
            connection_limit=128,
            max_request_body_size=3 * 1024 * 1024,
            clear_untrusted_proxy_headers=True,
        )
    else:
        print('Warning: waitress is unavailable; using the Flask development server')
        app.run(host=bind_host, port=panel_port, debug=False, threaded=True)
