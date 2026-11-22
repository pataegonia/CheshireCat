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
- 측정용 수(calls·failures·attempt_errors·completion_buckets)는 역할·고정 갈래·토큰 구간을 키로
  수만 센다. 답과 재시도 판단에서 읽지 않고 본문은 남기지 않는다.
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
from contextvars import ContextVar
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any, Literal

import openai
from guardrail.config import load_config as load_guard_config
from guardrail.judges.schemas import strict_schema
from guardrail.llm import ErrorCode
from pydantic import BaseModel

from tutor.runtime.config import PROXY_MAX_COMPLETION_TOKENS, TutorConfig

Send = Callable[[dict[str, Any]], Awaitable[Any]]  # chat.completions.create(**kwargs)와 같은 모양
Gate = Callable[[], AbstractAsyncContextManager[Any]]
Structured = Literal["json_schema", "json_object"]
Outcome = Literal["sent", "budget", "gate", "no_time"]
OPTIONAL_PARAMS = ("reasoning_effort", "verbosity", "max_completion_tokens")
RETRY_JITTER_S = (0.2, 0.5)  # 가드 기본 설정(llm.retry_jitter_s)과 같다
TOKEN_BUCKET = 100  # 성공한 시도의 completion_tokens 분포 구간 폭
# 요청 하나에서 보낸 시도 수(역할별). 라우터가 요청마다 새 Counter를 넣는다(없으면 세지 않는다).
# 접근 로그의 /tutor 호출 기록 칸(multi_draft를 켰을 때)에 쓴다. 본문은 없다
SENT: ContextVar[Counter[str] | None] = ContextVar("tutor_llm_sent", default=None)


@dataclass(frozen=True)
class Result:
    parsed: BaseModel | None = field(repr=False)  # 스키마를 통과한 것만. 답변이 로그에 새지 않게
    error: ErrorCode | None
    attempts: int  # 보낸 횟수(0이면 보내지 못함)
    latency_ms: float
    finish_reason: str | None  # 마지막 시도의(length면 잘림). 응답이 없으면 None
    completion_tokens: int | None  # 마지막 시도의 usage.completion_tokens
    gate_ms: float = 0.0  # 게이트 대기 합(모든 시도)


@dataclass(frozen=True)
class _Attempt:
    parsed: BaseModel | None = field(default=None, repr=False)
    error: ErrorCode | None = None
    retry: bool = False
    headers: Mapping[str, str] | None = None
    finish_reason: str | None = None
    completion_tokens: int | None = None
    cause: str | None = None  # bad_json의 갈래(_bad_json_cause). attempt_errors 키로 쓴다


def _token_bucket(tokens: int) -> int:
    """completion_tokens의 구간: TOKEN_BUCKET 단위로 올리고 0~프록시 상한 안에 둔다.

    구간 b는 (b - TOKEN_BUCKET, b] 토큰이다(상한 b 이하). 키는 역할마다 21개를 넘지 않는다.
    """
    bucket = math.ceil(tokens / TOKEN_BUCKET) * TOKEN_BUCKET
    return min(max(bucket, 0), PROXY_MAX_COMPLETION_TOKENS)


def _bad_json_cause(resp: Any) -> str:
    """형식 오류의 갈래. 본문은 읽어 남기지 않고 모양만 본다.

    length: 토큰 상한에 잘림, content_filter: 내용 필터에 걸림, refusal: 모델 거절,
    no_content: 내용이 문자열이 아님, schema: 그 밖(빈 문자열·JSON 아님·스키마 불일치),
    no_choice: choices가 없음.
    """
    try:
        choice = resp.choices[0]
    except (AttributeError, IndexError, TypeError):
        return "no_choice"
    finish = getattr(choice, "finish_reason", None)
    if finish in ("length", "content_filter"):
        return finish
    message = getattr(choice, "message", None)
    if getattr(message, "refusal", None):
        return "refusal"
    if not isinstance(getattr(message, "content", None), str):
        return "no_content"
    return "schema"


