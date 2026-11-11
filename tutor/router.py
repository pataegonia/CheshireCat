"""POST /tutor 라우터(TU-01·TU-12·TU-15). 가드 create_app(tutor_router=)에 주입된다.

인증(TEAM_TOKEN)·메타 로그(시각·경로·바이트·상태·ms·오류 클래스)·lifespan은 가드 골격이 맡는다.
주입된 /tutor에는 가드의 본문 상한과 시간 안전망이 걸리지 않으므로 여기서 직접 건다(통합 IN-05).
"""

import asyncio
import math
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from tutor.pipeline import respond
from tutor.request import load_json
from tutor.runtime.response import default_response, finalize

MAX_BODY_BYTES = 4 * 1024 * 1024  # 가드와 같은 본문 상한(#92). 넘으면 읽기를 멈추고 기본값
DEFAULT_BUDGET_S = 20.0  # 가드 설정의 budget_s를 못 읽었을 때. 가드 v6 기본값과 같다
BACKSTOP_MARGIN_S = 5.0  # 요청 예산 위의 안전망. 20 + 5 < 규격 30초
DEADLINE_CAP_S = 27.0  # 설정과 상관없는 전체 상한(규격 30초, 가드 시작용 /tutor와 같은 값)

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


def deadline_s(app_state: Any) -> float:
    """가드가 읽은 요청 예산(app.state.cfg.budget_s) + 안전망, 최대 DEADLINE_CAP_S.

    예산을 못 읽거나 쓸 수 없는 값(숫자 아님, bool, 0 이하, 무한, NaN)이면 기본 예산을 쓴다.
    """
    budget = getattr(getattr(app_state, "cfg", None), "budget_s", None)
    usable = (
        isinstance(budget, int | float)
        and not isinstance(budget, bool)
        and math.isfinite(budget)
        and budget > 0
    )
    return min((budget if usable else DEFAULT_BUDGET_S) + BACKSTOP_MARGIN_S, DEADLINE_CAP_S)


@router.post("/tutor")
async def tutor_route(request: Request) -> JSONResponse:
    """요청 하나. 본문 초과·시간 초과·예외는 모두 200 기본값이다(TU-12)."""
    try:
        async with asyncio.timeout(deadline_s(request.app.state)):
            body = load_json(await read_body(request))
            resp = finalize(await respond(body))
    except Exception as exc:  # 클래스 이름만 로그에 남긴다. 메시지는 남기지 않는다(TU-15)
        request.state.error = type(exc).__name__
        resp = default_response()
    return JSONResponse(resp)
