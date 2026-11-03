"""LLM 게이트웨이 (master-plan §4.3): 동시성 게이트(세마포어 + AIMD), 단일 재시도, 예산, faults.

- 호출 실패는 예외가 아니라 `LLMResult.error`로 돌려준다. 실패 처리(GR-14)는 호출한 쪽이 한다.
  `CancelledError`와 설정 오류(환경변수 없음, 고정 응답 미등록)만 밖으로 나간다.
- 재시도는 여기서만 한다(SDK는 max_retries=0). 규칙은 openai-python 3.42.1 `_base_client.py`와 같다.
- 마감은 보내는 시간과 백오프만 센다. 게이트 대기는 시도마다 `gate.max_wait_s`까지이고 마감에 넣지
  않는다(폭주 때 ⑤가 생성 뒤에 줄 서느라 마감을 다 쓰지 않게).
- 시계·sleep·난수를 주입받아 재시도·AIMD 테스트가 0ms에 돈다.
- 이 모듈은 guardrail.eval을 import하지 않는다. `fake_ok` 고정 응답은 FAKE_OK 한 곳에 등록한다.
"""

import asyncio
import email.utils
import json
import math
import os
import random
import re
import time
from collections import Counter, deque
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, ValidationError

from guardrail.config import GuardConfig
from guardrail.types import ROLES, Role

ErrorCode = Literal["timeout", "rate_limit", "server", "bad_json", "auth", "bad_request", "budget"]
Outcome = Literal["ok", "rate_limit", "error", "cancel"]
Structured = Literal["json_schema", "json_object", "prompt"]

MAX_ATTEMPTS = 3
RETRY_BUDGET_RATIO = 0.1
RETRY_BUDGET_FLOOR = 5
MAX_RETRY_AFTER_S = 120.0  # openai MAX_RETRY_AFTER_DELAY
INITIAL_RETRY_DELAY_S = 0.5
MAX_RETRY_DELAY_S = 8.0
_INF_LITERALS = {"inf", "+inf", "infinity", "+infinity"}

# 역할별 샘플링 상수(temperature, max_tokens). complete(max_tokens=)가 우선한다(GP0 S0.3)
SAMPLING: dict[Role, tuple[float, int]] = {
    "generator": (0.7, 512),
    "input_judge": (0.0, 384),  # ⑤ 출력은 reason에 JSON 필드가 붙어 256을 넘을 수 있다(S2.4)
    "exchange_judge": (0.0, 256),
    "attack_judge": (0.0, 256),
    "jev": (0.0, 256),
}

REASK = "앞 출력이 스키마에 맞지 않습니다. JSON 객체 하나만 다시 출력하세요."
_DOWNGRADE: dict[Structured, Structured | None] = {
    "json_schema": "json_object",
    "json_object": "prompt",
    "prompt": None,
}


@dataclass(frozen=True)
class Reply:
    """send 한 번의 결과. 토큰이 None이면 게이트웨이가 usage를 주지 않은 것이다."""

    text: str
    tokens_in: int | None
    tokens_out: int | None
    finish_reason: str | None = None


