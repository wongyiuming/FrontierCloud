"""Manually collect bounded native diagnostics on the development host.

Usage: python3 scripts/collect_native_evidence.py EVIDENCE NEW_PRIVATE_DIRECTORY
This does not run acceptance, infer business success or upload anything to CI.
"""
import argparse
import hashlib
import html
import json
import os
from pathlib import Path
import re
import stat
from urllib.parse import unquote

if __package__:
    from scripts import collect_operator_native_inputs as operator_inputs
else:
    import collect_operator_native_inputs as operator_inputs


# Retain the committed 580 native collector's explicit export roster.
ROOT_FILES = tuple('''source-commit.txt unit-coverage.json compose-layout.json nginx-real.json
storage-direct-real.json storage-relay-real.json federation.json nodes-browser.json nodes-browser.log
native-inventory.json native-inventory.log native-planning.json native-planning.log
native-reservations.json native-reservations.log native-admission.json native-admission.log
native-transfer.json native-transfer.log native-publication.json native-publication.log
player-storage-browser.json player-storage-browser.log native-retirement-runtime.json
native-retirement-runtime.log native-retirement-api.json native-retirement-api.log
retirement-api-https-mysql.json retirement-api-https-mysql.log five-node-traffic.json five-node-traffic.log
filesystem-upload-faults.json filesystem-upload-faults.log migration-browser-native.json
migration-browser-native.log migration-ui.log migration-hour-source-commit.txt migration-hour.json migration-hour.log
native-runner-cleanup.json native-runner-cleanup.log storage-direct-status-concurrent.json
storage-direct-status-after-restart.json storage-relay-status-concurrent.json storage-relay-status-after-restart.json
native-unit-cancel.json native-unit-cancel.log native-unit-cancel-mysql.jsonl native-unit-cancel-mysql.log
native-master-cancel.json native-master-cancel.log native-master-cancel-mysql.jsonl native-master-cancel-mysql.log
operator-sqlite-proof.json operator-mysql-proof.json operator-endpoint-sqlite-proof.json operator-endpoint-mysql-proof.json
player-touch-seek.log
native.log unit.log business-traffic.log certificate-nginx.log certificate-storage.log federation.log s3.log
mysql.log operator.log operator-backup.log operator-endpoint.log operator-discovery.log operator-discovery-inputs.jsonl
operator-prepare.log operator-prepare-inputs.jsonl operator-install.log operator-application.log operator-application-inputs.jsonl'''.split())
PREPARE_CASES = tuple('''test_actual_build_new_service_literal_inputs_and_moved_old_tag
test_real_build_failure_preserves_services_data_and_diagnostics
test_unsupported_persistent_addition_fails_before_build_or_any_new_service
test_real_cold_capture_of_published_generation4_database_and_resume
test_real_mysql_generation4_cold_capture_empty_target_restore_and_resume
test_real_published_sqlite_generation4_to_current_database_prepare
test_real_published_mysql_generation4_to_current_database_prepare
test_real_host_transaction_prepares_published_sqlite_database
test_real_host_transaction_prepares_published_mysql_database
test_real_candidate_configuration_failure_preserves_running_old_deployment
test_real_fenced_sqlite_application_after_database_preparation
test_real_fenced_mysql_application_after_database_preparation'''.split())
INSTALL_CASES = tuple('''test_business_sqlite_package test_business_mysql_package
test_direct_storage_package test_relay_storage_package
test_business_sqlite_owned_resources test_business_mysql_owned_resources
test_direct_storage_owned_resources test_relay_storage_owned_resources
test_business_sqlite_first_start test_business_mysql_first_start
test_direct_storage_first_start test_relay_storage_first_start
test_installed_sqlite_controlled_recovery test_installed_mysql_controlled_recovery
test_business_sqlite_entry_install test_business_mysql_entry_install
test_direct_storage_entry_install test_relay_storage_entry_install
test_same_directory_foreign_project_refuses_before_build'''.split())
APPLICATION_CASES = tuple('''test_actual_old_sqlite_application_to_prepared_current_generation
test_actual_old_mysql_application_to_prepared_current_generation
test_actual_old_sqlite_master_preserves_fixed_role_identity_and_resources
test_actual_old_mysql_master_preserves_fixed_role_identity_and_resources
test_actual_old_direct_storage_preserves_fixed_follower_identity
test_actual_old_relay_storage_preserves_fixed_follower_identity
test_actual_old_paired_direct_upgrade_preserves_relationship_and_media
test_actual_old_paired_relay_upgrade_preserves_relationship_and_media
test_actual_old_paired_direct_restore_preserves_relationship_and_media
test_actual_old_paired_relay_restore_preserves_relationship_and_media
test_controlled_sqlite_restore_preserves_failed_state_and_payload_inodes
test_controlled_mysql_restore_preserves_failed_state_and_payload_inodes
test_controlled_sqlite_master_restore_preserves_fixed_identity_and_resources
test_controlled_mysql_master_restore_preserves_fixed_identity_and_resources
test_early_sqlite_abort_preserves_incomplete_snapshot_and_original_resources
test_early_mysql_abort_preserves_incomplete_snapshot_and_original_resources
test_staged_host_entry_upgrades_old_sqlite_application
test_staged_host_entry_upgrades_old_mysql_application_and_adds_init
test_staged_recover_preserves_sqlite_original_resources
test_staged_recover_preserves_mysql_original_resources'''.split())
S3_FILES = tuple('''provider-initial.json provider-initial.log provider-restart.json provider-restart-client.log
transactions.json transactions.log browser.json browser.log credential-revocation-prepare.json
credential-revocation-prepare.log credential-revocation-provider.json credential-revocation.json
credential-revocation.log'''.split())
MAX_BYTES = 64 * 1024 * 1024
SECRET_KEYS = frozenset(('password', 'secret', 'secret_key', 'access_key', 'access_key_id', 'admin_key',
    'credential', 'credentials', 'session_token', 'access_token', 'token', 'authorization', 'cookie',
    'set_cookie', 'x_csrf_token', 'capability', 'presigned_url', 'private_key', 'client_secret'))
