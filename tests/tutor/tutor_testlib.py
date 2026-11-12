"""튜터 테스트 공용: 저장소 경로, 채점기(scoring) import, 계약 검사, 통합과 같은 앱 조립."""

import json
import sys
from pathlib import Path

import jsonschema

REPO = Path(__file__).resolve().parents[2]
COMPETITION = REPO / "competition"
SCHEMA = json.loads((REPO / "tutor" / "contract" / "tutor.schema.json").read_text("utf-8"))
TOKEN = "test-token"  # noqa: S105  테스트용 토큰(실제 값 아님)
AUTH = {"Authorization": f"Bearer {TOKEN}"}

_GRADER = str(COMPETITION / "grader")
if _GRADER not in sys.path:
    sys.path.append(_GRADER)

import scoring  # noqa: E402  채점기의 parse_tutor를 그대로 쓴다(다시 쓰지 않는다)


def assert_contract(obj) -> None:
    """응답이 튜터 계약 스키마와 채점기 parse_tutor를 모두 통과한다(TU-01)."""
    jsonschema.validate(obj, SCHEMA)
    assert scoring.parse_tutor(obj) is not None


def make_app():
    """통합 app.py(IN-05)와 같은 조립: 가드 create_app에 튜터 라우터·기본값·startup을 넣는다."""
    from guardrail.server import constant_factory, create_app

    import tutor

    return create_app(
        constant_factory,
        tutor_router=tutor.router,
        defaults={"/tutor": tutor.TUTOR_DEFAULT},
        startups=[tutor.startup],
    )