class TransportError(Exception):
    """보내기 실패. 메시지 없이 분류 정보만 담는다(키·요청 내용이 로그로 새지 않게)."""

    def __init__(
        self,
        kind: Literal["timeout", "connect", "status"],
        status: int | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        super().__init__()
        self.kind = kind
        self.status = status
        self.headers = {k.lower(): v for k, v in (headers or {}).items()}


class BudgetExceeded(Exception):
    """slot()에서 호출 상한에 닿음. 보내기 전에 막을 길이 예외뿐이라 예외로 알린다."""


Send = Callable[
    ..., Awaitable[Reply]
]  # send(model, messages, *, response_format, temperature, max_tokens, timeout)
Fake = Callable[[list[dict]], str]  # messages → fake_ok 고정 응답(순수 함수)


@dataclass
class LLMResult:
    text: str | None
    parsed: BaseModel | None
    tokens_in: int
    tokens_out: int
    latency_ms: float
    attempts: int
    finish_reason: str | None  # "length"면 max_tokens에서 잘림
    error: ErrorCode | None


@dataclass
class Usage:
    calls: int = 0  # 실제로 보낸 요청 수(slot 진입 포함)
    retries: int = 0
    reasks: int = 0
    faults: int = 0
    gate_timeouts: int = 0
    usage_missing: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    errors: Counter[str] = field(default_factory=Counter)
    latencies_ms: list[float] = field(default_factory=list)


# --- fake_ok 고정 응답(§4.3). 소유 슬라이스가 같은 dict에 등록한다 ---

_S1_LINE = re.compile(r"^\[S1\]\s*(?:\([^)\n]*\)\s*)?(?P<text>.+)$", re.M)
_SENTENCE_END = re.compile(r"(?<=[.!?。])\s")
# eval.mock_tutor.NOT_FOUND와 같은 문구(llm은 eval을 import하지 않는다)
_NOT_FOUND = "교재에서 찾지 못했어요."


def _fake_generator(messages: list[dict]) -> str:
    """user 메시지의 첫 청크([S1] 줄) 첫 문장 + " [S1]". 청크가 없으면 근거 없음 문구."""
    user = next((m["content"] for m in reversed(messages) if m.get("role") == "user"), "")
    m = _S1_LINE.search(user)
    if not m:
        return _NOT_FOUND
    first = _SENTENCE_END.split(m.group("text").strip(), maxsplit=1)[0]
    return f"{first} [S1]"


def _fake_attack_judge(messages: list[dict]) -> str:
    return json.dumps({"refused": False, "success": False, "reason": "fake"})


# exchange_judge(S1.5)·input_judge(S2.4)·jev(S2.10)는 각 소유 슬라이스가 더한다
FAKE_OK: dict[Role, Fake] = {"generator": _fake_generator, "attack_judge": _fake_attack_judge}


# --- 오류 분류와 재시도 대기 ---


def classify(err: TransportError) -> tuple[ErrorCode, bool]:
    """(error 코드, 재시도 대상). `x-should-retry` 헤더가 있으면 재시도 여부는 그것이 우선이다."""
    if err.kind == "timeout" or err.status == 408:
        code, retry = "timeout", True
    elif err.kind == "connect" or err.status == 409 or (err.status or 0) >= 500:
        code, retry = "server", True
    elif err.status == 429:
        code, retry = "rate_limit", True
    elif err.status in (401, 403):
        code, retry = "auth", False
    else:
        code, retry = "bad_request", False
    flag = err.headers.get("x-should-retry")
    if flag in ("true", "false"):
        retry = flag == "true"
    return code, retry


def _parse_retry_after(headers: Mapping[str, str], now: float) -> float | None:
    for name, divisor in (("retry-after-ms", 1000.0), ("retry-after", 1.0)):
        value = headers.get(name)
        if value is None:
            continue
        try:
            delay = float(value)
        except ValueError:
            continue
        if math.isinf(delay) and value.strip().lower() not in _INF_LITERALS:
            return MAX_RETRY_AFTER_S + 1  # 수치 넘침은 과도한 대기로 본다(SDK와 같음)
        return delay / divisor
    try:
        parsed = email.utils.parsedate_tz(headers.get("retry-after"))  # type: ignore[arg-type]
        if parsed is None:
            return None
        return float(email.utils.mktime_tz(parsed) - now)
    except (TypeError, ValueError, OverflowError):
        return None


def retry_wait(
    headers: Mapping[str, str], n: int, rand: Callable[[], float], now: float
) -> float | None:
    """n번째(0부터) 재시도 전 대기 초. None이면 재시도하지 않는다(서버가 120초 넘게 기다리라 함)."""
    headers = {k.lower(): v for k, v in headers.items()}
    after = _parse_retry_after(headers, now)
    if after is not None and math.isfinite(after):
        if after > MAX_RETRY_AFTER_S:
            return None
        if after > 0:
            return after
    backoff = min(INITIAL_RETRY_DELAY_S * 2.0**n, MAX_RETRY_DELAY_S)
    return backoff * (1 - 0.25 * rand())


# --- 동시성 게이트 ---


class Gate:
    """세마포어 + AIMD. 429에만 반응해 한도를 절반으로(같은 구간에서 한 번), 성공 N회마다 +1."""

    def __init__(self, initial: int, min_: int, max_: int, increase_after: int) -> None:
        self.limit = initial
        self.min = min_
        self.max = max_
        self.increase_after = increase_after
        self.in_flight = 0
        self.epoch = 0
        self._streak = 0
        self._waiters: deque[asyncio.Future[None]] = deque()

    async def acquire(self, max_wait_s: float) -> int:
        """허가를 받고 그때의 구간 번호를 돌려준다. max_wait_s를 넘기면 TimeoutError."""
        if self.in_flight < self.limit and not self._waiters:
            self.in_flight += 1
            return self.epoch
        fut: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._waiters.append(fut)
        try:
            async with asyncio.timeout(max_wait_s):
                await fut
        except BaseException:
            if fut.done() and not fut.cancelled():
                # 허가를 받은 직후에 취소·시간 초과가 겹쳤다. 받은 자리를 돌려준다
                self.in_flight -= 1
                self._wake()
            elif fut in self._waiters:
                self._waiters.remove(fut)
            raise
        return self.epoch

    def release(self, epoch: int, outcome: Outcome) -> None:
        """동기 함수다(취소 중 finally에서 await하지 않게)."""
        self.in_flight -= 1
        if outcome == "rate_limit":
            self._streak = 0
            if epoch == self.epoch:
                self.limit = max(self.min, self.limit // 2)
                self.epoch += 1
        elif outcome == "ok":
            self._streak += 1
            if self._streak % self.increase_after == 0:
                self.limit = min(self.max, self.limit + 1)
        else:
            self._streak = 0  # 5xx·타임아웃·취소는 과부하 신호로 세지 않는다
        self._wake()

    def _wake(self) -> None:
        while self._waiters and self.in_flight < self.limit:
            fut = self._waiters.popleft()
            if fut.done():
                continue
            self.in_flight += 1
            fut.set_result(None)


# --- 실제 SDK 연결 ---


def openai_send(client: Any) -> Send:
    """AsyncOpenAI(max_retries=0)를 Send로 감싼다. SDK 예외는 메시지 없는 TransportError로."""
    import openai  # 테스트·--fake 경로는 SDK를 쓰지 않으므로 여기서 가져온다

    async def send(
        model: str,
        messages: list[dict],
        *,
        response_format: dict | None,
        temperature: float,
        max_tokens: int,
        timeout: float,
    ) -> Reply:
        kwargs: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "timeout": timeout,
        }
        if response_format is not None:
            kwargs["response_format"] = response_format
        try:
            resp = await client.chat.completions.create(**kwargs)
        except openai.APITimeoutError:
            raise TransportError("timeout") from None
        except openai.APIConnectionError:
            raise TransportError("connect") from None
        except openai.APIStatusError as e:
            raise TransportError("status", e.status_code, dict(e.response.headers)) from None
        choice = resp.choices[0]
        usage = resp.usage
        return Reply(
            text=choice.message.content or "",
            tokens_in=usage.prompt_tokens if usage else None,
            tokens_out=usage.completion_tokens if usage else None,
            finish_reason=choice.finish_reason,
        )

    return send


def _env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"환경변수 {name}가 없다")  # 이름만. 값은 넣지 않는다
    return value


