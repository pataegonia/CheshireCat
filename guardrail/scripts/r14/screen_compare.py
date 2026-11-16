"""선별·확정 실행을 1.1 기준선(v1en-en-r1–r3)과 행 단위로 비교한다(API 0회). 폴드 0–2/3–4로 나눠 고친 행·새로 틀린 행을 센다.

실행(guardrail/에서): uv run python -X utf8 scripts/r14/screen_compare.py <기준 runs 폴더> runs/<a>/responses.jsonl [...]
"""
import json, sys
from collections import defaultdict
from pathlib import Path
from guardrail.config import load_config
from guardrail.data import load_public
from guardrail.eval import cv
from guardrail.eval.run import _item
from guardrail.eval.score import scoring
from guardrail.schema import decision_of
cfg = load_config(); block = frozenset(cfg.block_categories)
split = cv.load_splits(); held = set(split.holdout)
pub = {r.id: r for r in load_public(cfg.paths.public) if r.endpoint == 'guard'}
def runs(path):
    by = defaultdict(dict)
    for line in open(path, encoding='utf-8'):
        rec = json.loads(line); r = pub.get(rec['id'])
        if r is None or split.cluster[r.id] in held: continue
        by[rec.get('repeat')][r.id] = rec['response']
    return list(by.values())
def ok(rid, resp):
    g = pub[rid].labels['category']
    return decision_of(g, block) == resp['input']['decision'] if 'decision' in resp['input'] else decision_of(g, block) == decision_of(resp['input']['category'], block)
base_dir = Path(sys.argv[1]); base = []
for n in ('v1en-en-r1', 'v1en-en-r2', 'v1en-en-r3'): base += runs(base_dir / n / 'responses.jsonl')
always_wrong = {i for i in pub if all(i in b for b in base) and all(not ok(i, b[i]) for b in base)}
always_right = {i for i in pub if all(i in b for b in base) and all(ok(i, b[i]) for b in base)}
attacks_right = {i for i in always_right if decision_of(pub[i].labels['category'], block) == 'block'}
grp = lambda i: '0-2' if split.fold[split.cluster[i]] <= 2 else '3-4'
print(f'base: always_wrong {len(always_wrong)} always_right {len(always_right)} attacks_always_right {len(attacks_right)}')
for path in sys.argv[2:]:
    for k, run in enumerate(runs(Path(path))):
        res = [scoring.score_guard(*_item(pub[i]), run[i]) for i in run]
        cats = [x['category_correct'] for x in res if x['category_scored']]
        fixed = defaultdict(int); broke = defaultdict(int)
        for i in always_wrong:
            if i in run and ok(i, run[i]): fixed[grp(i)] += 1
        for i in always_right:
            if i in run and not ok(i, run[i]): broke[grp(i)] += 1
        lost_att = sum(1 for i in attacks_right if i in run and not ok(i, run[i]))
        njb = sum(1 for i in run if pub[i].labels['category'] == 'normal' and run[i]['input']['category'] == 'jailbreak_injection')
        miss = sum(1 for i in run if decision_of(pub[i].labels['category'], block) == 'block' and not ok(i, run[i]))
        print(f'{Path(path).parent.name} r{k}: blockF1 {scoring.block_f1(res):.3f} cat {sum(cats)/len(cats):.3f} n->jb {njb} miss {miss} | fixed 0-2 {fixed["0-2"]} 3-4 {fixed["3-4"]} | broke 0-2 {broke["0-2"]} 3-4 {broke["3-4"]} | lost_attacks {lost_att}')
