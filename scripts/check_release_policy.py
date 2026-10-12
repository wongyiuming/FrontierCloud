#!/usr/bin/env python3
"""Bounded policy checks for the frozen Python/FastAPI deployment contract."""
from pathlib import Path
import re
import tomllib

ROOT = Path(__file__).resolve().parents[1]


def read(name):
    return (ROOT / name).read_text(encoding='utf-8')


def require(value, reason):
    if not value:
        raise SystemExit(reason)


compose_files = ['docker-compose.yaml', 'docker-compose.mysql.yaml', 'docker-compose.storage.yaml']
for name in compose_files:
    if not (ROOT / name).exists():
        continue
    source = read(name)
    require('/var/run/docker.sock' not in source, f'{name}: product Docker socket access is retired')
    require('  updater:' not in source and '/run/frontiercloud-updater' not in source, f'{name}: online updater is retired')
    require('systemctl' not in source and 'privileged: true' not in source, f'{name}: host service/privileged runtime is forbidden')

for name in ['app/services/release_control.py', 'app/api/internal_cluster_update.py', 'updater/server.py',
             'static/js/release-admin.js', 'static/js/release-version-ui.js']:
    require(not (ROOT / name).exists(), f'Retired product surface reappeared: {name}')
require('python:3.14.7-slim' in read('Dockerfile'), 'Keep the Python base patch pinned')
for dependency in tomllib.loads(read('pyproject.toml'))['project']['dependencies']:
    require('==' in dependency and '>=' not in dependency, f'Runtime dependency must be pinned: {dependency}')
require('main' in read('CONTRIBUTING.md') and 'dev' in read('CONTRIBUTING.md'), 'Keep the dev/main promotion pair')
for name in ['.env.example', '.env.storage.example']:
    if not (ROOT / name).exists():
        continue
    require(not re.search(r'^\s*(?:SSL_CERT_PATH|SSL_KEY_PATH|STORAGE_TLS_CERT|STORAGE_TLS_KEY|ACME_WEBROOT|HTTP_PORT|HTTPS_PORT)\s*=', read(name), re.M),
            f'{name}: user certificate paths and public port overrides are retired')
require('priority-order' in read('app/api/v1/admin_priority.py'), 'Drag priority API is missing')
require('upload_encryption' in read('app/api/v1/admin_upload_guard.py'), 'Explicit browser upload mode validation is missing')
require('claim(upload_id)' in read('app/api/v1/admin_cluster_integrity.py'), 'Upload writer lease protection is missing')
require('NATIVE_MAINTENANCE' in read('app/services/site_control.py'), 'Operator fence must remain independent of the Admin GUI')
require('GetCertificate' not in read('Dockerfile') and 'go build' not in read('Dockerfile'), 'Archived runtime must remain pure Python')
print('Python deployment/source policy passed')
