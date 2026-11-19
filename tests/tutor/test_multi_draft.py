"""주 호출 여러 초안 + 고르기(`llm.multi_draft`, tutor.judge.multi_draft). LLM은 가짜다(API 0회).

고르기 규칙(결정적, 모델 호출 없음):
- action = 유효한 초안의 다수결. 동률이면 medium 초안의 action(유효하고 동률 안에 있으면),
  아니면 초안 A.
- 그 action을 낸 초안 가운데 근거가 확인된 claim 문장 비율 → 확인된 인용 수 → medium → A 순.
기다리기 규칙: 모두 끝나면 바로, 아니면 wait_s·(마감 − 여유)에서 유효한 초안이 있으면 남은 것을
취소한다. 유효한 초안이 없으면 하나가 유효해지거나 모두 끝나거나 요청 마감까지 기다린다.
"""

import asyncio

import pytest

from tests.tutor.test_flow import QUESTION, ask, state_with
from tutor.judge import flow, multi_draft
from tutor.judge.main_call import EvidenceOut, MainCallOut, SentenceOut
from tutor.retrieval import default_index
from tutor.runtime.config import load_config
from tutor.runtime.llm import Result

# PY-01-01 원문에 있는 문구 / 없는 문구
GOOD = EvidenceOut(sid="PY-01-01", quote="3회까지의 점수를 기록해요")
BAD = EvidenceOut(sid="PY-01-01", quote="원문에 없는 지어낸 문구입니다")
SECTIONS = [s for s in default_index().sections.values() if s.id == "PY-01-01"]
LOW, MED = "low", "medium"


def draft(action="answer", *evidence, category=None, claims=1):
    cat = category or {"answer": "normal", "abstain": "out_of_material"}.get(action, "normal")
    sents = [
        SentenceOut(text=f"문장 {i}", kind="claim", evidence=list(evidence)) for i in range(claims)
    ]
    return MainCallOut(category=cat, action=action, sentences=sents)


def pick(outs, efforts=None):
    efforts = efforts or [LOW, LOW, MED][: len(outs)]
    return multi_draft.select(outs, efforts, multi_draft.contract_sections(SECTIONS))


# --- 고르기(순수 함수) ---


def test_majority_action_wins():
    # answer 2표: A(확인 1/1)가 C(확인 0/1)를 이긴다
    assert pick([draft("answer", GOOD), draft("abstain"), draft("answer", BAD)]) == 0
    # A는 abstain 1표로 진다. B·C가 근거까지 같으면 medium(C)
    assert pick([draft("abstain"), draft("answer", GOOD), draft("answer", GOOD)]) == 2


def test_among_majority_the_best_verified_share_wins():
    # A·B 모두 answer: A는 근거 확인 0/1, B는 1/1 → B
    assert pick([draft("answer", BAD), draft("answer", GOOD), draft("abstain")]) == 1


def test_verified_share_tie_goes_to_more_verified_citations():
    two = EvidenceOut(sid="PY-01-01", quote="점수를 기록해요")
    assert pick([draft("answer", GOOD), draft("answer", GOOD, two), draft("abstain")]) == 1


def test_full_tie_prefers_medium_then_a():
    same = [draft("answer", GOOD), draft("answer", GOOD), draft("answer", GOOD)]
    assert pick(same) == 2  # medium
    assert pick(same, [LOW, LOW, LOW]) == 0  # medium 없음 → A


def test_action_tie_prefers_the_medium_action():
    outs = [draft("answer", GOOD), draft("abstain"), draft("clarify", category="low_signal_input")]
    assert pick(outs) == 2
    assert pick([draft("answer", GOOD), draft("abstain"), None]) == 0  # medium 무효 → A의 action


def test_action_tie_without_a_or_medium_takes_the_lowest_index():
    assert pick([None, draft("abstain"), draft("answer", GOOD)], [LOW, LOW, LOW]) == 1


def test_no_valid_draft_picks_nothing():
    assert pick([None, None, None]) is None


