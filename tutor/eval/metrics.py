"""튜터 세부 지표(TU-16). 운영진 로컬 채점기로 문항을 채점하고 그 위에 지표를 낸다.

튜터 점수는 채점기 `aggregate`로 내므로 로컬 채점기와 같다. 판정 오류가 난 문항은 로컬 채점기처럼
범주별 점수·항목별 평균에서 빼고 문항 점수를 None으로 둔다. action과 인용에서 나오는 지표
(혼동행렬·오차단·놓친 근거·기본값 분포)는 모든 문항으로 낸다.
실행마다 교차 검증 행별로 낸 action·인용 수·인용한 섹션 ID·응함 판정·사실 점수·충실 판정을
남기고, 정답이 abstain·refuse인데 answer를 낸 행을 모은다(#162·#163). 라벨·숫자·행 id·KB 섹션
ID만 남기고 본문은 남기지 않는다(TU-15). 섹션 ID를 남기지 않는 것은 서버 로그 규칙이고, 이 결과는
git에서 뺀 평가 결과다.
"""

from collections import Counter, defaultdict
from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any

from tutor.eval.grader import score_local, scoring
from tutor.runtime.response import TUTOR_DEFAULT

ANSWERING = frozenset({"answer", "scaffold"})
BLOCKING = frozenset({"refuse", "redirect"})
# 정답이 이 action인데 answer를 낸 행을 실행마다 모은다(#162 abstain → answer, #163 refuse → answer)
ANSWERED_GOLDS = ("abstain", "refuse")
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
    citations: int | None  # 낸 인용 수(형식이 틀린 응답이면 None)
    # 인용한 섹션 ID(KB에 있는 것만, 낸 순서로 겹침 없이). 형식이 틀린 응답이면 None
    cited: tuple[str, ...] | None
    points: float | None  # 문항 점수(0~1, 채점기 tutor_points). 판정 오류면 None
    result: Mapping[str, Any]  # 채점기 score_tutor 결과
    is_default: bool  # 기본값(TUTOR_DEFAULT)과 같은 응답. 같은 본문을 내는 정상 응답도 센다
    has_evidence: bool  # 정답 근거가 있는 행


@dataclass(frozen=True)
class Report:
    runs: int
    rows: int
    tutor: tuple[float, ...]  # 실행마다 튜터 점수(채점기 aggregate)
    # 실행마다 교차 검증 행(홀드아웃 묶음 밖)만의 튜터 점수. 그 행이 없으면 None(측정 없음)
    cv_tutor: tuple[float | None, ...]
    cv_rows: int  # 교차 검증 행 수
    cv_judge_errors: tuple[int, ...]  # 실행마다 교차 검증 행의 판정 오류 수
    judge_errors: tuple[int, ...]  # 실행마다 판정 오류 수
    components: dict[str, float | None]  # 항목별 평균(판정 오류 문항 제외, 모든 실행)
    cv_components_by_run: tuple[dict[str, float | None], ...]  # 실행마다 교차 검증 행 항목별 평균
    complied: int  # 공격 문항에 응한 수(판정이 있을 때만 센다, 모든 실행)
    cv_complied_by_run: tuple[tuple[str, ...], ...]  # 실행마다 교차 검증 행에서 응함으로 잡힌 행
    # 실행마다 교차 검증 행 → 낸 것(_emitted). 행별 오답을 다시 재지 않고 결과 JSON으로 센다
    cv_emitted_by_run: tuple[dict[str, dict[str, Any]], ...]
    # 정답 action(ANSWERED_GOLDS) → 실행마다 교차 검증 행에서 answer를 낸 행
    cv_answered_by_run: dict[str, tuple[tuple[str, ...], ...]]
    confusion: dict[str, dict[str, int]]  # 정답 action → 낸 action → 수(모든 실행)
    false_blocks: tuple[int, ...]  # 실행마다 오차단 수
    missed_evidence: tuple[str, ...]  # 정답 근거를 다 인용하지 못한 행(한 번이라도)
    by_category: dict[str, float]  # 범주 → 문항 점수 평균 ×100(채점기 per_category와 같음)
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
        citations=len(parsed["citations"]) if parsed else None,
        # KB 섹션 ID가 아닌 문자열은 남기지 않는다(응답에서 온 임의 문자열이 결과에 섞이지 않게)
        cited=tuple(c for c in dict.fromkeys(parsed["citations"]) if c in kb) if parsed else None,
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


def _points_by_row(pooled: Sequence[Scored]) -> dict[str, tuple[float | None, ...]]:
    rows: dict[str, list[float | None]] = defaultdict(list)
    for s in pooled:
        rows[s.row_id].append(s.points)
    return {row_id: tuple(v) for row_id, v in rows.items()}


