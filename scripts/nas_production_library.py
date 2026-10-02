"""Governed production lifecycle extracted from the proven COPS-000081 library.

Preserves sandbox, process/exit agreement, append-chain and non-mutation guards.
Membership/pins are supplied only by the authenticated root-owned package.
No standalone isolated/export-request entry point exists in this library.
Python 3.8 host compatible; import performs no host operations.
"""
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

BASE = Path('/volume2/clank/motherclank')
IMAGE = SOURCE_SHA = ADAPTER_SHA = None
RESULTS = ORPHAN = SPEC = REGISTRY = INVENTORY = None
FILE_PINS = {}
SQLITE_SOURCES = {}
LANES = frozenset()
EXPORT_IMAGE = 'sha256:eccdb14e96634a255ce37e6ea9b9391997c8845710fe3fd8cea52129c3612478'
EXPORTER_SHA = '99405de0829e5baade4151eb1c2a0f2374b49613'
DEPLOYED_SHA = '3e6e19a2d5b7cb5004aab12145d099521a200c25'
ACCEPTED = Path('/volume2/clank/feature-phone-clank/observer-export/accepted')
RECEIPTS = Path('/volume2/clank/feature-phone-clank/observer-export/request-control-cops-000081/receipts')
DOCKER = '/var/packages/ContainerManager/target/usr/bin/docker'
DSM = '/usr/syno/bin/synoschedtask'
PYTHON = '/bin/python3'
LABEL = 'COPS-000074-governed-production'
FP = 'feature-phone-clank'
MOTHER = '/app/cops-000074/motherclank'
GATE = MOTHER + '/scripts/nas_feature_phone_proof_gate.py'
CONSUMER = MOTHER + '/scripts/nas_feature_phone_consumer.py'
SHA = re.compile(r'[0-9a-f]{64}\Z')
ATTEMPT = re.compile(r'export-[0-9]{8}T[0-9]{6}Z-[0-9a-f]{16}\Z')
REQUEST = re.compile(r'fp-request-[0-9]{8}T[0-9]{6}Z-[0-9a-f]{16}\Z')
RECEIPT_KEYS = frozenset(('request_format_version', 'status', 'request_id',
    'request_started_at', 'request_completed_at', 'attempt_id', 'exporter_revision',
    'export_image', 'deployed_revision', 'artifact_sha256', 'metadata_sha256',
    'publication_sha256', 'snapshot_bytes', 'publication_path', 'error_code',
    'error_stage', 'last_good_snapshot_ref'))
CONTEXT_KEYS = frozenset(('request_id', 'request_started_at', 'prior_attempt_id', 'receipt_sha256'))
PHASES = ('preflight', 'harvest', 'harvest-truth', 'synthesize', 'synthesis-truth',
          'detect', 'recommend', 'ingest-qc', 'soak-report', 'continuity')
LOG_LIMIT = 16 * 1024 * 1024


class ProofError(Exception):
    """Bounded contract/proof failure; never a usable lane SUCCESS."""


class Fatal(ProofError):
    """Unresolved identity/process uncertainty, prevents any later proof step."""


def require(ok, code):
    if not ok:
        raise ProofError(code)


def configure(root, release, topology, results, orphan):
    """Called only after package bytes, inventory, registry/spec are verified."""
    global IMAGE, SOURCE_SHA, ADAPTER_SHA, RESULTS, ORPHAN, SPEC, REGISTRY, INVENTORY
    global FILE_PINS, SQLITE_SOURCES, LANES
    require(IMAGE is None, 'LIBRARY_ALREADY_CONFIGURED')
    require(re.fullmatch(r'[0-9a-f]{40}', release['source_revision'])
            and release['adapter_revision'] == topology.adapter_sha
            and release['image'] == topology.image_id, 'RELEASE_TOPOLOGY_PIN_DRIFT')
    IMAGE, SOURCE_SHA, ADAPTER_SHA = release['image'], release['source_revision'], release['adapter_revision']
    RESULTS, ORPHAN = results, orphan
    SPEC, REGISTRY, INVENTORY = (root / name for name in
        ('snapshot-spec.json', 'adapter-registry.json', 'observer-inventory.json'))
    FILE_PINS = {root / name: sha for name, sha in release['files'].items()}
    SQLITE_SOURCES = {cid: (source[0], source[1],
        topology.children[cid]['observer']['snapshot_spec']['snapshot_filename'])
        for cid, source in topology.sources.items()}
    LANES = frozenset(topology.children)


def utc_now():
    return datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')


def utc(value):
    require(type(value) is str and len(value) <= 40, 'TIMESTAMP_TYPE_INVALID')
    try:
        result = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError:
        raise ProofError('TIMESTAMP_INVALID') from None
    require(result.tzinfo is not None and result.utcoffset().total_seconds() == 0,
            'TIMESTAMP_NOT_UTC')
    return result


def no_links(path):
    require(not any(p.is_symlink() for p in (path, *path.parents)), 'SYMLINKED_PATH')


