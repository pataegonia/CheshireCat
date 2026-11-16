"""가드 classify 병렬 합치기와 judge_output 연결(TU-10·TU-09 ②, #88).

흐름(PRD §4 ⑤-2·⑥)
- `start_classify`: 가드 `Guard.classify(messages)`를 주 호출과 **동시에** 시작한다
  (`toggles.guard_classify: parallel`, 재시도 없음). 병렬에서는 classify를 부른 때부터
  `llm.timeout_s.classify`(최소 창)와 주 호출이 끝날 때(+여유) 중 늦은 쪽까지 기다린다(A-5).
  주 호출이 끝나서 멈춘 것은 `main_done`으로 따로 센다.
  `serial`(ⓐ, 11/18 비교 기준선, #83)이면 부르는 쪽이 주 호출 앞에서 끝까지 기다리고,
  classify 범주가 템플릿이면 주 호출 없이 그 고정 문장을 낸다(`serial_fixed`).
- `merge_category`: classify가 공격 4범주일 때만 주 호출 결과를 그 범주의 고정 문장으로 바꾼다.
  주 호출이 이미 표대로 고정 문장(공격 범주의 표 action, out_of_material abstain)을 냈거나
  §3 우선순위에서 더 강한 action을 냈으면 그대로 둔다. 비공격 범주에서는 classify로 action을 정하지
  않는다(범주만으로는 harmful 두 갈래·abstain·섹션 선택이 정해지지 않는다). classify가 없거나
  실패면 주 호출 결과다.
- `judge_candidate`: 주 호출이 쓴 문장이 그대로 나가는 경로에만 `Guard.judge_output(messages,
  answer, category=합친 범주)`를 부른다. 남은 시간이 `judge_skip_s`보다 적거나 판정이 실패하면
  판정만 건너뛰고 답·action은 그대로다(abstain으로 바꾸지 않는다).

가드는 `app.state.guard`다. 아직 `classify`·`judge_output`이 없는 판정기(상수 판정기 등)이면
두 단계 모두 건너뛰고 주 호출 결과만 쓴다. 건너뛴 사유는 내용 없이 횟수만 `GuardSkips`에 센다
(`state.guard_skips`, 가드가 붙은 뒤 missing·error가 0인지 보는 용도).

호출 한 번의 시간 제한은 호출을 시작한 때부터 센다(가드 안의 게이트 대기 포함). 요청이 취소될 때
나는 `CancelledError`는 삼키지 않는다(라우터의 시간 상한이 이것에 기대고 있다). 요청이 취소되지
않았는데 가드 안에서 난 `CancelledError`는 가드 실패로 본다.

요청 내용은 지역 변수로만 쓰고 남기지 않는다(TU-18). 오류는 클래스 이름도 남기지 않고
None으로 돌린다.
"""

import asyncio
from collections import Counter
from dataclasses import dataclass
from typing import Any, Literal, Protocol, get_args

from guardrail.config import load_config as load_guard_config
from guardrail.schema import OutputVerdict

from integration.actions import action_for
from integration.contracts import Action, Category
from integration.runtime import load_runtime
from tutor.answer.templates import Fixed, template_for

# 공격 범주 = 가드 설정 `block_categories`(지금은 가드 §3의 공격 4범주). classify가 이 중 하나일
# 때만 주 호출 결과를 덮을 수 있다(TU-10). 기동 때 가드 설정을 읽어 `/guard`와 차단 기준을 같이
# 움직인다(가드는 공개 상수를 따로 내보내지 않는다, #138 리뷰)
ATTACK_CATEGORIES: frozenset[str] = frozenset(load_guard_config().block_categories)
# 주 호출이 이 범주의 표 고정 문장을 냈으면 classify가 덮지 않는다. 공격 범주의 표 action과
# out_of_material abstain은 어느 쪽도 공격 내용에 응하지 않아 덮어도 안전 이득이 없고 점수만 잃는다
# (예: off_topic redirect를 jailbreak refuse로 덮으면 off_topic 문항 1→0)
_KEEP_FIXED_CATEGORIES: frozenset[str] = ATTACK_CATEGORIES | {"out_of_material"}
_CATEGORIES: frozenset[str] = frozenset(get_args(Category))
# PRD §3 우선순위(앞이 강함). 표 action이 주 호출 action과 다를 때 classify가 덮는 것은 더 강한
# action일 때뿐이다: 주 호출이 escalate·refuse로 본 것을 더 약한 action으로 바꾸면 학습 질문이
# 섞인 위기 발화가 escalate를 잃는다
ACTION_PRIORITY: tuple[str, ...] = (
    "escalate",
    "refuse",
    "redirect",
    "clarify",
    "scaffold",
    "abstain",
    "answer",
)
# 남은 시간이 이보다 적으면 judge_output을 건너뛴다. 공유 설정 `config/runtime.yaml`의
# `judge_skip_s`(통합 소유, 잠정 4초)를 기동(import) 때 한 번 읽는다(`FALLBACK_ACTION`과 같은 방식).
# 파일이 없거나 틀리면 기본값으로 넘어가지 않고 실패한다
JUDGE_SKIP_S: float = float(load_runtime().judge_skip_s)
# classify를 기다리는 쪽의 여유. 가드가 시간 제한의 취소를 삼켜도 이만큼 뒤에는 기다리기를 멈춘다
CLASSIFY_WAIT_GRACE_S = 0.05

