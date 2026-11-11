"""POST /guard HTTP 서버: 인증·200 보장·본문 없는 로그 (GR-01·09·12, ADR-0009, master-plan §3).

- 서버 계약은 하나다: `await guard.judge(req) -> Judgement`,
  응답은 `judgement.response.to_json(block)`.
- 인증 실패(401)를 뺀 모든 경로가 200 + 규격 응답이다. 422를 내지 않는다(본문 반사 방지).
- 로그는 한 줄 7필드(시각, 경로, 요청 바이트, 응답 바이트, 상태, ms, 오류 클래스)뿐이다.
  요청·답변 본문, 예외 메시지, 트레이스백을 남기지 않는다(실격 조건).
"""

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


class Judge(Protocol):
    async def judge(self, req: GuardRequest) -> Judgement: ...


GuardFactory = Callable[[GuardConfig], Awaitable[Judge]]


async def constant_factory(cfg: GuardConfig) -> Judge:
    return ConstantGuard()  # S2.8에서 Guard.build로 바뀐다


# --- 인증 ---


async def check_token(request: Request) -> None:
    """Bearer 토큰을 상수 시간으로 비교한다. 토큰이 아직 없으면(setup 전) 닫힌 채 401."""
    token = getattr(request.app.state, "team_token", None)
    given = request.headers.get("authorization", "").encode()
    if not token or not hmac.compare_digest(given, f"Bearer {token}".encode()):
        raise HTTPException(status_code=401, detail="unauthorized")


# --- 기본 응답 (GR-09) ---


def _default_body(state: Any, raw: bytes | None) -> dict:
    """allow·normal. 본문을 못 읽었거나 candidate_answer 키 문자열이 있으면 output을 넣는다.

    output이 남아도 채점기는 무시하지만 빠지면 형식 오류라, 모를 때는 넣는 쪽으로 기운다.
    """
    cfg: GuardConfig | None = getattr(state, "cfg", None)
    has_candidate = raw is None or b"candidate_answer" in raw
    grounded = cfg is None or cfg.output.failure_default == "grounded"
    output = OutputVerdict(grounded, (), True) if has_candidate else None
    return GuardResponse("normal", output).to_json(getattr(state, "block", frozenset()))


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
    state = request.app.state
    raw: bytes | None = None
    try:
        raw = await request.body()
        judgement = await state.guard.judge(parse_request(raw))
        body = judgement.response.to_json(state.block)
    except Exception as exc:  # 어떤 실패도 200 기본값(GR-09). 메시지는 남기지 않는다
        request.state.error = type(exc).__name__
        body = _default_body(state, raw)
    return JSONResponse(body)


async def setup_guard(
    app: FastAPI, cfg: GuardConfig, guard_factory: GuardFactory = constant_factory
) -> None:
    """팀 앱용 계약: app.state에 판정기·토큰·block 표를 둔다. TEAM_TOKEN이 없으면 실패."""
    token = os.environ.get("TEAM_TOKEN", "")
    if not token:
        raise RuntimeError("TEAM_TOKEN 환경변수가 없거나 비었다")
    _install_log_filters()
    app.state.cfg = cfg
    app.state.block = frozenset[Category](cfg.block_categories)
    app.state.team_token = token
    app.state.guard = await guard_factory(cfg)


@contextlib.asynccontextmanager
async def guard_lifespan(app: FastAPI) -> AsyncIterator[None]:
    await setup_guard(app, load_config())
    yield


# --- 로그 (GR-12) ---


class _ExceptionNameOnly(logging.Filter):
    """uvicorn.error 기록에서 트레이스백·예외 메시지를 지우고 예외 클래스 이름만 남긴다."""

    def filter(self, record: logging.LogRecord) -> bool:
        if record.exc_info:
            exc_type = record.exc_info[0]
            record.msg = f"exception {exc_type.__name__ if exc_type else 'unknown'}"
            record.args = ()
            record.exc_info = None
            record.exc_text = None
            record.stack_info = None
        return True