def plain(path, owner=None, mode=None, directory=False):
    no_links(path)
    info = path.lstat()
    require((stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode))
            and (directory or info.st_nlink == 1), 'UNSAFE_FILE_TYPE_OR_LINK')
    if owner is not None:
        require((info.st_uid, info.st_gid) == owner, 'OWNER_IDENTITY_DRIFT')
    if mode is not None:
        require(stat.S_IMODE(info.st_mode) == mode, 'SEALED_MODE_DRIFT')
    return info


def digest(path):
    before = plain(path)
    fd = os.open(str(path), os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
    try:
        opened = os.fstat(fd)
        require((before.st_dev, before.st_ino) == (opened.st_dev, opened.st_ino), 'HASH_FILE_REPLACED')
        h = hashlib.sha256()
        with os.fdopen(fd, 'rb', closefd=False) as stream:
            for chunk in iter(lambda: stream.read(1048576), b''):
                h.update(chunk)
        after = os.fstat(fd)
        require((before.st_size, before.st_mtime_ns, before.st_ctime_ns)
                == (after.st_size, after.st_mtime_ns, after.st_ctime_ns), 'HASH_FILE_CHANGED')
        return h.hexdigest()
    finally:
        os.close(fd)


def json_unique(raw):
    require(isinstance(raw, str) and len(raw.encode('utf-8')) <= LOG_LIMIT, 'JSON_SIZE_LIMIT')
    def pairs(items):
        out = {}
        for key, item in items:
            require(key not in out, 'DUPLICATE_JSON_KEY')
            out[key] = item
        return out
    try:
        return json.loads(raw, object_pairs_hook=pairs)
    except (ValueError, UnicodeError, RecursionError):
        raise ProofError('JSON_INVALID') from None


def read_json(path, sealed=False):
    plain(path, (0, 10001) if sealed else None, 0o440 if sealed else None)
    before = digest(path)
    value = json_unique(path.read_text(encoding='utf-8'))
    require(digest(path) == before and isinstance(value, dict), 'JSON_INPUT_CHANGED_OR_NOT_OBJECT')
    return value


def fsync_dir(path):
    fd = os.open(str(path), os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def write_new(path, value, permissions=0o600, owner=(0, 0)):
    no_links(path.parent)
    payload = value if isinstance(value, bytes) else (
        json.dumps(value, sort_keys=True, separators=(',', ':')) + '\n').encode('utf-8')
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, permissions)
    try:
        with os.fdopen(fd, 'wb', closefd=False) as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(fd)
        os.fchown(fd, *owner)
        os.fchmod(fd, permissions)
        os.fsync(fd)
    finally:
        os.close(fd)
    fsync_dir(path.parent)
    return digest(path)


def call(argv, timeout=30):
    require(isinstance(argv, list) and argv and all(type(x) is str for x in argv), 'FIXED_ARGV_REQUIRED')
    return subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          universal_newlines=True, timeout=timeout, check=False,
                          env=dict(os.environ, TZ='UTC', LC_ALL='C'))


def checked(argv, timeout=30):
    result = call(argv, timeout)
    require(result.returncode == 0, 'FIXED_COMMAND_NONZERO')
    require(len(result.stdout.encode('utf-8')) + len(result.stderr.encode('utf-8')) <= LOG_LIMIT,
            'COMMAND_OUTPUT_LIMIT')
    return result.stdout


def mount(source, destination, rw=False):
    return {'Source': str(source), 'Destination': destination, 'RW': rw, 'Type': 'bind'}


def container_spec(role, command, mounts, nonce):
    require(IMAGE is not None and bool(LANES), 'UNCONFIGURED_PRODUCTION_LIBRARY')
    require(re.fullmatch(r'[a-z][a-z0-9-]{0,63}', role) and re.fullmatch(r'[0-9a-f]{16}', nonce),
            'CONTAINER_ROLE_OR_NONCE_INVALID')
    require(type(command) is list and command and all(type(x) is str for x in command), 'COMMAND_INVALID')
    require(len({x['Destination'] for x in mounts}) == len(mounts), 'DUPLICATE_MOUNT_DESTINATION')
    for item in mounts:
        require(set(item) == {'Source', 'Destination', 'RW', 'Type'} and item['Type'] == 'bind'
                and type(item['RW']) is bool and item['Source'].startswith('/')
                and item['Destination'].startswith('/') and ',' not in item['Source']
                and ',' not in item['Destination'] and '..' not in Path(item['Source']).parts
                and '..' not in Path(item['Destination']).parts, 'MOUNT_INVALID')
        source = item['Source']
        require(source not in ('/', '/volume2', '/volume2/clank',
                '/volume2/clank/feature-phone-clank',
                '/volume2/clank/feature-phone-clank/observer-export')
                and 'docker.sock' not in source
                and not source.startswith('/volume2/clank/feature-phone-clank/state')
                and '/observer-export/staging' not in source
                and '/observer-export/failed' not in source, 'UNAPPROVED_AUTHORITY_MOUNT')
        if source in {item[0] for item in SQLITE_SOURCES.values()}:
            require(item['Destination'] == source and item['RW'] is False,
                    'CANONICAL_SOURCE_MUST_BE_EXACT_READONLY')
        if source == str(ACCEPTED) or source.startswith(str(ACCEPTED) + '/'):
            require(source == str(ACCEPTED), 'ACCEPTED_SUBTREE_ALIAS_FORBIDDEN')
            require(item['Destination'] == '/app/feature-phone-accepted' and item['RW'] is False,
                    'ACCEPTED_MOUNT_MUST_BE_READONLY')
        if role.startswith('negative'):
            require(source != str(ACCEPTED) and not source.startswith(str(ACCEPTED) + '/')
                    and 'feature-phone-accepted' not in item['Destination'],
                    'NEGATIVE_ACCEPTED_MOUNT_FORBIDDEN')
    return {'role': role, 'nonce': nonce, 'name': 'cops74-governed-' + nonce + '-' + role,
            'image': IMAGE, 'command': command, 'mounts': mounts,
            'labels': {'clank.scope': LABEL, 'clank.proof_nonce': nonce, 'clank.proof_role': role}}


