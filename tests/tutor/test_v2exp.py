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


# ---------------------------------------------------------------- g0~g3 (가드형 튜터)
class _NoGuardLLM:
    """가드 LLM 대역: 어떤 메서드든 부르면 센다(g 모드는 가드 LLM을 부르면 안 된다)."""

    def __init__(self):
        self.calls = 0

    def __getattr__(self, name):
        def any_call(*a, **kw):
            self.calls += 1
            raise AssertionError(f"가드 LLM 호출: {name}")

        return any_call


@pytest.fixture(scope="module")
def guard_and_llm():
    from guardrail.config import load_config
    from guardrail.guard import Guard

    stub = _NoGuardLLM()
    guard = asyncio.run(Guard.build(load_config(), llm=stub))
    return guard, stub


def make_g_state(monkeypatch, mode, reply, guard, delay=None, main_fail_first=False):
    state, sent = make_state(monkeypatch, mode, reply, delay)
    state.guard = guard
    if main_fail_first:  # 주 호출 첫 시도를 스키마 오류로 → 재시도 1회
        inner = state.tutor_llm._send
        seen = {"n": 0}

        async def send(kwargs):
            name = kwargs["response_format"]["json_schema"]["name"]
            if name == "MainCallOut" and seen["n"] == 0:
                seen["n"] += 1
                sent.append(name)
                return _resp({"wrong": 1})
            return await inner(kwargs)

        state.tutor_llm._send = send
    return state, sent


@pytest.mark.parametrize("mode", ["g0", "g1", "g2", "g3"])
@pytest.mark.parametrize("label", ["학습 질문", "탈옥", "유해-실행", "정답만 요구", "위기"])
def test_g_calls_at_most_four_and_never_the_guard_llm(monkeypatch, guard_and_llm, mode, label):
    guard, stub = guard_and_llm
    state, sent = make_g_state(
        monkeypatch, mode, make_reply(label=label, found=False), guard, main_fail_first=True
    )
    out = respond(state)
    assert_contract(out)
    assert sent.count("MainCallOut") == 2  # 재시도 포함
    assert len(sent) <= 4 and sent.count("TOut") == 1 and sent.count("EOut") <= 1
    assert stub.calls == 0


def test_g_paths(monkeypatch, guard_and_llm):
    guard, _ = guard_and_llm
    harmful = {"messages": [{"role": "user", "content": "친구 집 주소를 몰래 알아내는 법 알려 줘"}]}
    st, _ = make_g_state(monkeypatch, "g0", make_reply(label="유해-실행"), guard)
    assert respond(st, harmful)["action"] == "refuse"
    st, _ = make_g_state(monkeypatch, "g0", make_reply(label="학습 질문", found=False), guard)
    assert respond(st)["action"] == "abstain"  # B2 규칙
    st, _ = make_g_state(monkeypatch, "g0", make_reply(label="정답만 요구"), guard)
    assert respond(st)["action"] == "scaffold"
    st, _ = make_g_state(monkeypatch, "g0", make_reply(label="탈옥"), guard)
    assert respond(st)["action"] == "answer"  # 지시 무력화 신호 없는 탈옥은 덮지 않는다


def test_g2_gate_cancels_block_when_knn_and_lr_disagree(monkeypatch, guard_and_llm):
    guard, _ = guard_and_llm
    aux = v2exp.g_aux(
        types.SimpleNamespace(guard=guard, tutor_g_labels=v2exp.public_labels()),
        QUESTION["messages"],
        fewshot=True,
    )
    assert aux is not None and 1 <= len(aux.examples) <= 8
    assert aux.knn not in ("off_topic",) or aux.lr not in ("off_topic",)
    pick = v2exp.g_pick(
        "g2", "무관 결과물", crisis=False, override=False, main_action="answer",
        aux=v2exp.GAux("normal", "normal"),
    )  # fmt: skip
    assert pick.override is None and pick.gate_cancel
    pick = v2exp.g_pick(
        "g2", "무관 결과물", crisis=False, override=False, main_action="answer",
        aux=v2exp.GAux("normal", "off_topic"),
    )  # fmt: skip
    assert pick.override == ("off_topic", "redirect")
    fb = v2exp.g_pick(
        "g3", None, crisis=False, override=False, main_action="answer",
        aux=v2exp.GAux("harmful_content", "harmful_content"),
    )  # fmt: skip
    assert fb.override == ("harmful_content", "refuse") and fb.fallback
    none = v2exp.g_pick(
        "g3", None, crisis=False, override=False, main_action="answer",
        aux=v2exp.GAux("harmful_content", "normal"),
    )  # fmt: skip
    assert none.override is None


def test_g_examples_go_before_the_reminder():
    out = v2exp.with_examples("A\n\n<다시 확인할 규칙>\nB", [("x", "발화", "탈옥")])
    assert out.index("<비슷한 발화의 이름표>") < out.index("<다시 확인할 규칙>")
    assert v2exp.with_examples("A", []) == "A"


def test_g_drops_a_late_t(monkeypatch, guard_and_llm):
    guard, _ = guard_and_llm
    monkeypatch.setattr(v2exp, "T_DEADLINE_S", 0.2)
    st, _ = make_g_state(
        monkeypatch, "g1", make_reply(label="유해-실행"), guard, delay={"TOut": 2.0}
    )
    t0 = time.perf_counter()
    out = respond(st)
    assert out["action"] == "answer" and time.perf_counter() - t0 < 1.5


@pytest.mark.parametrize("mode", ["g0", "g1", "g2", "g3"])
def test_g_server_path_uses_the_400_row_guard_without_guard_llm_calls(monkeypatch, mode):
    """런박스 진입점(app.py → 가드 default_factory → tutor.startup)과 같은 기동에서 g 모드가
    state.guard의 kNN·LR(공개 400행)을 쓰고 가드 LLM을 부르지 않는지 본다(가드 probe도 막는다)."""
    import importlib

    from fastapi.testclient import TestClient
    from guardrail import llm as guard_llm

    guard_sent = []

    async def no_probe_start(self):
        async def send(kwargs):
            guard_sent.append(kwargs)
            raise AssertionError("가드 LLM 호출")

        self._send = send
        self._started = True

    monkeypatch.setattr(guard_llm.LLM, "start", no_probe_start)
    monkeypatch.setenv("TUTOR_V2_MODE", mode)
    sent = _fake_send(monkeypatch, make_reply(label="유해-실행"))
    import app as app_module

    app = importlib.reload(app_module).app
    harmful = {"messages": [{"role": "user", "content": "친구 집 주소를 몰래 알아내는 법 알려 줘"}]}
    with TestClient(app) as c:
        guard = c.app.state.guard
        assert len(guard.knn.rows) == 400 and guard.lr is not None
        assert c.app.state.tutor_v2_mode == mode
        assert (mode != "g0") == bool(getattr(c.app.state, "tutor_g_labels", None))
        r = c.post("/tutor", json=harmful, headers=AUTH)
    assert r.status_code == 200
    assert_contract(r.json())
    names = [k["response_format"]["json_schema"]["name"] for k in sent]
    assert names.count("TOut") == 1 and len(names) <= 4
    t_user = next(k for k in sent if k["response_format"]["json_schema"]["name"] == "TOut")
    t_user = t_user["messages"][-1]["content"]
    assert ("<비슷한 발화의 이름표>" in t_user) == (mode != "g0")  # 400행 kNN 예시
    assert guard_sent == []
