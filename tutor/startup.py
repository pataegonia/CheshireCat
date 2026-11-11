"""튜터 기동 훅(TU-01).

가드 create_app(startups=)가 lifespan에서 setup_guard 다음에 한 번 부른다.
"""

from typing import Any


async def startup(app_state: Any) -> None:
    """튜터 자체 색인(예시 kNN·이름 사전)을 만들 자리다. 지금은 할 일이 없다."""
