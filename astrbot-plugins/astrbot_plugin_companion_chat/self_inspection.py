"""Read-only runtime observations; no commands, network access, or persistence.

This module is a staging component and is not registered as an AstrBot tool yet.
"""
import os
import platform
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path


def runtime():
    checked_at = datetime.now(timezone.utc).isoformat()
    warnings = []
    try:
        in_container = Path('/.dockerenv').exists() or Path('/run/.containerenv').exists()
        scope = 'container' if in_container else 'process_environment'
        disk = shutil.disk_usage(Path.cwd())
        details = {'host_identifier': platform.node() or None,
                   'os': platform.system() or None,
                   'os_release': platform.release() or None,
                   'environment': 'container' if in_container else 'unknown',
                   'disk': {'total_bytes': disk.total, 'free_bytes': disk.free,
                            'scope': 'current_working_directory_filesystem'},
                   'cpu': {'status': 'unknown'},
                   'memory': {'status': 'unknown'},
                   'uptime': {'status': 'unsupported' if os.name != 'posix' else 'unknown'},
                   'host_resources': {'status': 'unknown'}}
        # /proc/uptime may reflect the kernel rather than container lifetime.
        if platform.system() == 'Linux':
            try:
                value = float(Path('/proc/uptime').read_text().split()[0])
                details['uptime'] = {'seconds': value, 'scope': 'kernel'}
            except (OSError, ValueError, IndexError):
                pass
        warnings.append('CPU、内存和宿主机资源未经采集；容器检测未命中不代表一定运行在宿主机。')
        return {'status': 'unknown', 'checked_at': checked_at, 'source': 'local_runtime',
                'scope': scope, 'complete': False, 'summary': '运行环境部分可见。',
                'details': details, 'warnings': warnings, 'errors': []}
    except Exception:
        return {'status': 'unknown', 'checked_at': checked_at, 'source': 'local_runtime',
                'scope': 'process_environment', 'complete': False,
                'summary': '运行环境信息不可用。', 'details': {}, 'warnings': [],
                'errors': ['runtime_observation_unavailable']}