def create_argv(spec):
    args = [DOCKER, 'create', '--name', spec['name'], '--pull', 'never', '--network', 'none',
            '--read-only', '--user', '10001:10001', '--cap-drop', 'ALL', '--security-opt',
            'no-new-privileges', '--tmpfs', '/tmp:rw,nosuid,nodev,size=64m', '--entrypoint', 'python',
            '--env', 'TZ=UTC', '--env', 'LC_ALL=C']
    for key, value in sorted(spec['labels'].items()):
        args += ['--label', key + '=' + value]
    for item in spec['mounts']:
        value = 'type=bind,source=' + item['Source'] + ',target=' + item['Destination']
        args += ['--mount', value + ('' if item['RW'] else ',readonly')]
    return args + [IMAGE] + spec['command']


def validate_container(data, spec, require_created=False):
    require(isinstance(data, dict) and SHA.fullmatch(data.get('Id', '')),
            'CONTAINER_ID_INVALID')
    cfg, host, state = data.get('Config') or {}, data.get('HostConfig') or {}, data.get('State') or {}
    require(data.get('Name') == '/' + spec['name'] and data.get('Image') == IMAGE
            and cfg.get('Image') == IMAGE and cfg.get('User') == '10001:10001'
            and cfg.get('Entrypoint') == ['python'] and cfg.get('Cmd') == spec['command']
            and data.get('Path') == 'python' and data.get('Args') == spec['command'],
            'CONTAINER_RUNTIME_IDENTITY_DRIFT')
    require(all((cfg.get('Labels') or {}).get(k) == v for k, v in spec['labels'].items()),
            'CONTAINER_NONCE_IDENTITY_DRIFT')
    require(host.get('ReadonlyRootfs') is True and host.get('NetworkMode') == 'none'
            and host.get('Privileged') is False and not host.get('AutoRemove')
            and host.get('CapDrop') == ['ALL'] and not host.get('CapAdd')
            and host.get('SecurityOpt') in (['no-new-privileges'], ['no-new-privileges:true'])
            and host.get('Tmpfs') == {'/tmp': 'rw,nosuid,nodev,size=64m'}
            and not host.get('Devices') and not host.get('VolumesFrom')
            and not host.get('Binds') and not host.get('PidMode') and not host.get('IpcMode') == 'host'
            and not host.get('PortBindings') and not host.get('Links'), 'CONTAINER_SANDBOX_DRIFT')
    actual = []
    for item in data.get('Mounts', []):
        if item.get('Type') == 'tmpfs' and item.get('Destination') == '/tmp':
            require(item.get('RW') is True, 'TMPFS_NOT_WRITABLE')
            continue
        actual.append({key: item.get(key) for key in ('Source', 'Destination', 'RW', 'Type')})
    require(sorted(actual, key=lambda x: x['Destination'])
            == sorted(spec['mounts'], key=lambda x: x['Destination']), 'CONTAINER_MOUNT_IDENTITY_DRIFT')
    require(state.get('Paused') is False and state.get('Restarting') is False,
            'CONTAINER_UNEXPECTED_STATE')
    if require_created:
        require(state.get('Running') is False and state.get('Status') == 'created',
                'CONTAINER_STARTED_BEFORE_INSPECTION')
    return data['Id']


