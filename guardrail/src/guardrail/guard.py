"""판정 결과 타입 (master-plan §4.1). S1.1은 타입만 둔다. 조율(Guard)은 S2.8.

서버는 Judgement.response만 직렬화한다. 나머지 필드는 하네스용이고 로그에 남기지 않는다.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

from guardrail.schema import Category, GuardResponse
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
