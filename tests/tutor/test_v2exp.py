"""실험 토글 TUTOR_V2_MODE(m·b2·c4): 가짜 LLM으로 경로·규격·요청당 호출 수를 본다."""

import asyncio
import json
import time
import types

import pytest

from tests.tutor.tutor_testlib import AUTH, OK_REPLIES, _fake_send, assert_contract, make_app
from tutor import pipeline
from tutor.judge import v2exp
from tutor.startup import prepare

QUESTION = {"messages": [{"role": "user", "content": "변수에 값을 넣는 방법이 뭐예요?"}]}


def _resp(obj):
    content = json.dumps(obj, ensure_ascii=False)
    choice = types.SimpleNamespace(
        message=types.SimpleNamespace(content=content), finish_reason="stop"
    )
    return types.SimpleNamespace(choices=[choice], usage=types.SimpleNamespace(completion_tokens=5))


def make_reply(*, label="학습 질문", found=True, w_ok=True):
    def reply(kwargs):
        name = kwargs["response_format"]["json_schema"]["name"]
        if name == "TOut":
            return _resp({"label": label, "self_info": False})
        if name == "EOut":
            return _resp({"asked": "x", "quote": "", "found": found})
        if name == "WOut":
            if not w_ok:
                return _resp({"wrong": 1})  # 스키마 불일치 → W 실패
            return _resp({"text": "자료에 따르면 이렇게 해요."})
        return _resp(OK_REPLIES[name])

    return reply


def make_state(monkeypatch, mode, reply, delay=None):
    monkeypatch.setenv("TUTOR_V2_MODE", mode)
    state = types.SimpleNamespace()
    prepare(state)
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


@pytest.mark.parametrize(
    ("value", "mode"), [(None, "m"), ("b2", "b2"), (" C4 ", "c4"), ("x", "m"), ("", "m")]
)
def test_read_mode(monkeypatch, value, mode):
    if value is None:
        monkeypatch.delenv("TUTOR_V2_MODE", raising=False)
    else:
        monkeypatch.setenv("TUTOR_V2_MODE", value)
    assert v2exp.read_mode() == mode


def test_m_calls_only_the_main_call(monkeypatch):
    state, sent = make_state(monkeypatch, "m", make_reply())
    assert_contract(respond(state))
    assert sent == ["MainCallOut"] and not hasattr(state, "tutor_v2_sys")


def test_b2_abstains_when_e_says_not_found(monkeypatch):
    state, sent = make_state(monkeypatch, "b2", make_reply(found=False))
    out = respond(state)
    assert_contract(out)
    assert out["action"] == "abstain" and out["citations"] == []
    assert sorted(sent) == ["EOut", "MainCallOut"]


def test_b2_keeps_the_main_answer_when_e_found(monkeypatch):
    state, sent = make_state(monkeypatch, "b2", make_reply(found=True))
    out = respond(state)
    assert_contract(out)
    assert out["action"] == "answer" and len(sent) <= 2


def test_b2_drops_a_late_e(monkeypatch):
    monkeypatch.setattr(v2exp, "E_DEADLINE_S", 0.2)
    state, _ = make_state(monkeypatch, "b2", make_reply(found=False), delay={"EOut": 2.0})
    t0 = time.perf_counter()
    out = respond(state)
    assert out["action"] == "answer"  # E가 마감을 넘겨 버렸다(주 호출 그대로)
    assert time.perf_counter() - t0 < 1.5


@pytest.mark.parametrize(
    ("label", "found", "action"),
    [
        ("학습 질문", True, "answer"),
        ("학습 질문", False, "abstain"),
        ("탈옥", False, "refuse"),
        ("유해-비하", True, "redirect"),
        ("정답만 요구", True, "scaffold"),
        ("저신호", True, "clarify"),
        ("위기", True, "escalate"),
    ],
)
def test_c4_paths(monkeypatch, label, found, action):
    state, sent = make_state(monkeypatch, "c4", make_reply(label=label, found=found))
    out = respond(state)
    assert_contract(out)
    assert out["action"] == action
    assert len(sent) <= 3 and "MainCallOut" not in sent


def test_c4_survives_w_failure_and_timeouts(monkeypatch):
    state, sent = make_state(monkeypatch, "c4", make_reply(w_ok=False))
    out = respond(state)
    assert_contract(out)
    assert out["action"] == "answer" and out["citations"]  # 자료 줄 인용 답
    monkeypatch.setattr(v2exp, "TE_DEADLINE_S", 0.2)
    state, _ = make_state(monkeypatch, "c4", make_reply(), delay={"TOut": 2.0, "EOut": 2.0})
    t0 = time.perf_counter()
    assert_contract(respond(state, budget=1.5))
    assert time.perf_counter() - t0 < 1.5


@pytest.mark.parametrize("mode", ["b2", "c4"])
def test_router_gives_200_in_contract(monkeypatch, mode):
    from fastapi.testclient import TestClient

    monkeypatch.setenv("TUTOR_V2_MODE", mode)
    sent = _fake_send(monkeypatch, make_reply())
    with TestClient(make_app()) as c:
        r = c.post("/tutor", json=QUESTION, headers=AUTH)
    assert r.status_code == 200
    assert_contract(r.json())
    assert 1 <= len(sent) <= 3
