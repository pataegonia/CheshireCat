"""LLM 호출 게이트 (master-plan §4.3, GR-10, GR-12).

- 실패는 예외가 아니라 `LLMResult.error` 코드로 돌려준다. `CancelledError`와 설정 오류(환경변수
  없음)·`start()` 전 호출만 밖으로 나간다. 오류 문자열에는 코드와 예외 클래스 이름만 쓴다:
  예외 메시지와 `body`는 학습자 발화를 담을 수 있다(GR-12).
- 재시도는 여기서만 한다(SDK는 max_retries=0). 재시도 1회, 마감(now + 대기 + read 한도) 안에서만.
- 게이트 대기도 마감 안에 든다. 허가는 시도마다 잡고 `finally`에서 돌려준다(백오프 중에는 놓는다).
- 시계·sleep·난수를 주입받아 테스트가 0ms에 돈다.
"""

import asyncio
import email.utils
import json
import math
import os
import random
import re
import time
from collections import Counter
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, Literal

import openai
from pydantic import BaseModel, ValidationError

from guardrail.config import Llm
from guardrail.judges.schemas import OutputOut, strict_schema

Role = Literal["input_judge", "output_judge", "jb_check", "oom_check"]
ErrorCode = Literal["timeout", "rate_limit", "server", "bad_json", "auth", "bad_request", "budget"]
Send = Callable[[dict[str, Any]], Awaitable[Any]]  # chat.completions.create(**kwargs)와 같은 모양

MAX_RETRY_AFTER_S = 120.0  # openai-python MAX_RETRY_AFTER_DELAY
REJECTED = (400, 422)  # probe가 "이 파라미터를 거절했다"로 보는 상태 코드
OPTIONAL_PARAMS = ("reasoning_effort", "verbosity", "max_completion_tokens")
# 짝 없는 surrogate는 UTF-8로 못 써서 SDK가 보내기 전에 실패한다(HTTP 0건) → 대체 문자로
_SURROGATE = re.compile("[\ud800-\udfff]")


@dataclass(frozen=True)
class LLMResult:
    parsed: BaseModel | None = field(
        repr=False
    )  # 스키마 검증을 통과한 것만. 판정 글이 로그에 새지 않게
    error: ErrorCode | None
    attempts: int  # 보낸 횟수(0이면 보내지 못함)
    latency_ms: float
    finish_reason: str | None  # 마지막 시도의. 응답이 없으면 None
    completion_tokens: int | None  # 마지막 시도의 usage.completion_tokens. 없으면 None


API_ENV = ("OPENAI_API_KEY", "OPENAI_MODEL", "OPENAI_BASE_URL")


def openai_env() -> dict[str, str]:
    """운영진 API 설정(앞뒤 공백 제거). 하나라도 비면 ValueError, 문구에는 이름만.

    주소가 비면 SDK가 공개 OpenAI 주소로 팀 키를 보내므로 클라이언트를 만들기 전에 막는다.
    """
    env = {k: os.environ.get(k, "").strip() for k in API_ENV}
    missing = [k for k, v in env.items() if not v]
    if missing:
        raise ValueError(f"환경변수 {'·'.join(missing)}이 필요하다")
    return env


@dataclass
class _Attempt:
    parsed: BaseModel | None = None
    error: ErrorCode | None = None
    retry: bool = False
    status: int | None = None
    headers: Mapping[str, str] | None = None
    finish_reason: str | None = None
    completion_tokens: int | None = None


def _parse_retry_after(headers: Mapping[str, str], wall_now: float) -> float | None:
    """대기 초. `retry-after-ms` → `retry-after`(초 또는 HTTP 날짜). 못 읽으면 None."""
    for name, divisor in (("retry-after-ms", 1000.0), ("retry-after", 1.0)):
        value = headers.get(name)
        if value is None:
            continue
        try:
            delay = float(value) / divisor
        except ValueError:
            if name == "retry-after-ms":
                continue
            try:  # 서버가 준 날짜라 터무니없는 값(년 99999)도 온다
                parsed = email.utils.parsedate_tz(value)
                return None if parsed is None else float(email.utils.mktime_tz(parsed) - wall_now)
            except (ValueError, OverflowError, TypeError, OSError):
                return None
        return delay if math.isfinite(delay) else None
    return None


def _status_code(status: int) -> tuple[ErrorCode, bool]:
    """(오류 코드, 재시도 대상)."""
    if status == 408:
        return "timeout", True
    if status == 429:
        return "rate_limit", True
    if status == 409 or status >= 500:
        return "server", True
    if status in (401, 403):
        return "auth", False
    return "bad_request", False


