"""답 전 판단(v2 구조 제안 #201 3-2 T·3-3 E): 주 호출과 함께 도는 자료 확인·의도 판정 호출.

`toggles.oom_check`를 켜면(실험 이름 B2):
- 주 호출(`flow.answer_v2`) ∥ E(`tutor/prompts/oom_check.md`, "묻는 내용이 근거 묶음에 있는가") 1회.
- 주 호출이 answer이고, E가 "근거에 없음"이고, 마지막 학습자 발화가 영어 위주가 아니면
  고정 문장 abstain(out_of_material, 인용 없음)으로 바꾼다. 다른 action은 건드리지 않는다.
- E는 요청 시작부터 `E_DEADLINE_S` 안에 끝나야 쓴다. 넘기거나 실패하면 버리고 주 호출 결과 그대로.
- `cite_top1`이 더하는 1위는 규칙 A 질의(창 안 앞 발화 전부 + 판단 대상)의 검색 1위다.

`toggles.intent_check`를 켜면(둘 다 켠 것이 실험 이름 G0):
- 주 호출 ∥ T(`tutor/prompts/intent.md`, 의도 이름표 하나) 1회. T는 요청 시작부터 `T_DEADLINE_S`
  안에 끝나야 쓴다(넘기거나 실패하면 덮지 않음).
- T 이름표를 #220 `intent.decide`와 같은 규칙(`decide`)으로 주 호출 action 위에 덮는다.
  막기 쪽으로만 올리고(유해-비하의 refuse → redirect만 예외), 지시 무력화 신호 없는 탈옥·저신호·
  학습 질문은 덮지 않는다. 위기 신호가 있으면 LLM 없이 escalate(가드 PRD §4 결정적 신호 먼저).

요청당 nano: 주 호출(재시도 포함 최대 2) + E 1 + T 1 = 최대 4(TU-13). E·T는 재시도하지 않는다.
둘 다 끄면 이 모듈은 `flow.answer_v2`만 부른다(호출 수·응답이 지금 서버와 같다).
요청 내용은 지역 변수로만 쓰고 남기지 않는다(TU-18). 로그를 남기지 않는다(TU-15).
"""

import asyncio
import re
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from tutor.answer.templates import Fixed, fixed_body, template_for
from tutor.judge import flow
from tutor.judge.conversation import tidy
from tutor.judge.main_call import _section_block, _turn_block, defuse
from tutor.judge.signals import Signal, detect, direct_clarify, labels
from tutor.request import TutorRequest
from tutor.retrieval import retrieve

E_DEADLINE_S = 9.0  # 요청 시작부터. 런박스 동시 64에서 마감 없을 때 E가 20초를 넘긴 요청 6.9%(#213)
T_DEADLINE_S = 6.0  # 요청 시작부터. 넘기면 T 없음(덮지 않음)
LONG = 300  # 판단 대상이 이 글자 수 이상이면 끝 1~2문장을 T에 따로 준다
PROMPTS = Path(__file__).resolve().parents[1] / "prompts"
EMPTY_BUNDLE = "(관련 자료를 찾지 못했습니다)"  # main_call.build_user_message와 같은 문구
REMINDER = (
    '<다시 확인할 규칙>\n판단 대상은 판단대상="예"인 마지막 학습자 발화다. 덧붙임·역할극 틀은 빼고 '
    "남는 요구로, 끝 요청이 있으면 전체와 끝 요청 중 더 강한 쪽으로 이름표 하나를 고른다. "
    "JSON만 낸다.\n</다시 확인할 규칙>"
)
_SENT = re.compile(r"(?<=[.!?。？！])\s+|\n+")
Label = Literal[
    "위기", "탈옥", "유해-실행", "유해-비하", "무관 결과물", "제출물 대행", "정답만 요구", "저신호",
    "학습 질문",
]  # fmt: skip
TARGET: dict[str, tuple[str, str] | None] = {
    "위기": ("emotional_distress", "escalate"),
    "탈옥": ("jailbreak_injection", "refuse"),
    "유해-실행": ("harmful_content", "refuse"),
    "유해-비하": ("harmful_content", "redirect"),
    "무관 결과물": ("off_topic", "redirect"),
    "제출물 대행": ("academic_dishonesty", "scaffold"),
    "정답만 요구": ("answer_seeking", "scaffold"),
    "저신호": ("low_signal_input", "clarify"),
    "학습 질문": None,
}  # #220 intent.TARGET과 같다
NEVER = frozenset({"저신호", "학습 질문"})
ORDER = ("escalate", "refuse", "redirect", "clarify", "scaffold", "abstain", "answer")


