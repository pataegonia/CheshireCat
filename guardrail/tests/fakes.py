"""테스트 전용 가짜 객체. 패키지 코드는 이 파일을 import하지 않는다.

S0.2: FakeTutor(정해진 청크·답을 돌려주는 TutorAdapter).
S0.3: FakeClock, FakeLLM(진짜 LLM에 가짜 send만 끼움)과 결과 타입 6개.
"""

import asyncio
from collections.abc import Callable
from dataclasses import dataclass

from guardrail.config import ROOT, GuardConfig, load_config
from guardrail.llm import LLM, Reply, TransportError
from guardrail.types import ROLES, Chunk, Role, Turn


class FakeTutor:
    """정해진 청크와 답을 돌려주고, 받은 인자를 기록한다."""

    def __init__(
        self,
        chunks: list[Chunk] | None = None,
        answer: str = "물은 0°C에서 언다. [S1]",
        system_prompt: str | None = "기본 지시",
    ) -> None:
        self.chunks = (
            chunks if chunks is not None else [Chunk("S1", "교재 1장", "물은 0°C에서 언다.")]
        )
        self.answer = answer
        self.system_prompt = system_prompt
        self.retrieve_calls: list[tuple[str, list[Turn]]] = []
        self.generate_calls: list[dict] = []

    async def retrieve(self, question: str, history: list[Turn]) -> list[Chunk]:
        self.retrieve_calls.append((question, history))
        return list(self.chunks)

    async def generate(
        self,
        question: str,
        history: list[Turn],
        chunks: list[Chunk],
        *,
        system_extra: str = "",
        instruction: str | None = None,
    ) -> str:
        self.generate_calls.append(
            {
                "question": question,
                "history": history,
                "chunks": chunks,
                "system_extra": system_extra,
                "instruction": instruction,
            }
        )
        return self.answer


# --- S0.3: FakeClock · FakeLLM ---


class FakeClock:
    """가짜 시계. sleep은 시각만 올리고 이벤트 루프에 한 번 양보한다(테스트가 0ms에 돈다)."""

    def __init__(self, start: float = 1000.0) -> None:
        self.t = start
        self.slept: list[float] = []

    def now(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds

    async def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.t += seconds
        await asyncio.sleep(0)


@dataclass(frozen=True)
class Ok:
    text: str
    tokens_in: int | None = 10
    tokens_out: int | None = 5
    finish_reason: str | None = None


@dataclass(frozen=True)
class Malformed:
    text: str = "JSON 아님"


@dataclass(frozen=True)
class Timeout:
    pass


@dataclass(frozen=True)
class RateLimited:
    retry_after: float | None = None


@dataclass(frozen=True)
class ServerError:
    status: int = 500


@dataclass(frozen=True)
class BadRequest:
    """json_schema 미지원 같은 400. 구조화 출력 단계에서는 한 단 내려간다."""

    status: int = 400


Outcome = Ok | Malformed | Timeout | RateLimited | ServerError | BadRequest


def fake_config(**overrides) -> GuardConfig:
    """역할마다 다른 가짜 모델명을 둔 무방비 설정. FakeLLM은 모델명으로 역할을 안다."""
    models = {f"models.{r}": f"fake-{r}" for r in ROLES}
    return load_config(ROOT / "configs" / "unguarded.yaml", overrides=models | overrides)


class FakeLLM(LLM):
    """진짜 LLM에 가짜 send만 끼운다. 게이트·재시도·예산·파싱은 그대로 돈다.

    script: 순서 목록 또는 역할별 목록(dict[Role, list]). responder(req): 반복 호출용.
    보낸 요청은 self.requests에 남는다(role·model·messages·response_format·max_tokens·timeout).
    """

    def __init__(
        self,
        cfg: GuardConfig | None = None,
        *,
        script: list[Outcome] | dict[Role, list[Outcome]] | None = None,
        responder: Callable[[dict], Outcome] | None = None,
        max_calls: int | None = None,
        clock: FakeClock | None = None,
        **kwargs,
    ) -> None:
        self.clock = clock or FakeClock()
        self.requests: list[dict] = []
        if isinstance(script, dict):
            self._script: dict[str | None, list[Outcome]] = {r: list(v) for r, v in script.items()}
        else:
            self._script = {None: list(script or [])}
        self._responder = responder
        kwargs.setdefault("rand", lambda: 0.0)
        super().__init__(
            cfg or fake_config(),
            send=self._fake_send,
            max_calls=max_calls,
            clock=self.clock.now,
            sleep=self.clock.sleep,
            **kwargs,
        )

    async def _fake_send(
        self, model, messages, *, response_format, temperature, max_tokens, timeout
    ):
        role = model.removeprefix("fake-") if isinstance(model, str) else None
        req = {
            "role": role,
            "model": model,
            "messages": messages,
            "response_format": response_format,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "timeout": timeout,
        }
        self.requests.append(req)
        await asyncio.sleep(0)
        if self._responder is not None:
            outcome = self._responder(req)
        else:
            queue = self._script.get(role) if role in self._script else self._script.get(None)
            if not queue:
                raise AssertionError(f"FakeLLM 스크립트가 비었다(role={role})")
            outcome = queue.pop(0)
        return _apply(outcome)


def _apply(outcome: Outcome) -> Reply:
    match outcome:
        case Ok(text, tin, tout, fin):
            return Reply(text, tin, tout, fin)
        case Malformed(text):
            return Reply(text, 10, 5, "stop")
        case Timeout():
            raise TransportError("timeout")
        case RateLimited(after):
            headers = {} if after is None else {"retry-after": str(after)}
            raise TransportError("status", 429, headers)
        case ServerError(status) | BadRequest(status):
            raise TransportError("status", status, {})
    raise AssertionError(f"모르는 결과 {outcome!r}")
