"""Root-owned governed task-14 assembly; no child domain mutation authority.

An external reviewed manifest hash binds source, image and explicit inventory.
The same consumer/gate/lifecycle runs in qualification and production. Only the
derived-state destination and explicit trigger differ. Qualification/operator
runs reuse a sealed publication; only scheduled runs request normal exports.
Python 3.8 compatible. --dry-check is strictly read-only.
"""
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import types
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

sys.dont_write_bytecode = True
BASE = Path('/volume2/clank/motherclank')
PACKAGE = BASE / 'deploy/cops-000074-governed'
MANIFEST_NAME = 'production-manifest.json'
RUNNER_NAME = 'nas_production_runner.py'
LIBRARY_NAME = 'nas_production_library.py'
TOPOLOGY_NAME = 'observer_topology.py'
LAUNCHER_NAME = 'scheduled-run.sh'
SPEC_NAME = 'snapshot-spec.json'
REGISTRY_NAME = 'adapter-registry.json'
INVENTORY_NAME = 'observer-inventory.json'
MANIFEST_SHA = None
HELPER = BASE / 'build/cops-000081/consumer-integration/feature_phone_request_export_cops_000081.py'
HELPER_SHA = 'a77fcf0f44761ce90530c82f5e9b9d410c891e9985fb2f40edcff9eb4a5faa93'
EXPECTED = {
    'format_version': '1.0', 'mission': 'COPS-000074', 'deployment_root': str(PACKAGE),
    'adapter_revision': '0770dd5f15be8a4a89bc43e5dd9644674d6683c0',
    'exporter_revision': '99405de0829e5baade4151eb1c2a0f2374b49613',
    'export_image': 'sha256:eccdb14e96634a255ce37e6ea9b9391997c8845710fe3fd8cea52129c3612478',
    'observer_contract_version': '0.2', 'snapshot_contract_version': '1.0',
    'artifact_scope': 'COPS-000074-governed-production',
}
STATE = BASE / 'state/task14-governed-cops-000074'
RESULTS = STATE / 'attempts'
ORPHAN = STATE / 'orphan-risk.json'
MARKER = BASE / 'deploy/authority-cops-000074'
LIVE_LAUNCHER = BASE / 'deploy/scheduled-run.sh'
LOCK = BASE / 'state/motherclank.lock'
LIVE_VAR = BASE / 'var'
SHA = re.compile(r'[0-9a-f]{64}\Z')
CODE = re.compile(r'[A-Z0-9_]{1,100}\Z')
READ_LIMIT = 16 * 1024 * 1024
P = None


class RunnerError(Exception):
    """Bounded, secret-safe host authority error."""


def require(ok, code):
    if not ok:
        raise RunnerError(code)


def utc_now():
    return datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')


def plain(path, owner=None, mode=None, directory=False):
    require(not any(p.is_symlink() for p in (path, *path.parents)), 'SYMLINKED_AUTHORITY_PATH')
    info = path.lstat()
    require((stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode))
            and (directory or info.st_nlink == 1), 'UNSAFE_AUTHORITY_FILE')
    if owner is not None:
        require((info.st_uid, info.st_gid) == owner, 'AUTHORITY_OWNER_DRIFT')
    if mode is not None:
        require(stat.S_IMODE(info.st_mode) == mode, 'AUTHORITY_MODE_DRIFT')
    return info


