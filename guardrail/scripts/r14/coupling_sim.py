"""입력 판정만 바꾼 실행(`--input-only`)이 출력 점수에 주는 영향을 호출 없이 근사한다.

입력 범주가 block이면 출력은 nano 없이 ①(grounded=true, 구간 없음)이 되고 safe는 범주로 정해진다
(`decide.output_verdict`·`safe_of`). 입력만 잰 실행은 출력을 상수로 두어 이 연결을 보지 못한다.
기준 실행(입력·출력 모두 잰 것)의 출력에 후보 실행의 입력 범주를 덮어써 가드 점수를 다시 계산한다.

실행(guardrail/에서):
uv run python -X utf8 scripts/r14/coupling_sim.py <기준 responses.jsonl ...> -- <후보 responses.jsonl ...>
기준과 후보를 같은 순서로 짝짓는다(회차끼리). 홀드아웃 행은 세지 않는다.
"""

import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

from guardrail.config import load_config
from guardrail.data import load_public
from guardrail.eval import cv
from guardrail.eval.run import _item
from guardrail.eval.score import scoring
from guardrail.schema import decision_of
from guardrail.signals import has_hidden_tags, is_refusal

cfg = load_config()
block = frozenset(cfg.block_categories)
split = cv.load_splits()
held = set(split.holdout)
pub = {r.id: r for r in load_public(cfg.paths.public) if r.endpoint == "guard"}


def runs(path: str) -> list[dict[str, dict]]:
    by: dict = defaultdict(dict)
    for line in open(path, encoding="utf-8"):
        rec = json.loads(line)
        r = pub.get(rec["id"])
        if r is None or split.cluster[r.id] in held:
            continue
        by[rec.get("repeat")][r.id] = rec["response"]
    return list(by.values())


def safe_for(cat: str, answer: str) -> bool:
    if has_hidden_tags(answer):
        return False
    return cat not in block or is_refusal(answer[: cfg.input.max_answer_chars])


def simulate(base: dict, cand: dict) -> tuple[dict, Counter]:
    """기준 응답에 후보 입력 범주를 덮어쓴다. 출력은 연결 규칙대로 다시 정한다."""
    out, kinds = {}, Counter()
    for rid, b in base.items():
        if rid not in cand:
            continue
        cat = cand[rid]["input"]["category"]
        resp = {"input": {"category": cat, "decision": decision_of(cat, block)}}
        if "output" in b:
            ans = pub[rid].candidate_answer or ""
            o = dict(b["output"])
            bcat = b["input"]["category"]
            if cat in block and bcat not in block:
                o.update(grounded=True, unsupported=[], safe=safe_for(cat, ans))
                kinds["allow→block(①로 고정)"] += 1
            elif cat not in block and bcat in block:
                o.update(safe=safe_for(cat, ans))  # nano 출력이 없어 기준(①) 그대로: 근사
                kinds["block→allow(출력 근사)"] += 1
            else:
                o.update(safe=safe_for(cat, ans))
                kinds["block 여부 같음"] += 1
            o["category"] = cat
            resp["output"] = o
        out[rid] = resp
    return out, kinds


def parts(resps: dict) -> dict:
    res = [scoring.score_guard(*_item(pub[i]), resps[i]) for i in resps]
    outs = [
        (float(x["output_correct"]) + x["span_f1"] + float(x["safe_correct"])) / 3
        for x in res
        if x["has_output"]
    ]
    cats = [x["category_correct"] for x in res if x["category_scored"]]
    return {
        "guard": scoring.guard_score(res),
        "blockF1": round(scoring.block_f1(res), 3),
        "cat": round(sum(cats) / len(cats), 3),
        "out": round(sum(outs) / len(outs), 3),
    }


sep = sys.argv.index("--")
bases = [r for p in sys.argv[1:sep] for r in runs(p)]
cands = [r for p in sys.argv[sep + 1 :] for r in runs(p)]
for k, (b, c) in enumerate(zip(bases, cands, strict=False)):
    sim, kinds = simulate(b, c)
    # 입력만 바꾸고 출력은 기준 그대로 둔 값(연결을 무시한 값)
    naive = {
        i: {**b[i], "input": {"category": c[i]["input"]["category"],
                              "decision": decision_of(c[i]["input"]["category"], block)}}
        for i in b if i in c
    }
    print(f"pair{k}: 기준 {parts(b)} | 입력만 {parts(naive)} | 연결 반영 {parts(sim)} | {dict(kinds)}")
