import json

p = 'closeout/inputs/scheduler-traces-2026-08-26.jsonl'
recs = [json.loads(l) for l in open(p, encoding='utf-8') if l.strip()]
for r in recs:
    r['observed_at'] = '2026-08-26T19:42:00Z'
open(p, 'w', encoding='utf-8').write('\n'.join(json.dumps(r) for r in recs) + '\n')

ev = 'closeout/inputs/live-evidence-2026-08-26.jsonl'
lines = open(ev, encoding='utf-8').read().splitlines()
out = []
for l in lines:
    r = json.loads(l)
    if r['clank_id'] == 'smartwatch-clank':
        # deployed-at-verification is 7a5e551; the cli-backup fix (29aeeb0)
        # is merged upstream but host redeploy is pending -> captured as debt.
        r['deployed_commit_sha'] = '7a5e551bd6cdd313b142ccbdb977a717f2a083a0'
    out.append(json.dumps(r))
open(ev, 'w', encoding='utf-8').write('\n'.join(out) + '\n')
print('inputs normalized:', len(recs), 'traces,', len(out), 'evidence lanes')
