"""가드 classify 병렬 합치기와 judge_output 연결(TU-10·TU-09 ②, #88).

흐름(PRD §4 ⑤-2·⑥)
- `start_classify`: 가드 `Guard.classify(messages)`를 주 호출과 **동시에** 시작한다
  (`toggles.guard_classify: parallel`, 시간 제한 `llm.timeout_s.classify`, 재시도 없음).
  `serial`이면 부르는 쪽이 주 호출 앞에서 끝까지 기다린다(11/18 ⓐ 비교용, #83).
- `merge_category`: classify가 공격 4범주인데 주 호출 action이 그 범주의 표 action과 다르면 classify
  범주의 고정 문장으로 바꾼다. 비공격 범주에서는 classify로 action을 정하지 않는다(범주만으로는
  harmful 두 갈래·abstain·섹션 선택이 정해지지 않는다). classify가 없거나 실패면 주 호출 결과다.
- `judge_candidate`: 주 호출이 쓴 문장이 그대로 나가는 경로에만 `Guard.judge_output(messages,
  answer, category=합친 범주)`를 부른다. 남은 시간이 `judge_skip_s`보다 적거나 판정이 실패하면
  판정만 건너뛰고 답·action은 그대로다(abstain으로 바꾸지 않는다).

가드는 `app.state.guard`다. 아직 `classify`·`judge_output`이 없는 판정기(상수 판정기 등)이면
두 단계 모두 조용히 건너뛰고 주 호출 결과만 쓴다. 호출 한 번의 시간 제한은 호출을 시작한 때부터
센다(가드 안의 게이트 대기 포함). 취소(`CancelledError`)는 삼키지 않는다: 라우터의 시간 상한이
이것에 기대고 있다.

요청 내용은 지역 변수로만 쓰고 남기지 않는다(TU-18). 오류는 클래스 이름도 남기지 않고
None으로 돌린다.
"""

import asyncio
from dataclasses import dataclass
from typing import Any, Protocol

from guardrail.schema import OutputVerdict

from integration.actions import action_for
from integration.contracts import Action, Category
from tutor.answer.templates import Fixed, template_for

# 가드 §3의 공격 4범주. classify가 이 중 하나일 때만 주 호출 결과를 덮을 수 있다(TU-10)
ATTACK_CATEGORIES: frozenset[str] = frozenset(
    {"jailbreak_injection", "harmful_content", "off_topic", "academic_dishonesty"}
)
# PRD §3 우선순위(앞이 강함). classify가 덮는 것은 주 호출 action보다 강한 action일 때뿐이다:
# 주 호출이 escalate·refuse로 본 것을 더 약한 action으로 바꾸면 "공격 재현율은 둘 중 높은 쪽
# 이상"이 깨지고, 학습 질문이 섞인 위기 발화가 escalate를 잃는다
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
# `judge_skip_s`(통합 소유, 잠정 4초)와 같은 값이다. 그 파일을 읽는 코드가 들어오면
# `state.judge_skip_s`로 넘긴다
JUDGE_SKIP_S = 4.0


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


def _remaining(deadline: float) -> float:
    return deadline - asyncio.get_running_loop().time()


async def _classify(
    guard: GuardLike, messages: list[dict[str, str]], timeout_s: float
) -> str | None:
    try:
        async with asyncio.timeout(timeout_s):
            category = await guard.classify(messages)
    except Exception:  # 시간 초과·가드 오류 모두 주 호출 결과만 쓴다(ⓑ). 메시지는 남기지 않는다
        return None
    return category if isinstance(category, str) else None


def start_classify(
    guard: Any,
    messages: list[dict[str, str]],
    *,
    mode: str,
    timeout_s: float,
    deadline: float,
) -> "asyncio.Task[str | None] | None":
    """classify를 시작한다. 꺼져 있거나 가드에 classify가 없거나 시간이 없으면 None.

    시간 제한은 `min(timeout_s, 요청 마감까지 남은 시간)`이다. 돌려받은 태스크는 부르는 쪽이
    `await`하고, 끝나기 전에 빠져나가면 `cancel()`한다.
    """
    if mode == "off" or not has_classify(guard):
        return None
    limit = min(timeout_s, _remaining(deadline))
    if limit <= 0:
        return None
    return asyncio.ensure_future(_classify(guard, messages, limit))


@dataclass(frozen=True)
class Merged:
    category: str  # judge_output·후처리에 넘기는 합친 범주
    fixed: Fixed | None  # classify가 주 호출을 덮었을 때의 고정 문장. 없으면 주 호출 문장을 쓴다


def _stronger(a: str, b: str) -> bool:
    """§3 우선순위에서 a가 b보다 앞인가. 모르는 action은 가장 약하게 본다."""
    rank = {act: i for i, act in enumerate(ACTION_PRIORITY)}
    return rank.get(a, len(rank)) < rank.get(b, len(rank))


def merge_category(main_category: str, action: Action, classified: str | None) -> Merged:
    """TU-10 합치기 규칙. `action`은 주 호출이 고른 최종 action이다.

    classify가 공격 4범주이고 그 범주 표의 action(harmful은 refuse·redirect 둘 다)에 `action`이
    없고, 그 표 action이 §3 우선순위에서 `action`보다 강하면 그 범주의 고정 문장으로 바꾼다.
    주 호출이 공격으로 봤는데 classify가 normal이면 주 호출을 따른다. 주 호출 action이 더
    강하면(예: escalate, classify가 off_topic인데 주 호출이 refuse) 주 호출을 따른다.
    """
    if classified in ATTACK_CATEGORIES:
        rule = action_for(classified)
        if action not in rule.actions and _stronger(rule.action, action):
            kind = template_for(classified, rule.action)
            if kind is not None:
                return Merged(classified, kind)
    return Merged(main_category, None)


async def judge_candidate(
    guard: Any,
    messages: list[dict[str, str]],
    candidate: str,
    category: str,
    *,
    timeout_s: float,
    deadline: float,
    skip_s: float = JUDGE_SKIP_S,
) -> OutputVerdict | None:
    """`judge_output` 한 번. 건너뜀·실패는 None(`finalize`가 판정 단계만 건너뛴다)."""
    if not candidate or not has_judge_output(guard):
        return None
    remaining = _remaining(deadline)
    if remaining < skip_s:
        return None
    try:
        async with asyncio.timeout(min(timeout_s, remaining)):
            verdict = await guard.judge_output(messages, candidate, category=category)  # type: ignore[arg-type]
    except Exception:  # 판정만 건너뛴다. 메시지는 남기지 않는다
        return None
    return verdict if isinstance(verdict, OutputVerdict) else None
