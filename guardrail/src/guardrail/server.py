"""POST /guard HTTP 서버: 인증·200 보장·본문 없는 로그 (GR-01·09·12, ADR-0009, master-plan §3).

- 서버 계약은 하나다: `await guard.judge(req) -> Judgement`,
  응답은 `judgement.response.to_json(block)`.
- 인증 실패(401)를 뺀 모든 경로가 200 + 규격 응답이다. 422를 내지 않는다(본문 반사 방지).
- 로그는 한 줄 7필드(시각, 경로, 요청 바이트, 응답 바이트, 상태, ms, 오류 클래스)뿐이다.
  요청·답변 본문, 예외 메시지, 트레이스백을 남기지 않는다(실격 조건).
- 팀 앱은 `include_router(router)`로 붙이고 lifespan은 `FastAPI(lifespan=...)` 생성자에 준다.
  `mount()`로 붙이면 하위 앱 lifespan이 돌지 않아 기동 검사가 빠진다(그래도 401로 닫힌다).
  라우터만 붙인 앱의 401 본문은 FastAPI 기본(`{"detail": ...}`)이다. `{"error": ...}`는
  `create_app()`의 처리기가 만든다(SPEC은 401 본문을 정하지 않는다).
"""

import asyncio
import contextlib
import hmac
import logging
import os
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, datetime
from typing import Any, Protocol

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from guardrail.config import GuardConfig, load_config
from guardrail.guard import ConstantGuard, Judgement
from guardrail.schema import Category, GuardRequest, GuardResponse, OutputVerdict, parse_request

access_log = logging.getLogger("guardrail.access")
LOGGED_PATHS = frozenset({"/guard", "/tutor"})  # 그 밖의 경로는 "other"(경로도 요청이 정한다)
UNAUTHORIZED = {"error": "unauthorized"}
MAX_BODY_BYTES = 4 * 1024 * 1024  # 요청 본문 상한. 넘으면 읽기를 멈추고 기본값(GR-09)
BACKSTOP_MARGIN_S = 5.0  # 판정기 마감(budget_s) 위의 서버 안전망. 20 + 5 < 규격 30초
SAFE_DEFAULT = {  # 기본 응답을 만들다 실패해도 내는 마지막 값(output은 넣는 쪽)
    "input": {"decision": "allow", "category": "normal"},
    "output": {"grounded": True, "unsupported": [], "safe": True, "category": "normal"},
}


class BodyTooLarge(Exception):
    """요청 본문이 MAX_BODY_BYTES를 넘었다(로그에는 이 클래스 이름만 남는다)."""


class Judge(Protocol):
    async def judge(self, req: GuardRequest) -> Judgement: ...


GuardFactory = Callable[[GuardConfig], Awaitable[Judge]]


async def constant_factory(cfg: GuardConfig) -> Judge:
    """S1.2–S1.5 기준선. S2.8에서 Guard.build로 바뀐다."""
    return ConstantGuard()


# --- 인증 ---


async def check_token(request: Request) -> None:
    """Bearer 토큰을 상수 시간으로 비교한다. 토큰이 아직 없으면(setup 전) 닫힌 채 401."""
    token = getattr(request.app.state, "team_token", None)
    given = request.headers.get("authorization", "").encode()
    if not token or not hmac.compare_digest(given, f"Bearer {token}".encode()):
        raise HTTPException(status_code=401, detail="unauthorized")


# --- 본문·기본 응답 (GR-09) ---


async def _read_body(request: Request) -> bytes:
    """MAX_BODY_BYTES까지만 읽는다. 읽은 길이는 로그용으로 request.state에 둔다."""
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


def _default_body(state: Any, raw: bytes | None) -> dict[str, Any]:
    """allow·normal. 본문을 못 읽었으면(None) output을 넣는다: 남는 output은 채점기가 무시하지만
    빠지면 형식 오류다. output은 output.failure_default, safe=true(GR-09).
    """
    cfg: GuardConfig | None = getattr(state, "cfg", None)
    has_candidate = raw is None or parse_request(raw).has_candidate
    grounded = cfg is None or cfg.output.failure_default == "grounded"
    output = OutputVerdict(grounded, (), True) if has_candidate else None
    return GuardResponse("normal", output).to_json(getattr(state, "block", frozenset()))


def _safe_default(state: Any, raw: bytes | None) -> dict[str, Any]:
    try:
        return _default_body(state, raw)
    except Exception:  # 마지막 안전망: 어떤 경우에도 200 규격 응답
        return SAFE_DEFAULT


# --- 라우터 ---