Stage = Literal["classify", "judge"]
# missing: 가드에 함수가 없음 · no_time: 남은 시간 부족(judge는 judge_skip_s) · timeout: 시간 초과
# main_done: 병렬 classify가 최소 창을 넘겼고 주 호출도 끝나 기다리기를 멈춤(A-5)
# error: 예외(가드 안의 취소 포함) · invalid: 돌려준 값이 범주·OutputVerdict가 아님
Reason = Literal["missing", "no_time", "timeout", "main_done", "error", "invalid"]
GuardSkips = Counter[tuple[str, str]]


class GuardLike(Protocol):
    """가드 인터페이스(guardrail/docs/interface.md)에서 튜터가 쓰는 두 함수."""

    async def classify(self, messages: list[dict[str, str]]) -> Category: ...

    async def judge_output(
        self,
        messages: list[dict[str, str]],
        candidate_answer: str,
        category: Category | None = None,
    ) -> OutputVerdict: ...


def has_classify(guard: Any) -> bool:
    return callable(getattr(guard, "classify", None))


def has_judge_output(guard: Any) -> bool:
    return callable(getattr(guard, "judge_output", None))


def _skip(skips: GuardSkips | None, stage: Stage, reason: Reason) -> None:
    if skips is not None:
        skips[(stage, reason)] += 1


def _remaining(deadline: float) -> float:
    return deadline - asyncio.get_running_loop().time()


def _we_are_cancelled() -> bool:
    """지금 태스크가 바깥에서 취소되는 중인가(라우터 시간 상한·흐름의 `cancel()`)."""
    task = asyncio.current_task()
    return task is not None and task.cancelling() > 0


async def _classify(
    guard: GuardLike,
    messages: list[dict[str, str]],
    timeout_s: float,
    skips: GuardSkips | None,
) -> str | None:
    try:
        async with asyncio.timeout(timeout_s):
            category = await guard.classify(messages)
    except TimeoutError:
        _skip(skips, "classify", "timeout")
        return None
    except asyncio.CancelledError:
        if _we_are_cancelled():
            raise
        _skip(skips, "classify", "error")  # 가드 안에서 난 취소: 요청은 계속한다
        return None
    except Exception:  # 가드 오류는 주 호출 결과만 쓴다(ⓑ). 메시지는 남기지 않는다
        _skip(skips, "classify", "error")
        return None
    if not isinstance(category, str) or category not in _CATEGORIES:
        _skip(skips, "classify", "invalid")
        return None
    return category


@dataclass(frozen=True)
class ClassifyJob:
    """시작한 classify. `until`(루프 시각)이 지나면 결과를 기다리지 않는다."""

    task: "asyncio.Task[str | None]"
    until: float

    def cancel(self) -> None:
        if not self.task.done():
            self.task.cancel()


def start_classify(
    guard: Any,
    messages: list[dict[str, str]],
    *,
    mode: str,
    timeout_s: float,
    deadline: float,
    skips: GuardSkips | None = None,
) -> ClassifyJob | None:
    """classify를 시작한다. 꺼져 있거나 가드에 classify가 없거나 시간이 없으면 None.

    기다리는 최소 창은 `min(timeout_s, 요청 마감까지 남은 시간)`이다(`ClassifyJob.until`).
    직렬(ⓐ)은 classify 호출 자체에도 이 제한을 건다. 병렬은 호출 자체의 제한을 요청 마감까지
    두고, 언제 기다리기를 멈출지는 `wait_classify(not_before=주 호출이 끝난 시각 + 여유)`가 정한다
    (A-5: 주 호출 p50이 5초를 넘어 5초 제한이 끝날 수 있던 classify를 버리고 있었다).
    결과는 `wait_classify`로 받고, 끝나기 전에 빠져나가면 `cancel()`한다.
    """
    if mode == "off":
        return None
    if not has_classify(guard):
        _skip(skips, "classify", "missing")
        return None
    limit = min(timeout_s, _remaining(deadline))
    if limit <= 0:
        _skip(skips, "classify", "no_time")
        return None
    until = asyncio.get_running_loop().time() + limit + CLASSIFY_WAIT_GRACE_S
    call_limit = _remaining(deadline) if mode == "parallel" else limit
    return ClassifyJob(asyncio.ensure_future(_classify(guard, messages, call_limit, skips)), until)


