"""후보 T5·T5-H2·T5-R0(`llm.multi_draft` 초안 5개, `max_calls`, `gate_hint: last2`).
LLM은 가짜다(API 0회)."""

import asyncio
from collections import Counter

import pytest

from tests.tutor.test_gate_hint import ABSENT, FOUND, N1, RoleLLM, run
from tests.tutor.test_multi_draft import ABSTAIN_OUT, GOOD_OUT, SECTIONS
from tests.tutor.test_tutor_llm import call, make, ok
from tutor.judge import flow, gate_hint, multi_draft
from tutor.runtime import config
from tutor.runtime import llm as tutor_llm

FIVE = [{"effort": "low"}] * 5
T5 = {**N1, "llm.multi_draft.drafts": FIVE, "llm.multi_draft.max_calls": 9}
ROLES = ["main", "main_b", "main_c", "main_d", "main_e"]


def plan(*outs, gate=(0.05, FOUND), delays=None):
    delays = delays or [0.0] * len(outs)
    steps = {role: (d, o) for role, d, o in zip(ROLES, delays, outs, strict=True)}
    return RoleLLM({"oom_gate": gate, **steps})


def hint_in(llm, role):
    return "<검사기 단서>" in llm.call(role)["messages"][-1]["content"]


# ---- 설정 ----

ROOT = config.ROOT / "config"


def _cfg(name):
    return config.load_config(ROOT / f"tutor_{name}.yaml")


def test_candidates_differ_from_r_only_where_named():
    r = _cfg("n1").model_dump()
    for name, extra in (
        ("t5", {}),
        ("t5_h2", {"gate_hint": "last2"}),
        ("t5_r0", {"top1_redraft": "off", "max_calls": 8}),
    ):
        got = _cfg(name).model_dump()
        md = got["llm"]["multi_draft"]
        assert [d["effort"] for d in md["drafts"]] == ["low"] * 5
        expect = {"max_calls": 9, **extra}
        for key, value in expect.items():
            assert md[key] == value
        md["drafts"] = md["drafts"][:3]
        for key in ("gate_hint", "top1_redraft", "max_calls"):
            md[key] = r["llm"]["multi_draft"][key]
        assert got == r  # 나머지(T·E·예산·top1·인용 규칙…)는 R 그대로


def test_worst_calls_per_candidate_and_r_keeps_its_cap():
    r = _cfg("n1")
    assert r.llm.multi_draft.max_calls == config.MULTI_DRAFT_MAX_CALLS == 8
    assert r.worst_calls() == 7
    assert _cfg("t5").worst_calls() == _cfg("t5_h2").worst_calls() == 9
    assert _cfg("t5_r0").worst_calls() == 8
    for name in ("n1", "t5", "t5_h2", "t5_r0"):
        cfg = _cfg(name)
        assert cfg.worst_calls() <= cfg.llm.multi_draft.max_calls


def test_five_drafts_need_the_raised_cap_and_the_cap_has_a_ceiling():
    t5 = ROOT / "tutor_t5.yaml"
    with pytest.raises(ValueError, match="nano 9회"):  # 기본 상한 8로는 T5를 못 쓴다
        config.load_config(t5, overrides={"llm.multi_draft.max_calls": 8})
    with pytest.raises(ValueError):
        config.load_config(t5, overrides={"llm.multi_draft.max_calls": 10})


def test_last2_needs_at_least_four_drafts():
    with pytest.raises(ValueError, match="last2"):
        config.load_config(overrides={**N1, "llm.multi_draft.gate_hint": "last2"})


def test_hinted_drafts():
    assert flow.hinted_drafts(3, "off") == frozenset()
    assert flow.hinted_drafts(3, "last") == {2}
    assert flow.hinted_drafts(5, "last") == {4}
    assert flow.hinted_drafts(5, "last2") == {3, 4}


# ---- 흐름 ----


def test_t5_sends_five_drafts_and_only_the_last_gets_the_hint():
    llm = plan(*[GOOD_OUT] * 5)
    body, shadow = run(llm, **T5)
    assert body["action"] == "answer"
    roles = [c["role"] for c in llm.calls]
    assert sorted(roles) == sorted([*ROLES, "oom_gate"])  # E는 한 번
    assert [hint_in(llm, r) for r in ROLES] == [False] * 4 + [True]
    assert [llm.call(r)["retries"] for r in ROLES] == [flow.MAIN_RETRIES, 0, 0, 0, 0]
    assert shadow.started == 5 and shadow.valid == 5 and shadow.hints == [gate_hint.USED]
    log = shadow.log_field()
    assert "md_n=5" in log and "md_ok3=3" in log and "md_p3_same=1" in log
    assert "md_hint2" not in log


def test_t5_h2_hints_the_last_two_from_one_gate_call():
    llm = plan(*[GOOD_OUT] * 5)
    body, shadow = run(llm, **{**T5, "llm.multi_draft.gate_hint": "last2"})
    assert [c["role"] for c in llm.calls].count("oom_gate") == 1  # E 추가 호출 없음
    assert [hint_in(llm, r) for r in ROLES] == [False, False, False, True, True]
    assert shadow.hints == [gate_hint.USED, gate_hint.USED]
    assert "md_hint=used" in shadow.log_field() and "md_hint2=used" in shadow.log_field()


