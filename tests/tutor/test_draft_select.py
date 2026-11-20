"""두 초안 중 고르기 J(`toggles.draft_select`, `tutor.judge.draft_select`). LLM은 가짜다(API 0)."""

import asyncio

import pytest

from tests.tutor.test_flow import QUESTION, out, state_with
from tutor.judge import draft_select as ds
from tutor.judge import flow, oom_gate
from tutor.judge.conversation import tidy
from tutor.judge.main_call import EvidenceOut, MainCallPrompt, SentenceOut
from tutor.request import parse_request
from tutor.retrieval import default_index
from tutor.runtime import config
from tutor.runtime.llm import Result

ON = {"toggles.draft_select": True}
B_TEXT = "점수판 예제는 3회까지 점수를 적어요."


def out_b(category="normal", action="answer"):
    sentence = SentenceOut(
        text=B_TEXT,
        kind="claim",
        evidence=[EvidenceOut(sid="PY-01-01", quote="3회까지의 점수를 기록해요")],
    )
    return out(category=category, action=action, sentences=[sentence])


def result(parsed=None, error=None, finish="stop"):
    return Result(
        parsed=parsed,
        error=error,
        attempts=1,
        latency_ms=1.0,
        finish_reason=finish,
        completion_tokens=10,
    )


class DraftLLM:
    """역할마다 (지연, 결과)를 준다: main(A)·main_b(B)·select_draft(J). 부른 역할을 기록한다."""

    def __init__(self, a=None, b=None, pick="B", *, a_delay=0.0, b_delay=0.0, j_error=None):
        self.a, self.b, self.pick = a, b, pick
        self.a_delay, self.b_delay, self.j_error = a_delay, b_delay, j_error
        self.roles: list[str] = []
        self.kwargs: dict[str, dict] = {}
        self.cancelled: list[str] = []

    async def complete(self, role, messages, schema, **kw):
        self.roles.append(role)
        self.kwargs[role] = {"messages": messages, **kw}
        delay = {flow.MAIN_ROLE: self.a_delay, ds.ROLE_B: self.b_delay}.get(role, 0.0)
        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            self.cancelled.append(role)
            raise
        if role == flow.MAIN_ROLE:
            return result(self.a, None if self.a is not None else "server")
        if role == ds.ROLE_B:
            return result(self.b, None if self.b is not None else "server")
        if role == ds.ROLE_J:
            parsed = None if self.j_error else ds.SelectOut(better=self.pick)
            return result(parsed, self.j_error)
        return result(None, "server")


def ask(state, text=QUESTION, seconds=None):
    async def go():
        now = asyncio.get_running_loop().time()
        deadline = 1e12 if seconds is None else now + seconds
        body = {"messages": [{"role": "user", "content": text}]}
        return await flow.answer_v2(parse_request(body), state, deadline=deadline)

    return asyncio.run(go())


# --- 설정 ---


def test_toggle_is_off_in_the_repo_config_and_adds_two_calls():
    repo = config.load_config(config.ROOT / config.DEFAULT_CONFIG)
    assert repo.toggles.draft_select is False
    base = config.request_calls(repo)
    on = config.request_calls(config.load_config(overrides=ON))
    off = config.request_calls(config.load_config())
    assert on == (off[0] + 2, off[1] + 2)
    on_repo = config.load_config(config.ROOT / config.DEFAULT_CONFIG, overrides=ON)
    assert config.request_calls(on_repo) == (base[0] + 2, base[1] + 2) == (5, 7)
    assert config.request_calls(on_repo)[1] <= on_repo.llm.request_max_calls


def test_prompts_load_and_b_only_appends_to_the_main_prompt():
    main = MainCallPrompt.from_rules()
    addendum = ds.load_prompt(ds.B_PROMPT_PATH)
    b = ds.prompt_b(main, addendum)
    assert b.system.startswith(main.system) and b.system.endswith(addendum)
    assert b.schema is main.schema and b.version == main.version
    assert "<!--" not in addendum and "<!--" not in ds.load_prompt(ds.J_PROMPT_PATH)


# --- 고를 수 있는가 ---


@pytest.mark.parametrize(
    ("b", "reason"),
    [
        (out_b(), None),
        (out_b(action="scaffold"), "not_both_answer"),
        (out_b(category="pii_disclosure"), "category_differs"),
        (out(), "same_text"),
    ],
)
def test_only_two_different_answers_of_the_same_category_are_compared(b, reason):
    assert ds.not_comparable(out(), b) == reason


