"""Host-only contract tests; no NAS, DB, export, Docker, or production run."""
import ast
import hashlib
import importlib.util
import json
import os
import re
import subprocess
import sys
import types
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from unittest.mock import Mock

import pytest

HERE = Path(__file__).parent
ROOT = HERE.parent


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, str(path))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


R = load('governed_runner_tested', ROOT / 'scripts/nas_production_runner.py')
P = load('governed_library_tested', ROOT / 'scripts/nas_production_library.py')


@pytest.fixture(autouse=True)
def posix_mount_model(monkeypatch):
    """Model NAS paths without pretending Windows temp directories are NAS."""
    monkeypatch.setattr(P, 'Path', PurePosixPath)
    monkeypatch.setattr(P, 'ACCEPTED', PurePosixPath(P.ACCEPTED.as_posix()))
    def mount(source, destination, rw=False):
        value = source.as_posix() if hasattr(source, 'as_posix') else str(source)
        if re.match(r'^[A-Za-z]:/', value):
            value = '/mock-filesystem/' + value[3:]
        return {'Source': value, 'Destination': destination, 'RW': rw, 'Type': 'bind'}
    monkeypatch.setattr(P, 'mount', mount)
    from test_observer_topology import governed_case, ADAPTER, IMAGE
    from motherclank.observer_topology import Topology
    inventory, _, _ = governed_case(True)
    policy = Topology(inventory, adapter_sha=ADAPTER, image_id=IMAGE)
    monkeypatch.setattr(P, 'IMAGE', IMAGE)
    monkeypatch.setattr(P, 'ADAPTER_SHA', ADAPTER)
    monkeypatch.setattr(P, 'SOURCE_SHA', 'a' * 40)
    monkeypatch.setattr(P, 'LANES', frozenset(policy.children))
    monkeypatch.setattr(P, 'SQLITE_SOURCES', {cid: (*source, policy.children[cid]['observer']['snapshot_spec']['snapshot_filename'])
                                          for cid, source in policy.sources.items()})
    monkeypatch.setattr(P, 'SPEC', PurePosixPath('/sealed/snapshot-spec.json'))
    monkeypatch.setattr(P, 'REGISTRY', PurePosixPath('/sealed/adapter-registry.json'))
    monkeypatch.setattr(P, 'INVENTORY', PurePosixPath('/sealed/observer-inventory.json'))


def receipt(status='SUCCESS'):
    value = {key: None for key in P.RECEIPT_KEYS}
    value.update(request_format_version='1.0', status=status,
        request_id='fp-request-20261002T061502Z-1234567890abcdef',
        request_started_at='2026-10-02T06:15:02Z', request_completed_at='2026-10-02T06:15:09Z',
        exporter_revision=P.EXPORTER_SHA, export_image=P.EXPORT_IMAGE, deployed_revision=P.DEPLOYED_SHA)
    if status == 'SUCCESS':
        value.update(attempt_id='export-20261002T061502Z-1234567890abcdef',
            artifact_sha256='a' * 64, metadata_sha256='b' * 64, publication_sha256='c' * 64,
            snapshot_bytes=100, publication_path=str(P.ACCEPTED / 'export-20261002T061502Z-1234567890abcdef'))
    else:
        value.update(error_code='BACKUP_FAILED', error_stage='EXPORT', last_good_snapshot_ref='f' * 64)
    context = {'request_id': value['request_id'], 'request_started_at': value['request_started_at'],
        'prior_attempt_id': 'export-20261001T101302Z-65343cdf997f4587', 'receipt_sha256': 'e' * 64}
    return value, context


@pytest.mark.parametrize('status', ['SUCCESS', 'FAILED'])
def test_real_receipts_dispatch(status):
    value, context = receipt(status)
    assert R.validate_dispatch(P, value, context, 'e' * 64, '2026-10-02T06:15:10Z',
                              '2026-10-02T06:15:00Z') == status