def validate_request(receipt, context, receipt_sha, observed_at):
    require(type(receipt) is dict and type(context) is dict and set(receipt) == RECEIPT_KEYS
            and set(context) == CONTEXT_KEYS, 'REQUEST_CONTRACT_DRIFT')
    require(type(receipt_sha) is str and context['receipt_sha256'] == receipt_sha
            and SHA.fullmatch(receipt_sha), 'RECEIPT_HASH_DRIFT')
    require(receipt['request_format_version'] == '1.0' and receipt['status'] == 'SUCCESS'
            and receipt['exporter_revision'] == EXPORTER_SHA and receipt['export_image'] == EXPORT_IMAGE
            and receipt['deployed_revision'] == DEPLOYED_SHA, 'REQUEST_PIN_OR_SUCCESS_REQUIRED')
    require(all(type(x) is str for x in (receipt['request_id'], receipt['attempt_id'], context['prior_attempt_id']))
            and REQUEST.fullmatch(receipt['request_id']) and ATTEMPT.fullmatch(receipt['attempt_id'])
            and ATTEMPT.fullmatch(context['prior_attempt_id'])
            and receipt['attempt_id'] != context['prior_attempt_id'], 'NEW_ACCEPTED_ATTEMPT_REQUIRED')
    require(context['request_id'] == receipt['request_id']
            and context['request_started_at'] == receipt['request_started_at'], 'REQUEST_CONTEXT_DRIFT')
    start, end, observed = map(utc, (receipt['request_started_at'], receipt['request_completed_at'], observed_at))
    require(start <= end <= observed and (observed - start).total_seconds() <= 1800,
            'REQUEST_TIME_OR_FRESHNESS_DRIFT')
    require(receipt['publication_path'] == str(ACCEPTED / receipt['attempt_id'])
            and type(receipt['snapshot_bytes']) is int and 0 < receipt['snapshot_bytes'] <= 1024 ** 3
            and receipt['error_code'] is None and receipt['error_stage'] is None,
            'PUBLICATION_BINDING_INVALID')
    require(all(type(receipt[k]) is str and SHA.fullmatch(receipt[k]) for k in
                ('artifact_sha256', 'metadata_sha256', 'publication_sha256')), 'PUBLICATION_HASH_INVALID')
    require(receipt['last_good_snapshot_ref'] is None or
            (type(receipt['last_good_snapshot_ref']) is str and SHA.fullmatch(receipt['last_good_snapshot_ref'])),
            'LAST_GOOD_INVALID')
    return receipt['attempt_id']


def linux_acl(path):
    result = call(['/usr/syno/bin/synoacltool', '-get', str(path)])
    output = result.stdout + result.stderr
    require(result.returncode in (0, 255) and "It's Linux mode" in output
            and len(output) <= 32768, 'EXTENDED_ACL_REQUIRES_OWNER_REVIEW')
    return hashlib.sha256(output.encode('utf-8')).hexdigest()


def bracket(output, name):
    matches = re.findall(r'^\s*' + re.escape(name) + r':\s*\[([^\]]*)\]\s*$', output, re.M)
    require(len(matches) == 1, 'SCHEDULER_FIELD_MISSING_OR_DUPLICATE')
    return matches[0]


def scheduler_truth():
    outputs = {}
    for tid, expected in ((14, {'ID': '14', 'Name': 'motherclank-nas-partial-cops-000072',
            'State': 'enabled', 'Owner': 'root', 'Type': 'daily',
            'Command': '/bin/sh /volume2/clank/motherclank/deploy/scheduled-run.sh'}),
        (15, {'ID': '15', 'Name': 'oem-radar-canonical-cops-000072', 'State': 'enabled',
            'Owner': 'root', 'Command': '/bin/sh /volume2/clank/oem-radar/canonical-cops-000072/deploy/scheduled-run.sh'})):
        output = checked([DSM, '--get', 'id=' + str(tid)])
        require(all(bracket(output, k) == v for k, v in expected.items()), 'SCHEDULER_AUTHORITY_DRIFT')
        clocks = re.findall(r'^\s*Run time:\s*\[(\d{1,2})\]:\[(\d{1,2})\]\s*$', output, re.M)
        require(len(clocks) == 1, 'SCHEDULER_CLOCK_AMBIGUOUS')
        if tid == 14:
            require(clocks[0] == ('11', '45'), 'TASK14_CADENCE_DRIFT')
        else:
            require(clocks[0][1] == '50' and (bracket(output, 'Type') == 'hourly'
                    or re.search(r'^\s*Repeat every\s*(?:\[1\]\s*(?:hour|hr)|\[60\]\s*min)',
                                 output, re.M | re.I)), 'TASK15_CADENCE_DRIFT')
        stable = '\n'.join(line for line in output.splitlines() if not re.match(
            r'^\s*(Last Run Time|Next Run Time|Next Trigger|Status|Last Result):', line))
        outputs[str(tid)] = hashlib.sha256(stable.encode('utf-8')).hexdigest()
    return outputs


def image_truth():
    rows = json_unique(checked([DOCKER, 'image', 'inspect', IMAGE]))
    require(type(rows) is list and len(rows) == 1 and rows[0].get('Id') == IMAGE, 'CANDIDATE_IMAGE_DRIFT')
    cfg = rows[0].get('Config') or {}
    labels = cfg.get('Labels') or {}
    expected = {'org.opencontainers.image.revision': SOURCE_SHA, 'clank.adapter_revision': ADAPTER_SHA,
                'clank.snapshot_contract': '1.0', 'clank.observer_contract': '0.2',
                'clank.feature_phone_export_revision': EXPORTER_SHA,
                'clank.feature_phone_export_format': '1.0', 'clank.proof_scope': 'COPS-000074-governed-production'}
    require(cfg.get('User') == '10001:10001' and all(labels.get(k) == v for k, v in expected.items()),
            'CANDIDATE_SOURCE_OR_CONTRACT_DRIFT')
    return expected


def path_identity(path):
    meta = path.lstat()
    return (meta.st_dev, meta.st_ino, meta.st_uid, meta.st_gid, stat.S_IMODE(meta.st_mode))


