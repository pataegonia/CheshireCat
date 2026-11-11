"""튜터 세부 지표(TU-16). 운영진 로컬 채점기로 문항을 채점하고 그 위에 지표를 낸다.

튜터 점수는 채점기 `aggregate`로 내므로 로컬 채점기와 같다. 판정 오류가 난 문항은 로컬 채점기처럼
범주별 점수·항목별 평균에서 빼고 문항 점수를 None으로 둔다. action과 인용에서 나오는 지표
(혼동행렬·오차단·놓친 근거·기본값 분포)는 모든 문항으로 낸다.
"""

from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any

from tutor.eval.grader import score_local, scoring
from tutor.runtime.response import TUTOR_DEFAULT

ANSWERING = frozenset({"answer", "scaffold"})
BLOCKING = frozenset({"refuse", "redirect"})
ONLY_TUTOR = {"tutor": 1, "guard": 0}
WORKERS = 8  # 동시 채점·요청 수(로컬 채점기 기본값과 같다)
Judge = Callable[[str, dict[str, Any]], dict[str, Any]]


@dataclass(frozen=True)
class Scored:
    """문항 하나를 한 번 채점한 결과."""

    row_id: str
    category: str
    gold_action: str
    pred_action: str | None  # 형식이 틀린 응답이면 None
    points: float | None  # 문항 점수(0~1, 채점기 tutor_points). 판정 오류면 None
    result: Mapping[str, Any]  # 채점기 score_tutor 결과
    is_default: bool  # 기본값(TUTOR_DEFAULT)과 같은 응답. 같은 본문을 내는 정상 응답도 센다
    has_evidence: bool  # 정답 근거가 있는 행


@dataclass(frozen=True)
class AxisScore:
    """점검 세트 축 하나의 결과(TU-19). 모든 실행을 합쳐 센다."""

    cases: int  # 축의 문항 수
    action: float  # action 정확도(형식이 틀린 응답은 틀림)
    complied: int | None  # 공격 문항에 응한 응답 수(실행 합). 판정한 공격 응답이 없으면 None


@dataclass(frozen=True)
class Report:
    runs: int
    rows: int
    tutor: tuple[float, ...]  # 실행마다 튜터 점수(채점기 aggregate)
    judge_errors: tuple[int, ...]  # 실행마다 판정 오류 수
    components: dict[str, float | None]  # 항목별 평균(판정 오류 문항 제외, 모든 실행)
    complied: int  # 공격 문항에 응한 수(판정이 있을 때만 센다)
    confusion: dict[str, dict[str, int]]  # 정답 action → 낸 action → 수(모든 실행)
    false_blocks: tuple[int, ...]  # 실행마다 오차단 수
    missed_evidence: tuple[str, ...]  # 정답 근거를 다 인용하지 못한 행(한 번이라도)
    by_category: dict[str, float]  # 범주 → 문항 점수 평균 ×100(채점기 per_category와 같음)
    by_axis: dict[str, AxisScore]  # 점검 세트 축 → 결과(점검 세트를 잴 때만, 아니면 빈 dict)
    stable_perfect: float  # 모든 실행에서 문항 점수가 1인 행 ÷ 전체 행
    flaky: tuple[str, ...]  # 실행마다 문항 점수가 다른 행
    default_gold: dict[str, int]  # 기본값 응답의 정답 action 분포(모든 실행)
    points: dict[str, tuple[float | None, ...]]  # 행 → 실행마다 문항 점수(회귀 비교에 쓴다)


def score_one(
    row: Mapping[str, Any], response: Any, *, judge: Judge | None = None, kb: Mapping[str, str]
) -> Scored:
    """채점기 그대로 채점한다. 판정이 없으면 action·인용만 채점한다(score_local과 같은 규칙)."""
    item, variant = score_local.as_scoring_input(row)
    result = scoring.score_tutor(item, variant, response, judge or score_local._no_judge, kb)
    if judge is None:
        result["judge_error"] = False
    parsed = scoring.parse_tutor(response)
    return Scored(
        row_id=row["id"],
        category=row["labels"]["category"],
        gold_action=row["labels"]["action"],
        pred_action=parsed["action"] if parsed else None,
        points=None if result["judge_error"] else scoring.tutor_points(result),
        result=result,
        is_default=response == TUTOR_DEFAULT,
        has_evidence=bool(item["labels"]["evidence"]),
    )


def score_run(
    rows: Sequence[Mapping[str, Any]],
    responses: Mapping[str, Any],
    *,
    judge: Judge | None = None,
    kb: Mapping[str, str],
) -> list[Scored]:
    """한 실행의 응답을 채점한다. 응답이 없는 행은 형식 오류로 친다(로컬 채점기와 같음)."""

    def one(row: Mapping[str, Any]) -> Scored:
        return score_one(row, responses.get(row["id"]), judge=judge, kb=kb)

    # 중단(Ctrl-C)되면 Executor.map이 줄 서 있던 행을 취소해 판정을 더 부르지 않는다
    with ThreadPoolExecutor(WORKERS) as pool:
        return list(pool.map(one, rows))


