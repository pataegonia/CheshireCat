"""튜터 LLM 호출(TU-13). 가드 전역 게이트(slot())를 같이 쓰고 AsyncOpenAI를 직접 부른다.

- 실패는 예외가 아니라 Result.error 코드로 돌려준다(가드 LLMResult와 같은 코드). 예외 메시지와
  응답 본문은 보지도 남기지도 않는다(TU-15). 취소(CancelledError)만 밖으로 나간다.
- SDK 재시도는 0이다. 재시도는 호출마다 retries번(주 호출만 1)이고, 다시 보낼 때 그 호출의 시간
  제한이 남은 예산 안에 들 때만 한다(게이트 대기 뒤에도 다시 본다). Retry-After가 그보다 길면
  기다리지 않는다. Retry-After는 숫자(retry-after-ms, retry-after 초)만 읽고, HTTP 날짜면 지터로
  기다린다(가드 llm은 날짜도 읽는다. 여기서 가드와 다르다).
- 게이트 대기도 요청 마감 안에 든다. 호출당 시간 제한은 보내는 시간 전체의 상한이다.
- 파라미터·구조화 방식은 부를 때마다 가드 LLM(probe 결과)에서 읽는다. 튜터 설정 값은 그 위에
  덮되, probe가 끈 파라미터(None)는 끈 채로 둔다.
"""

import asyncio
import contextlib
import json
import math
import os
import random
from collections import Counter
from collections.abc import Awaitable, Callable, Mapping
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any, Literal

import openai
from guardrail.config import load_config as load_guard_config
from guardrail.judges.schemas import strict_schema
from guardrail.llm import ErrorCode
from pydantic import BaseModel

from tutor.runtime.config import TutorConfig

Send = Callable[[dict[str, Any]], Awaitable[Any]]  # chat.completions.create(**kwargs)와 같은 모양
Gate = Callable[[], AbstractAsyncContextManager[Any]]
Structured = Literal["json_schema", "json_object"]
Outcome = Literal["sent", "budget", "gate", "no_time"]
OPTIONAL_PARAMS = ("reasoning_effort", "verbosity", "max_completion_tokens")
RETRY_JITTER_S = (0.2, 0.5)  # 가드 기본 설정(llm.retry_jitter_s)과 같다


@dataclass(frozen=True)
class Result:
    parsed: BaseModel | None = field(repr=False)  # 스키마를 통과한 것만. 답변이 로그에 새지 않게
    error: ErrorCode | None
    attempts: int  # 보낸 횟수(0이면 보내지 못함)
    latency_ms: float
    finish_reason: str | None  # 마지막 시도의(length면 잘림). 응답이 없으면 None
    completion_tokens: int | None  # 마지막 시도의 usage.completion_tokens


@dataclass(frozen=True)
class _Attempt:
    parsed: BaseModel | None = field(default=None, repr=False)
    error: ErrorCode | None = None
    retry: bool = False
    headers: Mapping[str, str] | None = None
    finish_reason: str | None = None
    completion_tokens: int | None = None


def _status_code(status: int) -> tuple[ErrorCode, bool]:
    """(오류 코드, 재시도 대상). 가드 llm과 같은 표(테스트로 맞춘다)."""
    if status == 408:
        return "timeout", True
    if status == 429:
        return "rate_limit", True
    if status == 409 or status >= 500:
        return "server", True
    if status in (401, 403):
        return "auth", False
    return "bad_request", False


def _retry_after(headers: Mapping[str, str]) -> float | None:
    """서버가 준 대기 초(retry-after-ms → retry-after). 유한한 숫자가 아니면 None(지터로 기다림)."""
    lowered = {k.lower(): v for k, v in headers.items()}
    for name, divisor in (("retry-after-ms", 1000.0), ("retry-after", 1.0)):
        try:
            delay = float(lowered[name]) / divisor
        except (KeyError, ValueError):
            continue
        return delay if math.isfinite(delay) else None
    return None


