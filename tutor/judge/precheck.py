"""답 전 판단(v2 구조 제안 #201 3-3 E): 주 호출과 함께 도는 자료 확인 호출.

`toggles.oom_check`를 켜면(실험 이름 B2):
- 주 호출(`flow.answer_v2`) ∥ E(`tutor/prompts/oom_check.md`, "묻는 내용이 근거 묶음에 있는가") 1회.
- 주 호출이 answer이고, E가 "근거에 없음"이고, 마지막 학습자 발화가 영어 위주가 아니면
  고정 문장 abstain(out_of_material, 인용 없음)으로 바꾼다. 다른 action은 건드리지 않는다.
- E는 요청 시작부터 `E_DEADLINE_S` 안에 끝나야 쓴다. 넘기거나 실패하면 버리고 주 호출 결과 그대로.
- `cite_top1`이 더하는 1위는 규칙 A 질의(창 안 앞 발화 전부 + 판단 대상)의 검색 1위다.
- 요청당 nano: 주 호출(재시도 포함 최대 2) + E 1 = 최대 3(TU-13). E는 재시도하지 않는다.
끄면 이 모듈은 `flow.answer_v2`만 부른다(호출 수·응답이 지금 서버와 같다).
요청 내용은 지역 변수로만 쓰고 남기지 않는다(TU-18). 로그를 남기지 않는다(TU-15).
"""

import asyncio
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict

from tutor.answer.templates import Fixed, fixed_body
from tutor.judge import flow
from tutor.judge.conversation import tidy
from tutor.judge.main_call import _section_block, _turn_block
from tutor.judge.signals import direct_clarify
from tutor.request import TutorRequest
from tutor.retrieval import retrieve

E_DEADLINE_S = 9.0  # 요청 시작부터. 런박스 동시 64에서 마감 없을 때 E가 20초를 넘긴 요청 6.9%(#213)
PROMPTS = Path(__file__).resolve().parents[1] / "prompts"
EMPTY_BUNDLE = "(관련 자료를 찾지 못했습니다)"  # main_call.build_user_message와 같은 문구


class EOut(BaseModel):
    """E 출력(harness `OomCheckOut`과 같은 스키마)."""

    model_config = ConfigDict(extra="forbid", strict=True)
    asked: str
    quote: str
    found: bool


def _system(path: Path) -> str:
    """프롬프트 파일: 첫 줄 출처 주석을 빼고 나머지를 그대로."""
    first, _, rest = path.read_text(encoding="utf-8").replace("\r\n", "\n").partition("\n")
    if not (first.startswith("<!--") and first.endswith("-->")):
        raise ValueError(f"{path.name} 첫 줄에 출처 주석이 없다")
    return rest.strip("\n")


def prepare_precheck(app_state: Any) -> None:
    """기동 때 한 번: 켠 판단 호출의 시스템 프롬프트. 요청 중 바뀌지 않는다."""
    toggles = app_state.tutor_cfg.toggles
    app_state.tutor_precheck_sys = (
        {"E": _system(PROMPTS / "oom_check.md")} if toggles.oom_check else {}
    )


def english(text: str) -> bool:
    """영어 위주 발화: 라틴 글자가 한글(음절·자모)보다 많다. 영어 질문은 E가 근거를 못 찾기 쉬워
    abstain으로 바꾸지 않는다(#213 영어 규칙)."""
    lat = sum(1 for c in text if ("a" <= c <= "z") or ("A" <= c <= "Z"))
    return lat > sum(1 for c in text if "가" <= c <= "힣" or "ㄱ" <= c <= "ㆎ")


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


async def answer(req: TutorRequest, state: Any, *, deadline: float) -> dict[str, Any]:
    """v2 흐름 진입점. 토글이 꺼져 있으면 `flow.answer_v2` 그대로."""
    if not state.tutor_cfg.toggles.oom_check:
        return await flow.answer_v2(req, state, deadline=deadline)
    loop = asyncio.get_running_loop()
    e_deadline = min(deadline, loop.time() + E_DEADLINE_S)
    conv = tidy(flow._messages(req))
    if direct_clarify(conv):  # 주 경로도 LLM 없이 고정 clarify
        return await flow.answer_v2(req, state, deadline=deadline)
    q, h = conv.search_query()
    k, index = state.tutor_cfg.retrieval.k, state.tutor_kb
    sections = list(await retrieve(q, h, k=k, index=index))
    prior = [t.text for t in conv.window if not t.is_target and not t.is_button]
    sections_a = list(await retrieve(q, prior, k=k, index=index))  # 규칙 A(#213)
    bundle = "\n".join(_section_block(s) for s in sections) or EMPTY_BUNDLE
    e_user = f"<근거 묶음>\n{bundle}\n</근거 묶음>\n\n{_turn_block(conv)}"
    e_task = asyncio.create_task(
        call(state, "e_check", state.tutor_precheck_sys["E"], e_user, EOut, e_deadline)
    )
    try:
        body = await flow.answer_v2(req, state, deadline=deadline, top_sections=sections_a)
        if body.get("action") == "answer" and not english(conv.target_raw):
            e = await e_task  # 스스로 e_deadline 안에 끝난다(넘으면 None)
            if e is not None and e.found is False:
                return flow.tutor_post(fixed_body(Fixed.ABSTAIN), "out_of_material")
        return body
    finally:
        e_task.cancel()  # 아직 돌면 취소(요청이 끝난 뒤 남는 작업이 없게, TU-18)
