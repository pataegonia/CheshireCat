"""튜터 기동 훅(TU-01).

가드 create_app(startups=)가 lifespan에서 setup_guard 다음에 한 번 부른다.
"""

from typing import Any

from tutor.retrieval import default_index


async def startup(app_state: Any) -> None:
    """KB 색인을 기동 때 만든다(첫 요청에서 만들지 않는다, SPEC §3).

    KB를 못 읽으면 예외가 나서 서버가 뜨지 않는다. 예시 kNN·이름 사전도 뒤 이슈에서 여기서 만든다.
    """
    app_state.tutor_kb = default_index()