@pytest.mark.parametrize('mutation', [
    lambda r, c: r.update(status='UNAVAILABLE'),
    lambda r, c: r.update(exporter_revision='0' * 40),
    lambda r, c: r.update(request_started_at='2026-10-01T06:15:00Z'),
    lambda r, c: c.update(receipt_sha256='d' * 64),
    lambda r, c: c.update(request_id='fp-request-20261002T061502Z-fedcba0987654321'),
    lambda r, c: r.update(request_completed_at='2026-10-02T06:15:11Z'),
    lambda r, c: r.update(attempt_id=c['prior_attempt_id']),
    lambda r, c: c.update(extra=True),
    lambda r, c: r.update(error_code='RAW secret error'),
])
def test_success_wrong_pins_time_context_never_dispatch(mutation):
    value, context = receipt()
    mutation(value, context)
    with pytest.raises((R.RunnerError, P.ProofError)):
        R.validate_dispatch(P, value, context, 'e' * 64, '2026-10-02T06:15:10Z', '2026-10-02T06:15:00Z')


@pytest.mark.parametrize('mutation', [
    lambda r, c: r.update(artifact_sha256='f' * 64),
    lambda r, c: r.update(attempt_id=c['prior_attempt_id']),
    lambda r, c: r.update(publication_path=str(P.ACCEPTED)),
    lambda r, c: r.update(error_stage='OTHER'),
    lambda r, c: r.update(error_code='Bad unbounded diagnostic'),
    lambda r, c: r.update(status='UNAVAILABLE'),
])
def test_failed_never_reuses_artifact_or_unavailable(mutation):
    value, context = receipt('FAILED')
    mutation(value, context)
    with pytest.raises(R.RunnerError):
        R.validate_dispatch(P, value, context, 'e' * 64, '2026-10-02T06:15:10Z', '2026-10-02T06:15:00Z')


@pytest.mark.parametrize('status', ['SUCCESS', 'FAILED'])
def test_producer_exact_ro_inputs_and_same_translator(tmp_path, status):
    library = types.SimpleNamespace(**P.__dict__)
    library.make_directory = lambda p, *a: p.mkdir() or p
    library.seal_tree = Mock()
    library.read_json = lambda p: json.loads(p.read_text())
    captured = []
    def run(spec, results):
        captured.append(spec)
        root = tmp_path / 'snapshots/snapshot-v1-20261002T061502Z'
        root.mkdir()
        four = {'lanes': [{'clank_id': cid} for cid in library.SQLITE_SOURCES]}
        fp = {'clank_id': library.FP, 'refresh_outcome': status,
              'freshness_state': 'REFRESH_FAILED', 'child_execution_freshness': 'UNKNOWN',
              'snapshot_path': None, 'snapshot_sha256': None, 'snapshot_bytes': None}
        (root / 'sqlite-source-manifest.json').write_text(json.dumps(four))
        (root / 'manifest.json').write_text(json.dumps({'lanes': four['lanes'] + [fp]}))
    library.run_container = run
    root = R.produce_inputs(library, status, Path('/sealed/receipt.json'), Path('/sealed/context.json'),
                            tmp_path, '1234567890abcdef')
    assert root.name.startswith('snapshot-v1-')
    spec = captured[0]
    assert spec['command'][0] == P.CONSUMER
    assert sum(m['RW'] for m in spec['mounts']) == 1
    assert {m['Source'] for m in spec['mounts'] if m['Source'] in {v[0] for v in P.SQLITE_SOURCES.values()}} == {v[0] for v in P.SQLITE_SOURCES.values()}
    assert all(not m['RW'] for m in spec['mounts'] if m['Source'] in {v[0] for v in P.SQLITE_SOURCES.values()})
    accepted = [m for m in spec['mounts'] if m['Destination'] == '/app/feature-phone-accepted']
    assert bool(accepted) == (status == 'SUCCESS')
    assert all(not m['RW'] for m in accepted)
    assert all(not m['Source'].startswith('/volume2/clank/feature-phone-clank/state') for m in spec['mounts'])
    assert not any('docker.sock' in m['Source'] or 'staging' in m['Source'] for m in spec['mounts'])
    library.seal_tree.assert_called_once()


@pytest.mark.parametrize('status', ['SUCCESS', 'FAILED'])
def test_frozen_all_ten_commands_and_mount_sandbox(status):
    commands = P.pipeline_commands(status)
    assert tuple(commands) == P.PHASES
    assert len(commands) == 10
    assert commands['continuity'][:3] == ['-m', 'motherclank.cli', 'validate-continuity']
    assert not any('inbox' in arg or 'send' in arg or 'deliver' in arg for cmd in commands.values() for arg in cmd)
    mounts = [P.mount(Path('/sealed/snapshots'), '/app/real-state'),
              P.mount(Path('/sealed/inventory'), '/app/inventory.yaml'), P.mount(R.LIVE_VAR, '/app/var', True)]
    if status == 'SUCCESS':
        mounts += [P.mount(P.ACCEPTED, '/app/feature-phone-accepted')]
    for phase, argv in commands.items():
        spec = P.container_spec(('positive-' if status == 'SUCCESS' else 'negative-') + phase,
                                argv, mounts, '1234567890abcdef')
        create = P.create_argv(spec)
        assert create.count('--mount') == len(mounts)
        assert create[create.index('--network') + 1] == 'none'
        assert create[create.index('--user') + 1] == '10001:10001'
        assert '--read-only' in create and '--cap-drop' in create
        assert sum(m['RW'] for m in mounts) == 1


