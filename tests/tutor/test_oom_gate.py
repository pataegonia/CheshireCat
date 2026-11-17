"""자료 밖 확인 E(`toggles.oom_gate`, #213 B2의 E). LLM은 역할별 가짜다(실제 API를 부르지 않는다).

입력 문장은 지어낸 것이다(공개 세트 · KB 문구를 옮기지 않는다).
"""

import asyncio
import re
import unicodedata

import pytest
from pydantic import ValidationError

from tests.tutor.test_flow import QUESTION, out, state_with
from tutor.answer.templates import QUOTE_APPEND_LEAD, TEXT, Fixed
from tutor.judge import flow, intent, oom_gate
from tutor.judge.intent import IntentOut
from tutor.judge.oom_gate import OomOut
from tutor.request import parse_request
from tutor.runtime import config
from tutor.runtime.llm import Result

ON = {"toggles.oom_gate": "all"}


def _result(parsed=None, error=None, finish="stop"):
    return Result(parsed=parsed, error=error, attempts=1, latency_ms=1.0, finish_reason=finish,
                  completion_tokens=10)  # fmt: skip


class RoleLLM:
    """역할(main·oom_gate·intent)마다 결과를 따로 준다. 부른 순서와 인자를 기록한다."""

    def __init__(self, main, found=None, *, label=None, gate_error=None, gate_sleep=0.0,
                 gate_boom=False, main_error=None, finish="stop"):  # fmt: skip
        self.main, self.found, self.label = main, found, label
        self.gate_error, self.gate_sleep, self.gate_boom = gate_error, gate_sleep, gate_boom
        self.main_error, self.finish = main_error, finish
        self.roles: list[str] = []
        self.kwargs: dict[str, dict] = {}
        self.gate_cancelled = False

    async def complete(self, role, messages, schema, **kw):
        self.roles.append(role)
        now = asyncio.get_running_loop().time()
        self.kwargs[role] = {"messages": messages, "schema": schema, "now": now, **kw}
        if role == oom_gate.ROLE:
            try:
                if self.gate_sleep:
                    await asyncio.sleep(self.gate_sleep)
            except asyncio.CancelledError:
                self.gate_cancelled = True
                raise
            if self.gate_boom:
                raise RuntimeError("boom")
            parsed = None if self.found is None else OomOut(asked="a", quote="", found=self.found)
            return _result(parsed, self.gate_error, self.finish)
        if role == intent.ROLE:
            return _result(IntentOut(label=self.label, self_info=False) if self.label else None)
        return _result(None if self.main_error else self.main, self.main_error)


def run(state, text=QUESTION, *, seconds=None):
    async def go():
        now = asyncio.get_running_loop().time()
        deadline = 1e12 if seconds is None else now + seconds
        body = {"messages": [{"role": "user", "content": text}]}
        return await flow.answer_v2(parse_request(body), state, deadline=deadline)

    return asyncio.run(go())


ABSTAIN = {"answer": TEXT[Fixed.ABSTAIN], "action": "abstain", "citations": []}


def test_prompt_is_the_v0_text_without_the_source_line():
    system = oom_gate.load_system()
    v0 = (oom_gate.PROMPT_PATH.parents[2] / "harness" / "prompts" / "oom_check.md").read_text(
        encoding="utf-8"
    )
    assert system == v0.replace("\r\n", "\n").partition("\n")[2].strip("\n")


def test_off_makes_no_gate_call():
    llm = RoleLLM(out(), found=False)
    assert run(state_with(llm))["action"] == "answer"
    assert oom_gate.ROLE not in llm.roles


def test_absent_answer_becomes_fixed_abstain():
    llm = RoleLLM(out(), found=False)
    state = state_with(llm, **ON)
    assert run(state) == ABSTAIN
    assert llm.roles[0] == oom_gate.ROLE  # 주 호출보다 먼저 띄운다(동시에 돈다)
    assert llm.kwargs[oom_gate.ROLE]["retries"] == 0
    assert state.oom_gate_counts == {"absent": 1}


