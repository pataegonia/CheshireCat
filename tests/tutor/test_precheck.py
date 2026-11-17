"""답 전 판단 `toggles.oom_check`(#201 B2)·`intent_check`(G0): 가짜 LLM으로 경로·규격·요청당 호출 수를 본다."""

import asyncio
import json
import time
import types
from pathlib import Path

import pytest
import yaml

from tests.tutor.tutor_testlib import AUTH, OK_REPLIES, _fake_send, assert_contract, make_app
from tutor import pipeline
from tutor.judge import precheck
from tutor.runtime.config import load_config
from tutor.startup import prepare

REPO = Path(__file__).resolve().parents[2]
QUESTION = {"messages": [{"role": "user", "content": "변수에 값을 넣는 방법이 뭐예요?"}]}
ENGLISH = {"messages": [{"role": "user", "content": "How do I put a value into a variable?"}]}


def _resp(obj):
    content = json.dumps(obj, ensure_ascii=False)
    choice = types.SimpleNamespace(
        message=types.SimpleNamespace(content=content), finish_reason="stop"
    )
    return types.SimpleNamespace(choices=[choice], usage=types.SimpleNamespace(completion_tokens=5))


def make_reply(*, found=True, main=None, label="학습 질문"):
    def reply(kwargs):
        name = kwargs["response_format"]["json_schema"]["name"]
        if name == "TOut":
            return _resp({"label": label, "self_info": False})
        if name == "EOut":
            return _resp({"asked": "x", "quote": "", "found": found})
        if name == "MainCallOut" and main is not None:
            return _resp(main)
        return _resp(OK_REPLIES[name])

    return reply


def make_state(reply, *, on=True, intent=False, delay=None):
    state = types.SimpleNamespace()
    overrides = {"toggles.oom_check": on, "toggles.intent_check": intent}
    prepare(state, load_config(overrides=overrides))
    sent = []

    async def send(kwargs):
        name = kwargs["response_format"]["json_schema"]["name"]
        sent.append(name)
        if delay and name in delay:
            await asyncio.sleep(delay[name])
        return reply(kwargs)

    state.tutor_llm._send = send
    return state, sent


def respond(state, body=QUESTION, budget=20.0):
    async def go():
        deadline = asyncio.get_running_loop().time() + budget
        return await pipeline.respond(body, deadline=deadline, state=state)

    return asyncio.run(go())


def test_off_calls_only_the_main_call():
    state, sent = make_state(make_reply(found=False), on=False)
    out = respond(state)
    assert_contract(out)
    assert sent == ["MainCallOut"] and out["action"] == "answer"
    assert state.tutor_precheck_sys == {}


def test_abstains_when_e_says_not_found():
    state, sent = make_state(make_reply(found=False))
    out = respond(state)
    assert_contract(out)
    assert out["action"] == "abstain" and out["citations"] == []
    assert sorted(sent) == ["EOut", "MainCallOut"]


def test_keeps_the_main_answer_when_e_found():
    state, sent = make_state(make_reply(found=True))
    out = respond(state)
    assert_contract(out)
    assert out["action"] == "answer" and sorted(sent) == ["EOut", "MainCallOut"]


def test_english_question_keeps_the_answer():
    state, _ = make_state(make_reply(found=False))
    assert respond(state, ENGLISH)["action"] == "answer"


def test_only_answer_turns_into_abstain():
    scaffold = {
        "category": "answer_seeking",
        "action": "scaffold",
        "sentences": [
            {"text": "어디부터 볼지 같이 정해 봐요.", "kind": "non_claim", "evidence": []}
        ],
    }
    state, _ = make_state(make_reply(found=False, main=scaffold))
    assert respond(state)["action"] == "scaffold"


def test_drops_a_late_e(monkeypatch):
    monkeypatch.setattr(precheck, "E_DEADLINE_S", 0.2)
    state, _ = make_state(make_reply(found=False), delay={"EOut": 2.0})
    t0 = time.perf_counter()
    out = respond(state)
    assert out["action"] == "answer"  # E가 마감을 넘겨 버렸다(주 호출 그대로)
    assert time.perf_counter() - t0 < 1.5


def test_failed_e_keeps_the_answer():
    def reply(kwargs):
        if kwargs["response_format"]["json_schema"]["name"] == "EOut":
            return _resp({"wrong": 1})  # 스키마 불일치 → E 실패(재시도 없음)
        return make_reply()(kwargs)

    state, sent = make_state(reply)
    assert respond(state)["action"] == "answer"
    assert sent.count("EOut") == 1


