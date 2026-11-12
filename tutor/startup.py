"""튜터 기동 훅(TU-01).

가드 create_app(startups=)가 lifespan에서 setup_guard 다음에 한 번 부른다.
"""

from typing import Any

from tutor.judge.flow import prepare_judge
from tutor.retrieval import default_index
from tutor.runtime.config import TutorConfig, load_config
from tutor.runtime.llm import build_llm


def prepare(app_state: Any, cfg: TutorConfig | None = None) -> None:
    """기동 때 만드는 것을 app_state에 둔다: 설정(TU-14)·KB 색인(TU-03)·LLM 호출기(TU-13).

    서버는 startup이 app.state에, 프로세스 안 호출(handle)은 기본 state에 한 번 만든다.
    cfg를 주면 그 설정을 쓴다(로컬 측정이 설정을 바꿔 부를 때).
    """
    app_state.tutor_cfg = cfg if cfg is not None else load_config()
    app_state.tutor_kb = default_index()
    app_state.tutor_llm = build_llm(app_state, app_state.tutor_cfg)
    prepare_judge(app_state)  # 주 호출 프롬프트(범주 → action 표 포함, #72)


async def startup(app_state: Any) -> None:
    """설정·KB 색인·LLM 호출기를 기동 때 만든다(첫 요청에서 만들지 않는다, SPEC §3).

    설정·KB를 못 읽으면 예외가 나서 서버가 뜨지 않는다. LLM 호출기는 가드 게이트를 같이 쓴다.
    예시 kNN·이름 사전도 뒤 이슈에서 여기서 만든다.
    """
    prepare(app_state)
