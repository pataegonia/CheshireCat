"""S1.3 테스트 보조: FakeClock·FakeLLM (master-plan §4.3).

`LLM(cfg.llm, send=fake.send, clock=clock, sleep=clock.sleep)`로 꽂으면 0ms에 돈다.
스크립트의 각 단계는 호출 하나에 대응한다: Ok·Malformed(응답), Timeout·ConnectFail·
RateLimited·ServerError·BadRequest·HttpStatus·Boom(예외), Hang(영영 기다림).
"""

import asyncio
import copy
import json
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

import httpx2 as httpx
import openai

_REQ = httpx.Request("POST", "http://fake.invalid/v1/chat/completions")


@dataclass(frozen=True)
class Ok:
    content: str = '{"evidence": "e", "category": "normal"}'
    finish_reason: str | None = "stop"
    completion_tokens: int | None = 7


def Malformed() -> Ok:  # noqa: N802 - 단계 이름은 클래스처럼 쓴다
    return Ok(content="not json")


@dataclass(frozen=True)
class Timeout: ...


@dataclass(frozen=True)
class ConnectFail: ...


@dataclass(frozen=True)
class HttpStatus:
    code: int
    headers: dict[str, str] = field(default_factory=dict)
    message: str = "boom"


def RateLimited(retry_after: str | None = None, header: str = "retry-after") -> HttpStatus:  # noqa: N802
    return HttpStatus(429, {header: retry_after} if retry_after is not None else {})


def ServerError() -> HttpStatus:  # noqa: N802
    return HttpStatus(500)


def BadRequest(message: str = "bad") -> HttpStatus:  # noqa: N802
    return HttpStatus(400, message=message)


@dataclass(frozen=True)
class Boom:
    """분류 밖 예외(RuntimeError). 메시지는 로그·오류 문자열에 새면 안 된다."""

    message: str = "boom"


@dataclass(frozen=True)
class Hang: ...


Step = Ok | Timeout | ConnectFail | HttpStatus | Boom | Hang


class FakeClock:
    """monotonic 대용. sleep이 시간을 당기고 0ms에 돌아온다."""

    def __init__(self, now: float = 0.0) -> None:
        self.now = now
        self.slept: list[float] = []

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


class FakeLLM:
    """`send` 주입 지점용. 스크립트가 떨어지면 `respond(kwargs)`(없으면 AssertionError)."""

    def __init__(
        self, *steps: Step, respond: Callable[[dict[str, Any]], Step] | None = None
    ) -> None:
        self.steps = deque(steps)
        self.respond = respond
        self.requests: list[dict[str, Any]] = []

    async def send(self, kwargs: dict[str, Any]) -> Any:
        # 실제 전송처럼 UTF-8 JSON으로 직렬화한다(짝 없는 surrogate는 보내기 전에 실패)
        json.dumps(kwargs.get("messages"), ensure_ascii=False).encode("utf-8")
        self.requests.append(copy.deepcopy(kwargs))
        if self.steps:
            step = self.steps.popleft()
        elif self.respond is not None:
            step = self.respond(kwargs)
        else:
            raise AssertionError("FakeLLM 스크립트가 떨어졌다")
        return await self._play(step)

    async def _play(self, step: Step) -> Any:
        match step:
            case Ok():
                usage = (
                    None
                    if step.completion_tokens is None
                    else SimpleNamespace(completion_tokens=step.completion_tokens)
                )
                choice = SimpleNamespace(
                    message=SimpleNamespace(content=step.content),
                    finish_reason=step.finish_reason,
                )
                return SimpleNamespace(choices=[choice], usage=usage)
            case Timeout():
                raise openai.APITimeoutError(request=_REQ)
            case ConnectFail():
                raise openai.APIConnectionError(request=_REQ)
            case HttpStatus():
                resp = httpx.Response(step.code, headers=step.headers, request=_REQ)
                raise openai.APIStatusError(step.message, response=resp, body=None)
            case Boom():
                raise RuntimeError(step.message)
            case Hang():
                await asyncio.Event().wait()
        raise AssertionError(f"알 수 없는 단계: {step!r}")