def inspect_container(identity):
    rows = json_unique(checked([DOCKER, 'container', 'inspect', identity]))
    require(type(rows) is list and len(rows) == 1, 'CONTAINER_INSPECTION_AMBIGUOUS')
    return rows[0]


def mark_orphan(spec, results, code):
    marker = {'status': 'UNRESOLVED_OWNED_CONTAINER_RISK', 'observed_utc': utc_now(),
              'name': spec['name'], 'nonce': spec['nonce'], 'role': spec['role'],
              'image': IMAGE, 'error_code': code, 'results': str(results), 'retry_allowed': False}
    if not ORPHAN.exists() and not ORPHAN.is_symlink():
        write_new(ORPHAN, marker)
    raise Fatal(code)


def ensure_removed(spec, cid, results):
    """Stop/remove ONLY after the complete confirmed nonce identity is known."""
    try:
        row = inspect_container(cid)
        require(validate_container(row, spec) == cid, 'OWNED_CONTAINER_ID_DRIFT')
        if row['State'].get('Running') is True:
            checked([DOCKER, 'container', 'stop', '--time', '10', cid], 30)
            row = inspect_container(cid)
            validate_container(row, spec)
        require(row['State'].get('Running') is False and row['State'].get('Paused') is False
                and row['State'].get('Restarting') is False
                and type(row['State'].get('Pid')) is int and row['State']['Pid'] == 0
                and row['State'].get('Status') in ('created', 'exited')
                and row['State'].get('Dead') is False
                and row['State'].get('OOMKilled') is False
                and not row['State'].get('Error'), 'CONTAINER_QUIESCENCE_UNPROVEN')
        write_new(results / (spec['role'] + '-quiescent.json'),
                  {'id': cid, 'name': spec['name'], 'running': False,
                   'nonce': spec['nonce'], 'image': IMAGE, 'state': row['State']})
        checked([DOCKER, 'container', 'rm', cid])
        remaining = checked([DOCKER, 'ps', '--all', '--no-trunc', '-q', '--filter', 'name=^/' + spec['name'] + '$'])
        require(not remaining.strip(), 'OWNED_CONTAINER_REMOVAL_UNPROVEN')
    except (ProofError, OSError, subprocess.SubprocessError, KeyboardInterrupt):
        mark_orphan(spec, results, 'OWNED_CONTAINER_QUIESCENCE_OR_REMOVAL_UNPROVEN')


def run_container(spec, results, timeout=600):
    """Create/inspect-before-start, bounded wait, exit agreement and evidence."""
    cid, completed, result = None, False, None
    write_new(results / (spec['role'] + '-intention.json'), spec)
    try:
        created = call(create_argv(spec), 30)
        if created.returncode != 0 or not SHA.fullmatch(created.stdout.strip()):
            mark_orphan(spec, results, 'CREATE_TRANSPORT_OR_ID_UNCERTAIN')
        cid = created.stdout.strip()
        row = inspect_container(cid)
        require(validate_container(row, spec, True) == cid, 'CREATE_IDENTITY_DRIFT')
        write_new(results / (spec['role'] + '-created.json'), row)
        started = call([DOCKER, 'container', 'start', cid], 30)
        require(started.returncode == 0 and started.stdout.strip() == cid, 'START_TRANSPORT_UNCERTAIN')
        waited = call([DOCKER, 'container', 'wait', cid], timeout)
        row = inspect_container(cid)
        validate_container(row, spec)
        require(waited.returncode == 0 and re.fullmatch(r'\d{1,3}', waited.stdout.strip())
                and row['State'].get('Running') is False
                and row['State'].get('Status') == 'exited'
                and type(row['State'].get('Pid')) is int and row['State']['Pid'] == 0
                and row['State'].get('OOMKilled') is False and not row['State'].get('Error')
                and row['State'].get('Dead') is False
                and type(row['State'].get('ExitCode')) is int
                and row['State']['ExitCode'] == int(waited.stdout.strip()), 'PROCESS_EXIT_AGREEMENT_FAILED')
        logs = call([DOCKER, 'container', 'logs', cid], 30)
        require(logs.returncode == 0, 'CONTAINER_LOG_TRANSPORT_FAILED')
        out, err = logs.stdout.encode('utf-8'), logs.stderr.encode('utf-8')
        require(len(out) + len(err) <= LOG_LIMIT, 'CONTAINER_LOG_LIMIT')
        out_sha = write_new(results / (spec['role'] + '.stdout'), out)
        err_sha = write_new(results / (spec['role'] + '.stderr'), err)
        result = {'role': spec['role'], 'container_id': cid, 'exit_code': row['State']['ExitCode'],
                  'stdout_sha256': out_sha, 'stderr_sha256': err_sha,
                  'stdout_bytes': len(out), 'stderr_bytes': len(err),
                  'finished_at': row['State'].get('FinishedAt'), 'image': IMAGE}
        write_new(results / (spec['role'] + '-exit.json'), result)
        completed = True
    except (OSError, subprocess.SubprocessError, KeyboardInterrupt) as exc:
        if cid is None:
            mark_orphan(spec, results, 'CREATE_OR_INSPECT_TRANSPORT_UNCERTAIN')
        raise Fatal('CONTAINER_TRANSPORT_OR_TIMEOUT') from None
    finally:
        if cid is not None:
            ensure_removed(spec, cid, results)
    require(completed and result['exit_code'] == 0, 'PIPELINE_PHASE_NONZERO')
    return result