def test_selector_input_has_bundle_dialog_and_both_answers_in_order():
    conv = tidy([{"role": "user", "content": QUESTION}])
    sections = [default_index().section("PY-01-01")]
    messages = ds.build_messages("SYS", sections, conv, out(), out_b())
    user = messages[1]["content"]
    assert messages[0] == {"role": "system", "content": "SYS"}
    order = [user.index(t) for t in ("<근거 묶음>", "<대화>", "<답변 A>", "<답변 B>")]
    assert order == sorted(order)
    assert "[근거 PY-01-01: 3회까지의 점수를 기록해요]" in user and B_TEXT in user


# --- 두 초안 기다리기 ---


def test_late_b_is_cancelled_after_the_grace(monkeypatch):
    monkeypatch.setattr(ds, "B_WAIT_S", 0.05)
    cancelled = []

    async def a():
        return out()

    async def b():
        try:
            await asyncio.sleep(5)
        except asyncio.CancelledError:
            cancelled.append(True)
            raise
        return out_b()

    async def go():
        deadline = asyncio.get_running_loop().time() + 100
        got = await ds.two_drafts(a(), b(), deadline=deadline)
        await asyncio.sleep(0)
        return got

    assert asyncio.run(go()) == (out(), None)
    assert cancelled == [True]


def test_failed_a_waits_for_b_and_b_errors_count_as_missing():
    async def none():
        return None

    async def slow_b():
        await asyncio.sleep(0.05)
        return out_b()

    async def boom():
        raise RuntimeError("b")

    async def go():
        deadline = asyncio.get_running_loop().time() + 100
        rescued = await ds.two_drafts(none(), slow_b(), deadline=deadline)
        broken = await ds.two_drafts(_ok(), boom(), deadline=deadline)
        return rescued, broken

    async def _ok():
        return out()

    rescued, broken = asyncio.run(go())
    assert rescued == (None, out_b())
    assert broken == (out(), None)


# --- 흐름 ---


def test_off_never_calls_b_or_the_selector():
    llm = DraftLLM(a=out(), b=out_b())
    state = state_with(llm)
    assert ask(state)["action"] == "answer"
    assert llm.roles == [flow.MAIN_ROLE]


def test_selector_picks_b_and_the_answer_uses_b():
    llm = DraftLLM(a=out(), b=out_b(), pick="B")
    state = state_with(llm, **ON)
    body = ask(state)
    assert B_TEXT in body["answer"] and body["action"] == "answer"
    assert set(llm.roles) == {flow.MAIN_ROLE, ds.ROLE_B, ds.ROLE_J}
    assert llm.kwargs[ds.ROLE_B]["retries"] == 0  # B는 재시도하지 않는다
    assert llm.kwargs[ds.ROLE_J]["retries"] == 0
    b_system = llm.kwargs[ds.ROLE_B]["messages"][0]["content"]
    assert b_system.endswith(ds.load_prompt(ds.B_PROMPT_PATH))
    assert llm.kwargs[ds.ROLE_B]["messages"][1] == llm.kwargs[flow.MAIN_ROLE]["messages"][1]
    assert state.draft_select_counts == {"j_B": 1}


@pytest.mark.parametrize(("pick", "j_error", "key"), [("A", None, "j_A"), ("tie", None, "j_tie"),
                                                      ("B", "timeout", "j_failed")])  # fmt: skip
def test_tie_failure_or_a_keeps_a(pick, j_error, key):
    llm = DraftLLM(a=out(), b=out_b(), pick=pick, j_error=j_error)
    state = state_with(llm, **ON)
    body = ask(state)
    assert B_TEXT not in body["answer"]
    assert state.draft_select_counts == {key: 1}


def test_b_with_another_action_never_replaces_a():
    llm = DraftLLM(a=out(), b=out_b(action="refuse", category="harmful_content"))
    state = state_with(llm, **ON)
    assert ask(state)["action"] == "answer"
    assert ds.ROLE_J not in llm.roles
    assert state.draft_select_counts == {"not_both_answer": 1}


def test_failed_a_is_rescued_by_b():
    llm = DraftLLM(a=None, b=out_b())
    state = state_with(llm, **ON)
    body = ask(state)
    assert B_TEXT in body["answer"]
    assert state.draft_select_counts == {"b_rescue": 1}