SECRET_NAMES = frozenset(name.replace('_', '') for name in SECRET_KEYS)
SECRET_ASSIGNMENT = re.compile(r'''(?ix)(?<![a-z0-9_])(?:["']?)(?:password|secret(?:[_-]?key)?|private[_-]?key|client[_-]?secret|admin[_-]?key|access[_-]?key(?:[_-]?id)?|
    session[_-]?token|access[_-]?token|token|authorization|cookie|set[_-]?cookie|x[_-]?csrf[_-]?token|credential(?:s)?|capability|presigned[_-]?url)
    ["']?\s*[:=]\s*[^\s,}]''')


def allowlist():
    result = {name: name for name in ROOT_FILES}
    for pair in ('DR', 'DD', 'RD', 'RR'):
        for name in (f'native-partial-cancel-{pair}.json', f'native-partial-cancel-{pair}.log',
                f'native-partial-cancel-mysql-{pair}.jsonl', f'native-partial-cancel-mysql-{pair}.log'):
            result[name] = name
    result['migration-ui/result.json'] = 'migration-ui-contract.json'
    result['private/player-touch-seek/result.json'] = 'player-touch-seek.json'
    result.update({f'unit-shards/shard-{index}.json': f'unit-shard-{index}.json' for index in range(4)})
    for kind, cases in (('operator-prepare', PREPARE_CASES), ('operator-install', INSTALL_CASES),
            ('operator-application', APPLICATION_CASES)):
        for case in cases:
            result[f'private/{kind}/{case}/result.json'] = f'operator-{case}.json'
            extras = ('http-inputs.jsonl', 'directory-conflict-inputs.jsonl', 'entry-bootstrap-docker-inputs.jsonl',
                'entry-target-docker-inputs.jsonl') if kind == 'operator-install' else (
                ('http-inputs.jsonl', 'inventory-inputs.jsonl') if kind == 'operator-application' else ())
            result.update({f'private/{kind}/{case}/{name}': f'operator-{case}-{name}' for name in extras})
    result.update({f's3/{name}': f's3-{name}' for name in S3_FILES})
    result.update({f'business-traffic/{name}': f'business-traffic-{name}' for name in ('native.json', 'resources.jsonl', 'nginx.log')})
    return result


def _safe_path(path):
    if not path.is_absolute() or '..' in path.parts or any(item.is_symlink() or getattr(item, 'is_junction', lambda: False)() for item in (path, *path.parents)):
        raise ValueError('unsafe_path')