async def _read_stream(stream: Any) -> SimpleNamespace:
    """스트림 조각 → 비스트림 응답 모양(choices[0].message.content·finish_reason, usage).
    가드 llm._read_stream과 같은 규칙: 내용은 이어 붙이고 finish_reason·usage는 마지막 값,
    내용 조각이 없으면 content None(예: 추론에 토큰을 다 써 length로 끝남)."""
    parts: list[str] = []
    finish, usage = None, None
    async for chunk in stream:
        usage = getattr(chunk, "usage", None) or usage
        for choice in getattr(chunk, "choices", None) or []:
            text = getattr(getattr(choice, "delta", None), "content", None)
            if isinstance(text, str):
                parts.append(text)
            finish = getattr(choice, "finish_reason", None) or finish
    message = SimpleNamespace(content="".join(parts) if parts else None, refusal=None)
    return SimpleNamespace(
        choices=[SimpleNamespace(message=message, finish_reason=finish)], usage=usage
    )


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
        # 요청당 시도 상한(재시도 포함, 설정 llm.multi_draft.max_calls). 요청의 SENT 합이 이 값에
        # 닿으면 보내지 않는다(error="budget"). None이면 요청 단위로 막지 않는다
        self.request_cap: int | None = None
        self.blocked: Counter[str] = Counter()  # 예산에 막혀 보내지 못한 호출 수(역할별)
        # 오류로 끝난 호출 수((역할, 최종 오류 코드)별, 하네스가 센다). complete 한 번이 1회다:
        # 재시도 뒤 최종 오류·게이트 대기 초과도 1회(calls는 보낸 시도 수라 단위가 다르다).
        # 예산 막힘은 blocked에만 센다
        self.failures: Counter[tuple[str, str]] = Counter()
        # 실패한 시도 수((역할, 갈래)별, 보낸 시도 단위). 재시도로 살아난 시도도 센다. 갈래는
        # bad_json이면 그 원인(_bad_json_cause), 그 밖은 오류 코드(timeout·server·rate_limit·
        # auth·bad_request)다. 게이트·마감·예산으로 보내지 못한 것은 세지 않는다
        self.attempt_errors: Counter[tuple[str, str]] = Counter()
        # 성공한 시도의 completion_tokens(추론 토큰 포함) 분포((역할, _token_bucket 구간)별 수)
        self.completion_buckets: Counter[tuple[str, int]] = Counter()
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
        effort: str | None = None,
        stream: bool = False,
        max_tokens: int | None = None,
    ) -> Result:
        """`effort`를 주면 이 호출만 reasoning_effort를 그 값으로 보낸다(multi_draft 초안).
        probe가 reasoning_effort를 껐으면 주어도 보내지 않는다(`sends_effort`).
        `stream`이면 스트리밍으로 보내 조각을 이어 붙인 뒤 같은 스키마로 읽고, 호출당 시간 제한은
        스트림 전체에 걸린다. `max_tokens`는 이 호출만의 max_completion_tokens(probe가 끄면
        보내지 않는다)."""
        result = await self._complete(
            role,
            messages,
            schema,
            deadline=deadline,
            timeout_s=timeout_s,
            retries=retries,
            effort=effort,
            stream=stream,
            max_tokens=max_tokens,
        )
        if result.error is not None and result.error != "budget":
            self.failures[(role, result.error)] += 1
        return result

    async def _complete(
        self,
        role: str,
        messages: list[dict[str, str]],
        schema: type[BaseModel],
        *,
        deadline: float,
        timeout_s: float,
        retries: int,
        effort: str | None = None,
        stream: bool = False,
        max_tokens: int | None = None,
    ) -> Result:
        t0 = self._now()
        kwargs = self._request(messages, schema)
        if effort is not None and "reasoning_effort" in kwargs:
            kwargs["reasoning_effort"] = effort
        if max_tokens is not None and "max_completion_tokens" in kwargs:
            kwargs["max_completion_tokens"] = max_tokens
        if stream:
            kwargs["stream"] = True
        attempts, last = 0, _Attempt(error="timeout")
        waited: list[float] = []  # 시도마다 게이트 대기(초)
        while True:
            if deadline - self._now() <= 0:
                return self._result(last, attempts, t0, waited)
            outcome, a = await self._attempt(
                role, kwargs, schema, deadline, timeout_s, retrying=attempts > 0, waited=waited
            )
            if outcome == "budget":
                self.blocked[role] += 1
                return self._result(_Attempt(error="budget"), attempts, t0, waited)
            if a is None:  # 게이트 대기로 마감이나 다시 보낼 시간을 다 썼다(앞 오류를 그대로)
                return self._result(last, attempts, t0, waited)
            attempts, last = attempts + 1, a
            if a.error is None or not a.retry or attempts > retries:
                return self._result(a, attempts, t0, waited)
            wait = _retry_after(a.headers or {})
            if wait is None or wait <= 0:
                lo, hi = RETRY_JITTER_S
                wait = lo + (hi - lo) * self._rand()
            if self._now() + wait + timeout_s > deadline:  # 다시 보낼 시간이 예산에 없다
                return self._result(a, attempts, t0, waited)
            await self._sleep(wait)

    # --- 내부 ---

    def _now(self) -> float:
        return self._clock() if self._clock is not None else asyncio.get_running_loop().time()

    @property
    def sends_effort(self) -> bool:
        """reasoning_effort를 보내는가(probe가 끄지 않았나)."""
        return self.params.get("reasoning_effort") is not None

    async def _attempt(
        self,
        role: str,
        kwargs: dict[str, Any],
        schema: type[BaseModel],
        deadline: float,
        limit: float,
        *,
        retrying: bool,
        waited: list[float] | None = None,
    ) -> tuple[Outcome, _Attempt | None]:
        async with contextlib.AsyncExitStack() as stack:
            g0 = self._now()
            try:
                async with asyncio.timeout(deadline - self._now()):  # 게이트 대기도 마감 안에
                    await stack.enter_async_context(self.gate())
            except TimeoutError:
                return "gate", None
            finally:
                if waited is not None:
                    waited.append(self._now() - g0)
            if self.max_calls is not None and sum(self.calls.values()) >= self.max_calls:
                return "budget", None
            sent = SENT.get()
            cap = self.request_cap
            if sent is not None and cap is not None and sum(sent.values()) >= cap:
                return "budget", None  # 요청 상한: 세기와 더하기 사이에 await가 없다(예약)
            left = deadline - self._now()
            if retrying and limit > left:  # 가드와 같이: 게이트 대기 뒤 다시 보낼 시간이 모자람
                return "no_time", None
            self.calls[role] += 1
            if sent is not None:
                sent[role] += 1
            cap = min(limit, left)
            resp = None
            try:
                async with asyncio.timeout(cap):  # 호출당 시간 제한(보내는 시간 전체, 스트림 포함)
                    resp = await self._send({**kwargs, "timeout": cap})
                    if kwargs.get("stream") and not hasattr(resp, "choices"):
                        resp = await _read_stream(resp)
            except Exception as exc:  # noqa: BLE001 - 코드로만 돌려준다(메시지·본문은 보지 않는다)
                return "sent", self._tally(role, self._fail(exc))
            finally:
                close = getattr(resp, "close", None) if kwargs.get("stream") else None
                if close is not None:  # 취소·시간 초과로 끊긴 스트림도 연결을 닫는다
                    with contextlib.suppress(Exception):
                        await close()
        return "sent", self._tally(role, self._parse(resp, schema))

    def _tally(self, role: str, a: _Attempt) -> _Attempt:
        """보낸 시도 하나를 측정용으로 센다. 답과 재시도 판단에는 쓰지 않는다."""
        if a.error is not None:
            self.attempt_errors[(role, a.cause or a.error)] += 1
        elif a.completion_tokens is not None:
            self.completion_buckets[(role, _token_bucket(a.completion_tokens))] += 1
        return a

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
            return _Attempt(error="bad_json", retry=True, cause=_bad_json_cause(resp), **meta)

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

    def _result(
        self, a: _Attempt, attempts: int, t0: float, waited: list[float] | None = None
    ) -> Result:
        return Result(
            parsed=a.parsed if a.error is None else None,
            error=a.error,
            attempts=attempts,
            latency_ms=(self._now() - t0) * 1000,
            finish_reason=a.finish_reason,
            completion_tokens=a.completion_tokens,
            gate_ms=sum(waited or ()) * 1000,
        )


