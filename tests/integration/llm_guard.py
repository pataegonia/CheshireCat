"""실제 판정기의 대역: 가드의 진짜 LLM 게이트(`guardrail.llm.LLM`)로 nano를 부르는 최소 판정기.

가드의 진짜 판정기(`Guard.build`, #148부터 키가 있으면 기본)에는 아직 튜터가 맡기는 classify ·
judge_output이 없다(#119). 그 전까지 튜터→가드 호출(가짜 전송 · 호출 수 · 요청 내용이 남지 않음)을
이 대역으로 본다. 판정 품질과는 무관하고, 가드가 두 메서드를 올리고 test_real_guard.py가 그 경로를
덮은 뒤에 지운다.

- `judge`: 입력 판정 1회, 후보 답변이 있으면 출력 판정 1회 더(가드 PRD의 정상 경로와 같은 수).
- `classify` · `judge_output`: 튜터가 맡기는 판정(guardrail/docs/interface.md의 이름과 인자).
  메시지는 `Message`와 `{"role", "content"}` dict를 둘 다 받는다.
- `llm`: 튜터가 게이트와 probe 결과를 같이 쓰려고 읽는 속성(`tutor.runtime.llm.build_llm`).
"""

import time
from collections.abc import Sequence
from typing import Any

from guardrail.config import GuardConfig
from guardrail.guard import Judgement
from guardrail.judges.schemas import InputOut, OutputOut
from guardrail.llm import LLM
from guardrail.schema import Category, GuardRequest, GuardResponse, OutputVerdict
from guardrail.signals import Signals

SYSTEM_INPUT = "입력 판정 대역"
SYSTEM_OUTPUT = "출력 판정 대역"


def _text(message: Any) -> str:
    """`Message`든 dict든 `role: content` 한 줄로."""
    if isinstance(message, dict):
        return f"{message.get('role', '')}: {message.get('content', '')}"
    return f"{message.role}: {message.content}"


class LlmBackedGuard:
    def __init__(self, cfg: GuardConfig, llm: LLM) -> None:
        self.cfg = cfg
        self.llm = llm

    def _deadline(self) -> float:
        return time.monotonic() + self.cfg.budget_s

    async def classify(self, messages: Sequence[Any]) -> Category:
        prompt = [
            {"role": "system", "content": SYSTEM_INPUT},
            {"role": "user", "content": "\n".join(_text(m) for m in messages)},
        ]
        result = await self.llm.complete("input_judge", prompt, InputOut, deadline=self._deadline())
        return result.parsed.category if isinstance(result.parsed, InputOut) else "normal"

    async def judge_output(
        self, messages: Sequence[Any], candidate_answer: str, category: Category | None = None
    ) -> OutputVerdict:
        prompt = [
            {"role": "system", "content": SYSTEM_OUTPUT},
            {
                "role": "user",
                "content": "\n".join([*(_text(m) for m in messages), f"답변: {candidate_answer}"]),
            },
        ]
        result = await self.llm.complete(
            "output_judge", prompt, OutputOut, deadline=self._deadline()
        )
        grounded = result.parsed.grounded if isinstance(result.parsed, OutputOut) else True
        return OutputVerdict(grounded, (), True)

    async def judge(self, req: GuardRequest) -> Judgement:
        before = sum(self.llm.calls.values())
        category = await self.classify(req.messages)
        output = None
        if req.has_candidate:
            output = await self.judge_output(req.messages, req.candidate_answer or "", category)
        return Judgement(
            response=GuardResponse(category, output),
            input_source="nano",
            output_tier=3 if output is not None else None,
            nano_grounded=output.grounded if output is not None else None,
            signals=Signals(False, False, False),
            knn_major=None,
            lr_pred=None,
            calls=sum(self.llm.calls.values()) - before,
            errors=(),
            latency_ms={},
        )


async def llm_backed_factory(cfg: GuardConfig) -> LlmBackedGuard:
    """`create_app`의 guard_factory 자리에 넣는다. LLM은 서버 기본 경로로 만든다(send 주입 없음)."""
    llm = LLM(cfg.llm)
    await llm.start()
    return LlmBackedGuard(cfg, llm)
