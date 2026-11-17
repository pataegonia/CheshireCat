"""T 의도 판정(#209 프롬프트 `tutor/prompts/intent.md`, `toggles.intent_override`). nano 1회.

T는 학습자의 마지막 발화에 이름표 하나(위기·탈옥·유해-실행·유해-비하·무관 결과물·제출물 대행·
정답만 요구·저신호·학습 질문)를 붙이고, `decide`가 그 이름표로 주 호출 action을 덮을지 정한다.

- parallel: 주 호출과 **동시에** 부르고 주 호출이 끝난 뒤 덮을지 정한다.
- serial: T를 먼저 끝까지 기다려, 주 호출 action 없이(None) 정한 덮기가 고정 문장 조합이면 주 호출
  없이 낸다. 아니면 주 호출을 부르고 parallel과 같이 정한다(가드 classify를 끈 설정에서만).
  다만 redirect·scaffold 고정 문장(무관 결과물·유해-비하·제출물 대행)은 주 호출도 불러, 주 호출이
  escalate면 escalate를 따르고 아니면 그 고정 문장을 낸다(flow `SERIAL_YIELDS_TO_ESCALATE`). 위기
  신호에 안 걸린 우회 표현을 T가 대행·무관 결과물로 보면 escalate가 빠지던 것이다(#220 가드 리뷰).
  refuse 고정 문장(유해-실행·탈옥)은 주 호출 없이 내므로, 우회 위기 표현에 유해·탈옥 요청이
  섞이면 escalate 대신 refuse가 나갈 수 있다.

덮기 규칙
- 학습 질문·실패·저신호는 덮지 않는다.
- 탈옥은 지시 무력화 신호가 있을 때만 덮는다(공격 기법을 배우는 정상 질문을 탈옥으로 본 오탐 때문).
- 위기 신호가 있거나 주 호출이 escalate면 위기만 따른다(위기를 약하게 바꾸지 않는다).
- T action이 §3 우선순위에서 주 호출보다 약하면 덮지 않는다. 단 유해-비하 redirect가 주 호출
  refuse를 바꾸는 것만 허용한다(표의 harmful 두 갈래). 지시 무력화 신호가 있으면 이 예외도 없고,
  serial에서는 refuse보다 약한 이름표면 주 호출을 본 뒤 정한다(#220 리뷰).
- 같은 action이면 덮지 않는다. 제출물 대행만 주 호출이 scaffold여도 고정 문장으로 바꾼다.
- 고정 문장이 없는 조합(정답만 요구 → answer_seeking scaffold)은 주 호출이 answer일 때만 덮는다.
  주 호출 문장·인용은 그대로 두고 범주·action만 바꾼다. 주 호출이 실패했으면 부르는 쪽이 기본값의
  action을 넘기고 고정 문장 조합만 쓴다.
- 부르는 쪽(flow)은 가드 classify가 위기·공격 범주면 T를 쓰지 않는다(합치기가 정한다).

측정(11/17 런박스, 교차 검증 160행, 판정 포함): 대행 덮기 + 자료 인용 답 위에 serial을 켜면 82.76
(3회) → 85.79(2회). T가 덮을 수 있던 행 55개에서 고침 17 · 망침 3, 정상 질문을 막은 행 0.

입력 블록은 프롬프트가 정한 넷(과목 목차, 신호, 대화, 끝 요청)이다. 대화·신호는 주 호출과 같은
블록을 쓴다. 재시도 없음. 실패·잘림·시간 초과면 None(주 호출 결과 유지)이고 사유는 횟수만 센다.
요청 내용은 지역 변수로만 쓴다(TU-18).
"""

import asyncio
import re
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from integration.contracts import Action
from tutor.answer.templates import template_for
from tutor.judge.conversation import Conversation
from tutor.judge.main_call import _turn_block, defuse
from tutor.judge.signals import Signal, labels