def _default_send() -> Send:
    from openai import AsyncOpenAI

    client = AsyncOpenAI(
        base_url=_env("GUARD_LLM_BASE_URL"), api_key=_env("GUARD_LLM_API_KEY"), max_retries=0
    )
    return openai_send(client)


# --- 게이트웨이 ---


class LLM:
    def __init__(
        self,
        cfg: GuardConfig,
        *,
        send: Send | None = None,
        fake_responders: Mapping[Role, Fake] = FAKE_OK,
        max_calls: int | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        rand: Callable[[], float] = random.random,
        wall: Callable[[], float] = time.time,
    ) -> None:
        unregistered = [
            r for r, v in cfg.faults.items() if v == "fake_ok" and r not in fake_responders
        ]
        if unregistered:
            raise ValueError(
                "faults의 fake_ok에 고정 응답이 없는 역할: " + ", ".join(unregistered)
                + " (그 역할을 만드는 슬라이스가 llm.FAKE_OK에 등록한다)"
            )  # fmt: skip
        self.cfg = cfg
        self._send = send
        self._fakes = fake_responders
        self.max_calls = max_calls
        self._clock = clock
        self._sleep = sleep
        self._rand = rand
        self._wall = wall
        g = cfg.gate
        self.gate = Gate(g.initial, g.min, g.max, g.increase_after)
        self.structured_level: Structured = g.structured_output
        self.usage: dict[Role, Usage] = {r: Usage() for r in ROLES}
        self._first_attempts = 0
        self._retries = 0

    # --- 예산 ---

    def total_calls(self) -> int:
        return sum(u.calls for u in self.usage.values())

    def _over_budget(self) -> bool:
        return self.max_calls is not None and self.total_calls() >= self.max_calls

    def _retry_allowed(self) -> bool:
        cap = max(RETRY_BUDGET_FLOOR, RETRY_BUDGET_RATIO * self._first_attempts)
        return self._retries < cap

    # --- 공개 ---

    async def complete(
        self,
        role: Role,
        messages: list[dict],
        *,
        schema: type[BaseModel] | None = None,
        deadline_s: float,
        max_tokens: int | None = None,
    ) -> LLMResult:
        t0 = self._clock()
        usage = self.usage[role]
        fault = self.cfg.faults.get(role)
        if fault is not None:
            usage.faults += 1
            if fault != "fake_ok":
                return self._result(t0, role, None, None, 0, 0, 0, None, fault)
            text = self._fakes[role](messages)
            parsed = _parse(text, schema, "json_schema") if schema else None
            error = "bad_json" if schema and parsed is None else None
            return self._result(t0, role, text, parsed, 0, 0, 0, None, error)
        model = getattr(self.cfg.models, role)
        if not model:
            # 모델명이 없으면 보내지 않는다. 실행 전 확인은 run.py가 한다(S0.7)
            return self._result(t0, role, None, None, 0, 0, 0, None, "bad_request")
        if self._send is None:
            self._send = _default_send()
        return await self._attempts(role, model, messages, schema, deadline_s, max_tokens, t0)

    @asynccontextmanager
    async def slot(self, role: Role) -> AsyncIterator[None]:
        """가드 밖 호출(실제 튜터 생성·의무 탐지)이 게이트와 예산만 함께 쓴다.

        재시도·토큰 집계는 하지 않는다. 그 호출의 429는 게이트가 보지 못한다(S3.1에서 필요하면).
        """
        if role in self.cfg.faults:
            yield
            return
        usage = self.usage[role]
        if self._over_budget():
            usage.errors["budget"] += 1
            raise BudgetExceeded(role)
        usage.calls += 1
        try:
            epoch = await self.gate.acquire(self.cfg.gate.max_wait_s)
        except TimeoutError:
            usage.gate_timeouts += 1
            raise
        outcome: Outcome = "error"
        try:
            yield
            outcome = "ok"
        finally:
            self.gate.release(epoch, outcome)

    # --- 내부 ---

    async def _attempts(
        self,
        role: Role,
        model: str,
        messages: list[dict],
        schema: type[BaseModel] | None,
        deadline_s: float,
        max_tokens: int | None,
        t0: float,
    ) -> LLMResult:
        usage = self.usage[role]
        temperature, default_mt = SAMPLING[role]
        mt = max_tokens if max_tokens is not None else default_mt
        remaining = deadline_s
        attempts = tokens_in = tokens_out = n_retry = 0
        reasked = downgraded = False
        convo = list(messages)
        self._first_attempts += 1
        while True:
            if self._over_budget():
                return self._result(
                    t0, role, None, None, tokens_in, tokens_out, attempts, None, "budget"
                )
            try:
                epoch = await self.gate.acquire(self.cfg.gate.max_wait_s)
            except TimeoutError:
                usage.gate_timeouts += 1
                return self._result(
                    t0, role, None, None, tokens_in, tokens_out, attempts, None, "timeout"
                )
            level = self.structured_level
            usage.calls += 1
            attempts += 1
            sent_at = self._clock()
            outcome: Outcome = "cancel"
            reply: Reply | None = None
            failure: TransportError | None = None
            try:
                reply = await self._send(
                    model,
                    _with_schema(convo, schema, level),
                    response_format=_response_format(schema, level),
                    temperature=temperature,
                    max_tokens=mt,
                    timeout=remaining,
                )
                outcome = "ok"
            except TransportError as e:
                failure = e
                outcome = "rate_limit" if e.status == 429 else "error"
            finally:
                self.gate.release(epoch, outcome)
                remaining -= self._clock() - sent_at

            if reply is not None:
                if reply.tokens_in is None or reply.tokens_out is None:
                    usage.usage_missing += 1
                tokens_in += reply.tokens_in or 0
                tokens_out += reply.tokens_out or 0
                parsed = _parse(reply.text, schema, level)
                if schema is None or parsed is not None:
                    return self._result(
                        t0, role, reply.text, parsed, tokens_in, tokens_out, attempts,
                        reply.finish_reason, None,
                    )  # fmt: skip
                if reasked or remaining <= 0 or self._over_budget():
                    # finish_reason은 text로 돌려주는 원문을 낸 시도의 값이다
                    return self._result(
                        t0, role, reply.text, None, tokens_in, tokens_out, attempts,
                        reply.finish_reason, "bad_json",
                    )  # fmt: skip
                reasked = True
                usage.reasks += 1
                convo = [
                    *convo,
                    {"role": "assistant", "content": reply.text},
                    {"role": "user", "content": REASK},
                ]
                continue

            assert failure is not None
            code, retryable = classify(failure)
            if code == "bad_request" and schema is not None and not downgraded:
                lower = _DOWNGRADE[level]
                if lower is not None:
                    # json_schema 미지원 같은 400: 한 단 내려 1회, 이 인스턴스에서 유지한다
                    downgraded = True
                    if self.structured_level == level:
                        self.structured_level = lower
                    continue
            wait = (
                retry_wait(failure.headers, n_retry, self._rand, self._wall())
                if retryable
                else None
            )
            if (
                wait is None
                or attempts >= MAX_ATTEMPTS
                or not self._retry_allowed()
                or wait >= remaining
            ):
                return self._result(
                    t0, role, None, None, tokens_in, tokens_out, attempts, None, code
                )
            self._retries += 1
            usage.retries += 1
            n_retry += 1
            await self._sleep(wait)
            remaining -= wait

    def _result(
        self,
        t0: float,
        role: Role,
        text: str | None,
        parsed: BaseModel | None,
        tokens_in: int,
        tokens_out: int,
        attempts: int,
        finish_reason: str | None,
        error: ErrorCode | None,
    ) -> LLMResult:
        usage = self.usage[role]
        latency = (self._clock() - t0) * 1000.0
        usage.tokens_in += tokens_in
        usage.tokens_out += tokens_out
        usage.latencies_ms.append(latency)
        if error is not None:
            usage.errors[error] += 1
        return LLMResult(
            text, parsed, tokens_in, tokens_out, latency, attempts, finish_reason, error
        )


