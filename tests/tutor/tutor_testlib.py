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


FAKE_OPENAI_ENV = {
    "OPENAI_API_KEY": "k",
    "OPENAI_MODEL": "m",
    "OPENAI_BASE_URL": "http://127.0.0.1:9",  # 로컬 9번 포트: 전송을 못 바꿔도 밖으로 나가지 않는다
}


def _fake_send(monkeypatch, reply) -> list[dict]:
    """가짜 환경변수를 넣고 튜터 LLM 전송을 reply(kwargs)로 바꾼다. 보내려던 요청을 차례로 돌려준다."""
    from tutor.runtime import llm as llm_mod

    for name, value in FAKE_OPENAI_ENV.items():
        monkeypatch.setenv(name, value)
    sent: list[dict] = []

    def make_send():
        async def send(kwargs):
            sent.append(kwargs)
            return reply(kwargs)

        return send

    monkeypatch.setattr(llm_mod, "_openai_send", make_send)
    return sent


def block_openai(monkeypatch) -> list[dict]:
    """운영진 API 환경변수 자리에 가짜 값을 넣고, 튜터 LLM 전송은 막는다(보내지 않고 auth 오류).

    run_local의 tutor 프리셋은 두 모드 모두 이 환경변수를 요구한다(#125). 셸에 진짜 키가 있어도
    테스트가 운영진 API를 부르지 않는다. 보내려던 요청(kwargs)을 차례로 돌려준다.
    """
    import openai

    def refuse(_kwargs):
        raise openai.OpenAIError("막은 전송")  # auth 오류: 다시 보내지 않는다

    return _fake_send(monkeypatch, refuse)


def rate_limit_openai(monkeypatch) -> list[dict]:
    """block_openai와 같되, 보내지 않고 429(속도 제한)로 답한다. 튜터 LLM은 다시 보낸다."""
    import httpx
    import openai

    def limited(_kwargs):
        request = httpx.Request("POST", FAKE_OPENAI_ENV["OPENAI_BASE_URL"])
        raise openai.RateLimitError("x", response=httpx.Response(429, request=request), body=None)

    return _fake_send(monkeypatch, limited)


# 응답 스키마 이름별 최소 정상 답(주 호출 MainCallOut, 베이스라인 BaselineOut, T IntentOut).
# 과목 사실은 없다. T는 학습 질문이라 흐름이 주 호출까지 간다
OK_REPLIES = {
    "IntentOut": {"label": "학습 질문", "self_info": False},
    "MainCallOut": {
        "category": "normal",
        "action": "answer",
        "sentences": [{"text": "자료를 바탕으로 답해요.", "kind": "non_claim", "evidence": []}],
    },
    "CheckedMainCallOut": {
        "asked_in_evidence": True,
        "category": "normal",
        "action": "answer",
        "sentences": [{"text": "자료를 바탕으로 답해요.", "kind": "non_claim", "evidence": []}],
    },
    "BaselineOut": {"answer": "자료를 바탕으로 답해요.", "action": "answer", "citations": []},
}


def _ok_reply(kwargs):
    """응답 스키마 이름에 맞는 최소 정상 JSON 응답(json_schema 모드만)."""
    import types

    name = kwargs["response_format"]["json_schema"]["name"]
    content = json.dumps(OK_REPLIES[name], ensure_ascii=False)
    choice = types.SimpleNamespace(
        message=types.SimpleNamespace(content=content), finish_reason="stop"
    )
    return types.SimpleNamespace(choices=[choice], usage=types.SimpleNamespace(completion_tokens=5))


def answer_openai(monkeypatch) -> list[dict]:
    """block_openai와 같되, 보내지 않고 응답 스키마 이름에 맞는 최소 정상 JSON으로 답한다.

    json_schema 모드(가드 설정 기본, structured_output)만 다룬다. json_object 모드면 스키마
    이름이 없어 전송 안에서 KeyError가 나고, 튜터 LLM이 그것을 server 오류로 센다.
    """
    return _fake_send(monkeypatch, _ok_reply)


def scattered_rate_limit_openai(monkeypatch, every: int) -> list[int]:
    """answer_openai와 같되, every번째 전송마다 429로 답한다. 429를 낸 전송 번호를 돌려준다."""
    import httpx
    import openai

    limited: list[int] = []
    count = iter(range(1, 10**9))

    def reply(kwargs):
        n = next(count)
        if n % every == 0:
            limited.append(n)
            request = httpx.Request("POST", FAKE_OPENAI_ENV["OPENAI_BASE_URL"])
            raise openai.RateLimitError(
                "x", response=httpx.Response(429, request=request), body=None
            )
        return _ok_reply(kwargs)

    _fake_send(monkeypatch, reply)
    return limited