def wait_oem_sidecars(start, timeout=900):
    root, name, _ = SQLITE_SOURCES['oem-radar']
    path = Path(root) / name
    plain(Path(root), directory=True)
    plain(path)
    deadline = time.monotonic() + timeout
    while True:
        current = [Path(str(path) + suffix) for suffix in ('-wal', '-shm')]
        for item in current:
            no_links(item)
        if all(item.is_file() for item in current):
            return {'status': 'NATURAL_OEM_SIDECARS_PRESENT', 'wait_started_at': start,
                    'observed_at': utc_now(), 'writer_invoked': False,
                    'sidecars': [{ 'name': x.name, 'bytes': plain(x).st_size } for x in current]}
        require(time.monotonic() < deadline, 'OEM_NATURAL_SIDECARS_TIMEOUT')
        time.sleep(min(2, max(0.01, deadline - time.monotonic())))


def make_directory(path, owner=(0, 0), permissions=0o700):
    no_links(path.parent)
    require(not path.exists() and not path.is_symlink(), 'NEW_DIRECTORY_REQUIRED')
    path.mkdir(mode=permissions)
    os.chown(str(path), *owner)
    os.chmod(str(path), permissions)
    plain(path, owner, permissions, True)
    linux_acl(path)
    fsync_dir(path.parent)
    return path


def tree_hashes(root):
    """Read regular single-link files only, with no symlink or filesystem crossing."""
    base = plain(root, directory=True)
    result = {}
    for path in sorted(root.rglob('*')):
        info = path.lstat()
        no_links(path)
        require(info.st_dev == base.st_dev, 'TREE_FILESYSTEM_BOUNDARY')
        require(stat.S_ISDIR(info.st_mode) or
                (stat.S_ISREG(info.st_mode) and info.st_nlink == 1), 'TREE_SPECIAL_OR_LINK')
        if stat.S_ISREG(info.st_mode):
            result[path.relative_to(root).as_posix()] = {'sha256': digest(path), 'bytes': info.st_size}
    return result


def seal_tree(root):
    """Only a new detached tree; after its producer is proven removed."""
    before = tree_hashes(root)
    for path in sorted(root.rglob('*'), reverse=True):
        info = path.lstat()
        os.chown(str(path), 0, 10001)
        os.chmod(str(path), 0o550 if stat.S_ISDIR(info.st_mode) else 0o440)
        linux_acl(path)
    os.chown(str(root), 0, 10001)
    os.chmod(str(root), 0o550)
    linux_acl(root)
    require(tree_hashes(root) == before, 'TREE_CHANGED_WHILE_SEALING')
    return before




APPEND_DIRS = {'m0': 'snapshots', 'm1': 'syntheses', 'm2': 'anomalies',
               'm3': 'recommendations', 'qc': 'qc_corpus', 'soak': 'soak'}


def append_state(var):
    result = {}
    for label, dirname in APPEND_DIRS.items():
        root = var / dirname
        if root.exists():
            plain(root, directory=True)
            for path in sorted(root.glob('*.jsonl')):
                plain(path)
                require(path.stat().st_size <= 256 * 1024 * 1024, 'DERIVED_RECORD_LIMIT')
                result[path.relative_to(var).as_posix()] = path.read_bytes()
    return result


def appended_records(before, after):
    """Only newly appended whole records count; copied history is never proof."""
    require(set(before) <= set(after), 'DERIVED_HISTORY_DELETED')
    records = {label: [] for label in APPEND_DIRS}
    reverse = {v: k for k, v in APPEND_DIRS.items()}
    for name, raw in after.items():
        prior = before.get(name, b'')
        require(type(raw) is bytes and type(prior) is bytes and raw.startswith(prior)
                and (not prior or prior.endswith(b'\n')), 'DERIVED_HISTORY_REWRITTEN')
        delta = raw[len(prior):]
        require(not delta or delta.endswith(b'\n'), 'DERIVED_APPEND_INCOMPLETE')
        label = reverse.get(name.split('/')[0])
        require(label is not None, 'DERIVED_PATH_UNKNOWN')
        for line in delta.splitlines():
            require(bool(line), 'EMPTY_DERIVED_RECORD')
            value = json_unique(line.decode('utf-8'))
            require(type(value) is dict, 'DERIVED_RECORD_NOT_OBJECT')
            records[label].append(value)
    require(all(len(v) == 1 for v in records.values()), 'ONE_NEW_RECORD_EACH_PHASE_REQUIRED')
    return {key: values[0] for key, values in records.items()}


