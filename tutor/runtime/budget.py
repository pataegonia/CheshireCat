"""요청 시간 예산(TU-13). 라우터와 handle이 같은 값을 쓴다."""

import math
from typing import Any

DEFAULT_BUDGET_S = 24.0  # 가드 설정의 budget_s를 못 읽었을 때. 가드 기본값과 같다
BACKSTOP_MARGIN_S = 3.0  # 요청 예산 위의 안전망
DEADLINE_CAP_S = 29.0  # 최악 응답(예산 + 안전망)의 전체 상한. 규격 30초보다 1초 아래


def _usable(value: Any) -> bool:
    return (
        isinstance(value, int | float)
        and not isinstance(value, bool)
        and math.isfinite(value)
        and value > 0
    )


def budget_s(app_state: Any) -> float:
    """요청 예산(초): 가드 cfg.budget_s(못 읽으면 기본) × 튜터 tutor_cfg.budget_scale(못 읽으면 1).

    가드 예산을 줄이면 튜터 예산도 준다. 숫자 아님·bool·0 이하·무한·NaN은 쓸 수 없는 값이다.
    예산 + 안전망이 DEADLINE_CAP_S를 넘지 않도록 자른다.
    """
    guard = getattr(getattr(app_state, "cfg", None), "budget_s", None)
    scale = getattr(getattr(app_state, "tutor_cfg", None), "budget_scale", None)
    base = float(guard) if _usable(guard) else DEFAULT_BUDGET_S
    return min(base * (float(scale) if _usable(scale) else 1.0), DEADLINE_CAP_S - BACKSTOP_MARGIN_S)


def backstop_s(budget: float) -> float:
    """요청 예산 위의 안전망 시간 상한(asyncio.timeout에 쓴다)."""
    return budget + BACKSTOP_MARGIN_S