def test_gate_that_ends_after_the_flow_is_still_awaited():
    # 주 호출이 먼저 끝나도 E 마감 안이면 E를 기다린다(B2의 await e_task)
    llm = RoleLLM(out(), found=False, gate_sleep=0.05)
    state = state_with(llm, **ON)
    assert run(state) == ABSTAIN
    assert state.oom_gate_counts == {"absent": 1}


def _ids(text):
    return re.findall(r'<섹션 id="([^"]+)">', text)


def test_gate_sends_the_v0_system_the_main_bundle_and_its_own_deadline():
    llm = RoleLLM(out(), found=True)
    run(state_with(llm, **ON))
    gate, main = llm.kwargs[oom_gate.ROLE], llm.kwargs["main"]
    system, user = (m["content"] for m in gate["messages"])
    assert system == oom_gate.load_system()
    main_user = main["messages"][-1]["content"]
    assert _ids(user) and _ids(user) == _ids(main_user)  # 주 호출과 같은 근거 묶음(같은 순서)
    assert gate["schema"] is OomOut
    assert gate["timeout_s"] == pytest.approx(9.0)
    assert gate["deadline"] <= gate["now"] + 9.0 + 1e-6  # 요청 마감(1e12)이 아니라 E 마감


def test_found_keeps_the_answer():
    llm = RoleLLM(out(), found=True)
    state = state_with(llm, **ON)
    resp = run(state)
    assert resp["action"] == "answer" and resp["citations"][0] == "PY-01-01"
    assert state.oom_gate_counts == {"found": 1}


@pytest.mark.parametrize(
    "main",
    [
        out("answer_seeking", "scaffold"),
        out("out_of_material", "abstain", sentences=[]),
        out("jailbreak_injection", "refuse", sentences=[]),
    ],
)
def test_only_answers_are_changed(main):
    llm = RoleLLM(main, found=False)
    state = state_with(llm, **ON)
    resp = run(state)
    assert resp["action"] == main.action
    assert "absent" not in state.oom_gate_counts  # answer가 아니면 E 결과를 보지 않는다


@pytest.mark.parametrize(
    ("kwargs", "reason"),
    [
        ({"gate_error": "timeout"}, "error"),
        ({"finish": "length"}, "truncated"),
        ({"gate_boom": True}, "error"),
    ],
)
def test_gate_failure_keeps_the_answer(kwargs, reason):
    llm = RoleLLM(out(), found=False, **kwargs)
    state = state_with(llm, **ON)
    assert run(state)["action"] == "answer"
    assert state.oom_gate_counts == {reason: 1}


def test_slow_gate_is_cut_at_its_deadline_and_cancelled():
    llm = RoleLLM(out(), found=False, gate_sleep=5.0)
    state = state_with(llm, **ON, **{"llm.timeout_s.oom_gate": 0.05})

    async def go():
        body = {"messages": [{"role": "user", "content": QUESTION}]}
        resp = await flow.answer_v2(parse_request(body), state, deadline=1e12)
        await asyncio.sleep(0)
        current = asyncio.current_task()
        return resp, [t for t in asyncio.all_tasks() if t is not current and not t.done()]

    resp, pending = asyncio.run(go())
    assert resp["action"] == "answer"
    assert state.oom_gate_counts == {"timeout": 1}
    assert pending == [] and llm.gate_cancelled  # 루프가 닫히기 전에 이미 취소됐다


def _tagged(text):
    """태그 문자(U+E0000대)로 숨긴 글자를 붙인다. 창 본문에는 "[숨긴 문자: …]" 표기가 붙는다."""
    return text + "".join(chr(0xE0000 + ord(c)) for c in "hi")


@pytest.mark.parametrize(
    "text",
    ["what is a variable in the scoreboard example", _tagged("what is a variable")],
    ids=["plain", "hidden-tag"],
)
def test_english_only_utterance_does_not_call_the_gate(text):
    llm = RoleLLM(out(), found=False)
    state = state_with(llm, **ON)
    assert run(state, text)["action"] == "answer"
    assert oom_gate.ROLE not in llm.roles
    assert state.oom_gate_counts == {"english": 1}