def test_helper_timeout_marks_fatal_no_retry(tmp_path):
    library = types.SimpleNamespace(**P.__dict__)
    library.call = Mock(side_effect=subprocess.TimeoutExpired(['fixed'], 900))
    library.write_new = Mock()
    old = R.ORPHAN
    try:
        R.ORPHAN = tmp_path / 'orphan-risk.json'
        with pytest.raises(R.RunnerError, match='QUIESCENCE_UNPROVEN'):
            R.request_export(library, tmp_path)
        marker = library.write_new.call_args.args[1]
        assert marker['retry_allowed'] is False
        assert marker['status'] == 'EXPORT_HELPER_QUIESCENCE_UNPROVEN'
        assert library.call.call_count == 1
    finally:
        R.ORPHAN = old


def test_helper_nonzero_cannot_replay_receipt(tmp_path):
    library = types.SimpleNamespace(**P.__dict__)
    library.call = Mock(return_value=types.SimpleNamespace(returncode=17, stdout='not a receipt', stderr=''))
    library.write_new = Mock()
    library.read_json = Mock(side_effect=AssertionError('receipt must not be opened'))
    old = R.ORPHAN
    try:
        R.ORPHAN = tmp_path / 'orphan-risk.json'
        with pytest.raises(R.RunnerError, match='NONZERO'):
            R.request_export(library, tmp_path)
        library.read_json.assert_not_called()
        assert library.write_new.call_args.args[1]['retry_allowed'] is False
    finally:
        R.ORPHAN = old


def scheduler_output(tid, state='enabled', hour='11', minute='45'):
    name = 'motherclank-nas-partial-cops-000072' if tid == 14 else 'oem-radar-canonical-cops-000072'
    command = '/bin/sh /volume2/clank/motherclank/deploy/scheduled-run.sh' if tid == 14 else '/bin/sh /volume2/clank/oem-radar/canonical-cops-000072/deploy/scheduled-run.sh'
    return ('ID: [%s]\nName: [%s]\nState: [%s]\nOwner: [root]\nType: [daily]\nCommand: [%s]\nRun time: [%s]:[%s]\n' %
            (tid, name, state, command, hour, minute)) + ('' if tid == 14 else 'Repeat every [1] hour\n')


@pytest.mark.parametrize('state', ['enabled', 'disabled'])
def test_readonly_dry_scheduler_states(state):
    library = types.SimpleNamespace(**P.__dict__)
    library.checked = Mock(side_effect=[scheduler_output(14, state), scheduler_output(15, hour='0', minute='50')])
    assert set(R.scheduler_truth(library, False)) == {'14', '15'}
    assert all(a.args[0][1] == '--get' for a in library.checked.call_args_list)


def test_task14_cadence_drift_is_fatal():
    library = types.SimpleNamespace(**P.__dict__)
    library.checked = Mock(return_value=scheduler_output(14, minute='46'))
    with pytest.raises(R.RunnerError, match='CADENCE'):
        R.scheduler_truth(library, False)


def test_existing_orphan_blocks_before_docker(tmp_path, monkeypatch):
    orphan = tmp_path / 'orphan-risk.json'
    orphan.write_text('{}')
    monkeypatch.setattr(R, 'ORPHAN', orphan)
    library = types.SimpleNamespace(**P.__dict__)
    library.checked = Mock()
    with pytest.raises(R.RunnerError, match='ORPHAN'):
        R.no_workers(library)
    library.checked.assert_not_called()


def test_runtime_does_not_call_frozen_isolated_entrypoints():
    source = (ROOT / 'scripts/nas_production_runner.py').read_text()
    tree = ast.parse(source, feature_version=(3, 8))
    forbidden = {'execute', 'admission', 'detached_vars', 'new_export', 'prepare_negative', 'negative_receipt'}
    assert not any(isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                   and node.func.attr in forbidden for node in ast.walk(tree))
    assert not hasattr(P, 'execute') and not hasattr(P, 'new_export')