class EOut(BaseModel):
    """E 출력(harness `OomCheckOut`과 같은 스키마)."""

    model_config = ConfigDict(extra="forbid", strict=True)
    asked: str
    quote: str
    found: bool


class TOut(BaseModel):
    """T 출력(`tutor/prompts/intent.md`의 출력 형식)."""

    label: Label
    self_info: bool


def _system(path: Path) -> str:
    """프롬프트 파일: 첫 줄 출처 주석을 빼고 나머지를 그대로."""
    first, _, rest = path.read_text(encoding="utf-8").replace("\r\n", "\n").partition("\n")
    if not (first.startswith("<!--") and first.endswith("-->")):
        raise ValueError(f"{path.name} 첫 줄에 출처 주석이 없다")
    return rest.strip("\n")


def prepare_precheck(app_state: Any) -> None:
    """기동 때 한 번: 켠 판단 호출의 시스템 프롬프트와(T면) 과목 목차. 요청 중 바뀌지 않는다."""
    toggles = app_state.tutor_cfg.toggles
    sysm: dict[str, str] = {}
    if toggles.oom_check:
        sysm["E"] = _system(PROMPTS / "oom_check.md")
    if toggles.intent_check:
        sysm["T"] = _system(PROMPTS / "intent.md")
        secs = app_state.tutor_kb.sections
        app_state.tutor_precheck_toc = "\n".join(
            f"- {sid} {s.title}" for sid, s in sorted(secs.items())
        )
    app_state.tutor_precheck_sys = sysm


def english(text: str) -> bool:
    """영어 위주 발화: 라틴 글자가 한글(음절·자모)보다 많다. 영어 질문은 E가 근거를 못 찾기 쉬워
    abstain으로 바꾸지 않는다(#213 영어 규칙)."""
    lat = sum(1 for c in text if ("a" <= c <= "z") or ("A" <= c <= "Z"))
    return lat > sum(1 for c in text if "가" <= c <= "힣" or "ㄱ" <= c <= "ㆎ")


def end_request(text: str) -> str | None:
    """긴 판단 대상의 끝 1~2문장(덧붙인 글 뒤에 숨긴 요청을 T가 따로 보게)."""
    if len(text) < LONG:
        return None
    parts = [s.strip() for s in _SENT.split(text) if s.strip()]
    if len(parts) < 2:
        return None
    tail = " ".join(parts[-2:])
    return tail if len(tail) < len(text) * 0.6 else parts[-1]


def _rank(action: str | None) -> int:
    return ORDER.index(action) if action in ORDER else len(ORDER)


def decide(
    label: str | None, *, crisis: bool, override: bool, main_action: str | None
) -> tuple[str, str] | None:
    """#220 `intent.decide`와 같은 덮기 규칙(main_action은 주 호출 action, 실패면 기본값 action)."""
    if label is None or label in NEVER:
        return None
    target = TARGET.get(label)
    if target is None:
        return None
    category, action = target
    if action == main_action and label != "제출물 대행":
        return None
    if template_for(category, action) is None and main_action != "answer":
        return None
    if label == "탈옥" and not override:
        return None
    if (crisis or main_action == "escalate") and label != "위기":
        return None
    weaker = _rank(action) > _rank(main_action)
    lowered = label == "유해-비하" and main_action == "refuse" and not override
    if weaker and not lowered:
        return None
    return category, action