def test_single_valid_draft_is_picked():
    assert pick([None, None, draft("answer", BAD)]) == 2


def test_claimless_drafts_score_zero_share():
    # 같은 action이면 claim 없는 초안(비율 0)보다 확인된 claim이 있는 초안
    assert pick([draft("answer", claims=0), draft("answer", GOOD), None]) == 1


# --- 기다리기 ---


def result(error=None, parsed=None, finish="stop"):
    return Result(
        parsed=parsed,
        error=error,
        attempts=1,
        latency_ms=1.0,
        finish_reason=finish,
        completion_tokens=10,
    )


class ScriptLLM:
    """부른 순서대로 (지연, 결과)를 준다. 역할·effort·재시도·취소를 기록한다."""

    def __init__(self, *script):
        self.script = list(script)
        self.calls: list[dict] = []
        self.cancelled: list[str] = []

    async def complete(self, role, messages, schema, **kw):
        index = len(self.calls)
        self.calls.append({"role": role, "schema": schema, **kw})
        delay, res = self.script[index]
        if callable(res):
            res = res(schema)
        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            self.cancelled.append(role)
            raise
        return res


def ok(out):
    return lambda schema: result(parsed=schema.model_validate(out.model_dump()))


MD = {
    "llm.multi_draft.enabled": True,
    "llm.multi_draft.wait_s": 0.3,
    "llm.multi_draft.margin_s": 0.05,
    "llm.multi_draft.drafts": [
        {"effort": "low"},
        {"effort": "low"},
        {"effort": "medium", "timeout_s": 5},
    ],
}


def run_flow(llm, *, left=1e6, **overrides):
    state = state_with(llm, **{**MD, **overrides})
    holder = multi_draft.Shadow()

    async def go():
        token = multi_draft.SHADOW.set(holder)
        try:
            deadline = asyncio.get_running_loop().time() + left
            from tutor.request import parse_request

            req = parse_request({"messages": [{"role": "user", "content": QUESTION}]})
            body = await flow.answer_v2(req, state, deadline=deadline)
            await asyncio.sleep(0)
            return body
        finally:
            multi_draft.SHADOW.reset(token)

    started = asyncio.run(_timed(go))
    return started[0], started[1], holder


async def _timed(go):
    loop = asyncio.get_running_loop()
    t0 = loop.time()
    body = await go()
    return body, loop.time() - t0


GOOD_OUT = MainCallOut(
    category="normal",
    action="answer",
    sentences=[
        SentenceOut(
            text="야구 점수판 예제는 3회까지의 점수를 기록해요.", kind="claim", evidence=[GOOD]
        )
    ],
)
ABSTAIN_OUT = MainCallOut(category="out_of_material", action="abstain", sentences=[])


def test_all_drafts_valid_sends_three_calls_with_their_efforts_and_selects():
    llm = ScriptLLM((0.0, ok(GOOD_OUT)), (0.0, ok(ABSTAIN_OUT)), (0.01, ok(GOOD_OUT)))
    body, _took, shadow = run_flow(llm)
    assert [c["role"] for c in llm.calls] == ["main", "main_b", "main_c"]
    assert [c["effort"] for c in llm.calls] == ["low", "low", "medium"]
    assert [c["retries"] for c in llm.calls] == [flow.MAIN_RETRIES, 0, 0]
    assert llm.calls[2]["timeout_s"] == 5
    assert body["action"] == "answer" and body["citations"][0] == "PY-01-01"
    assert shadow.started == 3 and shadow.valid == 3 and shadow.split
    assert shadow.chosen == 2 and shadow.a_action == "answer" and shadow.chosen_action == "answer"


def test_disagreement_majority_overrides_draft_a():
    llm = ScriptLLM((0.0, ok(ABSTAIN_OUT)), (0.0, ok(GOOD_OUT)), (0.0, ok(GOOD_OUT)))
    body, _took, shadow = run_flow(llm)
    assert body["action"] == "answer"
    assert shadow.a_action == "abstain" and shadow.split and shadow.chosen in (1, 2)
    assert "md_a_same=0" in shadow.log_field()