def test_fixed_mode_rejects_general_paths():
    assert R.main(['--run', '/tmp/unapproved']) == 2
    assert R.main([]) == 2
    assert R.main(['--dry-check', '--execute-export']) == 2


def test_ro_intake_no_sender_env_or_general_root_mount():
    source = (ROOT / 'scripts/nas_production_runner.py').read_text()
    assert '--env-file' not in source
    assert 'negative_receipt(' not in source
    assert 'prepare_negative(' not in source
    assert 'detached_vars(' not in source
    assert 'shutil.rmtree' not in source and 'shutil.move' not in source
    assert 'wait_oem_sidecars(utc_now(), 900)' in source


def test_lock_busy_fails_without_create_or_truncate(tmp_path, monkeypatch):
    lock = tmp_path / 'lock'
    lock.write_bytes(b'preserved')
    monkeypatch.setattr(R, 'LOCK', lock)
    def busy(fd, mode):
        raise BlockingIOError()
    fake = types.SimpleNamespace(LOCK_EX=2, LOCK_NB=4, flock=busy)
    monkeypatch.setitem(sys.modules, 'fcntl', fake)
    monkeypatch.setattr(os, 'O_NOFOLLOW', getattr(os, 'O_NOFOLLOW', 0), raising=False)
    with pytest.raises(R.RunnerError, match='BUSY'):
        with R.live_lock(P):
            raise AssertionError('should not enter')
    assert lock.read_bytes() == b'preserved'


def test_dispatch_failure_preserves_last_good_reference_only():
    value, context = receipt('FAILED')
    assert R.validate_dispatch(P, value, context, 'e' * 64, '2026-10-02T06:15:10Z', '2026-10-02T06:15:00Z') == 'FAILED'
    assert value['last_good_snapshot_ref'] == 'f' * 64
    assert value['publication_path'] is None and value['attempt_id'] is None


@pytest.mark.parametrize('status', ['SUCCESS', 'FAILED'])
@pytest.mark.parametrize('mode', ['SCHEDULED', 'OPERATOR_TRIGGERED', 'QUALIFICATION'])
def test_live_orchestration_uses_exact_pipeline_and_truthful_qualification(tmp_path, monkeypatch, status, mode):
    library = types.SimpleNamespace(**P.__dict__)
    saved, calls = {}, []
    def make(p, *args):
        p.mkdir()
        return p
    library.make_directory = make
    library.plain = Mock()
    library.write_new = lambda p, value: saved.__setitem__(p.name, value)
    library.digest = lambda p: 'a' * 64
    library.wait_oem_sidecars = Mock(return_value={'writer_invoked': False})
    library.complete_pipeline = lambda label, state, var, accepted, results, nonce: calls.append(
        (label, state, var, accepted, results, nonce)) or {'phases': [None] * 10}
    library.FILE_PINS = {library.INVENTORY: 'b' * 64}
    monkeypatch.setattr(R, 'STATE', tmp_path / 'state')
    monkeypatch.setattr(R, 'RESULTS', tmp_path / 'state/attempts')
    monkeypatch.setattr(R, 'LIVE_VAR', tmp_path / 'LIVE_VAR')
    monkeypatch.setattr(R, 'authority', Mock(return_value={'stable': True}))
    @contextmanager
    def locked(lib):
        yield
    monkeypatch.setattr(R, 'live_lock', locked)
    monkeypatch.setattr(R, 'open_scheduled_log', lambda: None)
    monkeypatch.setattr(R, 'emit_log', Mock())
    value, context = receipt(status)
    cp, rp = Path('/sealed/context.json'), Path('/sealed/receipt.json')
    accepted = Path('/sealed/accepted-attempt') if status == 'SUCCESS' else None
    monkeypatch.setattr(R, 'request_export', Mock(return_value=(value, context, rp, cp, accepted)))
    monkeypatch.setattr(R, 'reuse_export', Mock(return_value=(value, context, rp, cp, accepted)))
    detached = tmp_path / 'detached'
    monkeypatch.setattr(R, 'detached_var', Mock(return_value=detached))
    snapshot = Path('/new/snapshot-v1-current')
    monkeypatch.setattr(R, 'produce_inputs', Mock(return_value=snapshot))
    library.digest = lambda p: context['receipt_sha256'] if p == rp else 'a' * 64
    monkeypatch.setattr(R, 'digest', lambda p: 'c' * 64)
    assert R.execute(library, {}, 'c' * 64, mode) == 0
    assert len(calls) == 1
    assert calls[0][:4] == ('positive' if status == 'SUCCESS' else 'negative', snapshot,
                           detached if mode == 'QUALIFICATION' else R.LIVE_VAR, accepted)
    assert R.request_export.call_count == (1 if mode == 'SCHEDULED' else 0)
    assert R.reuse_export.call_count == (0 if mode == 'SCHEDULED' else 1)
    assert library.wait_oem_sidecars.call_args.args[1] == 900
    summary = saved['run-summary.json']
    assert summary['trigger'] == mode
    assert summary['production'] == (mode != 'QUALIFICATION')
    assert summary['live_var_written'] == (mode != 'QUALIFICATION')
    assert summary['export_requested'] == (mode == 'SCHEDULED')
    assert summary['feature_phone_refresh_outcome'] == status
    assert summary['accepted_mount'] == (status == 'SUCCESS')
    assert summary['manufactured_negative'] is False
    assert summary['scheduled_origin_independently_verified'] is False
    assert summary['board_observation_or_unblock_claimed'] is False
    assert summary['mission_completion_claimed'] is False
    assert summary['process_exit'] == 0
    if status == 'FAILED':
        assert summary['natural_qualification'] == 'FAILED_EXPORT_DEGRADED_NOT_CUTOVER_PASS'
    else:
        assert summary['natural_qualification'] == 'SUCCESS_EXPORT_REQUIRES_OWNER_POSTRUN_CENSUS'