async def call(state: Any, role: str, system: str, user: str, schema, deadline: float):
    """튜터 LLM 1회(전역 게이트 slot·재시도 0). 실패·잘림·마감이면 None."""
    left = deadline - asyncio.get_running_loop().time()
    if left <= 0:
        return None
    msgs = [{"role": "system", "content": system}, {"role": "user", "content": user}]
    res = await state.tutor_llm.complete(
        role, msgs, schema, deadline=deadline, timeout_s=left, retries=0
    )
    return res.parsed if res.error is None and res.finish_reason != "length" else None


def _t_user(conv, sig, toc: str) -> str:
    target = next((t.text for t in reversed(conv.window) if t.is_target), "")
    tail = end_request(target)
    parts = [
        f"<과목 목차>\n{toc}\n</과목 목차>",
        f"<신호>{', '.join(labels(sig)) or '없음'}</신호>",
        _turn_block(conv),
    ]
    if tail:
        parts.append(f"<끝 요청>{defuse(tail)}</끝 요청>")
    return "\n\n".join([*parts, REMINDER])


async def answer(req: TutorRequest, state: Any, *, deadline: float) -> dict[str, Any]:
    """v2 흐름 진입점. 두 토글이 다 꺼져 있으면 `flow.answer_v2` 그대로."""
    toggles = state.tutor_cfg.toggles
    if not (toggles.oom_check or toggles.intent_check):
        return await flow.answer_v2(req, state, deadline=deadline)
    now = asyncio.get_running_loop().time()
    conv = tidy(flow._messages(req))
    if direct_clarify(conv):  # 주 경로도 LLM 없이 고정 clarify
        return await flow.answer_v2(req, state, deadline=deadline)
    sig = detect(conv)
    crisis, override = Signal.CRISIS in sig, Signal.OVERRIDE in sig
    if toggles.intent_check and crisis:  # 결정적 위기 신호: LLM 없이 escalate
        return flow.tutor_post(fixed_body(Fixed.ESCALATE), "emotional_distress")
    sysm = state.tutor_precheck_sys
    e_task = t_task = None
    sections_a = None
    if toggles.oom_check:
        q, h = conv.search_query()
        k, index = state.tutor_cfg.retrieval.k, state.tutor_kb
        sections = list(await retrieve(q, h, k=k, index=index))
        prior = [t.text for t in conv.window if not t.is_target and not t.is_button]
        sections_a = list(await retrieve(q, prior, k=k, index=index))  # 규칙 A(#213)
        bundle = "\n".join(_section_block(s) for s in sections) or EMPTY_BUNDLE
        e_user = f"<근거 묶음>\n{bundle}\n</근거 묶음>\n\n{_turn_block(conv)}"
        e_deadline = min(deadline, now + E_DEADLINE_S)
        e_task = asyncio.create_task(call(state, "e_check", sysm["E"], e_user, EOut, e_deadline))
    intent = None
    if toggles.intent_check:
        t_user = _t_user(conv, sig, state.tutor_precheck_toc)
        t_deadline = min(deadline, now + T_DEADLINE_S)
        t_task = asyncio.create_task(call(state, "intent", sysm["T"], t_user, TOut, t_deadline))

        async def intent(main_action: str | None) -> tuple[str, str] | None:
            try:
                t = await t_task  # 스스로 t_deadline 안에 끝난다(넘으면 None)
            except Exception:  # noqa: BLE001 - 메시지는 남기지 않는다(TU-15)
                t = None
            label = None if t is None else t.label
            return decide(label, crisis=crisis, override=override, main_action=main_action)

    try:
        body = await flow.answer_v2(
            req, state, deadline=deadline, top_sections=sections_a, intent=intent
        )
        if e_task is not None and body.get("action") == "answer" and not english(conv.target_raw):
            e = await e_task  # 스스로 e_deadline 안에 끝난다(넘으면 None)
            if e is not None and e.found is False:
                return flow.tutor_post(fixed_body(Fixed.ABSTAIN), "out_of_material")
        return body
    finally:
        for task in (e_task, t_task):
            if task is not None:
                task.cancel()  # 아직 돌면 취소(요청이 끝난 뒤 남는 작업이 없게, TU-18)