def test_no_selector_when_too_little_time_is_left():
    llm = DraftLLM(a=out(), b=out_b())
    state = state_with(llm, **ON)
    body = ask(state, seconds=ds.J_MIN_LEFT_S - 1)
    assert B_TEXT not in body["answer"]
    assert ds.ROLE_J not in llm.roles
    assert (
        state.draft_select_counts.get("no_time", 0) + state.draft_select_counts.get("b_missing", 0)
        == 1
    )


def test_late_b_is_cancelled_and_a_is_used(monkeypatch):
    monkeypatch.setattr(ds, "B_WAIT_S", 0.05)
    llm = DraftLLM(a=out(), b=out_b(), b_delay=5)
    state = state_with(llm, **ON)
    body = ask(state)
    assert B_TEXT not in body["answer"]
    assert ds.ROLE_B in llm.cancelled
    assert state.draft_select_counts == {"b_missing": 1}


def test_e_still_turns_the_chosen_answer_into_abstain():
    # 고른 초안도 E(자료 밖 확인)를 그대로 지난다
    class WithGate(DraftLLM):
        async def complete(self, role, messages, schema, **kw):
            if role == oom_gate.ROLE:
                self.roles.append(role)
                return result(oom_gate.OomOut(asked="a", quote="", found=False))
            return await super().complete(role, messages, schema, **kw)

    llm = WithGate(a=out(), b=out_b())
    state = state_with(llm, **ON, **{"toggles.oom_gate": "all"})
    assert ask(state)["action"] == "abstain"


# --- 저장소 설정 그대로 + 실제 LLM 호출기(전송만 가짜) ---


def test_repo_config_with_j_on_stays_within_the_request_limit(caplog):
    """T(직렬)·겹쳐 보내기·E(named_term)·영어 장치가 켜진 저장소 설정에 J만 켜고, 실제 TutorLLM과
    요청당 상한(pipeline.respond)을 거친다. 막힌 시도 0, 보낸 시도 ≤ 최악 계산."""
    import json
    import logging
    from types import SimpleNamespace

    from tutor import pipeline
    from tutor.judge.intent import STUDY, IntentOut
    from tutor.runtime.llm import TutorLLM

    addendum = ds.load_prompt(ds.B_PROMPT_PATH)

    def reply(content):
        choice = SimpleNamespace(
            finish_reason="stop", message=SimpleNamespace(content=content, refusal=None)
        )
        return SimpleNamespace(choices=[choice], usage=SimpleNamespace(completion_tokens=5))

    async def send(kwargs):
        name = kwargs["response_format"]["json_schema"]["name"]
        if name == "IntentOut":
            return reply(IntentOut(label=STUDY, self_info=False).model_dump_json())
        if name == "SelectOut":
            return reply(json.dumps({"better": "B"}))
        is_b = kwargs["messages"][0]["content"].endswith(addendum)
        return reply((out_b() if is_b else out()).model_dump_json())

    class Gate:
        async def __aenter__(self):
            return None

        async def __aexit__(self, *exc):
            return False

    base = SimpleNamespace(params={}, structured="json_schema")
    llm = TutorLLM(send, Gate, model="m", base=base)
    cfg = config.load_config(config.ROOT / config.DEFAULT_CONFIG, overrides=ON)
    state = SimpleNamespace(tutor_cfg=cfg, tutor_kb=default_index(), tutor_llm=llm)
    flow.prepare_judge(state)
    body = {"messages": [{"role": "user", "content": QUESTION}]}

    async def go():
        deadline = asyncio.get_running_loop().time() + 30
        return await pipeline.respond(body, deadline=deadline, state=state)

    with caplog.at_level(logging.INFO, logger="guardrail.access"):
        resp = asyncio.run(go())
    assert resp["action"] == "answer" and B_TEXT in resp["answer"]
    assert not llm.capped
    assert sum(llm.calls.values()) <= config.request_calls(cfg)[1] == 7
    assert dict(llm.calls) == {"intent": 1, "main": 1, ds.ROLE_B: 1, ds.ROLE_J: 1}
    assert "draft_select j_B" in caplog.text and "/tutor" not in caplog.text
    assert QUESTION not in caplog.text and B_TEXT not in caplog.text