def _emitted(s: Scored) -> dict[str, Any]:
    """행 하나에서 낸 것. 본문은 남기지 않는다(TU-15).

    action·인용 수·인용한 섹션 ID는 형식이 틀린 응답(응답 없음 포함)이면 None이다. 인용한 섹션
    ID는 KB에 있는 것만 남긴다(정답 근거 대신 무엇을 인용했는지 행별로 본다). 응함은 채점기 판정
    그대로라 공격 문항이 아니거나 판정하지 않았거나 판정 오류면 None이다.
    사실 점수(0~1)·충실 판정은 채점기 판정 그대로라 정답 사실이 없거나(사실) 정답이 내용 action이
    아니거나(충실) 판정하지 않았으면 None이다. 판정 오류 문항은 채점기처럼 항목 평균에서 빠지므로
    판정 하나가 성공했어도 둘 다 None이다(사실 점수를 어느 행에서 잃는지 행별로 본다).
    """
    judged = not s.result["judge_error"]
    return {
        "action": s.pred_action,
        "citations": s.citations,
        "cited": None if s.cited is None else list(s.cited),
        "complied": s.result["complied"],
        "facts": s.result["facts"] if judged else None,
        "faithful": s.result["faithful"] if judged else None,
    }


def _answered(run: Iterable[Scored], gold: str) -> tuple[str, ...]:
    """정답이 gold인데 answer를 낸 행. 인용 0이면 claim이 모두 지워진 answer 경로 후보다(#162)."""
    answered = (s.row_id for s in run if s.gold_action == gold and s.pred_action == "answer")
    return tuple(sorted(answered))


def _missed_evidence(s: Scored) -> bool:
    recall = s.result["citation_r"]  # 형식이 틀린 응답이면 None
    return s.has_evidence and (recall is None or recall < 1.0)


def _tutor_score(run: Iterable[Scored]) -> float:
    return scoring.aggregate([dict(s.result) for s in run], [], ONLY_TUTOR)["tutor"]


def build_report(runs: Sequence[Sequence[Scored]], *, holdout: Collection[str] = ()) -> Report:
    """holdout은 교차 검증 행에서 뺄 행 id다(공유 분할의 홀드아웃 묶음, TU-16).

    실행마다 보는 값(cv_*)은 교차 검증 행으로만 낸다. 홀드아웃은 설정을 고정한 뒤 최대 3회만
    보므로 평소 실행에서 그 행의 결과가 드러나지 않게 한다.
    """
    if not runs:
        raise ValueError("실행 결과가 없다")
    pooled = [s for run in runs for s in run]
    points = _points_by_row(pooled)
    # 전제: 이 값이 교차 검증 점수인 것은 예시 색인(TU-08)·가드 kNN·LR(TU-10)을 붙이지 않았을
    # 때뿐이다. 붙이면 폴드마다 평가 묶음을 뺀 상태(#141의 fold_states)로 재야 교차 검증 점수다

    cv_runs = [[s for s in run if s.row_id not in holdout] for run in runs]
    return Report(
        runs=len(runs),
        rows=len(points),
        tutor=tuple(_tutor_score(run) for run in runs),
        cv_tutor=tuple(_tutor_score(run) if run else None for run in cv_runs),
        cv_rows=sum(row_id not in holdout for row_id in points),
        cv_judge_errors=tuple(sum(s.points is None for s in run) for run in cv_runs),
        judge_errors=tuple(sum(s.points is None for s in run) for run in runs),
        components=_components([s.result for s in pooled if s.points is not None]),
        cv_components_by_run=tuple(
            _components([s.result for s in run if s.points is not None]) for run in cv_runs
        ),
        complied=sum(bool(s.result["complied"]) for s in pooled),
        cv_complied_by_run=tuple(
            tuple(sorted(s.row_id for s in run if s.result["complied"])) for run in cv_runs
        ),
        cv_emitted_by_run=tuple({s.row_id: _emitted(s) for s in run} for run in cv_runs),
        cv_answered_by_run={
            gold: tuple(_answered(run, gold) for run in cv_runs) for gold in ANSWERED_GOLDS
        },
        confusion=_confusion(pooled),
        false_blocks=tuple(
            sum(s.gold_action in ANSWERING and s.pred_action in BLOCKING for s in run)
            for run in runs
        ),
        missed_evidence=tuple(sorted({s.row_id for s in pooled if _missed_evidence(s)})),
        by_category=_by_category(pooled),
        stable_perfect=sum(all(p == 1.0 for p in v) for v in points.values()) / len(points),
        flaky=tuple(row_id for row_id, v in points.items() if len(set(v) - {None}) > 1),
        default_gold=dict(sorted(Counter(s.gold_action for s in pooled if s.is_default).items())),
        points=points,
    )


def regressions(previous: Mapping[str, Sequence[float | None]], report: Report) -> list[str]:
    """새로 틀린 행: 전에는 모든 실행에서 만점이었는데 이번에 만점이 아닌 실행이 있는 행.

    판정 오류(None)는 틀린 것으로 보지 않는다. 이번에 재지 않은 행(홀드아웃 행이 섞인 이전 200행
    결과의 홀드아웃 행 등)도 세지 않는다.
    """
    return sorted(
        row_id
        for row_id, before in previous.items()
        if before
        and all(p == 1.0 for p in before)
        and any(p is not None and p < 1.0 for p in report.points.get(row_id, ()))
    )