@contextlib.asynccontextmanager
async def _require_setup(app: FastAPI) -> AsyncIterator[None]:
    """라우터만 붙이고 setup_guard를 부르지 않은 앱은 뜨지 않는다(조용한 기본 응답 금지)."""
    if getattr(app.state, "guard", None) is None:
        raise RuntimeError(
            "guard 라우터: lifespan에서 setup_guard 또는 guard_lifespan을 불러야 한다"
        )
    yield


router = APIRouter(dependencies=[Depends(check_token)], lifespan=_require_setup)


@router.post("/guard")
async def guard_route(request: Request) -> JSONResponse:
    """판정 하나. 판정기 예외·마감 초과·본문 초과는 모두 200 기본값이다(GR-09)."""
    state = request.app.state
    raw: bytes | None = None
    try:
        raw = await _read_body(request)
        async with asyncio.timeout(state.cfg.budget_s + BACKSTOP_MARGIN_S):
            judgement = await state.guard.judge(parse_request(raw))
        body = judgement.response.to_json(state.block)
    except Exception as exc:  # 메시지는 남기지 않는다(GR-12)
        request.state.error = type(exc).__name__
        body = _safe_default(state, raw)
    return JSONResponse(body)


async def setup_guard(
    app: FastAPI, cfg: GuardConfig, guard_factory: GuardFactory = constant_factory
) -> None:
    """팀 앱용 계약: app.state에 판정기·토큰·block 표를 둔다. TEAM_TOKEN이 없으면 실패."""
    _install_log_filters()  # 기동 실패 트레이스백보다 먼저
    token = os.environ.get("TEAM_TOKEN", "")
    if not token:
        raise RuntimeError("TEAM_TOKEN 환경변수가 없거나 비었다")
    app.state.cfg = cfg
    app.state.block = frozenset[Category](cfg.block_categories)
    app.state.team_token = token
    app.state.guard = await guard_factory(cfg)


@contextlib.asynccontextmanager
async def guard_lifespan(app: FastAPI) -> AsyncIterator[None]:
    _install_log_filters()  # load_config 실패 트레이스백보다 먼저
    await setup_guard(app, load_config())
    yield


# --- 로그 (GR-12) ---

_TRACEBACK = "Traceback (most recent call last)"


def _class_from_traceback_text(text: str) -> str:
    """트레이스백 문자열의 마지막 줄 `Name: 메시지`에서 Name만 꺼낸다."""
    last = next((line for line in reversed(text.splitlines()) if line.strip()), "")
    name = last.split(":", 1)[0].strip().rsplit(".", 1)[-1]
    return name if name.isidentifier() else "unknown"


