"""POST /tutor 라우터(TU-01·TU-12·TU-13·TU-15). 가드 create_app(tutor_router=)에 주입된다.

인증(TEAM_TOKEN)·메타 로그(시각·경로·바이트·상태·ms·오류 클래스)·lifespan은 가드 골격이 맡는다.
주입된 /tutor에는 가드의 본문 상한과 시간 안전망이 걸리지 않으므로 여기서 직접 건다(통합 IN-05).
"""

import asyncio
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from tutor.pipeline import respond
from tutor.request import load_json
from tutor.runtime.budget import backstop_s, budget_s
from tutor.runtime.response import default_response, ensure_contract

MAX_BODY_BYTES = 4 * 1024 * 1024  # 가드와 같은 본문 상한(#92). 넘으면 읽기를 멈추고 기본값
# 정상 요청은 수 KB다. 이보다 큰 본문만 JSON 해석을 스레드로 넘겨 이벤트 루프를 막지 않는다
THREAD_PARSE_BYTES = 1024 * 1024

router = APIRouter()


class BodyTooLarge(Exception):
    """요청 본문이 MAX_BODY_BYTES를 넘었다(로그에는 이 클래스 이름만 남는다)."""


async def read_body(request: Request) -> bytes:
    """MAX_BODY_BYTES까지만 읽는다. 길이만 request.state.req_bytes에 두고 본문은 두지 않는다."""
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        request.state.req_bytes = size
        if size > MAX_BODY_BYTES:
            raise BodyTooLarge
        chunks.append(chunk)
    request.state.req_bytes = size
    return b"".join(chunks)


async def parse_body(raw: bytes) -> Any:
    """JSON 값 하나(못 읽으면 None). 큰 본문은 스레드에서 해석한다(결과는 같다, TU-13)."""
    if len(raw) > THREAD_PARSE_BYTES:
        return await asyncio.to_thread(load_json, raw)
    return load_json(raw)


@router.post("/tutor")
async def tutor_route(request: Request) -> JSONResponse:
    """요청 하나. 본문 초과·시간 초과·예외는 모두 200 기본값이다(TU-12).

    요청 마감(이벤트 루프 시각)은 본문을 읽기 전에 정해 처리 단계에 넘긴다(TU-13).
    asyncio.timeout(예산 + 안전망)은 그래도 끝나지 않을 때의 마지막 상한이다.
    """
    budget = budget_s(request.app.state)
    deadline = asyncio.get_running_loop().time() + budget
    try:
        async with asyncio.timeout(backstop_s(budget)):
            body = await parse_body(await read_body(request))
            resp = ensure_contract(await respond(body, deadline=deadline))
    except Exception as exc:  # 클래스 이름만 로그에 남긴다. 메시지는 남기지 않는다(TU-15)
        request.state.error = type(exc).__name__
        resp = default_response()
    return JSONResponse(resp)