@pytest.mark.parametrize(
    "text",
    [
        "scoreboard example에서 inning loop의 range 끝 값은 뭐예요",  # 라틴이 더 많아도 한글이 있다
        # 풀어 쓴 자모(NFD)도 정규화 뒤에는 한글이다
        unicodedata.normalize("NFD", "scoreboard 예제는 몇 회까지 기록해?"),
        "3 * 4 + 5 = ?",  # 한글도 라틴도 없다(문서 규칙: 라틴 글자가 있어야 영어)
    ],
    ids=["mixed", "nfd", "no-letters"],
)
def test_non_english_utterance_calls_the_gate(text):
    llm = RoleLLM(out(), found=False)
    state = state_with(llm, **ON)
    assert run(state, text) == ABSTAIN
    assert oom_gate.ROLE in llm.roles


def test_missing_prompt_makes_no_gate_call():
    llm = RoleLLM(out(), found=False)
    state = state_with(llm, **ON)
    state.tutor_oom_gate_prompt = None  # 기동 때 읽지 않은 state(모의 부하 등)
    assert run(state)["action"] == "answer"
    assert oom_gate.ROLE not in llm.roles


def test_gate_also_checks_a_quote_answer_after_main_failure():
    # 주 호출 실패 → 자료 인용 답(answer)도 E가 근거에 없다면 자료 밖 고정 문장
    llm = RoleLLM(out(), found=False, main_error="timeout")
    state = state_with(llm, **ON, **{"toggles.fallback_quote": True})
    assert run(state) == ABSTAIN


def test_serial_intent_fixed_label_is_not_overridden_by_the_gate():
    llm = RoleLLM(out(), found=False, label="무관 결과물")
    state = state_with(llm, **ON, **{"toggles.intent_override": "serial"})
    resp = run(state)
    assert resp["action"] == "redirect"  # T 고정 문장(answer가 아님)은 그대로
    assert "main" not in llm.roles