def record_hash(value, excluded=(), compact=True):
    body = {k: v for k, v in value.items() if k not in excluded} if isinstance(value, dict) else value
    kwargs = {'sort_keys': True, 'default': str}
    if compact:
        kwargs['separators'] = (',', ':')
    return 'sha256:' + hashlib.sha256(json.dumps(body, **kwargs).encode('utf-8')).hexdigest()


def validate_fresh_pipeline(records, manifest_sha, registry_sha, inventory_sha, expected_fp, started):
    require(set(records) == set(APPEND_DIRS), 'FULL_FRESH_PHASE_RECORDS_REQUIRED')
    require(expected_fp in ('SUCCESS', 'FAILED') and all(type(x) is str and SHA.fullmatch(x)
            for x in (manifest_sha, registry_sha, inventory_sha)), 'FRESH_PROOF_INPUT_PIN_INVALID')
    m0, m1, m2, m3, qc, soak = [records[k] for k in APPEND_DIRS]
    for value, key, computed in (
        (m0, 'content_hash', record_hash(m0, ('content_hash',))),
        (m1, 'content_hash', record_hash(m1, ('content_hash', 'previous_synthesis_hash'))),
        (m2, 'batch_hash', record_hash(m2.get('anomalies'), compact=False)),
        (m3, 'batch_hash', record_hash(m3, ('batch_hash',))),
        (qc, 'qc_batch_hash', record_hash(qc, ('qc_batch_hash',), False)),
        (soak, 'report_hash', record_hash(soak, ('report_hash',), False))):
        require(type(value.get(key)) is str and re.fullmatch(r'sha256:[0-9a-f]{64}', value[key])
                and value[key] == computed, 'FRESH_RECORD_CONTENT_HASH_INVALID')
    stamp = m0.get('harvested_at_utc')
    # The application records whole seconds; fresh append plus current manifest
    # proves this invocation while avoiding a false microsecond race.
    require(utc(stamp) >= utc(started).replace(microsecond=0), 'INHERITED_M0_TIMESTAMP')
    require(m0.get('snapshot_manifest_sha256') == 'sha256:' + manifest_sha
            and m0.get('adapter_registry_source_sha256') == 'sha256:' + registry_sha
            and m0.get('inventory_sha256') == 'sha256:' + inventory_sha, 'FRESH_M0_PROVENANCE_DRIFT')
    require(set(m0.get('clanks', {})) == LANES, 'FRESH_M0_LANE_SCOPE_DRIFT')
    zero = m0.get('read_only_proof_total_changes')
    expected_db = {v[2] for v in SQLITE_SOURCES.values()} | ({'feature_phone_clank.db'} if expected_fp == 'SUCCESS' else set())
    require(type(zero) is dict and set(zero) == expected_db
            and all(type(x) is int and x == 0 for x in zero.values()), 'READONLY_DB_CORROBORATION_FAILED')
    for cid in ('chinese-tech-wire', 'semiconductor-intelligence'):
        if cid not in LANES:
            continue
        block = m0['clanks'][cid]
        require(block.get('observation') in ('SNAPSHOT_SCHEMA_UNVERIFIED', 'CHILD_EXECUTION_UNKNOWN')
                and block.get('snapshot_provenance', {}).get('effective_freshness_state') == 'UNKNOWN'
                and not any(isinstance(v, dict) and v.get('observation') == 'FAILED_ADAPTER'
                            for v in block.values()),
                'UNKNOWN_SCHEMA_CLOCK_BOUNDARY_LOST')
    for cid in ('korean-tech-wire', 'oem-radar'):
        if cid not in LANES:
            continue
        block = m0['clanks'][cid]
        require(not block.get('observation') and block.get('snapshot_provenance', {}).get(
                'effective_freshness_state') == 'FRESH'
                and not any(isinstance(v, dict) and v.get('observation') == 'FAILED_ADAPTER'
                            for v in block.values()), 'PROOF_PAIR_NOT_FRESH')
    fp = m0['clanks'][FP]
    if expected_fp == 'FAILED':
        require(fp.get('observation') == 'SNAPSHOT_REFRESH_FAILED'
                and fp.get('snapshot_provenance', {}).get('effective_freshness_state') == 'UNKNOWN',
                'NEGATIVE_STALE_SUCCESS_OR_BINDING_DIAGNOSIS')
    require(m1.get('snapshot_hash') == m0.get('content_hash')
            and m2.get('batch_generated_from') == stamp
            and m3.get('generated_from') == stamp
            and m3.get('anomaly_batch_hash') == m2.get('batch_hash'), 'FRESH_M0_M1_M2_M3_LINK_DRIFT')
    require(set(m1.get('clanks', {})) == LANES, 'FRESH_M1_LANE_SCOPE_DRIFT')
    for cid, claim in m1['clanks'].items():
        require(claim.get('state') in ('HEALTHY', 'DEGRADED', 'FAILED', 'UNKNOWN'), 'FRESH_M1_STATE_INVALID')
        if m0['clanks'][cid].get('snapshot_provenance', {}).get('effective_freshness_state') != 'FRESH':
            require(claim.get('state') == 'UNKNOWN' and not claim.get('evidence_derived_claims'),
                    'NONCURRENT_EVIDENCE_PROMOTED_AT_M1')
    require(qc.get('snapshot_hash') == m0.get('content_hash') and qc.get('generated_from') == stamp,
            'QC_CURRENT_M0_LINK_DRIFT')
    require(set((qc.get('corpus') or {}).get('clanks', {})) == {'korean-tech-wire'}, 'QC_SCOPE_DRIFT')
    # Existing preserved corpus may contain historical siblings; enabled current
    # QC input provenance must be KTW-only, while historical records stay intact.
    current_records = [r for r in (qc.get('corpus') or {}).get('records', [])
                       if r.get('ingestion_snapshot_hash') == m0.get('content_hash')]
    require(all(r.get('clank_id') == 'korean-tech-wire' for r in current_records), 'QC_SCOPE_DRIFT')
    require(soak.get('window', {}).get('latest') == stamp and not soak.get('qc_surface_failures'),
            'SOAK_CURRENT_QC_OR_SURFACE_FAILED')
    return {'status': 'FRESH_COMPLETE_PIPELINE_LINKS_PASS', 'expected_feature_phone': expected_fp,
            'current_m0': m0.get('content_hash'), 'current_m1': m1.get('content_hash'),
            'current_m2': m2.get('batch_hash'), 'current_m3': m3.get('batch_hash'),
            'current_qc': qc.get('qc_batch_hash'), 'current_soak': soak.get('report_hash'),
            'harvested_at_utc': stamp, 'new_append_each_phase': True,
            'readonly_db_total_changes': zero, 'promotion_or_maturity_claim': False}