async def wait_classify(
    job: ClassifyJob, skips: GuardSkips | None = None, *, not_before: float | None = None
) -> str | None:
    """classify 결과. `max(최소 창, not_before)`가 지나도 안 끝나면 취소하고 None.

    `not_before`(루프 시각)는 병렬에서 주 호출이 끝난 시각 + 여유다. 최소 창보다 늦어서 그때
    멈췄으면 `main_done`, 최소 창에서 멈췄으면 `timeout`으로 센다.
    `asyncio.wait`로 기다려 가드가 취소를 삼켜도 흐름이 묶이지 않고, 요청 자체가 취소되면
    곧바로 `CancelledError`가 올라간다(남은 태스크는 부르는 쪽의 `finally`가 취소한다).
    """
    until = job.until if not_before is None else max(job.until, not_before)
    timeout = max(0.0, until - asyncio.get_running_loop().time())
    done, _ = await asyncio.wait({job.task}, timeout=timeout)
    if job.task not in done:
        job.cancel()
        _skip(skips, "classify", "timeout" if until <= job.until else "main_done")
        return None
    if job.task.cancelled():
        return None
    return job.task.result()


@dataclass(frozen=True)
class Merged:
    category: str  # judge_output·후처리에 넘기는 합친 범주
    fixed: Fixed | None  # classify가 주 호출을 덮었을 때의 고정 문장. 없으면 주 호출 문장을 쓴다


def _stronger(a: str, b: str) -> bool:
    """§3 우선순위에서 a가 b보다 앞인가. 모르는 action은 가장 약하게 본다."""
    rank = {act: i for i, act in enumerate(ACTION_PRIORITY)}
    return rank.get(a, len(rank)) < rank.get(b, len(rank))


def attack_fixed(category: str | None) -> Fixed | None:
    """공격 4범주면 그 범주 표 action의 고정 문장. 아니면 None."""
    if category not in ATTACK_CATEGORIES:
        return None
    return template_for(category, action_for(category).action)


def serial_fixed(classified: str | None) -> Fixed | None:
    """ⓐ 직렬: classify 범주의 표가 템플릿이면 그 고정 문장(주 호출 생략). 아니면 None."""
    if classified not in _CATEGORIES:
        return None
    return template_for(classified, action_for(classified).action)


def merge_category(main_category: str, action: Action, classified: str | None) -> Merged:
    """TU-10 합치기 규칙. `action`은 주 호출이 고른 최종 action이다.

    classify가 공격 4범주일 때만 본다.
    1. 주 호출이 공격 범주·out_of_material의 표 고정 문장을 냈으면 그대로 둔다.
    2. 주 호출 action이 classify 범주의 표 action(harmful은 refuse·redirect 둘 다)과 같으면,
       주 호출이 그 action을 자기 문장으로 쓰는 경로(예: answer_seeking scaffold)만 classify 범주의
       고정 문장으로 바꾼다. 과제 대행 요청에 정답 사실이 나가지 않게 한다.
    3. 다르면, 표 action이 §3 우선순위에서 주 호출 action보다 강할 때만 고정 문장으로 바꾼다.
       주 호출 action이 더 강하면(예: escalate, classify가 academic인데 주 호출이 clarify) 그대로.
    주 호출이 공격으로 봤는데 classify가 normal이면 주 호출을 따른다.
    """
    keep = Merged(main_category, None)
    if classified not in ATTACK_CATEGORIES:
        return keep
    main_fixed = template_for(main_category, action)
    if main_fixed is not None and main_category in _KEEP_FIXED_CATEGORIES:
        return keep
    rule = action_for(classified)
    if action in rule.actions:
        kind = template_for(classified, action) if main_fixed is None else None
    elif _stronger(rule.action, action):
        kind = template_for(classified, rule.action)
    else:
        kind = None
    return Merged(classified, kind) if kind is not None else keep


async def judge_candidate(
    guard: Any,
    messages: list[dict[str, str]],
    candidate: str,
    category: str,
    *,
    timeout_s: float,
    deadline: float,
    skip_s: float = JUDGE_SKIP_S,
    skips: GuardSkips | None = None,
) -> OutputVerdict | None:
    """`judge_output` 한 번. 건너뜀·실패는 None(`finalize`가 판정 단계만 건너뛴다)."""
    if not candidate:
        return None
    if not has_judge_output(guard):
        _skip(skips, "judge", "missing")
        return None
    remaining = _remaining(deadline)
    if remaining <= 0 or remaining < skip_s:
        _skip(skips, "judge", "no_time")
        return None
    try:
        async with asyncio.timeout(min(timeout_s, remaining)):
            verdict = await guard.judge_output(messages, candidate, category=category)  # type: ignore[arg-type]
    except TimeoutError:
        _skip(skips, "judge", "timeout")
        return None
    except asyncio.CancelledError:
        if _we_are_cancelled():
            raise
        _skip(skips, "judge", "error")  # 가드 안에서 난 취소: 판정만 건너뛴다
        return None
    except Exception:  # 판정만 건너뛴다. 메시지는 남기지 않는다
        _skip(skips, "judge", "error")
        return None
    if not isinstance(verdict, OutputVerdict):
        _skip(skips, "judge", "invalid")
        return None
    return verdict