class LLM:
    def __init__(
        self,
        cfg: Llm,
        *,
        send: Send | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        rand: Callable[[], float] = random.random,  # noqa: S311 - 재시도 지터, 암호용 아님
    ) -> None:
        self.cfg = cfg
        self._send = send
        self._clock = clock
        self._sleep = sleep
        self._rand = rand
        self._gate = asyncio.Semaphore(cfg.gate)
        self._started = False
        self._start_lock = asyncio.Lock()
        self.probe_result: Mapping[str, ErrorCode | None] = {}
        self.model = os.environ.get("OPENAI_MODEL", "")
        self.params: dict[str, Any] = {k: getattr(cfg, k) for k in OPTIONAL_PARAMS}
        self.structured: Literal["json_schema", "json_object"] = cfg.structured_output
        self.calls: Counter[Role] = Counter()
        self.max_calls: int | None = None  # 하네스 예산(넘으면 error="budget", 호출 안 함)
        self.unexpected: Counter[str] = Counter()  # 분류 밖 예외의 클래스 이름별 수

    async def start(self) -> None:
        """프로세스당 1회: 클라이언트를 만들고 probe()를 부른다. 두 번째 호출은 무동작."""
        async with self._start_lock:
            if self._started:
                return
            if self._send is None:
                env = openai_env()
                self.model = env["OPENAI_MODEL"]
                t = self.cfg.timeout_s
                client = openai.AsyncOpenAI(
                    base_url=env["OPENAI_BASE_URL"],
                    api_key=env["OPENAI_API_KEY"],
                    timeout=openai.Timeout(
                        connect=t.connect, read=t.read, write=t.write, pool=t.pool
                    ),
                    max_retries=0,
                )

                async def send(kwargs: dict[str, Any]) -> Any:
                    return await client.chat.completions.create(**kwargs)

                self._send = send
            self.probe_result = await self.probe()
            if "auth" in self.probe_result.values():
                raise ValueError("OpenAI 인증이 거절됐다(OPENAI_API_KEY 확인)")
            self._started = True

    async def probe(self) -> Mapping[str, ErrorCode | None]:
        """파라미터를 하나씩 따로 시험해 거절(400·422)된 것만 끈다. 결과는 오류 코드로만 남긴다."""
        result: dict[str, ErrorCode | None] = {}
        base = {"model": self.model, "messages": [{"role": "user", "content": "ping"}]}
        try:  # 맨 요청이 실패하면 파라미터 탓이 아니니 아무것도 끄지 않는다
            await self._require_send()(base)
        except Exception as exc:  # noqa: BLE001 - 코드로만 남긴다
            return {"base": self._fail(exc).error}
        result["base"] = None
        trials: dict[str, dict[str, Any]] = {
            k: {k: v} for k, v in self.params.items() if v is not None
        }
        if self.structured == "json_schema":
            trials["response_format"] = {"response_format": self._response_format(OutputOut)}
        for name, extra in trials.items():
            try:
                await self._require_send()({**base, **extra})
                result[name] = None
            except Exception as exc:  # noqa: BLE001 - 분류해서 코드로만 남긴다
                a = self._fail(exc)
                result[name] = a.error
                if a.status in REJECTED:
                    if name == "response_format":
                        self.structured = "json_object"
                    else:
                        self.params[name] = None
        return result

    @asynccontextmanager
    async def slot(self) -> AsyncIterator[None]:
        """튜터가 같은 게이트를 쓰는 입구(PRD §9 T4). 마감 없음."""
        async with self._gate:
            yield

    async def complete(
        self,
        role: Role,
        messages: list[dict],
        schema: type[BaseModel],
        *,
        deadline: float,
        retries: int | None = None,
    ) -> LLMResult:
        """retries가 None이면 `llm.retries`. 튜터 공유 메서드는 0을 넘긴다(interface.md §2)."""
        self._require_send()  # start() 전 호출은 프로그래밍 오류라 코드로 바꾸지 않는다
        max_retries = self.cfg.retries if retries is None else retries
        t0 = self._clock()
        kwargs = self._request(role, messages, schema)
        attempts, last = 0, _Attempt()
        while True:
            remaining = deadline - self._clock()
            if remaining <= 0 or not await self._acquire(remaining):
                return self._result(last, last.error if attempts else "timeout", attempts, t0)
            try:
                # 허가를 쥔 뒤 await 없이 확인·증가: 대기열이 있어도 예산을 넘지 않는다
                if self.max_calls is not None and sum(self.calls.values()) >= self.max_calls:
                    return self._result(last, "budget", attempts, t0)
                if attempts and self._clock() + self.cfg.timeout_s.read > deadline:
                    return self._result(last, last.error, attempts, t0)  # 게이트 대기로 마감을 썼다
                attempts += 1
                self.calls[role] += 1
                last = await self._attempt(kwargs, schema, deadline)
            finally:
                self._gate.release()
            if last.error is None:
                return self._result(last, None, attempts, t0)
            if not last.retry or attempts > max_retries:
                return self._result(last, last.error, attempts, t0)
            wait = self._retry_wait(last.headers or {})
            if wait is None or self._clock() + wait + self.cfg.timeout_s.read > deadline:
                return self._result(last, last.error, attempts, t0)
            await self._sleep(wait)

    # --- 내부 ---

    def _require_send(self) -> Send:
        if self._send is None:
            raise RuntimeError("LLM.start()를 먼저 불러야 한다")
        return self._send

    async def _acquire(self, timeout: float) -> bool:
        try:
            await asyncio.wait_for(self._gate.acquire(), timeout)
        except TimeoutError:
            return False
        return True

    def _response_format(self, schema: type[BaseModel]) -> dict[str, Any]:
        if self.structured == "json_object":
            return {"type": "json_object"}
        return {
            "type": "json_schema",
            "json_schema": {
                "name": schema.__name__,
                "schema": strict_schema(schema),
                "strict": True,
            },
        }

    def _request(self, role: Role, messages: list[dict], schema: type[BaseModel]) -> dict[str, Any]:
        msgs = [dict(m) for m in messages]  # 복사본만 바꾼다: 응답 구간은 원문의 부분 문자열이다
        for m in msgs:
            if isinstance(m.get("content"), str):
                m["content"] = _SURROGATE.sub("\ufffd", m["content"])
        if self.structured == "json_object":
            note = "JSON 객체만 출력한다. 스키마:\n" + json.dumps(
                strict_schema(schema), ensure_ascii=False
            )
            if msgs and msgs[0].get("role") == "system":
                msgs[0]["content"] = f"{msgs[0]['content']}\n\n{note}"
            else:
                msgs.insert(0, {"role": "system", "content": note})
        kwargs: dict[str, Any] = {"model": self.model, "messages": msgs}
        over = self.cfg.per_role.get(role)
        for k, v in self.params.items():
            if v is not None:  # null이거나 probe()가 끈 파라미터는 호출별 값도 보내지 않는다
                o = getattr(over, k, None)
                kwargs[k] = v if o is None else o
        kwargs["response_format"] = self._response_format(schema)
        return kwargs

    async def _attempt(
        self, kwargs: dict[str, Any], schema: type[BaseModel], deadline: float
    ) -> _Attempt:
        try:
            async with asyncio.timeout(max(deadline - self._clock(), 0)):
                resp = await self._require_send()(kwargs)
        except Exception as exc:  # noqa: BLE001 - 코드로만 돌려준다
            return self._fail(exc)
        a = _Attempt()
        try:
            choice = resp.choices[0]
            content = choice.message.content
            a.finish_reason = choice.finish_reason
        except (AttributeError, IndexError, TypeError):
            content = None
        tokens = getattr(getattr(resp, "usage", None), "completion_tokens", None)
        a.completion_tokens = tokens if isinstance(tokens, int) else None
        try:
            if not isinstance(content, str):
                raise ValueError
            a.parsed = schema.model_validate_json(content)
        except (ValidationError, ValueError):
            a.error, a.retry = "bad_json", True
        return a

    def _fail(self, exc: Exception) -> _Attempt:
        """예외 → 오류 코드. 메시지·body는 보지 않는다."""
        if isinstance(exc, TimeoutError | openai.APITimeoutError):
            return _Attempt(error="timeout", retry=True)
        if isinstance(exc, openai.APIConnectionError):
            return _Attempt(error="server", retry=True)
        if isinstance(exc, openai.APIStatusError):
            code, retry = _status_code(exc.status_code)
            headers = exc.response.headers
            flag = headers.get("x-should-retry")
            if flag in ("true", "false"):
                retry = flag == "true"
            return _Attempt(error=code, retry=retry, status=exc.status_code, headers=headers)
        self.unexpected[type(exc).__name__] += 1
        return _Attempt(error="server")

    def _retry_wait(self, headers: Mapping[str, str]) -> float | None:
        """재시도 전 대기 초. None이면 재시도하지 않는다(서버가 120초 넘게 기다리라 함)."""
        after = _parse_retry_after({k.lower(): v for k, v in headers.items()}, time.time())
        if after is not None:
            if after > MAX_RETRY_AFTER_S:
                return None
            if after > 0:
                return after
        lo, hi = self.cfg.retry_jitter_s
        return lo + (hi - lo) * self._rand()

    def _result(self, a: _Attempt, error: ErrorCode | None, attempts: int, t0: float) -> LLMResult:
        return LLMResult(
            parsed=a.parsed if error is None else None,
            error=error,
            attempts=attempts,
            latency_ms=(self._clock() - t0) * 1000,
            finish_reason=a.finish_reason,
            completion_tokens=a.completion_tokens,
        )