def _mean(values: Iterable[float | None]) -> float | None:
    present = [v for v in values if v is not None]
    return sum(present) / len(present) if present else None


def _citation_f1(result: Mapping[str, Any]) -> float | None:
    p, r = result["citation_p"], result["citation_r"]
    if p is None or r is None:
        return None
    return 2 * p * r / (p + r) if p + r else 0.0


def _components(results: Sequence[Mapping[str, Any]]) -> dict[str, float | None]:
    return {
        "action": _mean(float(r["action_correct"]) for r in results),
        "citation": _mean(_citation_f1(r) for r in results),
        "facts": _mean(r["facts"] for r in results),
        "faithful": _mean(None if r["faithful"] is None else float(r["faithful"]) for r in results),
    }


def _confusion(pooled: Sequence[Scored]) -> dict[str, dict[str, int]]:
    table: dict[str, Counter[str]] = defaultdict(Counter)
    for s in pooled:
        table[s.gold_action][s.pred_action or "invalid"] += 1
    return {gold: dict(sorted(preds.items())) for gold, preds in sorted(table.items())}


def _by_category(pooled: Sequence[Scored]) -> dict[str, float]:
    groups: dict[str, list[float]] = defaultdict(list)
    for s in pooled:
        if s.points is not None:  # 로컬 채점기처럼 판정 오류 문항은 뺀다
            groups[s.category].append(s.points)
    return {c: round(100 * sum(v) / len(v), 2) for c, v in sorted(groups.items())}


def _by_axis(pooled: Sequence[Scored], axis_of: Mapping[str, str]) -> dict[str, AxisScore]:
    groups: dict[str, list[Scored]] = defaultdict(list)
    for s in pooled:
        groups[axis_of[s.row_id]].append(s)
    out: dict[str, AxisScore] = {}
    for axis, items in sorted(groups.items()):
        judged = [s.result["complied"] for s in items if s.result["complied"] is not None]
        out[axis] = AxisScore(
            cases=len({s.row_id for s in items}),
            action=round(sum(s.result["action_correct"] for s in items) / len(items), 3),
            complied=sum(judged) if judged else None,
        )
    return out


def _points_by_row(pooled: Sequence[Scored]) -> dict[str, tuple[float | None, ...]]:
    rows: dict[str, list[float | None]] = defaultdict(list)
    for s in pooled:
        rows[s.row_id].append(s.points)
    return {row_id: tuple(v) for row_id, v in rows.items()}


def _missed_evidence(s: Scored) -> bool:
    recall = s.result["citation_r"]  # 형식이 틀린 응답이면 None
    return s.has_evidence and (recall is None or recall < 1.0)


def build_report(
    runs: Sequence[Sequence[Scored]], *, axis_of: Mapping[str, str] | None = None
) -> Report:
    """axis_of(행 id → 점검 세트 축)를 주면 축별 결과도 낸다."""
    if not runs:
        raise ValueError("실행 결과가 없다")
    pooled = [s for run in runs for s in run]
    points = _points_by_row(pooled)
    return Report(
        runs=len(runs),
        rows=len(points),
        tutor=tuple(
            scoring.aggregate([dict(s.result) for s in run], [], ONLY_TUTOR)["tutor"]
            for run in runs
        ),
        judge_errors=tuple(sum(s.points is None for s in run) for run in runs),
        components=_components([s.result for s in pooled if s.points is not None]),
        complied=sum(bool(s.result["complied"]) for s in pooled),
        confusion=_confusion(pooled),
        false_blocks=tuple(
            sum(s.gold_action in ANSWERING and s.pred_action in BLOCKING for s in run)
            for run in runs
        ),
        missed_evidence=tuple(sorted({s.row_id for s in pooled if _missed_evidence(s)})),
        by_category=_by_category(pooled),
        by_axis=_by_axis(pooled, axis_of) if axis_of else {},
        stable_perfect=sum(all(p == 1.0 for p in v) for v in points.values()) / len(points),
        flaky=tuple(row_id for row_id, v in points.items() if len(set(v) - {None}) > 1),
        default_gold=dict(sorted(Counter(s.gold_action for s in pooled if s.is_default).items())),
        points=points,
    )


def regressions(previous: Mapping[str, Sequence[float | None]], report: Report) -> list[str]:
    """새로 틀린 행: 전에는 모든 실행에서 만점이었는데 이번에 만점이 아닌 실행이 있는 행.

    판정 오류(None)는 틀린 것으로 보지 않는다.
    """
    return sorted(
        row_id
        for row_id, before in previous.items()
        if before
        and all(p == 1.0 for p in before)
        and any(p is not None and p < 1.0 for p in report.points.get(row_id, ()))
    )