def test_orchestration_authority_drift_prevents_export(tmp_path, monkeypatch):
    library = types.SimpleNamespace(**P.__dict__)
    monkeypatch.setattr(R, 'authority', Mock(side_effect=R.RunnerError('TASK_AUTHORITY_DRIFT')))
    @contextmanager
    def locked(lib):
        yield
    monkeypatch.setattr(R, 'live_lock', locked)
    export = Mock(side_effect=AssertionError('must not export'))
    monkeypatch.setattr(R, 'request_export', export)
    monkeypatch.setattr(R, 'emit_log', Mock())
    assert R.execute(library, {}, 'a' * 64, 'SCHEDULED') == 1
    export.assert_not_called()


def test_authority_rehashes_files_helper_and_acl_each_check(tmp_path, monkeypatch):
    library = types.SimpleNamespace(**P.__dict__)
    library.INVENTORY = tmp_path / R.INVENTORY_NAME
    library.linux_acl = Mock(return_value='linux-acl-hash')
    library.image_truth = Mock()
    library.path_identity = Mock(return_value=(1, 2, 0, 0, 0o700))
    export = {'Id': P.EXPORT_IMAGE, 'Config': {'User': '10001:10001', 'Labels': {
        'org.opencontainers.image.revision': P.EXPORTER_SHA,
        'clank.observer_finalizer_sha256': '1a02db7ab0daadc3e7d5aaeca45eee1daf4bfccdcaa5b76826555d6ac6114fbe'}}}
    def checked(argv):
        return json.dumps([export]) if 'image' in argv else scheduler_output(14)
    library.checked = checked
    monkeypatch.setattr(R, 'scheduler_truth', Mock(return_value={'14': 'stable', '15': 'stable'}))
    monkeypatch.setattr(R, 'no_workers', Mock())
    monkeypatch.setattr(R, 'marker_truth', Mock(return_value={'hash': 'marker'}))
    monkeypatch.setattr(R, 'plain', Mock())
    pins = {R.RUNNER_NAME: 'a' * 64, R.SPEC_NAME: 'b' * 64}
    for name in set(pins) | {R.MANIFEST_NAME}:
        (tmp_path / name).touch()
    hashes = {library.INVENTORY.parent / R.MANIFEST_NAME: 'm' * 64,
              R.HELPER: R.HELPER_SHA}
    hashes.update({library.INVENTORY.parent / name: sha for name, sha in pins.items()})
    monkeypatch.setattr(R, 'digest', lambda p: hashes[p])
    assert R.authority(library, {'files': pins}, 'm' * 64, True)['pinned_linux_acl']
    assert library.linux_acl.call_count == 6
    hashes[library.INVENTORY.parent / R.RUNNER_NAME] = 'b' * 64
    with pytest.raises(R.RunnerError, match='PINNED_FILE_CHANGED'):
        R.authority(library, {'files': pins}, 'm' * 64, True)
    hashes[library.INVENTORY.parent / R.RUNNER_NAME] = 'a' * 64
    hashes[R.HELPER] = 'c' * 64
    with pytest.raises(R.RunnerError, match='PINNED_HELPER_CHANGED'):
        R.authority(library, {'files': pins}, 'm' * 64, True)


