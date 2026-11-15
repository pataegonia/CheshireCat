# ruff: noqa: T201, E501
"""1.1과 2.0 실행을 행 단위로 비교한다(API 0회). 행마다 3회 중 다수 범주를 잡고, 정답 대비 어느 쪽이 나빠졌는지 센다.

실행(guardrail/에서): uv run python -X utf8 scripts/r14/row_diff.py <1.1 responses.jsonl ...> -- <2.0 responses.jsonl ...>
"""

import json
import sys
from collections import Counter, defaultdict

from guardrail.config import load_config
from guardrail.data import load_public
from guardrail.eval import cv
from guardrail.schema import decision_of

cfg = load_config()
block = frozenset(cfg.block_categories)
split = cv.load_splits()
held = set(split.holdout)
pub = {r.id: r for r in load_public(cfg.paths.public) if r.endpoint == "guard"}


def votes(paths: list[str]) -> dict[str, Counter]:
    by: dict = defaultdict(Counter)
    for p in paths:
        for line in open(p, encoding="utf-8"):
            rec = json.loads(line)
            r = pub.get(rec["id"])
            if r is None or split.cluster[r.id] in held:
                continue
            by[r.id][rec["response"]["input"]["category"]] += 1
    return by


def top(c: Counter) -> str:
    return c.most_common(1)[0][0]


def dec(cat: str) -> str:
    return decision_of(cat, block)


sep = sys.argv.index("--")
a, b = votes(sys.argv[1:sep]), votes(sys.argv[sep + 1 :])
moves: Counter = Counter()
rows = []
for rid in sorted(set(a) & set(b)):
    gold = pub[rid].labels["category"]
    ca, cb = top(a[rid]), top(b[rid])
    if ca == cb:
        continue
    da, db = dec(ca) == dec(gold), dec(cb) == dec(gold)
    kind = (
        "block 고침"
        if db and not da
        else "block 망침"
        if da and not db
        else "범주 고침"
        if cb == gold
        else "범주 망침"
        if ca == gold
        else "범주 둘다 틀림"
    )
    moves[(kind, gold, ca, cb)] += 1
    text = pub[rid].messages[-1].content.replace("\n", " ")[:70]
    rows.append((kind, gold, ca, cb, dict(a[rid]), dict(b[rid]), text))

print("== 바뀐 행 요약 (종류, 정답, 1.1, 2.0): 수")
for k, n in sorted(moves.items(), key=lambda x: (x[0][0], -x[1])):
    print(n, k)
print("\n== 2.0이 나빠진 행")
for r in rows:
    if "망침" in r[0]:
        print(r)
