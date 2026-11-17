"""handle 흐름(TU-01). TUTOR_PRD 담당 표에서 A의 경로다.

읽을 수 있는 요청은 `mode`에 따라 베이스라인(#73) 또는 v2 흐름(`tutor.judge.flow`, #72)으로 간다.
"""

import asyncio
from functools import cache
from types import SimpleNamespace
from typing import Any

from tutor import baseline
from tutor.answer import postprocess
from tutor.judge import precheck
from tutor.request import TutorRequest, parse_request
from tutor.runtime.budget import backstop_s, budget_s
from tutor.runtime.response import default_response, ensure_contract
from tutor.startup import prepare


async def respond(body: Any, *, deadline: float, state: Any) -> dict[str, Any]:
    """요청 하나를 처리한다. 예외는 올린다(라우터가 클래스 이름만 기록하고 기본값을 낸다).

    deadline은 이벤트 루프 시각(loop.time()) 기준 요청 마감이다(TU-13). state는 기동 때 만든
    tutor_cfg·tutor_kb·tutor_llm을 가진다(서버는 app.state, 프로세스 안 호출은 default_state()).
    """
    req = parse_request(body)
    if req is None or req.last_user is None:
        return default_response()  # TU-12: 요청을 못 읽음 → clarify
    return await _answer(req, deadline=deadline, state=state)


async def _answer(req: TutorRequest, *, deadline: float, state: Any) -> dict[str, Any]:
    """답변을 만든다(TU-01·TU-06).

    주 호출·판정에는 deadline을 그대로 넘긴다. LLM 호출을 넣을 때 취소(CancelledError)를
    삼키지 않는다: asyncio.shield, except BaseException, gather(return_exceptions=True)로 막으면
    라우터의 시간 상한이 깨진다.
    """
    if state.tutor_cfg.mode == "baseline":  # TU-14 베이스라인(#73)
        return await baseline.answer(req, state, deadline=deadline)
    # v2(#72): 검색 → 주 호출 → IN-04. 자료 확인(toggles.oom_check)을 켜면 E가 함께 돈다(#201)
    body = await precheck.answer(req, state, deadline=deadline)
    if state.tutor_cfg.toggles.inline_cite:  # 답변 끝 (섹션 ID) 표기 ablation(TU-07, #82)
        body = postprocess.inline_cite(body)
    return body


@cache
def default_state() -> SimpleNamespace:
    """프로세스 안에서 부르는 쪽(하네스 등)을 위한 기동 결과. 처음 부를 때 한 번 만든다."""
    state = SimpleNamespace()
    prepare(state)
    return state


async def serve(body: Any, *, state: Any) -> dict[str, Any]:
    """handle과 같은 계약(어떤 입력에도 예외 없이 규격 응답)에 기동 결과(state)만 받는다.

    라우터와 같은 요청 마감과 시간 상한을 둔다. 로컬 측정이 설정을 바꾼 state로 부를 때 쓴다.
    """
    budget = budget_s(state)
    deadline = asyncio.get_running_loop().time() + budget
    try:
        async with asyncio.timeout(backstop_s(budget)):
            return ensure_contract(await respond(body, deadline=deadline, state=state))
    except Exception:  # 시간 초과도 기본값. 메시지는 남기지 않는다(TU-15)
        return default_response()


async def handle(body: dict[str, Any]) -> dict[str, Any]:
    """TU-01 계약: 어떤 입력에도 예외 없이 튜터 규격 응답. 헤더는 받지 않는다(TU-18).

    프로세스 안에서 부르는 쪽(하네스 등)을 위한 계약이고 기본 state로 serve를 부른다. 기본 state를
    만들지 못하면(설정·색인 실패) 기본값이다. HTTP 라우터는 오류 클래스 이름을 로그에 남기려고
    respond를 직접 부른다.
    """
    try:
        state = default_state()
    except Exception:  # 기동 실패도 기본값. 메시지는 남기지 않는다(TU-15)
        return default_response()
    return await serve(body, state=state)
