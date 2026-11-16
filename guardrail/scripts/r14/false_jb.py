# ruff: noqa: T201, E501
"""정답 normal인데 jailbreak_injection으로 막은 행을 1.1·2.0 실행에서 모아 본다(API 0회).

실행(guardrail/에서): uv run python -X utf8 scripts/r14/false_jb.py <1.1 responses.jsonl ...> -- <2.0 responses.jsonl ...>
"""

import json
import sys
from collections import Counter, defaultdict

from guardrail.config import load_config
from guardrail.data import load_public
from guardrail.eval import cv

cfg = load_config()
split = cv.load_splits()
held = set(split.holdout)
pub = {r.id: r for r in load_public(cfg.paths.public) if r.endpoint == "guard"}


def jb(paths: list[str]) -> dict[str, Counter]:
    """행마다 jailbreak_injection으로 본 횟수와 근거 문구."""
    by: dict = defaultdict(Counter)
    for p in paths:
        for line in open(p, encoding="utf-8"):
            rec = json.loads(line)
            r = pub.get(rec["id"])
            if r is None or split.cluster[r.id] in held or r.labels["category"] != "normal":
                continue
            by[r.id]["n"] += 1
            if rec["response"]["input"]["category"] == "jailbreak_injection":
                by[r.id]["jb"] += 1
    return by


sep = sys.argv.index("--")
a, b = jb(sys.argv[1:sep]), jb(sys.argv[sep + 1 :])
ids = sorted(
    {i for i in a if a[i]["jb"]} | {i for i in b if b[i]["jb"]},
    key=lambda i: -(a[i]["jb"] + b[i]["jb"]),
)
print(f"오탐 행 {len(ids)}개 (1.1 jb/회, 2.0 jb/회, 폴드, 턴 수)")
for i in ids:
    r = pub[i]
    turns = " | ".join(f"{m.role[0]}:{m.content}" for m in r.messages[-3:]).replace("\n", " ")
    print(
        f"- {a[i]['jb']}/{a[i]['n']} {b[i]['jb']}/{b[i]['n']} f{split.fold[split.cluster[i]]} t{len(r.messages)} :: {turns[-260:]}"
    )