def _read(path, limit=MAX_BYTES):
    _safe_path(path)
    before = path.stat()
    if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size > limit:
        raise ValueError('unsafe_or_oversized_file')
    descriptor = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
    with os.fdopen(descriptor, 'rb') as stream:
        opened = os.fstat(stream.fileno())
        if (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns) != (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns):
            raise ValueError('source_changed')
        raw = stream.read(limit + 1)
        after = os.fstat(stream.fileno())
    # Windows path stat and fd stat expose different ctime semantics. Compare
    # the same descriptor before/after while retaining path identity and mtime.
    if len(raw) > limit or (after.st_size, after.st_mtime_ns, after.st_ctime_ns) != (opened.st_size, opened.st_mtime_ns, opened.st_ctime_ns):
        raise ValueError('source_changed')
    return raw


def _text(raw):
    text = raw.decode('utf-8')
    if '\x00' in text:
        raise ValueError('non_text_content')
    return text


def _sensitive(text, values=None):
    normalized = re.sub(r'\\u([0-9a-fA-F]{4})', lambda match: chr(int(match[1], 16)), text)
    for _ in range(2):
        normalized = unquote(html.unescape(normalized))
    if re.search(r'PRIVATE KEY-----|X-Amz-(?:Signature|Credential|Security-Token)\s*=|AWS4-HMAC-SHA256\s+Credential=', normalized, re.I):
        return True
    if re.search(r'[?&](?:token|access_token|signature|sig|capability|grant)\s*=', normalized, re.I):
        return True
    if values is None:
        return bool(SECRET_ASSIGNMENT.search(normalized))
    def unsafe(value):
        if isinstance(value, dict):
            for key, item in value.items():
                name = str(key).lower().replace('-', '').replace('_', '')
                if name in SECRET_NAMES and item not in (None, False, True, ''):
                    redacted = isinstance(item, dict) and set(item) <= {'redacted', 'sha256'} and item.get('redacted') is True and re.fullmatch('[0-9a-f]{64}', item.get('sha256', ''))
                    if not redacted:
                        return True
                if unsafe(item):
                    return True
        elif isinstance(value, list):
            return any(unsafe(item) for item in value)
        elif isinstance(value, str):
            return bool(SECRET_ASSIGNMENT.search(value))
        return False
    return any(unsafe(value) for value in values)


def _parse(text, suffix):
    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError('duplicate_json_key')
            result[key] = value
        return result
    if suffix == '.json':
        return [json.loads(text, object_pairs_hook=unique_object)]
    if suffix == '.jsonl':
        lines = text.splitlines()
        if not lines:
            raise ValueError('empty_jsonl')
        return [json.loads(line, object_pairs_hook=unique_object) for line in lines]
    return None


