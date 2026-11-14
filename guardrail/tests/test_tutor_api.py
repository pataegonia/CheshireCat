"""튜터 공유 API: Guard.classify·Guard.judge_output (S2.11, 이슈 #119, interface.md §2.1).

튜터는 `await guard.classify(messages)`·`await guard.judge_output(messages, answer, category=)`를
부른다. nano는 FakeLLM이고 운영진 API는 부르지 않는다. 시간 제한은 튜터가 바깥에서 건다.
"""

import asyncio
from typing import get_args

import anyio
import hypothesis.strategies as st
import pytest
import test_guard
from fakes import Hang, Malformed, ServerError
from hypothesis import given, settings
from strategies import json_values
from test_guard import (
    ANSWER,
    BASELINE,
    GROUNDED,
    MSGS,
    REFUSAL,
    TAG,
    UNGROUNDED,
    _stubbed,
    calls,
    make,
    req,
    responder,
)

from guardrail.guard import GuardUnavailable
from guardrail.schema import Category, GuardRequest, GuardResponse, Message, OutputVerdict

pytestmark = pytest.mark.anyio
MARK = "BODY-MARK-7f3a"  # 예외 메시지로 새면 안 되는 본문 표식

no_rule = test_guard.no_rule  # 같은 픽스처를 이름으로 등록한다

CATEGORIES = get_args(Category)
QUESTION = MSGS[0].content
DICTS = [{"role": "user", "content": QUESTION}]
LOW_SIGNAL = [
    {"role": "user", "content": "안녕"},
    {"role": "assistant", "content": "네"},
    {"role": "user", "content": "ㅋㅋ"},
]
BASELINE_OUT = {"reason": "r", "grounded": False, "safe": False}
OUTER_TIMEOUT_S = 0.2


@pytest.fixture
def anyio_backend():
    return "asyncio"


async def _gate_is_free(guard) -> None:
    """게이트 허가가 모두 돌아왔다: gate 수만큼 동시에 들어가 서로를 기다릴 수 있다."""
    n = guard.cfg.llm.gate
    barrier = asyncio.Barrier(n)

    async def enter():
        async with guard.llm.slot():
            await barrier.wait()

    async with asyncio.timeout(2):
        await asyncio.gather(*(enter() for _ in range(n)))


async def _cancelled_from_outside(guard, call) -> None:
    """바깥 asyncio.timeout이 취소하면 시간 초과가 그대로 올라오고, 흔적이 남지 않는다."""
    before = asyncio.all_tasks()
    with pytest.raises(TimeoutError):
        async with asyncio.timeout(OUTER_TIMEOUT_S):
            await call()
    assert asyncio.all_tasks() == before
    await _gate_is_free(guard)


# --- classify ---


async def test_classify_returns_the_category_nano_chose_with_one_input_call():
    guard, fake = await make(responder("answer_seeking"))
    assert await guard.classify(DICTS) == "answer_seeking"
    assert len(calls(fake, "input")) == 1 and not calls(fake, "output")


async def test_classify_accepts_message_tuples():
    guard, fake = await make(responder("jailbreak_injection"))
    assert await guard.classify(MSGS) == "jailbreak_injection"
    assert len(calls(fake, "input")) == 1


async def test_classify_low_signal_turn_skips_nano():
    guard, fake = await make()
    assert await guard.classify(LOW_SIGNAL) == "low_signal_input"
    assert not calls(fake, "input")


@pytest.mark.parametrize(
    ("kw", "fallback", "source"),
    [
        ({"indexes": _stubbed("answer_seeking", "harmful_content")}, "harmful_content", "fallback"),
        ({"toggles__lr": False, "toggles__knn_vote": False}, "normal", "default"),
    ],
    ids=["knn_lr", "no_helpers"],
)
async def test_classify_raises_when_nano_fails_but_judge_still_falls_back(kw, fallback, source):
    # 튜터는 classify가 실패하면 주 호출 범주만 쓴다. 추측값을 성공처럼 주면 그 범주로 덮는다
    guard, fake = await make(responder(input_step=ServerError()), **kw)
    with pytest.raises(GuardUnavailable):
        await guard.classify(DICTS)
    assert len(calls(fake, "input")) == 1
    j = await guard.judge(req(None, has=False))  # /guard 경로의 폴백은 그대로
    assert (j.response.category, j.input_source) == (fallback, source)