def _schema_text(schema: type[BaseModel]) -> str:
    return "다음 JSON 스키마에 맞는 JSON 객체 하나만 출력하세요.\n" + json.dumps(
        schema.model_json_schema(), ensure_ascii=False
    )


def _with_schema(
    messages: list[dict], schema: type[BaseModel] | None, level: Structured
) -> list[dict]:
    """json_object·prompt 단에서는 시스템 메시지에 스키마를 붙인다."""
    if schema is None or level == "json_schema":
        return messages
    note = _schema_text(schema)
    if messages and messages[0].get("role") == "system":
        first = {**messages[0], "content": f"{messages[0]['content']}\n\n{note}"}
        return [first, *messages[1:]]
    return [{"role": "system", "content": note}, *messages]


def _response_format(schema: type[BaseModel] | None, level: Structured) -> dict | None:
    if schema is None or level == "prompt":
        return None
    if level == "json_object":
        return {"type": "json_object"}
    return {
        "type": "json_schema",
        "json_schema": {
            "name": schema.__name__,
            "schema": schema.model_json_schema(),
            "strict": True,
        },
    }


def _parse(text: str, schema: type[BaseModel] | None, level: Structured) -> BaseModel | None:
    if schema is None:
        return None
    candidate = text
    if level == "prompt":
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end < start:
            return None
        candidate = text[start : end + 1]
    try:
        return schema.model_validate_json(candidate)
    except (ValidationError, ValueError):
        return None