def test_medium_timeout_still_answers_from_low_at_the_wait_cut():
    llm = ScriptLLM((0.0, ok(GOOD_OUT)), (0.0, ok(GOOD_OUT)), (10.0, ok(ABSTAIN_OUT)))
    body, took, shadow = run_flow(llm)
    assert body["action"] == "answer"
    assert took < 1.0  # wait_s 0.3에서 끊는다(medium 10초를 기다리지 않는다)
    assert llm.cancelled == ["main_c"]
    assert shadow.valid == 2 and shadow.chosen in (0, 1)
    assert [d.code for d in shadow.drafts][2] == "cut"


def test_all_but_one_fail_uses_the_survivor():
    llm = ScriptLLM(
        (0.0, result(error="timeout")),
        (0.0, result(error="bad_json", finish="length")),
        (0.0, ok(GOOD_OUT)),
    )
    body, _took, shadow = run_flow(llm)
    assert body["action"] == "answer" and shadow.valid == 1 and shadow.chosen == 2
    assert [d.code for d in shadow.drafts] == ["timeout", "length", "-"]


def test_no_valid_draft_at_the_cut_keeps_waiting_for_a_low_draft():
    # cut(0.3초)에 유효한 초안이 없으면 마지막 low 초안을 끝까지 기다린다
    llm = ScriptLLM(
        (0.0, result(error="timeout")), (0.6, ok(GOOD_OUT)), (0.0, result(error="server"))
    )
    body, took, shadow = run_flow(llm)
    assert body["action"] == "answer" and shadow.chosen == 1
    assert 0.5 < took < 2.0 and llm.cancelled == []


def test_all_fail_falls_to_the_tu12_default():
    llm = ScriptLLM(*[(0.0, result(error="timeout"))] * 3)
    body, _took, shadow = run_flow(llm)
    assert body["action"] == "clarify" and shadow.valid == 0 and shadow.chosen is None
    assert "md_pick" not in shadow.log_field()


def test_deadline_path_ends_by_the_request_deadline():
    # 요청 마감 0.4초(여유 0.05 → cut 0.35). A는 바로 실패, B·C는 마감보다 늦다. 가짜는 마감을
    # 모르므로 기다리기 규칙 자체가 요청 마감에서 끊어야 한다: 안전망(asyncio.timeout)이 먼저
    # 터지지 않고 TU-12 기본값으로 끝나며, 남은 초안은 모두 취소된다
    llm = ScriptLLM((0.0, result(error="timeout")), (5.0, ok(GOOD_OUT)), (5.0, ok(GOOD_OUT)))
    state = state_with(llm, **MD)
    from tutor.request import parse_request

    req = parse_request({"messages": [{"role": "user", "content": QUESTION}]})

    async def go():
        loop = asyncio.get_running_loop()
        t0 = loop.time()
        async with asyncio.timeout(0.4 + 3.0):  # 라우터 안전망(예산 + 3초)과 같은 구조
            body = await flow.answer_v2(req, state, deadline=t0 + 0.4)
        await asyncio.sleep(0)
        return body, loop.time() - t0

    body, took = asyncio.run(go())
    assert body["action"] == "clarify"  # 주 호출 실패 기본값(TU-12)
    assert 0.35 < took < 0.6
    assert sorted(llm.cancelled) == ["main_b", "main_c"]  # 남는 작업이 없다(TU-18)


def test_medium_draft_deadline_is_request_deadline_minus_margin():
    llm = ScriptLLM((0.0, ok(GOOD_OUT)), (0.0, ok(GOOD_OUT)), (0.0, ok(GOOD_OUT)))
    run_flow(llm, left=100.0)
    a, _b, c = (call["deadline"] for call in llm.calls)
    assert a - c == pytest.approx(0.05)


def test_hedge_is_not_used_with_multi_draft():
    llm = ScriptLLM((0.2, ok(GOOD_OUT)), (0.2, ok(GOOD_OUT)), (0.2, ok(GOOD_OUT)))
    run_flow(llm, **{"llm.timeout_s.main_hedge": 0.01})
    assert flow.MAIN_HEDGE_ROLE not in [c["role"] for c in llm.calls]
    assert len(llm.calls) == 3