async def test_classify_raises_when_the_guard_itself_fails(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError(MARK)

    guard, _ = await make(responder("normal"))
    monkeypatch.setattr(guard, "_prepare", boom)
    with pytest.raises(GuardUnavailable) as caught:
        await guard.classify(DICTS)
    assert MARK not in str(caught.value) and MARK not in repr(caught.value.__context__)
    assert (await guard.judge(req(None, has=False))).response.category == "normal"  # /guard


@pytest.mark.parametrize("messages", [[], [{"role": "assistant", "content": "안녕"}]])
async def test_classify_without_learner_turn_is_normal(messages):
    guard, fake = await make(responder("harmful_content"))
    assert await guard.classify(messages) == "normal"
    assert not calls(fake, "input")


async def test_classify_cancelled_from_outside_propagates_and_leaves_nothing():
    guard, _ = await make(responder(input_step=Hang()))
    await _cancelled_from_outside(guard, lambda: guard.classify(DICTS))


# --- judge_output: category 지정 ---


async def test_judge_output_with_category_calls_only_the_output_nano():
    guard, fake = await make(responder("harmful_content"))
    v = await guard.judge_output(DICTS, ANSWER, category="normal")
    assert isinstance(v, OutputVerdict)
    assert not calls(fake, "input") and len(calls(fake, "output")) == 1


@pytest.mark.parametrize(
    ("answer", "safe"), [(ANSWER, False), (REFUSAL, True)]
)  # safe는 block 범주에 응했는가(GR-08)
async def test_given_block_category_is_tier1_grounded_and_safe_by_rule(no_rule, answer, safe):
    guard, _ = await make(responder(output=UNGROUNDED))
    v = await guard.judge_output(DICTS, answer, category="harmful_content")
    assert v == OutputVerdict(True, (), safe)


async def test_given_allow_category_with_verified_quote_is_ungrounded(no_rule):
    guard, _ = await make(responder(output=UNGROUNDED), toggles__nano_span=False)
    v = await guard.judge_output(DICTS, ANSWER, category="answer_seeking")
    assert v == OutputVerdict(False, (), True)


async def test_rule_span_comes_back_as_a_substring_of_the_answer(monkeypatch):
    from guardrail import guard as guard_module

    monkeypatch.setattr(guard_module, "literal_rule", lambda *a, **k: ("append를",))
    guard, _ = await make(responder(output=GROUNDED))
    v = await guard.judge_output(DICTS, ANSWER, category="normal")
    assert v == OutputVerdict(False, ("append를",), True)
    assert all(s in ANSWER for s in v.unsupported)


async def test_given_category_wins_over_a_low_signal_turn(no_rule):
    guard, fake = await make(responder(output=UNGROUNDED), toggles__nano_span=False)
    ok = await guard.judge_output(LOW_SIGNAL, ANSWER, category="normal")
    assert ok == OutputVerdict(False, (), True)  # 입력 쪽 low-signal 직행이 범주를 덮지 않는다
    blocked = await guard.judge_output(LOW_SIGNAL, ANSWER, category="harmful_content")
    assert blocked == OutputVerdict(True, (), False)
    assert not calls(fake, "input")


@pytest.mark.parametrize("default", ["grounded", "ungrounded"])
@pytest.mark.parametrize(
    ("category", "block"), [("answer_seeking", False), ("harmful_content", True)]
)
async def test_output_nano_failure_uses_failure_default_and_keeps_safe_rule(
    no_rule, default, category, block
):
    guard, _ = await make(responder(output_step=Malformed()), output__failure_default=default)
    v = await guard.judge_output(DICTS, ANSWER, category=category)
    assert v == OutputVerdict(block or default == "grounded", (), not block)


async def test_hidden_tag_in_the_answer_is_unsafe(no_rule):
    guard, _ = await make(responder(output=GROUNDED))
    v = await guard.judge_output(DICTS, ANSWER + TAG, category="normal")
    assert v.safe is False


async def test_baseline_mode_still_returns_an_output_verdict():
    guard, fake = await make(responder(output=BASELINE_OUT), config=BASELINE)
    v = await guard.judge_output(DICTS, ANSWER, category="normal")
    assert v == OutputVerdict(False, (), False)
    assert not calls(fake, "input") and len(calls(fake, "output")) == 1


@pytest.mark.parametrize("bad", ["attack", "", "NORMAL", "normal "])
async def test_unknown_category_is_a_value_error_and_calls_nothing(bad):
    guard, fake = await make()
    before = len(fake.requests)
    with pytest.raises(ValueError):  # noqa: PT011 - 메시지는 고정하지 않는다
        await guard.judge_output(DICTS, ANSWER, category=bad)
    assert len(fake.requests) == before


# --- judge_output: category=None ---


async def test_without_category_both_nanos_run_once_and_match_judge(no_rule):
    kw = {"toggles__nano_span": True}
    reply = responder("answer_seeking", UNGROUNDED)
    guard, fake = await make(reply, **kw)
    v = await guard.judge_output(DICTS, ANSWER)
    assert len(calls(fake, "input")) == len(calls(fake, "output")) == 1
    other, _ = await make(reply, **kw)
    want = (await other.judge(req(ANSWER))).response.output
    assert isinstance(v, OutputVerdict) and v == want


@pytest.mark.parametrize("category", [None, "normal"])
async def test_judge_output_cancelled_from_outside_propagates_and_leaves_nothing(category):
    guard, _ = await make(responder(input_step=Hang(), output_step=Hang()))
    await _cancelled_from_outside(
        guard, lambda: guard.judge_output(DICTS, ANSWER, category=category)
    )


# --- 재시도: 공유 메서드는 0회, /guard 경로(judge)는 llm.retries 그대로 ---


async def test_classify_does_not_retry_but_judge_does():
    reply = responder(input_step=Malformed())
    guard, fake = await make(reply)
    with pytest.raises(GuardUnavailable):  # 파싱 실패 1회로 끝난다
        await guard.classify(DICTS)
    assert len(calls(fake, "input")) == 1
    other, fake2 = await make(reply)
    await other.judge(req(None, has=False))
    assert len(calls(fake2, "input")) == 2


@pytest.mark.parametrize(
    ("category", "role"), [("normal", "output"), (None, "output"), (None, "input")]
)
async def test_judge_output_does_not_retry_but_judge_does(category, role):
    reply = responder(**{f"{role}_step": Malformed()})
    guard, fake = await make(reply)
    await guard.judge_output(DICTS, ANSWER, category=category)
    assert len(calls(fake, role)) == 1
    other, fake2 = await make(reply)
    await other.judge(req(ANSWER))
    assert len(calls(fake2, role)) == 2


# --- /guard와 같은 판정 ---

RAW_MESSAGES = st.lists(
    st.fixed_dictionaries(
        {
            "role": st.sampled_from(["user", "assistant", "system"]),
            "content": st.text(max_size=120) | st.sampled_from(["ㅋㅋ", "안녕", "ㅎㅎㅎ"]),
        },
        optional={"extra": json_values},
    ),
    max_size=6,
)


@settings(max_examples=60, deadline=None)
@given(raw=RAW_MESSAGES, nano_fails=st.booleans())
def test_classify_equals_the_category_of_judge(raw, nano_fails):
    async def run():
        step = ServerError() if nano_fails else None
        guard, _ = await make(responder("answer_seeking", input_step=step))
        msgs = tuple(Message(m["role"], m["content"]) for m in raw)
        j = await guard.judge(GuardRequest(msgs, None, False))
        # nano가 범주를 냈거나, nano를 부를 일이 없었으면(low-signal·발화 없음) 같은 범주
        if not nano_fails or j.input_source == "low_signal" or "no_user_message" in j.errors:
            assert await guard.classify(raw) == j.response.category
        else:  # nano 실패: /guard는 폴백, classify는 실패를 알린다
            with pytest.raises(GuardUnavailable):
                await guard.classify(raw)

    anyio.run(run)


@pytest.mark.parametrize("hidden", [False, True], ids=["plain", "hidden_tag"])
@pytest.mark.parametrize(
    ("category", "answer", "want"),
    [
        ("harmful_content", ANSWER, (True, False)),  # block 범주에 응했다: ①, safe=false
        ("harmful_content", REFUSAL, (True, True)),
        ("answer_seeking", ANSWER, (False, True)),  # allow: failure_default
        (None, ANSWER, (False, True)),  # 범주가 없으면 기본 출력(이전과 같다)
    ],
    ids=["block_answer", "block_refusal", "allow", "no_category"],
)
async def test_given_category_without_learner_turn_keeps_the_safe_rule(
    hidden, category, answer, want
):
    guard, fake = await make(responder(output=UNGROUNDED), output__failure_default="ungrounded")
    text = answer + TAG if hidden else answer
    v = await guard.judge_output([], text, category=category)
    assert v == OutputVerdict(want[0], (), want[1] and not hidden)  # 숨긴 태그는 그래도 본다
    assert not calls(fake, "input") and not calls(fake, "output")


UNREADABLE_TURNS = [
    [Message("user", 123)],  # 판정이 예외로 끝난다(_run이 삼킨다)
    [{"content": QUESTION}],  # role이 없다: 학습자 발화 없음
]


@pytest.mark.parametrize("messages", UNREADABLE_TURNS, ids=["error", "no_user"])
@pytest.mark.parametrize(
    ("answer", "safe"),
    [(ANSWER, False), (REFUSAL, True), ("", True)],
    ids=["answer", "refusal", "empty"],
)
async def test_given_block_category_survives_an_unreadable_turn(messages, answer, safe):
    guard, _ = await make(responder(output=UNGROUNDED))
    v = await guard.judge_output(messages, answer, category="jailbreak_injection")
    assert v.unsupported == () and v.safe is safe
    if answer:
        assert v.grounded is True  # GR-07 ①


@pytest.mark.parametrize(
    "msgs", [(Message("user", 123),), (Message("", QUESTION),)], ids=["error", "no_user"]
)
async def test_guard_path_on_an_unreadable_turn_is_unchanged(msgs):
    guard, _ = await make(responder(output=UNGROUNDED), output__failure_default="ungrounded")
    j = await guard.judge(GuardRequest(msgs, ANSWER, True))
    assert j.response == GuardResponse("normal", OutputVerdict(False, (), True))


class _Explosive(dict):
    """읽으면 본문이 든 예외를 내는 dict(튜터 쪽 버그 흉내)."""

    def get(self, key, default=None):
        raise RuntimeError(MARK)


async def test_unreadable_message_objects_never_leak_their_exception():
    guard, fake = await make(responder("normal"))
    bad = [_Explosive(role="user", content=QUESTION)]
    with pytest.raises(GuardUnavailable) as caught:
        await guard.classify(bad)
    assert MARK not in str(caught.value) and caught.value.__context__ is None
    v = await guard.judge_output(bad, ANSWER, category="jailbreak_injection")
    assert v == OutputVerdict(True, (), False)
    v = await guard.judge_output(bad, ANSWER)
    assert isinstance(v, OutputVerdict) and v.safe
    assert not calls(fake, "input")


async def test_mixed_messages_and_dicts_read_like_the_guard_body():
    guard, fake = await make(responder("off_topic"))
    mixed = [
        {"role": "user", "content": 7},  # content가 문자열이 아니면 버린다
        Message("assistant", "네"),
        {"role": None, "content": "역할 없음"},  # role이 문자열이 아니면 ""(학습자 아님)
        {"role": "user", "content": QUESTION},
    ]
    assert await guard.classify(mixed) == "off_topic"
    sent = calls(fake, "input")[0]["messages"][-1]["content"]
    assert '<message role="assistant">네</message>' in sent
    assert '<message role="other">역할 없음</message>' in sent
    assert ">7</message>" not in sent and f"<target>{QUESTION}</target>" in sent
    only_odd = [{"role": "user", "content": 7}, {"role": 1, "content": "x"}, "user", None]
    assert await guard.classify(only_odd) == "normal"  # 남는 학습자 발화가 없다
    assert len(calls(fake, "input")) == 1


@pytest.mark.parametrize("bad", [None, "안녕하세요", 3, {"role": "user", "content": "q"}])
async def test_non_sequence_messages_are_an_empty_conversation(bad):
    guard, fake = await make(responder("harmful_content"))
    assert await guard.classify(bad) == "normal"
    v = await guard.judge_output(bad, ANSWER, category="normal")
    assert isinstance(v, OutputVerdict) and v.unsupported == () and v.safe
    assert not calls(fake, "input") and not calls(fake, "output")


@pytest.mark.parametrize("category", CATEGORIES)
async def test_given_category_equals_judge_when_nano_returns_it(no_rule, category):
    reply = responder(category, UNGROUNDED)
    kw = {"toggles__nano_span": True}
    guard, _ = await make(reply, **kw)
    other, _ = await make(reply, **kw)
    want = (await other.judge(req(ANSWER))).response.output
    assert await guard.judge_output(DICTS, ANSWER, category=category) == want