def test_t5_h2_absent_gate_sends_plain_drafts_and_still_abstains():
    llm = plan(*[GOOD_OUT] * 5, gate=(0.05, ABSENT))
    body, shadow = run(llm, **{**T5, "llm.multi_draft.gate_hint": "last2"})
    assert body["action"] == "abstain"
    assert shadow.hints == [gate_hint.ABSENT] * 2
    assert llm.call("main_e")["messages"] == llm.call("main")["messages"]


def test_completion_order_does_not_change_the_pick():
    outs = [GOOD_OUT, ABSTAIN_OUT, ABSTAIN_OUT, GOOD_OUT, GOOD_OUT]
    fast_first = plan(*outs, gate=(0.0, None), delays=[0.0, 0.01, 0.02, 0.03, 0.04])
    slow_first = plan(*outs, gate=(0.0, None), delays=[0.04, 0.03, 0.02, 0.01, 0.0])
    over = {**T5, "llm.multi_draft.gate_hint": "off", "toggles.oom_gate": "off"}
    over["toggles.english_embedding"] = False
    a, sa = run(fast_first, **over)
    b, sb = run(slow_first, **over)
    assert a == b
    assert sa.chosen == sb.chosen == 0 and sa.actions == sb.actions
    # 3표(answer 1 · abstain 2)와 5표(answer 3 · abstain 2)가 갈린다: 측정 칸에 남는다
    assert sa.pick3_action == "abstain" and sa.chosen_action == "answer"
    assert "md_p3_same=0" in sa.log_field()


def test_select_is_index_ordered_on_ties():
    outs = [ABSTAIN_OUT, GOOD_OUT, GOOD_OUT, ABSTAIN_OUT, None]
    efforts = ["low"] * 5
    assert multi_draft.select(outs, efforts, SECTIONS) == 0  # 2대2 동률은 초안 A의 action
    assert multi_draft.select([None, *outs[1:]], efforts, SECTIONS) == 1


def test_failed_and_late_drafts_are_not_votes():
    outs = [GOOD_OUT, None, None, ABSTAIN_OUT, None]  # 실패 3: 유효 2(answer 1 · abstain 1)
    llm = plan(*outs, gate=(0.0, None))
    over = {**T5, "llm.multi_draft.gate_hint": "off", "toggles.oom_gate": "off"}
    over["toggles.english_embedding"] = False
    body, shadow = run(llm, **over)
    assert shadow.valid == 2 and shadow.chosen == 0 and body["action"] == "answer"
    assert shadow.actions == ["answer", None, None, "abstain", None]


def test_all_drafts_failing_still_returns_a_contract_response():
    llm = plan(*[None] * 5, gate=(0.0, None))
    over = {**T5, "llm.multi_draft.gate_hint": "off", "toggles.oom_gate": "off"}
    over["toggles.english_embedding"] = False
    body, shadow = run(llm, **over)
    assert shadow.valid == 0 and shadow.chosen is None
    assert body["action"] in {"answer", "clarify", "abstain", "scaffold"}


# ---- 요청 상한(보내기 직전 예약) ----


def test_request_cap_blocks_attempts_past_the_cap_including_retries():
    class Send:
        def __init__(self):
            self.n = 0

        async def __call__(self, kwargs):
            self.n += 1
            return ok(content="not json")  # 실패라 재시도한다

    send = Send()
    llm, clock = make(send)
    llm.request_cap = 3
    sent: Counter[str] = Counter()

    async def go():
        token = tutor_llm.SENT.set(sent)
        try:
            first = await call(llm, clock, retries=1)  # 2회(재시도 포함)
            second = await call(llm, clock, retries=1)  # 1회 보내고 재시도는 막힌다
            third = await call(llm, clock, retries=0)  # 보내지 않는다
            return first, second, third
        finally:
            tutor_llm.SENT.reset(token)

    _first, _second, third = asyncio.run(go())
    assert send.n == 3 and sum(sent.values()) == 3
    assert third.error == "budget"


def test_request_cap_is_off_without_a_request_counter_or_cap():
    send_n = []

    async def send(kwargs):
        send_n.append(1)
        return ok()

    llm, clock = make(send)
    llm.request_cap = 1
    asyncio.run(call(llm, clock))
    asyncio.run(call(llm, clock))  # SENT가 없으면 요청 단위로 막지 않는다
    assert len(send_n) == 2


def test_build_llm_sets_the_cap_from_the_config(monkeypatch):
    monkeypatch.setenv("OPENAI_MODEL", "m")
    state = type("S", (), {})()
    t5 = _cfg("t5")
    assert tutor_llm.build_llm(state, t5).request_cap == 9
    off = config.load_config(overrides={"llm.multi_draft.enabled": False})
    assert tutor_llm.build_llm(state, off).request_cap is None