def _openai_send() -> Send:
    """AsyncOpenAI는 이벤트 루프마다 첫 호출 때 만든다(httpx 연결이 루프에 묶이므로).

    환경변수: OPENAI_API_KEY·OPENAI_BASE_URL. 하나라도 비면 보내지 않고 auth 오류다. 주소가 비면
    SDK가 기본 주소(공개 OpenAI)로 보내기 때문이다(운영진 API만 부른다, SPEC §7). SDK 재시도 0.
    """
    made: dict[str, Any] = {"loop": None, "client": None}

    async def send(kwargs: dict[str, Any]) -> Any:
        loop = asyncio.get_running_loop()
        if made["loop"] is not loop:
            key = (os.environ.get("OPENAI_API_KEY") or "").strip()
            if not key:
                raise openai.OpenAIError("OPENAI_API_KEY가 없다")
            base_url = (os.environ.get("OPENAI_BASE_URL") or "").strip()
            if not base_url:
                raise openai.OpenAIError("OPENAI_BASE_URL이 없다")
            made["client"] = openai.AsyncOpenAI(api_key=key, base_url=base_url, max_retries=0)
            made["loop"] = loop
        return await made["client"].chat.completions.create(**kwargs)

    return send


def _own_gate(size: int) -> Gate:
    """가드에 LLM이 없을 때 튜터만 쓰는 게이트. 그때는 튜터만 LLM을 부르므로 합계 상한이 같다.

    세마포어는 이벤트 루프마다 하나다(_openai_send와 같은 방식). 경합한 세마포어는 그 루프에
    묶여, 측정 도구가 asyncio.run을 여러 번 쓰면 다음 루프에서 RuntimeError가 나기 때문이다.
    """
    made: dict[str, Any] = {"loop": None, "sem": None}

    def gate() -> asyncio.Semaphore:
        loop = asyncio.get_running_loop()
        if made["loop"] is not loop:
            made["sem"] = asyncio.Semaphore(size)
            made["loop"] = loop
        return made["sem"]

    return gate