def test_log_field_has_only_numbers_codes_and_names():
    llm = ScriptLLM((0.0, ok(GOOD_OUT)), (0.0, ok(ABSTAIN_OUT)), (10.0, ok(GOOD_OUT)))
    _body, _took, shadow = run_flow(llm)
    field = shadow.log_field()
    import re

    assert re.fullmatch(r"calls=\d+(;[a-z_]+=\d+(/\d+/(-|[a-z_]+)/\d+)?)*", field), field
    assert field.startswith("calls=") and ";md_n=3;md_ok=2;" in field
    assert ";low_a=" in field and ";low_b=" in field and ";med_c=" in field
    for word in ("answer", "abstain", "normal", "PY-"):
        assert word not in field


# --- 끔 = 지금과 같다 ---


class OneLLM:
    def __init__(self):
        self.calls = []

    async def complete(self, role, messages, schema, **kw):
        self.calls.append({"role": role, **kw})
        return result(parsed=schema.model_validate(GOOD_OUT.model_dump()))


def test_toggle_off_sends_one_main_call_without_effort_and_no_shadow():
    llm = OneLLM()
    state = state_with(llm)
    assert state.tutor_cfg.llm.multi_draft.enabled is False  # 테스트 기준 설정은 끔
    holder = multi_draft.Shadow()

    async def go():
        token = multi_draft.SHADOW.set(holder)
        try:
            from tutor.request import parse_request

            req = parse_request({"messages": [{"role": "user", "content": QUESTION}]})
            return await flow.answer_v2(req, state, deadline=1e12)
        finally:
            multi_draft.SHADOW.reset(token)

    body = asyncio.run(go())
    assert [c["role"] for c in llm.calls] == ["main"]
    assert "effort" not in llm.calls[0]
    assert holder.started == 0 and holder.log_field() is None
    assert body == ask(state_with(OneLLM()), QUESTION)


# --- 설정 검증 ---


def cfg(**over):
    return load_config(overrides={**MD, **over})


def test_config_accepts_the_branch_drafts():
    c = cfg()
    assert [d.effort for d in c.llm.multi_draft.drafts] == ["low", "low", "medium"]


@pytest.mark.parametrize(
    "drafts",
    [
        [{"effort": "medium"}, {"effort": "low"}],  # 초안 A는 low(오늘의 주 호출)
        [{"effort": "low"}, {"effort": "medium"}, {"effort": "medium"}],  # medium은 하나까지
        [{"effort": "low"}],  # 두 개 이상
        [{"effort": "low"}, {"effort": "minimal"}],  # minimal 금지(TU-13)
    ],
)
def test_config_rejects_bad_drafts(drafts):
    with pytest.raises(ValueError):
        cfg(**{"llm.multi_draft.drafts": drafts})


def test_global_medium_is_rejected_with_multi_draft():
    with pytest.raises(ValueError):
        cfg(**{"llm.reasoning_effort": "medium"})


def test_call_cap_is_six_with_multi_draft():
    # 주 호출 A 2(재시도) + B·C 2 + T 1 + E 1 = 6 (TU-13 4회를 넘는다: 이 브랜치만)
    cfg(**{"toggles.intent_override": "serial", "toggles.oom_gate": "all"})
    with pytest.raises(ValueError):
        cfg(
            **{
                "toggles.intent_override": "serial",
                "toggles.oom_gate": "all",
                "llm.multi_draft.drafts": [{"effort": "low"}] * 3 + [{"effort": "medium"}],
            }
        )


# --- 서버 경로: 접근 로그 끝 칸 · 로컬 측정 행별 결과 ---


