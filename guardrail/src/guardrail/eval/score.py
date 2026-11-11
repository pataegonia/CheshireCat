"""S1.4 채점 재현 (GR-13, master-plan §4.6).

채점 로직은 competition/grader·harness를 import해 그대로 쓴다(다시 쓰지 않는다).
가드 점수는 가드 200행으로, 튜터 행은 범주 정확도와 범주에서 도출한 decision의 block F1을 따로 낸다.
"""

import sys
from collections.abc import Iterable, Mapping

from guardrail.config import ROOT
from guardrail.data import PublicRow
from guardrail.schema import Category, decision_of

COMPETITION = ROOT.parent / "competition"
if not COMPETITION.is_dir():
    raise ImportError("competition/ 폴더를 찾을 수 없다(채점 코드 import용)")
for _sub in ("grader", "harness"):  # 이 모듈 안에서만 경로를 넣는다
    _p = str(COMPETITION / _sub)
    if _p not in sys.path:
        sys.path.append(_p)

import scoring  # noqa: E402
from score_local import as_scoring_input  # noqa: E402


def _mean(xs: list[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0


def _tutor_report(
    rows: list[PublicRow], responses: Mapping[str, dict], block: frozenset[Category]
) -> dict:
    cats, f1_rows = [], []
    for r in rows:
        gold = r.labels["category"]
        parsed = scoring.parse_guard(responses.get(r.id), False)
        pred = parsed["input"] if parsed else None
        gold_dec = decision_of(gold, block)
        f1_rows.append(
            {
                "gold_block": gold_dec == "block",
                "input_correct": pred is not None and pred["decision"] == gold_dec,
            }
        )
        if gold != "normal":
            cats.append(float(pred is not None and pred["category"] == gold))
    return {"n": len(rows), "category_accuracy": _mean(cats), "block_f1": scoring.block_f1(f1_rows)}


def score_responses(
    rows: Iterable[PublicRow], responses: Mapping[str, dict], block: frozenset[Category]
) -> dict:
    """responses: {row id: /guard 응답 dict}. 없거나 형식이 틀리면 invalid로 센다."""
    rows = list(rows)
    guard = [r for r in rows if r.endpoint == "guard"]
    results = []
    for r in guard:
        item, variant = as_scoring_input(_as_dict(r))
        results.append(scoring.score_guard(item, variant, responses.get(r.id)))
    outs = [x for x in results if x["has_output"]]
    return {
        "guard_score": scoring.guard_score(results),
        "block_f1": scoring.block_f1(results),
        "category_accuracy": _mean(
            [float(x["category_correct"]) for x in results if x["category_scored"]]
        ),
        "invalid": sum(not x["valid"] for x in results),
        "output": {
            "n": len(outs),
            "mean": _mean(
                [
                    (float(x["output_correct"]) + x["span_f1"] + float(x["safe_correct"])) / 3
                    if x["valid"]
                    else 0.0
                    for x in outs
                ]
            ),
            "grounded": _mean([float(bool(x["output_correct"])) for x in outs]),
            "span_f1": _mean([x["span_f1"] or 0.0 for x in outs]),
            "safe": _mean([float(bool(x["safe_correct"])) for x in outs]),
        },
        "tutor": _tutor_report([r for r in rows if r.endpoint == "tutor"], responses, block),
    }


def _as_dict(r: PublicRow) -> dict:
    d = {
        "id": r.id,
        "endpoint": r.endpoint,
        "messages": [{"role": m.role, "content": m.content} for m in r.messages],
        "labels": dict(r.labels),
    }
    if r.candidate_answer is not None:
        d["candidate_answer"] = r.candidate_answer
    return d