def build_llm(app_state: Any, cfg: TutorConfig) -> TutorLLM:
    """서버에서는 가드 LLM의 게이트·probe 결과를 같이 쓴다(TU-13, 가드 D4).

    가드에 LLM이 없으면(루트 앱의 상수 판정기, 프로세스 안 측정) 가드 설정의 파라미터 값을
    쓰고, 가드 게이트와 같은 크기(llm.gate)의 튜터 게이트를 둔다(#126 리뷰: 없으면 동시 상한이
    없다). 튜터 설정의 reasoning_effort·max_completion_tokens를 그 위에 덮는다.
    """
    guard_llm = getattr(getattr(app_state, "guard", None), "llm", None)
    if guard_llm is not None:
        gate, base = guard_llm.slot, guard_llm
    else:
        guard_cfg = load_guard_config().llm
        gate = _own_gate(guard_cfg.gate)
        params = {k: getattr(guard_cfg, k) for k in OPTIONAL_PARAMS}
        base = SimpleNamespace(params=params, structured=guard_cfg.structured_output)
    overrides = {
        "reasoning_effort": cfg.llm.reasoning_effort,
        "max_completion_tokens": cfg.llm.max_completion_tokens,
    }
    model = (os.environ.get("OPENAI_MODEL") or "").strip()  # .env 줄 끝 공백
    llm = TutorLLM(_openai_send(), gate, model=model, base=base, overrides=overrides)
    md = cfg.llm.multi_draft
    llm.request_cap = md.max_calls if md.enabled else None
    return llm


API_ENV = ("OPENAI_API_KEY", "OPENAI_MODEL", "OPENAI_BASE_URL")  # 튜터 LLM이 부를 때 필요한 것


def missing_api_env() -> list[str]:
    """비었거나 공백뿐인 운영진 API 환경변수 이름. 하나라도 있으면 튜터 LLM은 보내지 못한다."""
    return [name for name in API_ENV if not (os.environ.get(name) or "").strip()]
