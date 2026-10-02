"""Exact production expiry: accepted copy 06:15:03Z, harvest 16:26:23Z."""
import copy
import importlib.util
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location('copy_proof', Path(__file__).parents[1] / 'scripts/nas_production_library.py')
P = importlib.util.module_from_spec(spec)
spec.loader.exec_module(P)


def evidence(age=36679.789932):
    created = '2026-10-02T06:15:03.864539Z'
    clock = datetime.fromisoformat(created.replace('Z', '+00:00')) + timedelta(seconds=age)
    row = dict(clank_id=P.FP, instance_id='feature-phone-clank-nas-cops-000072', lane_id='experimental',
        snapshot_created_at=created, snapshot_path='/app/feature-phone-accepted/export-20261002T061502Z-716e3870f39d42ed/feature_phone_clank.db',
        snapshot_sha256='dbd412a3cb1898620e0fca010f6f34d9c5ac95107cc054298203ce944b63d6c4',
        child_as_of='2026-10-02T01:19:15.409216Z', freshness_horizon=dict(max_age_seconds=36000),
        freshness_state='FRESH', refresh_outcome='SUCCESS', observed_at=created,
        child_export=dict(attempt_id='export-20261002T061502Z-716e3870f39d42ed',status='SUCCESS'),
        adapter_package_sha='0770dd5f15be8a4a89bc43e5dd9644674d6683c0',adapter_artifact_sha256='a'*64)
    state = 'STALE' if age > 36000 else 'FRESH'
    block = dict(observation='SNAPSHOT_STALE' if state == 'STALE' else 'CHILD_EXECUTION_STALE',
        snapshot_provenance=dict(copy.deepcopy(row),intake_observed_at=clock.isoformat(),
                                 intake_freshness_state=state,effective_freshness_state='STALE'))
    return block,row


@pytest.mark.parametrize('age,expected',[(35999.999999,'VALID_AND_FRESH'),(36000,'VALID_AND_FRESH'),
                                       (36000.000001,'VALID_BUT_STALE'),(36679.789932,'VALID_BUT_STALE')])
def test_exact_ten_hour_boundary(age,expected):
    block,row=evidence(age)
    before=copy.deepcopy((block,row))
    assert P.feature_phone_copy_proof(block,row,'SUCCESS')==expected
    assert (block,row)==before


def test_actual_historical_success_does_not_require_current_proof():
    block,row=evidence()
    observed={'chinese_tech_wire.db':0,'korean_tech_wire.db':0,'radar.db':0,'semiconductor_intelligence.db':0}
    old_expected=set(observed)|{'feature_phone_clank.db'}
    assert old_expected != set(observed)  # Exact pre-repair failure.
    validity=P.feature_phone_copy_proof(block,row,'SUCCESS')
    required=set(observed)|({'feature_phone_clank.db'} if validity=='VALID_AND_FRESH' else set())
    assert required==set(observed) and validity=='VALID_BUT_STALE'


@pytest.mark.parametrize('field,value',[
    ('snapshot_sha256','0'*64),('child_export',{'attempt_id':'wrong'}),
    ('snapshot_created_at','2026-10-03T00:00:00Z'),('adapter_package_sha','0'*40),
    ('snapshot_path','/unapproved.db'),('refresh_outcome','FAILED')])
def test_tampered_derived_authority_rejected(field,value):
    block,row=evidence();block['snapshot_provenance'][field]=value
    with pytest.raises(P.ProofError,match='FP_MANIFEST_PROVENANCE_DRIFT'):
        P.feature_phone_copy_proof(block,row,'SUCCESS')


@pytest.mark.parametrize('age',[-301])
def test_future_copy_rejected(age):
    block,row=evidence(age)
    with pytest.raises(P.ProofError,match='FP_FUTURE_COPY_OR_OBSERVATION'):
        P.feature_phone_copy_proof(block,row,'SUCCESS')


def test_stale_cannot_be_promoted_or_freshness_policy_extended():
    block,row=evidence();block['observation']=None
    with pytest.raises(P.ProofError,match='FP_STALE_COPY_PROMOTED'): P.feature_phone_copy_proof(block,row,'SUCCESS')
    block,row=evidence()
    row['freshness_horizon']['max_age_seconds']=72000
    block['snapshot_provenance']['freshness_horizon']=copy.deepcopy(row['freshness_horizon'])
    with pytest.raises(P.ProofError,match='FP_FRESHNESS_POLICY_DRIFT'): P.feature_phone_copy_proof(block,row,'SUCCESS')


def test_invalid_clock_rejected():
    block,row=evidence();block['snapshot_provenance']['intake_observed_at']='not-a-clock'
    with pytest.raises(P.ProofError):P.feature_phone_copy_proof(block,row,'SUCCESS')
