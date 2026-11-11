"""handle 흐름(TU-01). TUTOR_PRD 담당 표에서 A의 경로이고, 지금은 #69가 둔 골격이다.

답변 생성(검색 #70, 주 호출 #72)이 들어오기 전까지는 읽을 수 있는 요청에도 TU-12 기본값을 낸다.
"""

from typing import Any

from tutor.request import TutorRequest, parse_request
from tutor.runtime.response import default_response, finalize


async def respond(body: Any) -> dict[str, Any]:
    """요청 하나를 처리한다. 예외는 올린다(라우터가 클래스 이름만 기록하고 기본값을 낸다)."""
    req = parse_request(body)
    if req is None or req.last_user is None:
        return default_response()  # TU-12: 요청을 못 읽음 → clarify
    return await _answer(req)


async def _answer(req: TutorRequest) -> dict[str, Any]:
    """답변을 만드는 자리(#70 검색, #72 주 호출). 그 전까지는 기본값.

    여기에 LLM 호출을 넣을 때 취소(CancelledError)를 삼키지 않는다: asyncio.shield,
    except BaseException, gather(return_exceptions=True)로 막으면 라우터의 시간 상한이 깨진다.
    """
    return default_response()


async def handle(body: dict[str, Any]) -> dict[str, Any]:
    """TU-01 계약: 어떤 입력에도 예외 없이 튜터 규격 응답. 헤더는 받지 않는다(TU-18).

    프로세스 안에서 부르는 쪽(하네스 등)을 위한 계약이다. HTTP 라우터는 오류 클래스 이름을
    로그에 남기려고 respond를 직접 부르고, 같은 finalize·기본값 규칙을 쓴다.
    """
    try:
        return finalize(await respond(body))
    except Exception:  # 메시지는 남기지 않는다(TU-15)
        return default_response()