ROLE = "intent"  # TutorLLM.calls에 세는 역할 이름
PROMPT_PATH = Path(__file__).resolve().parents[1] / "prompts" / "intent.md"
LONG_TARGET = 300  # 마지막 발화가 이 글자 수 이상이면 끝 요청을 따로 보인다
WAIT_GRACE_S = 0.05  # 주 호출이 끝난 뒤 더 기다리는 여유(classify A-5와 같다)
IntentSkips = Counter[str]  # no_time · error · truncated · invalid · timeout → 횟수

Label = Literal[
    "위기", "탈옥", "유해-실행", "유해-비하", "무관 결과물", "제출물 대행", "정답만 요구", "저신호",
    "학습 질문",
]  # fmt: skip
# 이름표 → (범주, action). 학습 질문은 덮지 않는다
TARGET: Mapping[str, tuple[str, Action] | None] = {
    "위기": ("emotional_distress", "escalate"),
    "탈옥": ("jailbreak_injection", "refuse"),
    "유해-실행": ("harmful_content", "refuse"),
    "유해-비하": ("harmful_content", "redirect"),
    "무관 결과물": ("off_topic", "redirect"),
    "제출물 대행": ("academic_dishonesty", "scaffold"),
    "정답만 요구": ("answer_seeking", "scaffold"),
    "저신호": ("low_signal_input", "clarify"),
    "학습 질문": None,
}
STUDY = "학습 질문"  # 대행 덮기·자료 인용 답의 T 관문(flow)
NEVER = frozenset({"저신호", STUDY})  # 덮지 않는 이름표
# §3 우선순위(앞이 강함, PRD·주 호출 프롬프트와 같다)
ORDER = ("escalate", "refuse", "redirect", "clarify", "scaffold", "abstain", "answer")
_SENT_END = re.compile(r"(?<=[.!?。？！])\s+")


class IntentOut(BaseModel):
    """T 출력(json_schema strict)."""

    model_config = ConfigDict(extra="forbid", strict=True)

    label: Label
    self_info: bool


@dataclass(frozen=True)
class Override:
    category: str
    action: Action


def load_system(path: Path = PROMPT_PATH) -> str:
    """system 문구. 첫 줄 출처 주석(<!-- … -->)은 빼고 나머지를 글자 그대로(앞뒤 빈 줄만 뺀다)."""
    text = path.read_text(encoding="utf-8").replace("\r\n", "\n")
    first, _, rest = text.partition("\n")
    if not (first.startswith("<!--") and first.endswith("-->")):
        raise ValueError(f"{path.name} 첫 줄에 출처 주석이 없다")
    return rest.strip("\n")


def table_of_contents(sections: Mapping[str, Any]) -> str:
    """<과목 목차>: 섹션 ID와 제목(기동 때 KB 색인에서 한 번 만든다)."""
    lines = [f"- {sid} {s.title}" for sid, s in sorted(sections.items())]
    return "<과목 목차>\n" + "\n".join(lines) + "\n</과목 목차>"


def end_request(text: str) -> str | None:
    """마지막 발화가 길면 끝 두 문장. 짧으면 None."""
    if len(text) < LONG_TARGET:
        return None
    parts = [p for p in _SENT_END.split(text.strip()) if p.strip()]
    return " ".join(parts[-2:]) if parts else None


def build_messages(
    system: str, toc: str, conv: Conversation, signals: Sequence[Signal]
) -> list[dict[str, str]]:
    hint = ", ".join(labels(signals)) if signals else "없음"
    parts = [toc, f"<신호>{defuse(hint)}</신호>", _turn_block(conv)]
    target = next((t.text for t in conv.window if t.is_target), "")
    tail = end_request(target)
    if tail:
        parts.append(f"<끝 요청>{defuse(tail)}</끝 요청>")
    user = "\n\n".join(parts)
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def _rank(action: str | None) -> int:
    return ORDER.index(action) if action in ORDER else len(ORDER)