class TutorLLM:
    def __init__(
        self,
        send: Send,
        gate: Gate,
        *,
        model: str,
        base: Any,  # .params·.structured를 가진 것(가드 LLM). 부를 때마다 읽는다
        overrides: Mapping[str, Any] | None = None,  # 튜터 설정 값(reasoning_effort 등)
        clock: Callable[[], float] | None = None,  # 기본은 이벤트 루프 시각(요청 마감과 같은 시계)
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        rand: Callable[[], float] = random.random,  # noqa: S311 - 재시도 지터, 암호용 아님
    ) -> None:
        self.gate = gate
        self.model = model
        self.calls: Counter[str] = Counter()  # 보낸 호출 수(역할별, 하네스가 센다)
        self.unexpected: Counter[str] = Counter()  # 분류 밖 예외의 클래스 이름별 수(메시지 없음)
        self.max_calls: int | None = None  # 하네스 예산(넘으면 error="budget", 보내지 않음)
        self._base = base
        self._overrides = dict(overrides or {})
        self._send = send
        self._clock = clock
        self._sleep = sleep
        self._rand = rand

    @property
    def params(self) -> dict[str, Any]:
        """지금 보낼 파라미터. probe가 끈 것(None)은 끈 채로, 켜진 것은 튜터 설정 값으로 덮는다."""
        base = self._base.params
        return {
            k: None if base.get(k) is None else self._overrides.get(k, base[k])
            for k in OPTIONAL_PARAMS
        }

    @property
    def structured(self) -> Structured:
        return self._base.structured

    async def complete(
        self,
        role: str,
        messages: list[dict[str, str]],
        schema: type[BaseModel],
        *,
        deadline: float,
        timeout_s: float,
        retries: int,
    ) -> Result:
        t0 = self._now()
        kwargs = self._request(messages, schema)
        attempts, last = 0, _Attempt(error="timeout")
        while True:
            if deadline - self._now() <= 0:
                return self._result(last, attempts, t0)
            outcome, a = await self._attempt(
                role, kwargs, schema, deadline, timeout_s, retrying=attempts > 0
            )
            if outcome == "budget":
                return self._result(_Attempt(error="budget"), attempts, t0)
            if a is None:  # 게이트 대기로 마감이나 다시 보낼 시간을 다 썼다(앞 오류를 그대로)
                return self._result(last, attempts, t0)
            attempts, last = attempts + 1, a
            if a.error is None or not a.retry or attempts > retries:
                return self._result(a, attempts, t0)
            wait = _retry_after(a.headers or {})
            if wait is None or wait <= 0:
                lo, hi = RETRY_JITTER_S
                wait = lo + (hi - lo) * self._rand()
            if self._now() + wait + timeout_s > deadline:  # 다시 보낼 시간이 예산에 없다
                return self._result(a, attempts, t0)
            await self._sleep(wait)

    # --- 내부 ---

    def _now(self) -> float:
        return self._clock() if self._clock is not None else asyncio.get_running_loop().time()

    async def _attempt(
        self,
        role: str,
        kwargs: dict[str, Any],
        schema: type[BaseModel],
        deadline: float,
        limit: float,
        *,
        retrying: bool,
    ) -> tuple[Outcome, _Attempt | None]:
        async with contextlib.AsyncExitStack() as stack:
            try:
                async with asyncio.timeout(deadline - self._now()):  # 게이트 대기도 마감 안에
                    await stack.enter_async_context(self.gate())
            except TimeoutError:
                return "gate", None
            if self.max_calls is not None and sum(self.calls.values()) >= self.max_calls:
                return "budget", None
            left = deadline - self._now()
            if retrying and limit > left:  # 가드와 같이: 게이트 대기 뒤 다시 보낼 시간이 모자람
                return "no_time", None
            self.calls[role] += 1
            cap = min(limit, left)
            try:
                async with asyncio.timeout(cap):  # 호출당 시간 제한(보내는 시간 전체)
                    resp = await self._send({**kwargs, "timeout": cap})
            except Exception as exc:  # noqa: BLE001 - 코드로만 돌려준다(메시지·본문은 보지 않는다)
                return "sent", self._fail(exc)
        return "sent", self._parse(resp, schema)

    def _request(self, messages: list[dict[str, str]], schema: type[BaseModel]) -> dict[str, Any]:
        msgs = [dict(m) for m in messages]
        if self.structured == "json_object":
            note = "JSON 객체만 출력한다. 스키마:\n" + json.dumps(
                strict_schema(schema), ensure_ascii=False
            )
            if msgs and msgs[0].get("role") == "system":
                msgs[0]["content"] = f"{msgs[0]['content']}\n\n{note}"
            else:
                msgs.insert(0, {"role": "system", "content": note})
            response_format: dict[str, Any] = {"type": "json_object"}
        else:
            response_format = {
                "type": "json_schema",
                "json_schema": {
                    "name": schema.__name__,
                    "schema": strict_schema(schema),
                    "strict": True,
                },
            }
        kwargs: dict[str, Any] = {"model": self.model, "messages": msgs}
        kwargs.update({k: v for k, v in self.params.items() if v is not None})
        kwargs["response_format"] = response_format
        return kwargs

    @staticmethod
    def _parse(resp: Any, schema: type[BaseModel]) -> _Attempt:
        tokens = getattr(getattr(resp, "usage", None), "completion_tokens", None)
        meta: dict[str, Any] = {"completion_tokens": tokens if isinstance(tokens, int) else None}
        try:
            choice = resp.choices[0]
            meta["finish_reason"] = getattr(choice, "finish_reason", None)
            content = choice.message.content
            if not isinstance(content, str):
                raise ValueError
            return _Attempt(parsed=schema.model_validate_json(content), **meta)
        except (AttributeError, IndexError, TypeError, ValueError):  # ValidationError도 ValueError
            return _Attempt(error="bad_json", retry=True, **meta)

    def _fail(self, exc: Exception) -> _Attempt:
        """예외 → 오류 코드(가드 llm과 같은 규칙). 메시지·body는 보지 않는다."""
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
            return _Attempt(error=code, retry=retry, headers=headers)
        if isinstance(exc, openai.OpenAIError):  # 클라이언트 설정 오류(키 없음 등)
            return _Attempt(error="auth")
        self.unexpected[type(exc).__name__] += 1
        return _Attempt(error="server")

    def _result(self, a: _Attempt, attempts: int, t0: float) -> Result:
        return Result(
            parsed=a.parsed if a.error is None else None,
            error=a.error,
            attempts=attempts,
            latency_ms=(self._now() - t0) * 1000,
            finish_reason=a.finish_reason,
            completion_tokens=a.completion_tokens,
        )