def _install_log_filters() -> None:
    uv = logging.getLogger("uvicorn.error")
    if not any(isinstance(f, _ExceptionNameOnly) for f in uv.filters):
        uv.addFilter(_ExceptionNameOnly())
    access_log.setLevel(logging.INFO)
    if not access_log.handlers:
        access_log.addHandler(logging.StreamHandler())


def _log_line(request: Request, status: int, resp_bytes: int, started: float) -> None:
    path = request.url.path if request.url.path in LOGGED_PATHS else "other"
    req_bytes = request.headers.get("content-length", "0")
    error = getattr(request.state, "error", None) or "-"
    ms = (time.perf_counter() - started) * 1000
    ts = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    req_bytes = req_bytes if req_bytes.isdigit() else "0"
    access_log.info("%s %s %s %d %d %.1f %s", ts, path, req_bytes, resp_bytes, status, ms, error)


async def _access_log(request: Request, call_next: Callable) -> Any:
    started = time.perf_counter()
    try:
        response = await call_next(request)
    except Exception as exc:  # 라우트 밖 예외도 200 기본값. 메시지는 남기지 않는다
        request.state.error = type(exc).__name__
        response = await _fallback_response(request)
    size = response.headers.get("content-length", "0")
    _log_line(request, response.status_code, int(size) if size.isdigit() else 0, started)
    return response


async def _fallback_response(request: Request) -> JSONResponse:
    if request.url.path == "/tutor":
        return JSONResponse(_tutor_body(TUTOR_FALLBACK))
    raw: bytes | None
    try:
        raw = await request.body()
    except Exception:  # 본문을 못 읽으면 output을 넣는 쪽(_default_body)
        raw = None
    return JSONResponse(_default_body(request.app.state, raw))


# --- 시작용 /tutor (배포용 임시 라우트, 팀 /tutor 코드가 아니다) ---
# competition/server.py의 라우트를 복사했다. print를 지우고, 클라이언트는 첫 호출 때 만들고,
# 판정기에 LLM이 있으면 같은 게이트(LLM.slot, PRD §9 T4)를 거친다.

TUTOR_SYSTEM = "당신은 온라인 학습 플랫폼의 AI 튜터입니다. 한국어로 답합니다."
TUTOR_FALLBACK = "지금은 답변을 만들 수 없어요. 잠시 후 다시 질문해 주세요."
TUTOR_ANSWER_MAX = 4000


def _tutor_body(answer: str) -> dict:
    return {"answer": answer, "action": "answer", "citations": []}


async def tutor_route(request: Request) -> JSONResponse:
    state = request.app.state
    try:
        req = parse_request(await request.body())
        model = os.environ["OPENAI_MODEL"]
        if getattr(state, "tutor_client", None) is None:
            from openai import AsyncOpenAI  # 첫 호출 때만 환경변수를 읽는다

            state.tutor_client = AsyncOpenAI(timeout=25, max_retries=0)
        llm = getattr(state.guard, "llm", None)
        gate = llm.slot() if llm is not None else contextlib.nullcontext()
        messages = [{"role": "system", "content": TUTOR_SYSTEM}]
        messages += [{"role": m.role, "content": m.content} for m in req.messages]
        async with gate:
            r = await state.tutor_client.chat.completions.create(
                model=model,
                messages=messages,
                max_completion_tokens=2000,
                extra_body={"reasoning_effort": "low"},
            )
        answer = (r.choices[0].message.content or "").strip()[:TUTOR_ANSWER_MAX] or TUTOR_FALLBACK
    except Exception as exc:  # 메시지는 남기지 않는다
        request.state.error = type(exc).__name__
        answer = TUTOR_FALLBACK
    return JSONResponse(_tutor_body(answer))


# --- 앱 ---


async def _http_error(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    if exc.status_code == 401:
        return JSONResponse(UNAUTHORIZED, status_code=401)
    return JSONResponse(
        {"error": "not_found" if exc.status_code == 404 else "error"}, status_code=exc.status_code
    )


async def _validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
    return JSONResponse(_default_body(request.app.state, None))  # 422 대신 기본값, 반사 없음


def create_app(guard_factory: GuardFactory = constant_factory) -> FastAPI:
    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
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


app = create_app()
