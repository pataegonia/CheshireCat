"""베이스라인(TU-14 mode: baseline, T1.5 #73): 검색 상위 3개 + 일반 튜터 프롬프트 1회 + 형식 후처리.

설정의 토글은 하나도 쓰지 않는다(범주 판단·신호·kNN 예시·검증 없음).
v2와 견줄 "nano 단독" 기준선이다. action 설명은 SPEC §4의 정의를 그대로 쓴다.
요청 안의 system 역할 메시지는 지시로 올리지 않는다.
"""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from tutor.request import TutorRequest
from tutor.retrieval import Section, retrieve
from tutor.runtime.response import default_response, ensure_contract

BASELINE_K = 3  # TU-14 베이스라인 정의(검색 상위 3개)
ROLE = "baseline"  # TutorLLM.calls에 세는 역할 이름
CHAT_ROLES = ("user", "assistant")
SYSTEM = """당신은 학생의 수업 질문을 돕는 AI 튜터입니다. 한국어로 1–3문장으로 답합니다.
아래 근거 섹션의 내용으로 답하고, 답에 쓴 섹션의 ID를 citations에 넣습니다.
쓰지 않았으면 빈 목록입니다.
action은 다음 중 하나입니다.
- answer: 답변
- scaffold: 정답 대신 힌트·단계로 유도
- clarify: 무엇이 궁금한지 되묻기
- abstain: 자료에 없다고 말하기
- redirect: 수업 내용으로 돌리기
- refuse: 거절
- escalate: 학습을 멈추고 도움 받을 곳 안내"""


class BaselineOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    answer: str
    action: Literal["answer", "scaffold", "clarify", "abstain", "redirect", "refuse", "escalate"]
    citations: list[str]


def _sections_block(sections: list[Section]) -> str:
    if not sections:
        return "(근거 섹션 없음)"
    return "\n\n".join(f"[{s.id}] {s.title}\n{s.full_text()}" for s in sections)


def messages_for(req: TutorRequest, sections: list[Section]) -> list[dict[str, str]]:
    """고정 지시를 앞에, 근거 섹션·대화를 뒤에 둔다(접두 캐시, TU-13)."""
    system = f"{SYSTEM}\n\n근거 섹션:\n{_sections_block(sections)}"
    turns = [{"role": t.role, "content": t.content} for t in req.window() if t.role in CHAT_ROLES]
    return [{"role": "system", "content": system}, *turns]


async def answer(req: TutorRequest, state: Any, *, deadline: float) -> dict[str, Any]:
    query, history = req.search_query()
    sections = await retrieve(query, history, k=BASELINE_K)
    result = await state.tutor_llm.complete(
        ROLE,
        messages_for(req, sections),
        BaselineOut,
        deadline=deadline,
        timeout_s=state.tutor_cfg.llm.timeout_s.main,
        retries=1,  # 주 호출과 같이 1회(TU-13)
    )
    if result.error is not None or not isinstance(result.parsed, BaselineOut):
        return default_response()  # TU-12
    out = result.parsed
    allowed = {s.id for s in sections}
    citations = list(dict.fromkeys(c for c in out.citations if c in allowed))  # 근거 묶음 안 ID만
    return ensure_contract({"answer": out.answer, "action": out.action, "citations": citations})
