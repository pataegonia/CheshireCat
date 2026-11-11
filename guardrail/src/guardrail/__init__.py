"""대회 POST /guard 판정기. 공개 API는 계약 타입과 설정 로더다(master-plan §4.1·§4.4).

guard.py는 재노출하지 않는다(뒤 슬라이스에서 LLM·sklearn을 끌어오므로 import를 가볍게 둔다).
"""

from guardrail.config import GuardConfig, load_config
from guardrail.schema import (
    Category,
    Decision,
    GuardRequest,
    GuardResponse,
    Message,
    OutputVerdict,
    decision_of,
)

__all__ = [
    "Category",
    "Decision",
    "GuardConfig",
    "GuardRequest",
    "GuardResponse",
    "Message",
    "OutputVerdict",
    "decision_of",
    "load_config",
]