def test_gate_task_is_cancelled_when_the_flow_raises(monkeypatch):
    llm = RoleLLM(out(), found=False, gate_sleep=5.0)
    state = state_with(llm, **ON)

    async def boom(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(flow, "_answer_v2", boom)

    async def go():
        body = {"messages": [{"role": "user", "content": QUESTION}]}
        with pytest.raises(RuntimeError):
            await flow.answer_v2(parse_request(body), state, deadline=1e12)
        await asyncio.sleep(0)
        current = asyncio.current_task()
        return [t for t in asyncio.all_tasks() if t is not current and not t.done()]

    assert asyncio.run(go()) == []
    assert llm.gate_cancelled


@pytest.mark.parametrize(
    "extra",
    [{}, {"toggles.intent_override": "serial"}, {"toggles.verify": "guard"}],
    ids=["alone", "with-intent", "with-verify"],
)
def test_gate_fits_the_call_limit(extra):
    assert config.load_config(overrides={**ON, **extra}).toggles.oom_gate


@pytest.mark.parametrize(
    "extra",
    [
        {"toggles.guard_classify": "parallel"},
        {"toggles.intent_override": "parallel", "toggles.verify": "guard"},
    ],
    ids=["with-classify", "intent-and-verify"],
)
def test_gate_over_the_call_limit_fails_at_load(extra):
    with pytest.raises(ValidationError, match="TU-13"):
        config.load_config(overrides={**ON, **extra})


NAMED = {"toggles.oom_gate": "named_term"}
TERM_Q = "꿈틀 중학교 동아리의 은하수 등급 기준은 몇 단계예요?"  # 지어낸 이름(KB에 없음)


def test_named_term_scope_skips_questions_without_a_kb_absent_name():
    llm = RoleLLM(out(), found=False)
    state = state_with(llm, **NAMED)
    assert run(state)["action"] == "answer"  # QUESTION에는 이름 붙은 말이 없다
    assert oom_gate.ROLE not in llm.roles
    assert state.oom_gate_counts == {"no_term": 1}


def test_named_term_scope_calls_the_gate_for_a_kb_absent_name():
    llm = RoleLLM(out(), found=False)
    state = state_with(llm, **NAMED)
    assert run(state, TERM_Q) == ABSTAIN
    assert llm.roles[0] == oom_gate.ROLE
    assert state.oom_gate_counts == {"absent": 1}


def test_named_term_scope_keeps_the_answer_when_the_gate_finds_it():
    llm = RoleLLM(out(), found=True)
    state = state_with(llm, **NAMED)
    assert run(state, TERM_Q)["action"] == "answer"
    assert state.oom_gate_counts == {"found": 1}


def test_named_term_scope_without_kb_text_makes_no_call():
    llm = RoleLLM(out(), found=False)
    state = state_with(llm, **NAMED)
    assert state.tutor_kb_blob is not None  # 기동 때 만든다
    state.tutor_kb_blob = None
    assert run(state, TERM_Q)["action"] == "answer"
    assert oom_gate.ROLE not in llm.roles


def test_all_scope_does_not_build_the_kb_text():
    state = state_with(RoleLLM(out(), found=True), **ON)
    assert state.tutor_kb_blob is None  # all은 이름 검사를 하지 않는다


def test_named_term_scope_fits_the_call_limit_like_all():
    serial = {"toggles.intent_override": "serial"}
    assert config.load_config(overrides={**NAMED, **serial}).toggles.oom_gate == "named_term"
    with pytest.raises(ValidationError, match="TU-13"):
        config.load_config(overrides={**NAMED, "toggles.guard_classify": "parallel"})


def test_named_term_scope_looks_only_at_the_last_learner_turn():
    # 앞 턴에 KB에 없는 이름이 있어도 마지막 발화에 없으면 E를 부르지 않는다
    llm = RoleLLM(out(), found=False)
    state = state_with(llm, **NAMED)
    messages = [
        {"role": "user", "content": TERM_Q},
        {"role": "assistant", "content": "자료에서 찾아볼게요."},
        {"role": "user", "content": QUESTION},
    ]

    async def go():
        return await flow.answer_v2(parse_request({"messages": messages}), state, deadline=1e12)

    assert asyncio.run(go())["action"] == "answer"
    assert oom_gate.ROLE not in llm.roles
    assert state.oom_gate_counts == {"no_term": 1}


def test_named_term_scope_counts_english_before_names():
    llm = RoleLLM(out(), found=False)
    state = state_with(llm, **NAMED)
    assert run(state, "what is the scoring rule in this course")["action"] == "answer"
    assert oom_gate.ROLE not in llm.roles
    assert state.oom_gate_counts == {"english": 1}


# E와 자료 문장 덧붙이기(TU-07)를 함께 켠 경우: 덧붙이기는 E가 abstain으로 바꾸지 않은 결과에만 한다
QUOTED_NAMED = {**NAMED, "toggles.quote_append": True}


def test_gate_abstain_gets_no_material_sentences():
    llm = RoleLLM(out(), found=False)
    assert run(state_with(llm, **QUOTED_NAMED), TERM_Q) == ABSTAIN


def test_answer_the_gate_finds_still_gets_material_sentences():
    llm = RoleLLM(out(), found=True)
    resp = run(state_with(llm, **{"toggles.oom_gate": "all", "toggles.quote_append": True}))
    assert oom_gate.ROLE in llm.roles
    assert resp["action"] == "answer" and QUOTE_APPEND_LEAD in resp["answer"]


def test_answer_without_a_gate_call_still_gets_material_sentences():
    # named_term에서 이름 붙은 말이 없어 E를 부르지 않는 요청(대부분)도 덧붙이기는 그대로다
    llm = RoleLLM(out(), found=False)
    state = state_with(llm, **QUOTED_NAMED)
    resp = run(state)
    assert oom_gate.ROLE not in llm.roles and state.oom_gate_counts == {"no_term": 1}
    assert resp["action"] == "answer" and QUOTE_APPEND_LEAD in resp["answer"]
