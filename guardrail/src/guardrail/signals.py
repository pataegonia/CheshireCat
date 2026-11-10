"""결정적 신호 (GR-06, ADR-0014). S1.1은 타입만 둔다. 판정 함수는 S2.3."""

from dataclasses import dataclass


@dataclass(frozen=True)
class Signals:
    low_signal: bool
    refusal: bool
    hidden_tags: bool