def _write(destination, name, raw):
    descriptor = os.open(destination / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    with os.fdopen(descriptor, 'wb') as stream:
        if hasattr(os, 'fchmod'):
            os.fchmod(stream.fileno(), 0o600)
        else:
            (destination / name).chmod(0o600)
        stream.write(raw)


def collect(evidence, destination):
    evidence, destination = Path(evidence), Path(destination)
    _safe_path(evidence)
    _safe_path(destination)
    if not evidence.is_dir() or destination.exists() or not destination.parent.is_dir():
        raise ValueError('existing_evidence_and_fresh_destination_required')
    if evidence == destination or evidence in destination.parents or destination in evidence.parents:
        raise ValueError('overlapping_directories')
    destination.mkdir(mode=0o700)
    destination.chmod(0o700)
    entries = []
    def export(source, target, *, value=None, private=False):
        entry = dict(source=source.relative_to(evidence).as_posix(), destination=target)
        try:
            raw = _read(source, 1024 * 1024 if private else MAX_BYTES)
            entry.update(bytes=len(raw), sha256=hashlib.sha256(raw).hexdigest())
            text = _text(raw)
            try:
                parsed = _parse(text, source.suffix)
                if private and parsed != [value]:
                    raise ValueError('operator_receipt_changed')
            except (ValueError, RecursionError):
                if private:
                    raise ValueError('invalid_operator_receipt') from None
                if _sensitive(text):
                    raise ValueError('sensitive_content') from None
                target += '.raw.txt'
                _write(destination, target, raw)
                entry.update(destination=target, status='invalid_json', error='invalid_json_preserved_as_text')
            else:
                if _sensitive(text, parsed):
                    raise ValueError('sensitive_content')
                _write(destination, target, raw)
                entry['status'] = 'copied'
        except FileNotFoundError:
            entry['status'] = 'missing'
        except (OSError, ValueError, RecursionError) as error:
            entry.update(status='rejected', error=str(error) if isinstance(error, ValueError) else type(error).__name__)
        entries.append(entry)

    for source, target in sorted(allowlist().items()):
        export(evidence / source, target)

    # Reuse the existing CLI schemas; never publish request bodies, env, CA,
    # locks or raw child logs merely because they are beside an argv receipt.
    for kind in ('operator-install', 'operator-application'):
        base = evidence / 'private' / kind
        if not base.exists() and not base.is_symlink():
            continue
        try:
            _safe_path(base)
            cases = sorted(base.iterdir())
        except (OSError, ValueError) as error:
            entries.append(dict(source=f'private/{kind}', status='rejected', error=type(error).__name__))
            continue
        for case in cases:
            if not re.fullmatch('test_[a-z0-9_]+', case.name):
                continue
            try:
                _safe_path(case)
                paths = [path for path in case.glob('*-input.json') if re.fullmatch('[a-z0-9_-]+-input.json', path.name)]
                entry_host = case / 'entry-host'
                _safe_path(entry_host)
                for attempt in entry_host.glob('attempt-*'):
                    if not re.fullmatch('attempt-[a-z0-9_-]+', attempt.name):
                        continue
                    _safe_path(attempt)
                    paths.extend(attempt / name for name in ('handoff-input.json', 'fixture-exec-input.json') if (attempt / name).exists())
                for path in sorted(paths):
                    try:
                        raw = _read(path, 1024 * 1024)
                        value = json.loads(_text(raw))
                        if not isinstance(value, dict):
                            raise ValueError('invalid_operator_receipt')
                        bridge = bool(re.fullmatch('cli-call-[0-9a-f]{16}-input.json', path.name))
                        if bridge:
                            if set(value) - operator_inputs.BRIDGE_FIELDS or not operator_inputs._arguments(value.get('real_cli_main_arguments')):
                                raise ValueError('invalid_operator_bridge')
                        elif not operator_inputs._argv(value.get('argv')):
                            argv = value.get('argv')
                            if isinstance(argv, list) and any(isinstance(item, str) and Path(item).name in operator_inputs.CLI_SCRIPTS for item in argv):
                                raise ValueError('invalid_operator_arguments')
                            continue
                        elif set(value) - operator_inputs.INPUT_FIELDS:
                            raise ValueError('invalid_operator_fields')
                        if 'simulated_old_discovery_rejects' in value and value['simulated_old_discovery_rejects'] != 'media-init':
                            raise ValueError('invalid_operator_context')
                        if 'product_argv' in value and not operator_inputs._argv(value['product_argv']):
                            raise ValueError('invalid_product_arguments')
                        name = 'operator-' + case.name + '-' + path.relative_to(case).as_posix().replace('/', '-')
                        export(path, name, value=value, private=True)
                        result = path.with_name(path.name.removesuffix('-input.json') + '-result.json')
                        if not bridge and result.exists():
                            observed = json.loads(_text(_read(result, 1024 * 1024)))
                            if not isinstance(observed, dict) or set(observed) - operator_inputs.RESULT_FIELDS or observed.get('argv') != value['argv']:
                                raise ValueError('operator_result_mismatch')
                            export(result, name.removesuffix('-input.json') + '-result.json', value=observed, private=True)
                    except (OSError, ValueError, RecursionError) as error:
                        entries.append(dict(source=path.relative_to(evidence).as_posix(), status='rejected', error='invalid_operator_receipt'))
            except (OSError, ValueError):
                entries.append(dict(source=case.relative_to(evidence).as_posix(), status='rejected', error='unsafe_operator_directory'))
    manifest = dict(version=1, scope='Manual development native diagnostics; not acceptance or CI release proof',
        complete=all(entry['status'] in {'copied', 'missing'} for entry in entries),
        copied=sum(entry['status'] == 'copied' for entry in entries), entries=entries)
    _write(destination, 'manifest.json', (json.dumps(manifest, sort_keys=True, indent=2) + '\n').encode())
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('evidence', type=Path)
    parser.add_argument('destination', type=Path)
    options = parser.parse_args()
    manifest = collect(options.evidence, options.destination)
    print(json.dumps(dict(complete=manifest['complete'], copied=manifest['copied'], destination=str(options.destination))))
    raise SystemExit(0 if manifest['complete'] else 1)


if __name__ == '__main__':
    main()