def _openai_send() -> Send:
    """AsyncOpenAI는 이벤트 루프마다 첫 호출 때 만든다(httpx 연결이 루프에 묶이므로).

    환경변수: OPENAI_API_KEY(없으면 auth 오류), OPENAI_BASE_URL(비어 있으면 SDK 기본). SDK 재시도 0.
    """
    made: dict[str, Any] = {"loop": None, "client": None}

    async def send(kwargs: dict[str, Any]) -> Any:
        loop = asyncio.get_running_loop()
        if made["loop"] is not loop:
            key = os.environ.get("OPENAI_API_KEY")
            if not key:
                raise openai.OpenAIError("OPENAI_API_KEY가 없다")
            base_url = os.environ.get("OPENAI_BASE_URL") or None
            made["client"] = openai.AsyncOpenAI(api_key=key, base_url=base_url, max_retries=0)
            made["loop"] = loop
        return await made["client"].chat.completions.create(**kwargs)

    return send


def build_llm(app_state: Any, cfg: TutorConfig) -> TutorLLM:
    """서버에서는 가드 LLM의 게이트·probe 결과를 같이 쓴다(TU-13, 가드 D4).

    가드가 없으면(프로세스 안 측정) 가드 설정의 파라미터 값을 쓰고 게이트는 두지 않는다.
    튜터 설정의 reasoning_effort·max_completion_tokens를 그 위에 덮는다.
    """
    guard_llm = getattr(getattr(app_state, "guard", None), "llm", None)
    if guard_llm is not None:
        gate, base = guard_llm.slot, guard_llm
    else:
        guard_cfg = load_guard_config().llm
        gate = contextlib.nullcontext
        params = {k: getattr(guard_cfg, k) for k in OPTIONAL_PARAMS}
        base = SimpleNamespace(params=params, structured=guard_cfg.structured_output)
    overrides = {
        "reasoning_effort": cfg.llm.reasoning_effort,
        "max_completion_tokens": cfg.llm.max_completion_tokens,
    }
    model = os.environ.get("OPENAI_MODEL", "")
    return TutorLLM(_openai_send(), gate, model=model, base=base, overrides=overrides)
