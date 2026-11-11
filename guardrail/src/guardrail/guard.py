"""판정 결과 타입과 상수 판정기 (master-plan §4.1). 조율(Guard)은 S2.8.

서버는 Judgement.response만 직렬화한다. 나머지 필드는 하네스용이고 로그에 남기지 않는다.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

from guardrail.schema import Category, GuardRequest, GuardResponse, OutputVerdict
from guardrail.signals import Signals


@dataclass(frozen=True)
class Judgement:
    response: GuardResponse
    input_source: Literal["low_signal", "nano", "third_call", "fallback", "default"]
    output_tier: Literal[1, 2, 3, 4] | None  # GR-07 표의 ①–④
    nano_grounded: bool | None  # 출력 nano 원값
    signals: Signals
    knn_major: Category | None
    lr_pred: Category | None
    calls: int
    errors: tuple[str, ...]  # 오류 코드만
    latency_ms: Mapping[str, float]  # 단계별


class ConstantGuard:
    """상수 판정(S1.2–S1.5 기준선): allow·normal, 후보 답변이 있으면 true/[]/safe=true."""

    async def judge(self, req: GuardRequest) -> Judgement:
        output = OutputVerdict(True, (), True) if req.has_candidate else None
        return Judgement(
            response=GuardResponse("normal", output),
            input_source="default",
            output_tier=None,
            nano_grounded=None,
            signals=Signals(False, False, False),
            knn_major=None,
            lr_pred=None,
            calls=0,
            errors=(),
            latency_ms={},
        )