class _ExceptionNameOnly(logging.Filter):
    """uvicorn.error 기록에서 트레이스백·예외 메시지를 지우고 예외 클래스 이름만 남긴다.

    exc_info가 있는 기록과, 기동 실패처럼 트레이스백을 문자열로 넘기는 기록을 둘 다 다룬다.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        name: str | None = None
        if record.exc_info:
            exc_type = record.exc_info[0]
            name = exc_type.__name__ if exc_type else "unknown"
        elif _TRACEBACK in str(record.msg):
            name = _class_from_traceback_text(str(record.msg))
        if name is not None:
            record.msg, record.args = f"exception {name}", ()
            record.exc_info = None
            record.exc_text = None
            record.stack_info = None
        return True


def _install_log_filters() -> None:
    uv = logging.getLogger("uvicorn.error")
    if not any(isinstance(f, _ExceptionNameOnly) for f in uv.filters):
        uv.addFilter(_ExceptionNameOnly())
    access_log.setLevel(logging.INFO)
    # 루트에 핸들러가 없을 때만 직접 쓴다(있으면 전파로 한 번만 찍힌다)
    if not access_log.handlers and not logging.getLogger().handlers:
        access_log.addHandler(logging.StreamHandler())


def _int_header(value: str) -> int:
    return int(value) if value.isascii() and value.isdecimal() else 0


def _log_line(request: Request, status: int, resp_bytes: int, started: float) -> None:
    path = request.url.path if request.url.path in LOGGED_PATHS else "other"
    req_bytes = getattr(request.state, "req_bytes", None)
    if req_bytes is None:
        req_bytes = _int_header(request.headers.get("content-length", ""))
    error = getattr(request.state, "error", None) or "-"
    ms = (time.perf_counter() - started) * 1000
    ts = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    access_log.info("%s %s %d %d %d %.1f %s", ts, path, req_bytes, resp_bytes, status, ms, error)


async def _access_log(request: Request, call_next: Callable[[Request], Awaitable[Any]]) -> Any:
    started = time.perf_counter()
    try:
        response = await call_next(request)
    except Exception as exc:  # 라우트 밖 예외도 200 기본값. 메시지는 남기지 않는다
        request.state.error = type(exc).__name__
        response = await _fallback_response(request)
    size = _int_header(response.headers.get("content-length", ""))
    _log_line(request, response.status_code, size, started)
    return response


async def _fallback_response(request: Request) -> JSONResponse:
    if request.url.path == "/tutor":
        return JSONResponse(_tutor_body(TUTOR_FALLBACK))
    return JSONResponse(_safe_default(request.app.state, None))  # 본문은 다시 읽지 않는다


# --- 시작용 /tutor (배포용 임시 라우트, 팀 /tutor 코드가 아니다) ---
# competition/server.py의 라우트를 복사했다. 출력문을 지우고, 클라이언트는 첫 호출 때 만들고,
# 판정기에 LLM이 있으면 같은 게이트(LLM.slot, PRD §9 T4)를 거친다.

TUTOR_SYSTEM = "당신은 온라인 학습 플랫폼의 AI 튜터입니다. 한국어로 답합니다."
TUTOR_FALLBACK = "지금은 답변을 만들 수 없어요. 잠시 후 다시 질문해 주세요."
TUTOR_ANSWER_MAX = 4000
TUTOR_MAX_TOKENS = 2000
TUTOR_TIMEOUT_S = 25  # 시작용 서버 값
TUTOR_DEADLINE_S = 27  # 게이트 대기 포함 전체 상한(규격 30초)


def _tutor_body(answer: str) -> dict[str, Any]:
    return {"answer": answer, "action": "answer", "citations": []}


async def _tutor_answer(state: Any, req: GuardRequest) -> str:
    model = os.environ["OPENAI_MODEL"]
    if getattr(state, "tutor_client", None) is None:
        from openai import AsyncOpenAI  # 첫 호출 때만 환경변수를 읽는다

        state.tutor_client = AsyncOpenAI(timeout=TUTOR_TIMEOUT_S, max_retries=0)
    llm = getattr(state.guard, "llm", None)
    gate = llm.slot() if llm is not None else contextlib.nullcontext()
    messages = [{"role": "system", "content": TUTOR_SYSTEM}]
    messages += [{"role": m.role, "content": m.content} for m in req.messages]
    async with gate:
        r = await state.tutor_client.chat.completions.create(
            model=model,
            messages=messages,
            max_completion_tokens=TUTOR_MAX_TOKENS,
            extra_body={"reasoning_effort": "low"},
        )
    return (r.choices[0].message.content or "").strip()[:TUTOR_ANSWER_MAX] or TUTOR_FALLBACK


async def tutor_route(request: Request) -> JSONResponse:
    """시작용 튜터. 실패는 모두 안내 문구 200이다."""
    try:
        async with asyncio.timeout(TUTOR_DEADLINE_S):
            req = parse_request(await _read_body(request))
            answer = await _tutor_answer(request.app.state, req)
    except Exception as exc:  # 메시지는 남기지 않는다
        request.state.error = type(exc).__name__
        answer = TUTOR_FALLBACK
    return JSONResponse(_tutor_body(answer))


# --- 앱 ---


async def _http_error(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    request.state.error = type(exc).__name__
    if exc.status_code == 401:
        return JSONResponse(UNAUTHORIZED, status_code=401)
    error = "not_found" if exc.status_code == 404 else "error"
    return JSONResponse({"error": error}, status_code=exc.status_code)


async def _validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
    """지금 라우트는 pydantic 인자가 없어 닿지 않는다. 팀 라우트가 붙어도 422(반사)를 막는다."""
    request.state.error = type(exc).__name__
    return await _fallback_response(request)


def create_app(guard_factory: GuardFactory = constant_factory) -> FastAPI:
    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        _install_log_filters()  # load_config 실패 트레이스백보다 먼저
        await setup_guard(app, load_config(), guard_factory)
        yield

    app = FastAPI(
        lifespan=lifespan, redirect_slashes=False, docs_url=None, redoc_url=None, openapi_url=None
    )
    app.include_router(router)
    app.add_api_route("/tutor", tutor_route, methods=["POST"], dependencies=[Depends(check_token)])
    app.add_exception_handler(StarletteHTTPException, _http_error)
    app.add_exception_handler(RequestValidationError, _validation_error)
    app.middleware("http")(_access_log)
    return app


_install_log_filters()  # import 때 한 번(uvicorn은 로그 설정 뒤에 앱을 불러온다)
app = create_app()