def proc_record(root, pid, parent, argv, uid=0, executable='/resolved/sh'):
    directory = root / str(pid)
    directory.mkdir()
    fields = ['S', str(parent)] + ['0'] * 17 + [str(pid * 100)]
    (directory / 'stat').write_text('%s (odd ) comm) %s\n' % (pid, ' '.join(fields)))
    (directory / 'status').write_text('Uid:\t%s\t%s\t%s\t%s\n' % (uid, uid, uid, uid))
    (directory / 'cmdline').write_bytes(b'\0'.join(x.encode() for x in argv) + b'\0')
    (directory / 'exe-target').write_text(executable)
    return directory


def proc_fixture(tmp_path, monkeypatch, sibling=False):
    proc = tmp_path / 'proc'
    proc.mkdir()
    launch = str(R.LIVE_LAUNCHER)
    proc_record(proc, 100, 90, ['/bin/python3', R.RUNNER_NAME, '--run'], executable='/resolved/python3')
    proc_record(proc, 90, 80, ['/bin/sh', launch])
    proc_record(proc, 80, 1, ['/bin/sh', '-c', '/bin/sh ' + launch])
    proc_record(proc, 1, 0, ['/sbin/init'], executable='/sbin/init')
    if sibling:
        proc_record(proc, 91, 80, ['/bin/sh', launch])
    monkeypatch.setattr(R, 'Path', lambda p: proc if str(p) == '/proc' else Path(p))
    monkeypatch.setattr(os, 'getpid', lambda: 100)
    monkeypatch.setattr(os, 'readlink', lambda p: (Path(p).parent / 'exe-target').read_text())
    monkeypatch.setattr(os.path, 'realpath', lambda p: '/resolved/sh' if p in ('/bin/sh', '/bin/bash', '/bin/ash') else p)
    monkeypatch.setattr(R, 'ORPHAN', tmp_path / 'no-orphan.json')
    return proc


def test_two_shell_dsm_ancestry_only_is_exempt(tmp_path, monkeypatch):
    proc_fixture(tmp_path, monkeypatch)
    library = types.SimpleNamespace(**P.__dict__)
    library.checked = Mock(return_value='')
    R.no_workers(library)


def test_sibling_launcher_worker_remains_blocked(tmp_path, monkeypatch):
    proc_fixture(tmp_path, monkeypatch, sibling=True)
    library = types.SimpleNamespace(**P.__dict__)
    library.checked = Mock(return_value='')
    with pytest.raises(R.RunnerError, match='RELEVANT_HOST_WORKER_PRESENT'):
        R.no_workers(library)


@pytest.mark.parametrize('changed', ['uid', 'exe', 'argv', 'parent'])
def test_ancestor_exemption_requires_root_exact_shell_invocation(tmp_path, monkeypatch, changed):
    proc = proc_fixture(tmp_path, monkeypatch)
    ancestor = proc / '80'
    identity = R.process_identity(ancestor)
    if changed == 'uid':
        (ancestor / 'status').write_text('Uid:\t10001\t10001\t10001\t10001\n')
    elif changed == 'exe':
        (ancestor / 'exe-target').write_text('/unknown/interpreter')
    elif changed == 'argv':
        (ancestor / 'cmdline').write_bytes(b'/bin/sh\0-c\0unapproved; /bin/sh ' + str(R.LIVE_LAUNCHER).encode() + b'\0')
    else:
        raw = (ancestor / 'stat').read_text().replace('S 1 ', 'S 2 ')
        (ancestor / 'stat').write_text(raw)
    assert not R.approved_launcher_shell(ancestor, (ancestor / 'cmdline').read_bytes(), identity)