def multi_on_config(tmp_path, monkeypatch):
    """테스트 기준 설정(conftest)에서 multi_draft만 켠 사본을 TUTOR_CONFIG로 넣는다."""
    import os
    from pathlib import Path

    import yaml

    from tutor.runtime import config as tutor_config

    raw = yaml.safe_load(Path(os.environ[tutor_config.CONFIG_ENV]).read_text(encoding="utf-8"))
    raw["llm"]["multi_draft"]["enabled"] = True
    path = tmp_path / "tutor-multi.yaml"
    path.write_text(yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8")
    monkeypatch.setenv(tutor_config.CONFIG_ENV, str(path))


def test_router_appends_the_draft_field_to_the_tutor_access_line(tmp_path, monkeypatch, caplog):
    import logging
    import re

    from fastapi.testclient import TestClient

    from tests.integration.test_app import ACCESS_LINE
    from tests.tutor import tutor_testlib

    multi_on_config(tmp_path, monkeypatch)
    sent = tutor_testlib.answer_openai(monkeypatch)
    caplog.set_level(logging.INFO, logger="guardrail.access")
    body = {"messages": [{"role": "user", "content": QUESTION}]}
    with TestClient(tutor_testlib.make_app()) as c:
        r = c.post("/tutor", json=body, headers={"Authorization": f"Bearer {tutor_testlib.TOKEN}"})
    assert r.status_code == 200 and r.json()["action"] == "answer"
    assert [k.get("reasoning_effort") for k in sent] == ["low", "low", "medium"]
    lines = [x.getMessage() for x in caplog.records if x.name == "guardrail.access"]
    line = next(x for x in lines if " /tutor " in x)
    assert ACCESS_LINE.fullmatch(line), line
    tail = line.rsplit(" ", 1)[1]
    assert re.fullmatch(
        r"calls=3;md_n=3;md_ok=3;md_split=0;md_pick=2;md_a_same=1"
        r";low_a=\d+/1/-/\d+;low_b=\d+/1/-/\d+;med_c=\d+/1/-/\d+",
        tail,
    ), tail
    from guardrail.eval.load import _fields

    assert _fields(line)[-6] == "/tutor" and _fields(line)[-1] == "-"  # 가드 로그 파서도 그대로


def test_router_line_has_no_field_when_multi_draft_is_off(monkeypatch, caplog):
    import logging

    from fastapi.testclient import TestClient

    from tests.tutor import tutor_testlib

    tutor_testlib.answer_openai(monkeypatch)
    caplog.set_level(logging.INFO, logger="guardrail.access")
    body = {"messages": [{"role": "user", "content": QUESTION}]}
    with TestClient(tutor_testlib.make_app()) as c:
        c.post("/tutor", json=body, headers={"Authorization": f"Bearer {tutor_testlib.TOKEN}"})
    line = next(
        x.getMessage()
        for x in caplog.records
        if x.name == "guardrail.access" and " /tutor " in x.getMessage()
    )
    assert "calls=" not in line and line.endswith(" -")


def test_run_local_rows_jsonl_has_ids_scores_and_draft_record(tmp_path, monkeypatch):
    import json

    from tests.tutor import tutor_testlib
    from tutor.eval import run_local

    tutor_testlib.answer_openai(monkeypatch)
    rows = tmp_path / "rows.jsonl"
    argv = ["--preset", "tutor", "--out", str(tmp_path), "--rows-jsonl", str(rows)]
    argv += ["--tutor-set", "llm.multi_draft.enabled=true"]
    assert run_local.main(argv) == 0
    recs = [json.loads(x) for x in rows.read_text(encoding="utf-8").splitlines()]
    assert recs and {r["run"] for r in recs} == {0}
    keys = {"id", "points", "action", "action_ok", "citation_f1", "facts", "faithful", "complied"}
    assert keys <= set(recs[0])
    drafted = [r for r in recs if r["md_n"]]
    assert drafted and all(r["md_n"] == 3 and r["md_ok"] == 3 for r in drafted)
    assert all(r["md_a_action"] == r["md_pick_action"] == "answer" for r in drafted)
    text = rows.read_text(encoding="utf-8")
    assert "자료를 바탕으로" not in text  # 답변 본문은 남기지 않는다
