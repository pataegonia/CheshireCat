"""`--fake`용 고정 응답 LLM: 입력은 `normal`, 출력은 grounded(API 0회).

런타임 코드라 tests/를 import하지 않는다. `LLM(send=...)` 주입 지점에 꽂아 재시도·게이트·probe까지
진짜 `LLM` 경로를 그대로 탄다.
"""

import json
from types import SimpleNamespace
from typing import Any

from guardrail.config import Llm
from guardrail.llm import LLM

INPUT_REPLY = {"evidence": "fake", "category": "normal"}
MODE_REPLY = {"evidence": "fake", "mode": "none", "category": "normal"}
ACT_REPLY = {"request": "fake", "act": "learning_question"}
OUTPUT_REPLY = {"answer_quote": "", "kb_quote": "", "grounded": True, "refuses": False}
BASELINE_REPLY = {"reason": "fake", "grounded": True, "safe": True}


def _reply(kwargs: dict[str, Any]) -> dict[str, Any]:
    # json_schema 모드는 스키마 이름이, json_object 모드는 시스템 메모의 스키마 title이 단서다
    msgs = kwargs.get("messages") or [{}]
    system = msgs[0].get("content", "") if msgs[0].get("role") == "system" else ""
    probe = json.dumps([kwargs.get("response_format"), system], ensure_ascii=False)
    if "InputActOut" in probe:
        return ACT_REPLY
    if "InputModeOut" in probe:
        return MODE_REPLY
    if "OutputBaselineOut" in probe:
        return BASELINE_REPLY
    if "OutputOut" in probe:
        return OUTPUT_REPLY
    return INPUT_REPLY


async def _send(kwargs: dict[str, Any]) -> Any:
    choice = SimpleNamespace(
        message=SimpleNamespace(content=json.dumps(_reply(kwargs))), finish_reason="stop"
    )
    return SimpleNamespace(choices=[choice], usage=SimpleNamespace(completion_tokens=1))


def make_fake_llm(cfg: Llm) -> LLM:
    return LLM(cfg, send=_send)