def decide(
    label: str | None, signals: Sequence[Signal], main_action: str | None
) -> Override | None:
    """T 이름표로 주 호출 action을 덮을지. 덮지 않으면 None(규칙은 모듈 설명)."""
    if label is None or label in NEVER:
        return None
    target = TARGET.get(label)
    if target is None:
        return None
    category, action = target
    if action == main_action and label != "제출물 대행":
        # 제출물 대행은 주 호출이 scaffold여도 고정 문장으로 바꾼다
        # (모델 scaffold 문장에 답이 샐 수 있다)
        return None
    if Signal.OVERRIDE in signals and main_action is None and _rank(action) > _rank("refuse"):
        # serial: 지시 무력화 신호가 있으면 refuse보다 약한 이름표(유해-비하·무관 결과물·
        # 제출물 대행)는 주 호출(refuse일 수 있다)을 보고 정한다. 실패 기본값·대행 덮기도
        # 무력화 신호를 앞에 둔다
        return None
    if template_for(category, action) is None and main_action != "answer":
        # 고정 문장이 없는 조합(answer_seeking scaffold)은 주 호출의 answer 문장을 그대로 쓰므로
        # answer일 때만 덮는다(주 호출 실패·abstain·clarify 문장을 scaffold로 내지 않는다)
        return None
    if label == "탈옥" and Signal.OVERRIDE not in signals:
        return None
    if (Signal.CRISIS in signals or main_action == "escalate") and label != "위기":
        return None
    weaker = _rank(action) > _rank(main_action)
    lowered = label == "유해-비하" and main_action == "refuse" and Signal.OVERRIDE not in signals
    if weaker and not lowered:
        return None
    return Override(category, action)


def _skip(skips: IntentSkips | None, reason: str) -> None:
    if skips is not None:
        skips[reason] += 1


@dataclass(frozen=True)
class IntentJob:
    """시작한 T 호출. `until`(루프 시각)이 지나면 결과를 기다리지 않는다."""

    task: "asyncio.Task[str | None]"
    until: float

    def cancel(self) -> None:
        if not self.task.done():
            self.task.cancel()


async def _call(
    llm: Any,
    messages: list[dict[str, str]],
    deadline: float,
    limit: float,
    skips: IntentSkips | None,
) -> str | None:
    try:
        result = await llm.complete(
            ROLE, messages, IntentOut, deadline=deadline, timeout_s=limit, retries=0
        )
    except asyncio.CancelledError:
        raise
    except Exception:  # 메시지는 남기지 않는다
        _skip(skips, "error")
        return None
    if result.finish_reason == "length":
        _skip(skips, "truncated")
        return None
    if result.error is not None or not isinstance(result.parsed, IntentOut):
        _skip(skips, "error" if result.error is not None else "invalid")
        return None
    return result.parsed.label


def start_intent(
    llm: Any,
    messages: list[dict[str, str]],
    *,
    timeout_s: float,
    deadline: float,
    skips: IntentSkips | None = None,
) -> IntentJob | None:
    """T 호출을 시작한다. 시간이 없으면 None. 결과는 `wait_intent`로 받는다."""
    now = asyncio.get_running_loop().time()
    limit = min(timeout_s, deadline - now)
    if limit <= 0:
        _skip(skips, "no_time")
        return None
    task = asyncio.ensure_future(_call(llm, messages, deadline, limit, skips))
    return IntentJob(task, now + limit + WAIT_GRACE_S)


async def wait_intent(
    job: IntentJob, skips: IntentSkips | None = None, *, not_before: float | None = None
) -> str | None:
    """T 이름표. `max(호출 제한, not_before)`가 지나도 안 끝나면 취소하고 None."""
    until = job.until if not_before is None else max(job.until, not_before)
    timeout = max(0.0, until - asyncio.get_running_loop().time())
    done, _ = await asyncio.wait({job.task}, timeout=timeout)
    if job.task not in done:
        job.cancel()
        _skip(skips, "timeout")
        return None
    if job.task.cancelled():
        return None
    return job.task.result()