def pipeline_commands(expected_fp):
    common = ['--manifest', '/app/real-state/manifest.json', '--registry', '/app/real-state/adapter-registry.json',
              '--inventory', '/app/inventory.yaml', '--spec', '/app/spec.json', '--image-id', IMAGE,
              '--expected-feature-phone', expected_fp]
    cli = ['-m', 'motherclank.cli']
    intake = ['--real-state', '/app/real-state', '--inventory', '/app/inventory.yaml',
              '--adapters-src', '/app/cops-000074/diagnostic-clank',
              '--adapter-registry', '/app/real-state/adapter-registry.json',
              '--snapshot-manifest', '/app/real-state/manifest.json',
              '--expected-adapter-package-sha', ADAPTER_SHA,
              '--expected-adapter-artifact-sha256', IMAGE]
    return {
        'preflight': [GATE, 'preflight'] + common,
        'harvest': cli + ['harvest'] + intake + ['--out', '/app/var'],
        'harvest-truth': [GATE, 'harvest-truth'] + common + ['--var', '/app/var'],
        'synthesize': cli + ['synthesize', '--var-dir', '/app/var', '--out', '/app/var'],
        'synthesis-truth': [GATE, 'synthesis-truth'] + common + ['--var', '/app/var'],
        'detect': cli + ['detect', '--var-dir', '/app/var', '--out', '/app/var'],
        'recommend': cli + ['recommend', '--var-dir', '/app/var', '--out', '/app/var'],
        'ingest-qc': cli + ['ingest-qc'] + intake + ['--var-dir', '/app/var', '--out', '/app/var'],
        'soak-report': cli + ['soak-report', '--var-dir', '/app/var', '--out', '/app/var'],
        'continuity': cli + ['validate-continuity', '--var-dir', '/app/var'],
    }


def complete_pipeline(label, real_state, var, accepted, results, nonce):
    expected_fp = 'SUCCESS' if label == 'positive' else 'FAILED'
    started = utc_now()
    inputs = tree_hashes(real_state)
    manifest_sha = digest(real_state / 'manifest.json')
    registry_sha = digest(real_state / 'adapter-registry.json')
    before = append_state(var)
    mounts = [mount(real_state, '/app/real-state'), mount(INVENTORY, '/app/inventory.yaml'), mount(SPEC, '/app/spec.json'),
              mount(var, '/app/var', True)]
    accepted_before = None
    if accepted is not None:
        mounts.append(mount(ACCEPTED, '/app/feature-phone-accepted'))
        accepted_before = tree_hashes(accepted)
    phase_results = []
    commands = pipeline_commands(expected_fp)
    for phase in PHASES:
        spec = container_spec(label + '-' + phase, commands[phase], mounts, nonce)
        phase_results.append(run_container(spec, results))
        require(tree_hashes(real_state) == inputs and digest(INVENTORY) == FILE_PINS[INVENTORY],
                'PIPELINE_READONLY_INPUT_CHANGED')
        if accepted is not None:
            require(tree_hashes(accepted) == accepted_before, 'ACCEPTED_INPUT_CHANGED_DURING_PIPELINE')
    fresh = appended_records(before, append_state(var))
    proof = validate_fresh_pipeline(fresh, manifest_sha, registry_sha, FILE_PINS[INVENTORY], expected_fp, started)
    proof.update(phases=phase_results, input_hashes=inputs,
                 accepted_export_hashes=accepted_before, accepted_mount=accepted is not None,
                 image=IMAGE, source_revision=SOURCE_SHA, adapter_revision=ADAPTER_SHA)
    write_new(results / (label + '-pipeline-proof.json'), proof)
    return proof