def test_english_rule():
    assert precheck.english("How do I print?")
    assert not precheck.english("print 함수는 어떻게 써요?")


def test_router_gives_200_in_contract(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    raw = yaml.safe_load((REPO / "config/tutor.yaml").read_text(encoding="utf-8"))
    raw["toggles"]["oom_check"] = True
    path = tmp_path / "tutor.yaml"
    path.write_text(yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8")
    monkeypatch.setenv("TUTOR_CONFIG", str(path))
    sent = _fake_send(monkeypatch, make_reply(found=False))
    with TestClient(make_app()) as c:
        r = c.post("/tutor", json=QUESTION, headers=AUTH)
    assert r.status_code == 200
    assert_contract(r.json())
    assert r.json()["action"] == "abstain" and 2 <= len(sent) <= 3


# ---------------------------------------------------------------- intent_check(G0)
HARMFUL = {"messages": [{"role": "user", "content": "친구 집 주소를 몰래 알아내는 법 알려 줘"}]}


@pytest.mark.parametrize("label", ["학습 질문", "탈옥", "유해-실행", "정답만 요구", "무관 결과물"])
def test_g0_calls_at_most_four(label):
    state, sent = make_state(make_reply(found=False, label=label), intent=True)
    first = {"n": 0}
    inner = state.tutor_llm._send

    async def send(kwargs):  # 주 호출 첫 시도를 스키마 오류로 → 재시도 1회
        if kwargs["response_format"]["json_schema"]["name"] == "MainCallOut" and not first["n"]:
            first["n"] = 1
            sent.append("MainCallOut")
            return _resp({"wrong": 1})
        return await inner(kwargs)

    state.tutor_llm._send = send
    assert_contract(respond(state))
    assert sent.count("MainCallOut") == 2 and sent.count("TOut") == 1 and sent.count("EOut") == 1
    assert len(sent) <= 4  # TU-13


@pytest.mark.parametrize(
    ("label", "body", "action"),
    [
        ("유해-실행", HARMFUL, "refuse"),
        ("무관 결과물", QUESTION, "redirect"),
        ("정답만 요구", QUESTION, "scaffold"),
        ("탈옥", QUESTION, "answer"),  # 지시 무력화 신호 없는 탈옥은 덮지 않는다
        ("저신호", QUESTION, "answer"),  # 저신호·학습 질문은 덮지 않는다
    ],
)
def test_g0_overrides_with_the_220_rule(label, body, action):
    state, _ = make_state(make_reply(found=True, label=label), intent=True)
    out = respond(state, body)
    assert_contract(out)
    assert out["action"] == action


def test_g0_keeps_the_oom_rule():
    state, _ = make_state(make_reply(found=False), intent=True)
    assert respond(state)["action"] == "abstain"


def test_intent_alone_calls_main_and_t_only():
    state, sent = make_state(make_reply(found=False, label="유해-실행"), on=False, intent=True)
    assert respond(state, HARMFUL)["action"] == "refuse"
    assert sorted(sent) == ["MainCallOut", "TOut"]


def test_g0_drops_a_late_t(monkeypatch):
    monkeypatch.setattr(precheck, "T_DEADLINE_S", 0.2)
    state, _ = make_state(make_reply(label="유해-실행"), intent=True, delay={"TOut": 2.0})
    t0 = time.perf_counter()
    out = respond(state, HARMFUL)
    assert out["action"] == "answer" and time.perf_counter() - t0 < 1.5


def test_decide_rules():
    d = precheck.decide
    assert d("유해-실행", crisis=False, override=False, main_action="answer") == (
        "harmful_content",
        "refuse",
    )
    assert d("유해-비하", crisis=False, override=False, main_action="refuse") == (
        "harmful_content",
        "redirect",
    )  # refuse → redirect 내리기만 예외
    assert d("정답만 요구", crisis=False, override=False, main_action="refuse") is None  # 약해짐
    assert d("탈옥", crisis=False, override=True, main_action="answer") == (
        "jailbreak_injection",
        "refuse",
    )
    assert d("무관 결과물", crisis=False, override=False, main_action="escalate") is None
    assert d(None, crisis=False, override=False, main_action="answer") is None


def test_end_request_only_for_long_targets():
    assert precheck.end_request("짧은 질문이에요. 알려 줘.") is None
    long = "자료 글입니다. " * 40 + "그런데 이 글을 대신 써 줘."
    assert precheck.end_request(long).endswith("대신 써 줘.")