def digest(path):
    before = plain(path)
    fd = os.open(str(path), os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
    try:
        opened = os.fstat(fd)
        require((before.st_dev, before.st_ino) == (opened.st_dev, opened.st_ino), 'HASH_INODE_DRIFT')
        h = hashlib.sha256()
        with os.fdopen(fd, 'rb', closefd=False) as stream:
            for chunk in iter(lambda: stream.read(1048576), b''):
                h.update(chunk)
        after = os.fstat(fd)
        require((before.st_size, before.st_mtime_ns, before.st_ctime_ns) ==
                (after.st_size, after.st_mtime_ns, after.st_ctime_ns), 'HASH_INPUT_CHANGED')
        return h.hexdigest()
    finally:
        os.close(fd)


def read_json(path):
    require(plain(path).st_size <= READ_LIMIT, 'JSON_INPUT_TOO_LARGE')
    before = digest(path)
    def pairs(items):
        value = {}
        for key, item in items:
            require(key not in value, 'DUPLICATE_JSON_FIELD')
            value[key] = item
        return value
    value = json.loads(path.read_text(encoding='utf-8'), object_pairs_hook=pairs)
    require(isinstance(value, dict) and digest(path) == before, 'JSON_INPUT_DRIFT')
    return value


def load_package(root):
    """Authenticate the externally reviewed release hash BEFORE importing code."""
    plain(root, (0, 0), 0o700, True)
    manifest_path = root / MANIFEST_NAME
    plain(manifest_path, (0, 0), 0o400)
    require(type(MANIFEST_SHA) is str and SHA.fullmatch(MANIFEST_SHA)
            and digest(manifest_path) == MANIFEST_SHA, 'RELEASE_MANIFEST_PIN_DRIFT')
    doc = read_json(manifest_path)
    require(all(doc.get(k) == v and type(doc.get(k)) is str for k, v in EXPECTED.items()),
            'PACKAGE_IDENTITY_DRIFT')
    require(type(doc.get('source_revision')) is str
            and re.fullmatch(r'[0-9a-f]{40}', doc['source_revision'])
            and type(doc.get('image')) is str and re.fullmatch(r'sha256:[0-9a-f]{64}', doc['image']),
            'RELEASE_SOURCE_OR_IMAGE_INVALID')
    pins = doc.get('files')
    mandatory = {SPEC_NAME, REGISTRY_NAME, INVENTORY_NAME, LIBRARY_NAME, RUNNER_NAME,
                 TOPOLOGY_NAME}
    require(type(pins) is dict and set(pins) == mandatory and
            all(type(h) is str and SHA.fullmatch(h) for h in pins.values()), 'PACKAGE_FILE_SET_INVALID')
    require({p.name for p in root.iterdir()} == mandatory | {MANIFEST_NAME}, 'UNLISTED_PACKAGE_MEMBER')
    for name, sha in pins.items():
        readable = name in (SPEC_NAME, REGISTRY_NAME, INVENTORY_NAME)
        plain(root / name, (0, 10001) if readable else (0, 0), 0o440 if readable else 0o400)
        require(digest(root / name) == sha, 'PACKAGE_FILE_HASH_DRIFT')
    require(doc.get('request_helper') == {'path': str(HELPER), 'sha256': HELPER_SHA},
            'REQUEST_HELPER_PIN_DRIFT')
    plain(HELPER.parent, (0, 10001), 0o750, True)
    plain(HELPER, (0, 10001), 0o440)
    require(digest(HELPER) == HELPER_SHA, 'REQUEST_HELPER_HASH_DRIFT')
    def verified_module(name, filename):
        module = types.ModuleType(name)
        module.__file__ = str(root / filename)
        source = (root / filename).read_bytes()
        require(hashlib.sha256(source).hexdigest() == pins[filename], 'CODE_PREIMPORT_DRIFT')
        exec(compile(source, module.__file__, 'exec'), module.__dict__)
        return module
    policy_module = verified_module('governed_observer_topology', TOPOLOGY_NAME)
    topology = policy_module.Topology(read_json(root / INVENTORY_NAME),
        adapter_sha=doc['adapter_revision'], image_id=doc['image'])
    topology.validate_spec(read_json(root / SPEC_NAME))
    topology.validate_registry(read_json(root / REGISTRY_NAME))
    library = verified_module('governed_production_library', LIBRARY_NAME)
    library.configure(root, doc, topology, RESULTS, ORPHAN)
    library.FILE_PINS[HELPER] = HELPER_SHA
    library.TOPOLOGY = topology
    require(digest(manifest_path) == MANIFEST_SHA, 'PACKAGE_MANIFEST_CHANGED')
    return library, doc, MANIFEST_SHA


def load_publisher_library():
    blob = HELPER.read_bytes()
    require(hashlib.sha256(blob).hexdigest() == HELPER_SHA, 'PUBLISHER_HELPER_IMPORT_DRIFT')
    helper = types.ModuleType('fixed_publisher_boundary_cops_000081')
    helper.__file__ = str(HELPER)
    exec(compile(blob, helper.__file__, 'exec'), helper.__dict__)
    return helper


def publisher_boundary():
    """Dry-only fixed publisher admission; no DB connection, lock or request.

    Reuse the exact child-owned helper's qualified read-only admission routines.
    Do not initialize_control, call main/request_export, or bypass retention.
    Sealed historical export files may be hashed; the canonical DB is stat-only.
    """
    helper = load_publisher_library()
    try:
        _guard, prep = helper.load_runtime()
        record, _child_authority, _acl = helper.layout_admission(prep)
        source = helper.SOURCE.lstat()
        require(stat.S_ISREG(source.st_mode) and source.st_nlink == 1,
                'PUBLISHER_CANONICAL_SOURCE_METADATA_INVALID')
        pins = helper.retention_admission(source.st_size, prep)
        require(helper.digest(helper.PREFLIGHT) == helper.PREFLIGHT_SHA ==
                'cedc18d482ee245e3e0c4d1a8ecf7b6bc1cd5a44d6ddaf085d5b7a7d09d99dbe',
                'PUBLISHER_PREFLIGHT_HASH_DRIFT')
        accepted_count = sum(1 for _ in (helper.ROOT / 'accepted').iterdir())
        private_count = sum(1 for _ in (helper.ROOT / 'staging').iterdir())
        private_count += sum(1 for _ in (helper.ROOT / 'failed').iterdir())
        require(record['live_db_opened'] is False, 'PUBLISHER_RECORD_BOUNDARY_DRIFT')
        return {'status': 'PUBLISHER_BOUNDARY_DRY_VALIDATED', 'preflight_sha256': helper.PREFLIGHT_SHA,
            'accepted_count': accepted_count, 'private_count': private_count,
            'pin_count': len(pins['attempt_ids']), 'canonical_source_access': 'STAT_ONLY_NO_SQLITE_OPEN',
            'export_requested': False, 'control_initialized': False, 'retention_changed': False}
    except (helper.Fatal, helper.LaneFailure) as exc:
        code = str(exc)
        raise RunnerError(code if CODE.fullmatch(code) else 'PUBLISHER_BOUNDARY_DRY_FAILED') from None


def scheduler_truth(library, enabled_required):
    """Same existing daily cadence; dry-check allows old enabled/disabled."""
    if enabled_required:
        return library.scheduler_truth()
    result = {}
    for tid, name, owner, command in (
        (14, 'motherclank-nas-partial-cops-000072', 'root', '/bin/sh /volume2/clank/motherclank/deploy/scheduled-run.sh'),
        (15, 'oem-radar-canonical-cops-000072', 'root', '/bin/sh /volume2/clank/oem-radar/canonical-cops-000072/deploy/scheduled-run.sh')):
        output = library.checked([library.DSM, '--get', 'id=' + str(tid)])
        expected = {'ID': str(tid), 'Name': name, 'Owner': owner, 'Command': command}
        require(all(library.bracket(output, k) == v for k, v in expected.items()), 'TASK_AUTHORITY_DRIFT')
        state = library.bracket(output, 'State')
        require(state in ('enabled', 'disabled') if tid == 14 else state == 'enabled', 'TASK_STATE_DRIFT')
        clocks = re.findall(r'^\s*Run time:\s*\[(\d{1,2})\]:\[(\d{1,2})\]\s*$', output, re.M)
        require(len(clocks) == 1, 'TASK_CLOCK_AMBIGUOUS')
        if tid == 14:
            require(library.bracket(output, 'Type') == 'daily' and clocks[0] == ('11', '45'), 'TASK14_CADENCE_DRIFT')
        else:
            require(clocks[0][1] == '50' and (library.bracket(output, 'Type') == 'hourly'
                    or re.search(r'^\s*Repeat every\s*(?:\[1\]\s*(?:hour|hr)|\[60\]\s*min)', output, re.M | re.I)),
                    'TASK15_CADENCE_DRIFT')
        stable = '\n'.join(line for line in output.splitlines() if not re.match(
            r'^\s*(Last Run Time|Next Run Time|Next Trigger|Status|Last Result):', line))
        result[str(tid)] = hashlib.sha256(stable.encode('utf-8')).hexdigest()
    return result


def process_identity(proc):
    """PID/parent/start ticks; comm may contain parentheses, so split at last )."""
    raw = (proc / 'stat').read_bytes()
    require(len(raw) <= 16384 and b')' in raw and b'(' in raw, 'HOST_PROCESS_STAT_INVALID')
    prefix, suffix = raw.rsplit(b')', 1)
    pid = int(prefix.split(b'(', 1)[0].strip())
    fields = suffix.split()
    require(len(fields) >= 20 and pid == int(proc.name), 'HOST_PROCESS_STAT_INVALID')
    return (pid, int(fields[1]), int(fields[19]))


def process_ancestors(proc_root, current):
    """Read the actual parent chain, not a UID-wide or guessed PID exemption."""
    identities, seen = {}, set()
    identity = process_identity(proc_root / str(current))
    self_identity = identity
    for _ in range(64):
        pid, parent, _start = identity
        require(pid not in seen, 'HOST_ANCESTRY_LOOP')
        seen.add(pid)
        if parent == 0:
            break
        identity = process_identity(proc_root / str(parent))
        identities[parent] = identity
    else:
        raise RunnerError('HOST_ANCESTRY_LIMIT')
    require(process_identity(proc_root / str(current)) == self_identity, 'HOST_ANCESTRY_CHANGED')
    return identities


def approved_launcher_shell(proc, argv, ancestor_identity):
    """Only root shells in that exact stable ancestry invoking the fixed script."""
    if process_identity(proc) != ancestor_identity:
        return False
    fields = argv.split(b'\0')
    if fields and not fields[-1]:
        fields.pop()
    try:
        args = [part.decode('utf-8') for part in fields]
    except UnicodeError:
        return False
    shell = args[0] if args else ''
    approved_shells = {'/bin/sh', '/bin/bash', '/bin/ash', 'sh', 'bash', 'ash'}
    if shell not in approved_shells:
        return False
    invocation = '/bin/sh ' + str(LIVE_LAUNCHER)
    direct = len(args) == 2 and args[1] == str(LIVE_LAUNCHER)
    nested = len(args) == 3 and args[1] == '-c' and args[2] in (invocation, 'exec ' + invocation)
    if not (direct or nested):
        return False
    status = (proc / 'status').read_bytes()
    uid = re.search(rb'^Uid:\s*(\d+)\s+(\d+)\s+(\d+)\s+(\d+)\s*$', status, re.M)
    if uid is None or any(int(v) != 0 for v in uid.groups()):
        return False
    executable = os.readlink(str(proc / 'exe'))
    allowed_executables = {os.path.realpath(path) for path in ('/bin/sh', '/bin/bash', '/bin/ash')}
    if executable not in allowed_executables:
        return False
    return process_identity(proc) == ancestor_identity


def no_workers(library):
    """Inspect only; do not print command lines, env or untrusted payloads."""
    require(not ORPHAN.exists() and not ORPHAN.is_symlink(), 'UNRESOLVED_PRODUCTION_ORPHAN')
    historical = BASE / 'build/cops-000081/sealed-proof-coordinator-spec-normalized/orphan-risk.json'
    require(not historical.exists() and not historical.is_symlink(), 'UNRESOLVED_STAGING_ORPHAN')
    ids = library.checked([library.DOCKER, 'ps', '--all', '--no-trunc', '-q']).splitlines()
    require(len(ids) <= 256, 'CONTAINER_CENSUS_LIMIT')
    for cid in ids:
        require(SHA.fullmatch(cid), 'CONTAINER_CENSUS_ID_INVALID')
        row = library.inspect_container(cid)
        cfg = row.get('Config') or {}
        scope = (cfg.get('Labels') or {}).get('clank.scope', '')
        relevant = row.get('Image') in (library.IMAGE, library.EXPORT_IMAGE,
                'sha256:4fa465d97e841c53a04ee35b06a75b91be619733ac8b349a64a630ff73b24964')
        state = row.get('State') or {}
        uncertain = state.get('Running') is True or state.get('Paused') is True or state.get('Restarting') is True
        uncertain = uncertain or state.get('Status') not in ('exited', 'dead')
        # Retained stopped staging containers are evidence, not authority. A
        # production-owned leftover, even exited, is unexplained/orphan risk.
        var_mounted = any(type(m.get('Source')) is str and
                (m['Source'] == str(LIVE_VAR) or m['Source'].startswith(str(LIVE_VAR) + '/'))
                for m in row.get('Mounts', []))
        require(not ((relevant or var_mounted) and uncertain) and scope not in ('COPS-000081-task14-production', 'COPS-000074-governed-production'),
                'RELEVANT_CONTAINER_PRESENT')
    # Exempt this process plus ONLY approved root launcher shells in its actual
    # stable ancestry. DSM can use two nested shells; siblings remain workers.
    current = os.getpid()
    proc_root = Path('/proc')
    ancestors = process_ancestors(proc_root, current)
    needles = (str(HELPER).encode(), b'nas_feature_phone_consumer.py',
               b'run_sealed_consumer_proofs_cops_000081.py', b'motherclank_partial_input_snapshot_cops_000072.py',
               str(LIVE_LAUNCHER).encode(), RUNNER_NAME.encode(), b'-m\x00motherclank.cli')
    for proc in proc_root.iterdir():
        if not proc.name.isdigit() or int(proc.name) == current:
            continue
        try:
            argv = (proc / 'cmdline').read_bytes()
        except FileNotFoundError:
            continue
        except OSError:
            raise RunnerError('HOST_WORKER_CENSUS_UNREADABLE') from None
        if any(token in argv for token in needles):
            try:
                allowed = int(proc.name) in ancestors and approved_launcher_shell(
                    proc, argv, ancestors[int(proc.name)])
            except FileNotFoundError:
                continue
            except OSError:
                raise RunnerError('HOST_WORKER_CENSUS_UNREADABLE') from None
            require(allowed, 'RELEVANT_HOST_WORKER_PRESENT')
    require(process_ancestors(proc_root, current) == ancestors, 'HOST_ANCESTRY_CHANGED')


def launcher_bytes(package_sha):
    return ('#!/bin/sh\nset -eu\numask 077\n'
        'export TZ=UTC LC_ALL=C PYTHONDONTWRITEBYTECODE=1\n'
        'exec /bin/python3 ' + str(PACKAGE / RUNNER_NAME) +
        ' --run --package-sha256 ' + package_sha + '\n').encode('utf-8')


def marker_truth(library, doc, package_sha):
    plain(MARKER, (0, 0), 0o600)
    marker = read_json(MARKER)
    wanted = {'format_version': '1.0', 'mission': 'COPS-000074', 'package_manifest_sha256': package_sha}
    wanted.update({k: doc[k] for k in ('source_revision', 'adapter_revision', 'image')})
    require(set(marker) == set(wanted) | {'backup', 'manifest_sha256'}
            and all(marker.get(k) == v for k, v in wanted.items()), 'ADMISSION_IDENTITY_DRIFT')
    backup = marker.get('backup')
    require(type(backup) is str and type(marker.get('manifest_sha256')) is str
            and SHA.fullmatch(marker['manifest_sha256']), 'ROLLBACK_BINDING_INVALID')
    transaction = Path(backup)
    require(transaction.parent == BASE / 'backups'
            and transaction.name.startswith('cops-000074-pre-admission-'), 'ROLLBACK_PATH_DRIFT')
    plain(transaction, (0, 0), 0o700, True)
    transaction_manifest = transaction / 'rollback-manifest.json'
    require(digest(transaction_manifest) == marker['manifest_sha256'], 'ROLLBACK_MANIFEST_DRIFT')
    rollback = read_json(transaction_manifest)
    require(digest(transaction / 'five-child-rollback.tar.gz') == rollback['archive_sha256'],
            'ROLLBACK_ARCHIVE_DRIFT')
    require(digest(LIVE_LAUNCHER) == hashlib.sha256(launcher_bytes(package_sha)).hexdigest(),
            'LIVE_LAUNCHER_HASH_DRIFT')
    return {'marker_sha256': digest(MARKER), 'backup': backup, 'manifest_sha256': marker['manifest_sha256']}


def authority(library, doc, sha, runtime):
    root = library.INVENTORY.parent
    plain(root, (0, 0), 0o700, True)
    plain(root / MANIFEST_NAME, (0, 0), 0o400)
    require({p.name for p in root.iterdir()} == set(doc['files']) | {MANIFEST_NAME}, 'UNLISTED_PACKAGE_MEMBER')
    require(digest(root / MANIFEST_NAME) == sha, 'PACKAGE_MANIFEST_DRIFT')
    for name, expected_sha in doc['files'].items():
        readable = name in (SPEC_NAME, REGISTRY_NAME, INVENTORY_NAME)
        plain(root / name, (0, 10001) if readable else (0, 0), 0o440 if readable else 0o400)
        require(digest(root / name) == expected_sha, 'PINNED_FILE_CHANGED_DURING_RUN')
    plain(HELPER.parent, (0, 10001), 0o750, True)
    plain(HELPER, (0, 10001), 0o440)
    require(digest(HELPER) == HELPER_SHA, 'PINNED_HELPER_CHANGED_DURING_RUN')
    acl = {str(path): library.linux_acl(path) for path in
        (root, root / SPEC_NAME, root / REGISTRY_NAME, root / INVENTORY_NAME, HELPER.parent, HELPER)}
    library.image_truth()
    export = library.json_unique(library.checked([library.DOCKER, 'image', 'inspect', library.EXPORT_IMAGE]))
    require(type(export) is list and len(export) == 1 and export[0].get('Id') == library.EXPORT_IMAGE,
            'EXPORT_IMAGE_ID_DRIFT')
    cfg = export[0].get('Config') or {}
    labels = cfg.get('Labels') or {}
    require(cfg.get('User') == '10001:10001'
            and labels.get('org.opencontainers.image.revision') == library.EXPORTER_SHA
            and labels.get('clank.observer_finalizer_sha256') ==
                '1a02db7ab0daadc3e7d5aaeca45eee1daf4bfccdcaa5b76826555d6ac6114fbe',
            'EXPORT_IMAGE_REVISION_OR_FINALIZER_DRIFT')
    schedule = scheduler_truth(library, runtime)
    # Even the frozen enabled-only scheduler parser must reject daily repeats.
    output = library.checked([library.DSM, '--get', 'id=14'])
    require(not re.search(r'^\s*Repeat every\s*\[', output, re.M), 'TASK14_REPEAT_NOT_APPROVED')
    no_workers(library)
    marker = marker_truth(library, doc, sha) if runtime else None
    plain(LIVE_VAR, (10001, 10001), directory=True)
    return {'scheduler': schedule, 'marker': marker, 'package_manifest_sha256': sha,
            'pinned_linux_acl': acl,
            'pinned_path_identity': {str(p): list(library.path_identity(p))
                for p in (root, HELPER.parent, HELPER, LOCK, LIVE_VAR)},
            'task14_runtime_enabled_required': runtime, 'hetzner_current_state_claimed': False}


@contextmanager
def live_lock(library):
    """Same existing inode; O_RDONLY avoids truncation/creation of the lock."""
    import fcntl
    before = plain(LOCK)
    fd = os.open(str(LOCK), os.O_RDONLY | os.O_NOFOLLOW)
    try:
        opened = os.fstat(fd)
        require((before.st_dev, before.st_ino) == (opened.st_dev, opened.st_ino), 'LIVE_LOCK_REPLACED')
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise RunnerError('LIVE_LOCK_BUSY') from None
        yield
        after = plain(LOCK)
        require((before.st_dev, before.st_ino) == (after.st_dev, after.st_ino), 'LIVE_LOCK_REPLACED')
    finally:
        os.close(fd)


def validate_dispatch(library, receipt, context, receipt_sha, observed_at, invocation_started):
    """No unvalidated status branch and never a last-good artifact fallback."""
    require(type(receipt) is dict and set(receipt) == library.RECEIPT_KEYS
            and type(context) is dict and set(context) == library.CONTEXT_KEYS, 'REQUEST_CONTRACT_DRIFT')
    require(type(receipt_sha) is str and SHA.fullmatch(receipt_sha)
            and context['receipt_sha256'] == receipt_sha, 'RECEIPT_HASH_DRIFT')
    require(receipt['request_format_version'] == '1.0'
            and receipt['exporter_revision'] == library.EXPORTER_SHA
            and receipt['export_image'] == library.EXPORT_IMAGE
            and receipt['deployed_revision'] == library.DEPLOYED_SHA, 'REQUEST_IDENTITY_DRIFT')
    require(type(receipt['request_id']) is str and library.REQUEST.fullmatch(receipt['request_id'])
            and context['request_id'] == receipt['request_id']
            and context['request_started_at'] == receipt['request_started_at'], 'REQUEST_CONTEXT_DRIFT')
    require(type(context['prior_attempt_id']) is str and library.ATTEMPT.fullmatch(context['prior_attempt_id']),
            'PRIOR_ATTEMPT_INVALID')
    start, end, observed, invoked = map(library.utc, (receipt['request_started_at'],
            receipt['request_completed_at'], observed_at, invocation_started))
    require(invoked <= start <= end <= observed and (observed - start).total_seconds() <= 1800,
            'REQUEST_TIME_OR_FRESHNESS_DRIFT')
    last = receipt['last_good_snapshot_ref']
    require(last is None or (type(last) is str and SHA.fullmatch(last)), 'LAST_GOOD_INVALID')
    status = receipt['status']
    require(status in ('SUCCESS', 'FAILED'), 'UNAVAILABLE_EXPORT_NOT_QUALIFIED')
    if status == 'SUCCESS':
        library.validate_request(receipt, context, receipt_sha, observed_at)
    else:
        require(all(receipt[k] is None for k in ('attempt_id', 'artifact_sha256', 'metadata_sha256',
                'publication_sha256', 'snapshot_bytes', 'publication_path')), 'FAILED_HAS_CURRENT_ARTIFACT')
        require(type(receipt['error_code']) is str and re.fullmatch(r'[A-Z0-9_]{1,80}', receipt['error_code'])
                and receipt['error_stage'] in ('ADMISSION', 'EXPORT', 'PUBLISH', 'VERIFY', 'RETENTION', 'LOCK'),
                'FAILED_DIAGNOSTIC_INVALID')
    return status


def orphan_helper(library, results, code):
    if not ORPHAN.exists() and not ORPHAN.is_symlink():
        library.write_new(ORPHAN, {'status': 'EXPORT_HELPER_QUIESCENCE_UNPROVEN',
            'observed_utc': utc_now(), 'helper_sha256': HELPER_SHA, 'results': str(results),
            'retry_allowed': False, 'error_code': code})
    raise RunnerError(code)


def request_export(library, results):
    started = utc_now()
    try:
        result = library.call([library.PYTHON, str(HELPER)], 900)
    except (OSError, subprocess.SubprocessError, KeyboardInterrupt):
        orphan_helper(library, results, 'EXPORT_HELPER_QUIESCENCE_UNPROVEN')
    out, err = result.stdout.encode('utf-8'), result.stderr.encode('utf-8')
    require(len(out) + len(err) <= library.LOG_LIMIT, 'EXPORT_HELPER_LOG_LIMIT')
    library.write_new(results / 'export-helper.stdout', out)
    library.write_new(results / 'export-helper.stderr', err)
    library.write_new(results / 'export-helper-exit.json', {'exit_code': result.returncode,
        'started_at': started, 'completed_at': utc_now(), 'helper_sha256': HELPER_SHA})
    if result.returncode != 0:
        orphan_helper(library, results, 'FIXED_EXPORT_HELPER_NONZERO')
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    require(bool(lines), 'EXPORT_HELPER_RESULT_MISSING')
    outer = library.json_unique(lines[-1])
    require(type(outer) is dict and set(outer) == {'status', 'request_id', 'receipt_path',
            'receipt_sha256', 'context_path', 'context_sha256'} and type(outer['request_id']) is str
            and library.REQUEST.fullmatch(outer['request_id']), 'EXPORT_OUTER_SHAPE_INVALID')
    rp = library.RECEIPTS / (outer['request_id'] + '.json')
    cp = library.RECEIPTS / (outer['request_id'] + '.context.json')
    require(outer['receipt_path'] == str(rp) and outer['context_path'] == str(cp), 'REQUEST_PATH_DRIFT')
    receipt, context = library.read_json(rp, True), library.read_json(cp, True)
    require(library.digest(rp) == outer['receipt_sha256'] and library.digest(cp) == outer['context_sha256'],
            'REQUEST_RECORD_HASH_DRIFT')
    status = validate_dispatch(library, receipt, context, outer['receipt_sha256'], utc_now(), started)
    require(outer['status'] == status, 'EXPORT_OUTER_STATUS_DRIFT')
    for path in (rp, cp):
        library.linux_acl(path)
    accepted = None
    if status == 'SUCCESS':
        accepted = library.ACCEPTED / receipt['attempt_id']
        library.plain(library.ACCEPTED, (0, 10001), 0o550, True)
        library.plain(accepted, (0, 10001), 0o550, True)
        require({p.name for p in accepted.iterdir()} == {'feature_phone_clank.db', 'metadata.json', 'publication.json'},
                'ACCEPTED_LAYOUT_DRIFT')
        for name, key in (('feature_phone_clank.db', 'artifact_sha256'), ('metadata.json', 'metadata_sha256'),
                          ('publication.json', 'publication_sha256')):
            path = accepted / name
            library.plain(path, (0, 10001), 0o440)
            require(library.digest(path) == receipt[key], 'ACCEPTED_EXPORT_HASH_DRIFT')
            library.linux_acl(path)
    library.write_new(results / 'new-export-evidence.json', dict(outer, production=True,
        accepted_mount=status == 'SUCCESS', manufactured_negative=False))
    return receipt, context, rp, cp, accepted


def produce_inputs(library, status, receipt_path, context_path, results, nonce):
    output = library.make_directory(results / 'snapshots', (10001, 10001), 0o700)
    mounts = [library.mount(Path(source), source) for source, _, _ in library.SQLITE_SOURCES.values()]
    if status == 'SUCCESS':
        mounts.append(library.mount(library.ACCEPTED, '/app/feature-phone-accepted'))
    mounts += [library.mount(library.SPEC, '/app/spec.json'), library.mount(library.REGISTRY, '/app/registry.json'), library.mount(library.INVENTORY, '/app/inventory.json'),
               library.mount(receipt_path, '/app/receipt.json'), library.mount(context_path, '/app/request-context.json'),
               library.mount(output, '/app/snapshot-output', True)]
    command = [library.CONSUMER, '--spec', '/app/spec.json', '--receipt', '/app/receipt.json',
        '--request-context', '/app/request-context.json', '--registry', '/app/registry.json',
        '--output-root', '/app/snapshot-output', '--image-id', library.IMAGE, '--inventory', '/app/inventory.json']
    library.run_container(library.container_spec('production-producer', command, mounts, nonce), results)
    roots = list(output.iterdir())
    require(len(roots) == 1 and roots[0].is_dir() and roots[0].name.startswith('snapshot-v1-'),
            'PRODUCER_NEW_OUTPUT_AMBIGUOUS')
    root = roots[0]
    doc = library.read_json(root / 'manifest.json')
    four = library.read_json(root / 'sqlite-source-manifest.json')
    require({r['clank_id'] for r in doc['lanes']} == library.LANES
            and {r['clank_id'] for r in four['lanes']} == set(library.SQLITE_SOURCES), 'PRODUCER_LANE_DRIFT')
    rows = {r['clank_id']: r for r in doc['lanes']}
    require(all(rows[r['clank_id']] == r for r in four['lanes']), 'SQLITE_ROWS_CHANGED')
    fp = rows[library.FP]
    require(fp['refresh_outcome'] == status, 'EXPORT_TRANSLATION_STATUS_DRIFT')
    if status == 'FAILED':
        require(fp['freshness_state'] == 'REFRESH_FAILED' and fp['child_execution_freshness'] == 'UNKNOWN'
                and all(fp[k] is None for k in ('snapshot_path', 'snapshot_sha256', 'snapshot_bytes')),
                'FAILED_EXPORT_TRANSLATION_NOT_TRUTHFUL')
    library.seal_tree(output)
    return root


def reuse_export(library, doc, results):
    """Reuse exactly the reviewed publication; never issue or fake a request."""
    ref = doc.get('qualification_publication')
    require(type(ref) is dict and set(ref) == {'request_id', 'receipt_sha256', 'context_sha256'},
            'SEALED_PUBLICATION_REFERENCE_REQUIRED')
    require(type(ref['request_id']) is str and library.REQUEST.fullmatch(ref['request_id']),
            'SEALED_REQUEST_ID_INVALID')
    rp = library.RECEIPTS / (ref['request_id'] + '.json')
    cp = library.RECEIPTS / (ref['request_id'] + '.context.json')
    receipt, context = library.read_json(rp, True), library.read_json(cp, True)
    require(library.digest(rp) == ref['receipt_sha256'] and library.digest(cp) == ref['context_sha256'],
            'SEALED_REQUEST_RECORD_CHANGED')
    # Validate the ORIGINAL request interval. This is not a current-request
    # freshness claim; the normal translator evaluates publication/native age
    # at current observation time, and may truthfully emit STALE/UNKNOWN.
    library.validate_request(receipt, context, ref['receipt_sha256'], receipt['request_completed_at'])
    require(library.utc(receipt['request_completed_at']) <= library.utc(utc_now()),
            'FUTURE_PUBLICATION_FORBIDDEN')
    accepted = library.ACCEPTED / receipt['attempt_id']
    library.plain(library.ACCEPTED, (0, 10001), 0o550, True)
    library.plain(accepted, (0, 10001), 0o550, True)
    require({p.name for p in accepted.iterdir()} == {'feature_phone_clank.db', 'metadata.json', 'publication.json'},
            'ACCEPTED_LAYOUT_DRIFT')
    for name, key in (('feature_phone_clank.db', 'artifact_sha256'), ('metadata.json', 'metadata_sha256'),
                      ('publication.json', 'publication_sha256')):
        path = accepted / name
        library.plain(path, (0, 10001), 0o440)
        require(library.digest(path) == receipt[key], 'ACCEPTED_EXPORT_HASH_DRIFT')
        library.linux_acl(path)
    library.write_new(results / 'reused-export-evidence.json', dict(ref, export_requested=False,
        original_request_started_at=receipt['request_started_at'], accepted_attempt=receipt['attempt_id'],
        publication_clocks_changed=False, current_freshness_claimed=False))
    return receipt, context, rp, cp, accepted


def detached_var(library, results):
    """Caller holds the production lock; only the new disposable copy is writable."""
    before = library.tree_hashes(LIVE_VAR)
    target = results / 'detached-var'
    require(not target.exists() and not target.is_symlink(), 'DETACHED_VAR_ALREADY_EXISTS')
    shutil.copytree(str(LIVE_VAR), str(target), copy_function=shutil.copy2)
    require(library.tree_hashes(target) == before and library.tree_hashes(LIVE_VAR) == before,
            'DETACHED_VAR_COPY_DRIFT')
    for path in (target, *target.rglob('*')):
        os.chown(str(path), 10001, 10001)
        os.chmod(str(path), 0o700 if path.is_dir() else 0o600)
    require(library.tree_hashes(target) == before, 'DETACHED_VAR_OWNERSHIP_CHANGED_BYTES')
    library.write_new(results / 'detached-var-basis.json', {'files': before, 'live_var_written': False})
    return target


def execute(library, doc, package_sha, mode):
    require(mode in ('SCHEDULED', 'OPERATOR_TRIGGERED', 'QUALIFICATION'), 'FIXED_TRIGGER_REQUIRED')
    results, log_fd, stage = None, None, 'AUTHORITY'
    try:
        with live_lock(library):
            runtime = mode != 'QUALIFICATION'
            before = authority(library, doc, package_sha, runtime)
            for path in (STATE, RESULTS):
                if not path.exists() and not path.is_symlink():
                    library.make_directory(path)
                library.plain(path, (0, 0), 0o700, True)
            nonce = uuid.uuid4().hex[:16]
            results = library.make_directory(RESULTS / (mode.lower() + '-' +
                library.utc(utc_now()).strftime('%Y%m%dT%H%M%SZ') + '-' + nonce))
            log_fd = open_scheduled_log() if runtime else None
            emit_log(log_fd, {'status': mode + '_START', 'started_at': utc_now(),
                             'attempt': str(results), 'production': runtime})
            library.write_new(results / 'authority-before.json', before)
            stage = 'EXPORT_REQUEST'
            receipt, context, rp, cp, accepted = (request_export(library, results) if mode == 'SCHEDULED'
                else reuse_export(library, doc, results))
            require(authority(library, doc, package_sha, runtime) == before, 'AUTHORITY_CHANGED_AFTER_EXPORT')
            stage = 'OEM_WAIT'
            library.write_new(results / 'oem-natural-sidecar-evidence.json', library.wait_oem_sidecars(utc_now(), 900))
            stage = 'PRODUCER'
            status = receipt['status']
            inputs = produce_inputs(library, status, rp, cp, results, nonce)
            require(library.digest(rp) == context['receipt_sha256'], 'REQUEST_CHANGED_AFTER_PRODUCER')
            require(authority(library, doc, package_sha, runtime) == before, 'AUTHORITY_CHANGED_BEFORE_PIPELINE')
            stage = 'LIVE_PIPELINE'
            # Frozen labels choose only the already-proven SUCCESS/FAILED gate.
            # They do NOT mean this natural operation is an isolated fixture.
            label = 'positive' if status == 'SUCCESS' else 'negative'
            var = LIVE_VAR if runtime else detached_var(library, results)
            pipeline = library.complete_pipeline(label, inputs, var, accepted, results, nonce)
            stage = 'FINAL_AUTHORITY'
            require(authority(library, doc, package_sha, runtime) == before, 'AUTHORITY_CHANGED_AFTER_PIPELINE')
            require(digest(library.INVENTORY.parent / MANIFEST_NAME) == package_sha, 'PACKAGE_CHANGED_DURING_RUN')
            summary = {'status': mode + '_COMPLETE', 'production': runtime, 'trigger': mode,
                'expected_trigger': 'DSM_TASK14_DAILY_1145_IST' if mode == 'SCHEDULED' else mode, 'scheduled_origin_independently_verified': False,
                'observed_utc': utc_now(), 'attempt': str(results), 'image': library.IMAGE,
                'source_revision': library.SOURCE_SHA, 'adapter_revision': library.ADAPTER_SHA,
                'exporter_revision': library.EXPORTER_SHA, 'export_image': library.EXPORT_IMAGE,
                'request_id': receipt['request_id'], 'accepted_attempt': receipt['attempt_id'],
                'export_requested': mode == 'SCHEDULED',
                'feature_phone_refresh_outcome': status, 'accepted_mount': accepted is not None,
                'natural_qualification': 'SUCCESS_EXPORT_REQUIRES_OWNER_POSTRUN_CENSUS' if status == 'SUCCESS'
                    else 'FAILED_EXPORT_DEGRADED_NOT_CUTOVER_PASS',
                'pipeline_proof_sha256': library.digest(results / (label + '-pipeline-proof.json')),
                'snapshot_root': str(inputs), 'snapshot_manifest_sha256': library.digest(inputs / 'manifest.json'),
                'adapter_registry_sha256': library.digest(inputs / 'adapter-registry.json'),
                'observer_inventory_sha256': library.FILE_PINS[library.INVENTORY],
                'successful_phases': len(pipeline['phases']), 'child_writer_invoked': False,
                'canonical_child_nonmutation_claim': 'KERNEL_RO_CONSUMER_MOUNTS_AND_ZERO_CHANGES',
                'active_child_file_hash_invariance_claimed': False, 'manufactured_negative': False,
                'live_var_written': runtime, 'task14_changed': False, 'admission_changed': False,
                'package_manifest_sha256': package_sha, 'authority': before,
                'hetzner_current_state_claimed': False, 'mission_completion_claimed': False,
                'board_observation_or_unblock_claimed': False, 'process_exit': 0}
            library.write_new(results / 'run-summary.json', summary)
            emit_log(log_fd, summary)
            return 0
    except (RunnerError, library.ProofError, OSError, ValueError, KeyError, TypeError,
            AttributeError, subprocess.SubprocessError, KeyboardInterrupt) as exc:
        code = str(exc) if isinstance(exc, (RunnerError, library.ProofError)) else 'NATURAL_HOST_OPERATION_FAILED'
        if not CODE.fullmatch(code):
            code = 'NATURAL_HOST_OPERATION_FAILED'
        summary = {'status': mode + '_FAILED_CLOSED', 'observed_utc': utc_now(),
            'error_stage': stage, 'error_code': code, 'attempt': str(results) if results else None,
            'production': mode != 'QUALIFICATION', 'evidence_preserved': True, 'task14_changed': False,
            'admission_changed': False, 'mission_completion_claimed': False, 'process_exit': 1,
            'live_var_may_have_changed': mode != 'QUALIFICATION' and stage in ('LIVE_PIPELINE', 'FINAL_AUTHORITY')}
        if results is not None:
            library.write_new(results / 'run-failure.json', summary)
        emit_log(log_fd, summary)
        return 1
    finally:
        if log_fd is not None:
            os.close(log_fd)


def open_scheduled_log():
    """Only after authority+lock checks; no shell redirection before admission."""
    root = BASE / 'logs'
    info = plain(root, directory=True)
    require(info.st_uid in (0, 1026) and not stat.S_IMODE(info.st_mode) & 0o022,
            'SCHEDULED_LOG_PARENT_UNSAFE')
    path = root / ('scheduled-' + datetime.now(timezone.utc).strftime('%Y%m%d') + '.log')
    exists = path.exists() or path.is_symlink()
    before = plain(path, (0, 0), 0o600) if exists else None
    flags = os.O_WRONLY | os.O_APPEND | os.O_NOFOLLOW
    if not exists:
        flags |= os.O_CREAT | os.O_EXCL
    fd = os.open(str(path), flags, 0o600)
    try:
        after = os.fstat(fd)
        require(stat.S_ISREG(after.st_mode) and after.st_nlink == 1
                and (after.st_uid, after.st_gid, stat.S_IMODE(after.st_mode)) == (0, 0, 0o600),
                'SCHEDULED_LOG_FILE_UNSAFE')
        require(before is None or (before.st_dev, before.st_ino) == (after.st_dev, after.st_ino),
                'SCHEDULED_LOG_REPLACED')
        return fd
    except BaseException:
        os.close(fd)
        raise


def emit_log(fd, value):
    payload = (json.dumps(value, sort_keys=True, separators=(',', ':')) + '\n').encode('utf-8')
    require(len(payload) <= 32768, 'SCHEDULED_METADATA_LIMIT')
    if fd is not None:
        offset = 0
        while offset < len(payload):
            wrote = os.write(fd, payload[offset:])
            require(wrote > 0, 'SCHEDULED_LOG_WRITE_FAILED')
            offset += wrote
        os.fsync(fd)
    print(payload.decode('utf-8').rstrip(), flush=True)


def main(argv=None):
    global P, MANIFEST_SHA, RESULTS, STATE, ORPHAN
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 3 or args[0] not in ('--run', '--operator-run', '--qualify', '--dry-check') \
            or args[1] != '--package-sha256' or not SHA.fullmatch(args[2]):
        print('final_task14=FIXED_MODE_REQUIRED', file=sys.stderr)
        return 2
    MANIFEST_SHA = args[2]
    if os.name != 'posix' or not hasattr(os, 'geteuid') or os.geteuid() != 0:
        print('final_task14=OWNER_ROOT_REQUIRED', file=sys.stderr)
        return 2
    runtime = args[0] in ('--run', '--operator-run')
    try:
        root = Path(__file__).absolute().parent
        require(not runtime or root == PACKAGE, 'FIXED_PRODUCTION_PACKAGE_REQUIRED')
        if args[0] == '--qualify':
            require(root.parent == BASE / 'build/cops-000074-governed', 'QUALIFICATION_PACKAGE_PATH_INVALID')
            STATE = root.parent / ('qualification-' + MANIFEST_SHA[:16])
            RESULTS, ORPHAN = STATE / 'attempts', STATE / 'orphan-risk.json'
        P, doc, sha = load_package(root)
        if args[0] != '--dry-check':
            mode = {'--run': 'SCHEDULED', '--operator-run': 'OPERATOR_TRIGGERED', '--qualify': 'QUALIFICATION'}[args[0]]
            return execute(P, doc, sha, mode)
        observed = authority(P, doc, sha, False)
        boundary = publisher_boundary()
        print(json.dumps({'status': 'FINAL_ASSEMBLY_DRY_CHECK_PASS', 'production_changed': False,
            'child_databases_opened': False, 'export_requested': False, 'container_started': False,
            'package_manifest_sha256': sha, 'authority': observed,
            'publisher_boundary': boundary}, sort_keys=True))
        return 0
    except (RunnerError, OSError, ValueError, KeyError, TypeError, AttributeError,
            subprocess.SubprocessError) as exc:
        code = str(exc) if isinstance(exc, RunnerError) else 'FINAL_AUTHORITY_CHECK_FAILED'
        if not CODE.fullmatch(code):
            code = 'FINAL_AUTHORITY_CHECK_FAILED'
        print(json.dumps({'status': 'FINAL_ASSEMBLY_CHECK_FAILED_CLOSED', 'error_code': code,
                          'production_changed': None if runtime else False}, sort_keys=True))
        return 1
    except Exception as exc:
        # The imported frozen library has its own bounded ProofError type.
        if P is None or not isinstance(exc, P.ProofError):
            raise
        code = str(exc) if CODE.fullmatch(str(exc)) else 'FINAL_AUTHORITY_CHECK_FAILED'
        print(json.dumps({'status': 'FINAL_ASSEMBLY_CHECK_FAILED_CLOSED', 'error_code': code,
                          'production_changed': None if runtime else False}, sort_keys=True))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