@pytest.mark.parametrize('bad', ['extra-file', 'extra-directory', 'extra-pin', 'missing-pin'])
def test_package_exact_members_fail_before_import(tmp_path, monkeypatch, bad):
    members = {R.SPEC_NAME, R.REGISTRY_NAME, R.INVENTORY_NAME, R.RUNNER_NAME, R.LIBRARY_NAME, R.TOPOLOGY_NAME}
    pins = {name: 'a' * 64 for name in members}
    for name in members | {R.MANIFEST_NAME}:
        (tmp_path / name).touch()
    if bad == 'extra-file':
        (tmp_path / 'unlisted.py').touch()
    elif bad == 'extra-directory':
        (tmp_path / '__pycache__').mkdir()
    elif bad == 'extra-pin':
        pins['unreviewed.py'] = 'f' * 64
    else:
        pins.pop(R.TOPOLOGY_NAME)
    monkeypatch.setattr(R, 'plain', Mock())
    monkeypatch.setattr(R, 'digest', lambda p: 'b' * 64)
    monkeypatch.setattr(R, 'MANIFEST_SHA', 'b' * 64)
    doc = dict(R.EXPECTED, files=pins, source_revision='c' * 40, image='sha256:' + 'd' * 64)
    monkeypatch.setattr(R, 'read_json', lambda p: doc)
    with pytest.raises(R.RunnerError, match='PACKAGE_FILE_SET_INVALID|UNLISTED_PACKAGE_MEMBER'):
        R.load_package(tmp_path)


def fake_publisher(tmp_path):
    class Fatal(Exception):
        pass
    class LaneFailure(Exception):
        pass
    root = tmp_path / 'publication'
    root.mkdir()
    for directory in ('accepted', 'staging', 'failed'):
        (root / directory).mkdir()
    (root / 'accepted/old-sealed').mkdir()
    (root / 'failed/retained-failure').mkdir()
    source = tmp_path / 'canonical-stat-only.db'
    source.write_bytes(b'stat only')
    prep = object()
    sha = 'cedc18d482ee245e3e0c4d1a8ecf7b6bc1cd5a44d6ddaf085d5b7a7d09d99dbe'
    return types.SimpleNamespace(Fatal=Fatal, LaneFailure=LaneFailure, ROOT=root,
        SOURCE=source, PREFLIGHT=root / 'preflight.json', PREFLIGHT_SHA=sha,
        load_runtime=Mock(return_value=(object(), prep)),
        layout_admission=Mock(return_value=({'live_db_opened': False}, {}, {})),
        retention_admission=Mock(return_value={'attempt_ids': ['old-sealed']}),
        digest=Mock(return_value=sha),
        initialize_control=Mock(side_effect=AssertionError('must not initialize')),
        request_export=Mock(side_effect=AssertionError('must not export')),
        main=Mock(side_effect=AssertionError('must not execute')))


def test_publisher_dry_only_qualified_readonly_calls(tmp_path, monkeypatch):
    helper = fake_publisher(tmp_path)
    monkeypatch.setattr(R, 'load_publisher_library', lambda: helper)
    result = R.publisher_boundary()
    helper.load_runtime.assert_called_once_with()
    prep = helper.load_runtime.return_value[1]
    helper.layout_admission.assert_called_once_with(prep)
    helper.retention_admission.assert_called_once_with(helper.SOURCE.stat().st_size, prep)
    assert result['accepted_count'] == result['private_count'] == result['pin_count'] == 1
    assert result['canonical_source_access'] == 'STAT_ONLY_NO_SQLITE_OPEN'
    assert result['export_requested'] is False
    for method in (helper.initialize_control, helper.request_export, helper.main):
        method.assert_not_called()


@pytest.mark.parametrize('failure', ['capacity', 'boundary'])
def test_publisher_dry_failure_is_not_bypassed(tmp_path, monkeypatch, failure):
    helper = fake_publisher(tmp_path)
    if failure == 'capacity':
        helper.retention_admission.side_effect = helper.LaneFailure('ACCEPTED_CAPACITY_EXHAUSTED')
        expected = 'ACCEPTED_CAPACITY_EXHAUSTED'
    else:
        helper.layout_admission.side_effect = helper.Fatal('PREFLIGHT_RECORD_CHANGED')
        expected = 'PREFLIGHT_RECORD_CHANGED'
    monkeypatch.setattr(R, 'load_publisher_library', lambda: helper)
    with pytest.raises(R.RunnerError, match=expected):
        R.publisher_boundary()
    helper.main.assert_not_called()
    helper.request_export.assert_not_called()
