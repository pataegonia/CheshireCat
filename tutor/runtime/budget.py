"""요청 시간 예산(TU-13). 라우터와 handle이 같은 값을 쓴다."""

import math
from typing import Any

DEFAULT_BUDGET_S = 20.0  # 가드 설정의 budget_s를 못 읽었을 때. 가드 v6 기본값과 같다
BACKSTOP_MARGIN_S = 5.0  # 요청 예산 위의 안전망
DEADLINE_CAP_S = 27.0  # 설정과 상관없는 전체 상한(규격 30초, 가드 시작용 /tutor와 같은 값)


def budget_s(app_state: Any) -> float:
    """요청 예산(초): 가드가 읽은 app.state.cfg.budget_s, 쓸 수 없으면 기본 예산.

    숫자 아님·bool·0 이하·무한·NaN은 쓸 수 없는 값이다. 예산 + 안전망이 DEADLINE_CAP_S를
    넘지 않도록 자른다.
    """
    budget = getattr(getattr(app_state, "cfg", None), "budget_s", None)
    usable = (
        isinstance(budget, int | float)
        and not isinstance(budget, bool)
        and math.isfinite(budget)
        and budget > 0
    )
    chosen = float(budget) if usable else DEFAULT_BUDGET_S
    return min(chosen, DEADLINE_CAP_S - BACKSTOP_MARGIN_S)


def backstop_s(budget: float) -> float:
    """요청 예산 위의 안전망 시간 상한(asyncio.timeout에 쓴다)."""
    return budget + BACKSTOP_MARGIN_S
