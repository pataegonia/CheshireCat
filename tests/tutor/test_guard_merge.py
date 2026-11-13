"""가드 classify 병렬 합치기·judge_output 연결(TU-10·TU-09 ②, #88).

가드와 LLM은 가짜다. 검색·표·결정적 수정(IN-04)은 main의 실제 코드를 쓴다. 실제 가드
(`Guard.classify`·`judge_output`)가 들어오면 같은 프로토콜(`GuardLike`)로 연결한다.
"""

import asyncio

import pytest
from guardrail.schema import OutputVerdict

from tests.tutor.test_flow import QUESTION, FakeLLM, out, state_with
from tutor.answer.templates import TEXT, Fixed
from tutor.judge import flow
from tutor.judge.guard_merge import ATTACK_CATEGORIES, Merged, merge_category
from tutor.judge.main_call import EvidenceOut, SentenceOut
from tutor.request import parse_request

GOOD = SentenceOut(
    text="야구 점수판 예제는 3회까지의 점수를 기록해요.",
    kind="claim",
    evidence=[EvidenceOut(sid="PY-01-01", quote="3회까지의 점수를 기록해요")],
)
EXTRA = SentenceOut(text="그리고 틀린 주장이 하나 더 있어요.", kind="non_claim", evidence=[])


class FakeGuard:
    """`classify`·`judge_output`을 가진 가짜 가드. 호출 인자와 순서를 기록한다."""

    def __init__(
        self,
        category="normal",
        verdict=None,
        *,
        classify_delay=0.0,
        judge_delay=0.0,
        classify_error=None,
        judge_error=None,
        log=None,
    ):
        self.category = category
        self.verdict = verdict if verdict is not None else OutputVerdict(True, (), True)
        self.classify_delay = classify_delay
        self.judge_delay = judge_delay
        self.classify_error = classify_error
        self.judge_error = judge_error
        self.log = log if log is not None else []
        self.classify_calls = []
        self.judge_calls = []
        self.cancelled = False

    async def classify(self, messages):
        self.log.append("classify:start")
        self.classify_calls.append(messages)
        try:
            await asyncio.sleep(self.classify_delay)
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        if self.classify_error is not None:
            raise self.classify_error
        self.log.append("classify:end")
        return self.category

    async def judge_output(self, messages, candidate_answer, category=None):
        self.log.append("judge:start")
        self.judge_calls.append({"candidate": candidate_answer, "category": category})
        await asyncio.sleep(self.judge_delay)
        if self.judge_error is not None:
            raise self.judge_error
        return self.verdict


class LoggingLLM(FakeLLM):
    def __init__(self, parsed, log):
        super().__init__(parsed)
        self.log = log

    async def complete(self, role, messages, schema, **kw):
        self.log.append("main:call")
        return await super().complete(role, messages, schema, **kw)


class RaisingLLM:
    async def complete(self, role, messages, schema, **kw):
        raise RuntimeError("boom")


def build(guard, parsed=None, llm=None, **overrides):
    s = state_with(llm or FakeLLM(parsed or out(sentences=[GOOD])), **overrides)
    s.guard = guard
    return s


def run(state, text=QUESTION, *, deadline=1e12):
    body = {"messages": [{"role": "user", "content": text}]}
    return asyncio.run(flow.answer_v2(parse_request(body), state, deadline=deadline))


def run_soon(state, seconds, text=QUESTION):
    """요청 마감이 `seconds`초 뒤인 호출(남은 시간이 judge_skip_s보다 적은 경우)."""

    async def go():
        deadline = asyncio.get_running_loop().time() + seconds
        body = {"messages": [{"role": "user", "content": text}]}
        return await flow.answer_v2(parse_request(body), state, deadline=deadline)

    return asyncio.run(go())


# --- 합치기 규칙(TU-10) ---


@pytest.mark.parametrize(
    ("classified", "main_action", "kind"),
    [
        ("jailbreak_injection", "answer", Fixed.REFUSE_JAILBREAK),
        ("jailbreak_injection", "scaffold", Fixed.REFUSE_JAILBREAK),
        ("harmful_content", "answer", Fixed.REFUSE_HARMFUL),
        ("off_topic", "answer", Fixed.REDIRECT_OFF_TOPIC),
        ("off_topic", "clarify", Fixed.REDIRECT_OFF_TOPIC),
        ("harmful_content", "scaffold", Fixed.REFUSE_HARMFUL),
        ("academic_dishonesty", "answer", Fixed.SCAFFOLD_ACADEMIC),
        ("academic_dishonesty", "abstain", Fixed.SCAFFOLD_ACADEMIC),
    ],
)
def test_attack_category_overrides_different_action(classified, main_action, kind):
    assert merge_category("normal", main_action, classified) == Merged(classified, kind)


@pytest.mark.parametrize(
    ("classified", "main_action"),
    [
        ("jailbreak_injection", "refuse"),
        ("harmful_content", "refuse"),
        ("harmful_content", "redirect"),  # harmful은 refuse·redirect 둘 다 표 action
        ("off_topic", "redirect"),
        ("academic_dishonesty", "scaffold"),
    ],
)
def test_attack_category_keeps_main_when_action_matches_table(classified, main_action):
    assert merge_category("normal", main_action, classified) == Merged("normal", None)


@pytest.mark.parametrize(
    ("main_category", "main_action", "classified"),
    [
        # §3 우선순위: 주 호출 action이 더 강하면 덮지 않는다(재현율은 둘 중 높은 쪽 이상)
        ("emotional_distress", "escalate", "jailbreak_injection"),
        ("emotional_distress", "escalate", "academic_dishonesty"),
        ("emotional_distress", "escalate", "off_topic"),
        ("jailbreak_injection", "refuse", "off_topic"),
        ("jailbreak_injection", "refuse", "academic_dishonesty"),
        ("harmful_content", "redirect", "academic_dishonesty"),
        ("low_signal_input", "clarify", "academic_dishonesty"),
    ],
)
def test_attack_category_never_weakens_a_stronger_main_action(
    main_category, main_action, classified
):
    assert merge_category(main_category, main_action, classified) == Merged(main_category, None)


@pytest.mark.parametrize(
    "classified",
    [
        "normal",
        "answer_seeking",
        "out_of_material",
        "pii_disclosure",
        "emotional_distress",
        None,
        "?",
    ],
)
def test_non_attack_or_missing_classify_never_overrides(classified):
    # 비공격 범주에서는 classify로 action을 정하지 않는다. 실패(None)·모르는 값도 주 호출 결과다
    assert merge_category("normal", "answer", classified) == Merged("normal", None)


def test_attack_categories_are_the_four_of_guard_section_3():
    assert ATTACK_CATEGORIES == {
        "jailbreak_injection",
        "harmful_content",
        "off_topic",
        "academic_dishonesty",
    }


def test_main_attack_with_classify_normal_follows_main():
    # 주 호출이 공격으로 봤는데 classify가 normal이면 주 호출을 따른다(재현율은 둘 중 높은 쪽)
    assert merge_category("jailbreak_injection", "refuse", "normal") == Merged(
        "jailbreak_injection", None
    )


# --- 흐름: 병렬 합치기 ---


def test_classify_attack_replaces_main_answer_with_fixed_sentence():
    guard = FakeGuard("jailbreak_injection")
    resp = run(build(guard))
    assert resp == {
        "answer": TEXT[Fixed.REFUSE_JAILBREAK],
        "action": "refuse",
        "citations": [],
    }
    assert guard.judge_calls == []  # 고정 문장은 판정하지 않는다


def test_classify_normal_keeps_main_result():
    guard = FakeGuard("normal")
    resp = run(build(guard))
    assert resp["action"] == "answer"
    assert resp["citations"] == ["PY-01-01"]
    assert "3회까지의 점수를 기록해요" in resp["answer"]


def test_classify_cannot_turn_main_escalate_into_a_weaker_fixed_sentence():
    # 학습 질문이 섞여도 escalate(§3). classify가 공격 범주여도 위기 응답을 지운다면 안 된다
    guard = FakeGuard("academic_dishonesty")
    resp = run(build(guard, out(category="emotional_distress", action="escalate", sentences=[])))
    assert resp["action"] == "escalate"


def test_classify_receives_the_request_messages_once():
    guard = FakeGuard("normal")
    run(build(guard))
    assert guard.classify_calls == [[{"role": "user", "content": QUESTION}]]


def test_classify_runs_in_parallel_with_main_call():
    log = []
    guard = FakeGuard("normal", log=log, classify_delay=0.05)
    run(build(guard, llm=LoggingLLM(out(sentences=[GOOD]), log)))
    # 병렬: classify가 먼저 시작하고, 끝나기 전에 주 호출이 들어간다
    assert log.index("classify:start") < log.index("main:call") < log.index("classify:end")


def test_serial_mode_waits_for_classify_before_main_call():
    log = []
    guard = FakeGuard("normal", log=log, classify_delay=0.05)
    run(
        build(
            guard,
            llm=LoggingLLM(out(sentences=[GOOD]), log),
            **{"toggles.guard_classify": "serial"},
        )
    )
    assert log.index("classify:end") < log.index("main:call")


def test_serial_mode_merges_the_same_way():
    guard = FakeGuard("harmful_content")
    resp = run(build(guard, **{"toggles.guard_classify": "serial"}))
    assert resp["answer"] == TEXT[Fixed.REFUSE_HARMFUL]


def test_classify_off_never_calls_guard_classify():
    guard = FakeGuard("jailbreak_injection")
    resp = run(build(guard, **{"toggles.guard_classify": "off"}))
    assert guard.classify_calls == []
    assert resp["action"] == "answer"  # 주 호출 단독(ⓑ)


def test_classify_error_falls_back_to_main_result():
    guard = FakeGuard("jailbreak_injection", classify_error=RuntimeError("x"))
    resp = run(build(guard))
    assert resp["action"] == "answer"
    assert len(guard.judge_calls) == 1  # 검증은 계속 돈다


def test_classify_time_limit_stops_the_wait_and_keeps_main_result():
    import time

    guard = FakeGuard("jailbreak_injection", classify_delay=5.0)
    state = build(guard, **{"llm.timeout_s.classify": 0.1})
    t0 = time.perf_counter()
    resp = run(state)
    assert time.perf_counter() - t0 < 2.0  # 5초를 기다리지 않는다
    assert resp["action"] == "answer"  # 시간 초과면 주 호출 결과만 쓴다(ⓑ)
    assert guard.cancelled


def test_guard_without_classify_is_skipped_silently():
    # 가드 구현이 아직 classify·judge_output을 내지 않을 때(상수 판정기 등): 주 호출 결과만 쓴다
    class OnlyJudge:
        async def judge(self, req):  # 가드 골격의 판정 함수만 있는 판정기
            raise AssertionError("튜터가 부를 함수가 아니다")

    resp = run(build(OnlyJudge()))
    assert resp["action"] == "answer"
    assert resp["citations"] == ["PY-01-01"]


def test_no_guard_on_state_is_main_call_only():
    s = state_with(FakeLLM(out(sentences=[GOOD])))  # state.guard 없음(프로세스 안 호출)
    assert run(s)["action"] == "answer"


@pytest.mark.parametrize(
    ("classified", "kind"),
    [
        ("jailbreak_injection", Fixed.REFUSE_JAILBREAK),
        ("harmful_content", Fixed.REFUSE_HARMFUL),
        ("off_topic", Fixed.REDIRECT_OFF_TOPIC),
        ("academic_dishonesty", Fixed.SCAFFOLD_ACADEMIC),
    ],
)
def test_main_call_failure_uses_classify_attack_fixed_sentence(classified, kind):
    # 주 호출이 실패해도 classify가 공격 범주면 그 고정 문장(TU-12·TU-10)
    guard = FakeGuard(classified, classify_delay=0.01)
    resp = run(build(guard, llm=FakeLLM(error="timeout")))
    assert resp["answer"] == TEXT[kind]
    assert resp["citations"] == []


@pytest.mark.parametrize("classified", ["normal", "answer_seeking", "emotional_distress"])
def test_main_call_failure_with_non_attack_classify_uses_tu12_default(classified):
    guard = FakeGuard(classified)
    resp = run(build(guard, llm=FakeLLM(error="timeout")))
    assert resp["action"] == "clarify"  # 잠정 기본값(fallback_action)


def test_main_call_failure_with_classify_error_uses_tu12_default():
    guard = FakeGuard("jailbreak_injection", classify_error=RuntimeError("x"))
    resp = run(build(guard, llm=FakeLLM(error="timeout")))
    assert resp["action"] == "clarify"


def test_main_call_failure_crisis_signal_beats_classify_attack():
    # 위기 신호가 먼저다(§3 우선순위)
    guard = FakeGuard("off_topic")
    text = "몇 주째 너무 힘들고 혼자 버티기 힘들어요 아무것도 하기 싫어요"
    resp = run(build(guard, llm=FakeLLM(error="timeout")), text)
    assert resp["action"] == "escalate"


def test_pending_classify_is_cancelled_when_flow_raises():
    guard = FakeGuard("normal", classify_delay=5.0)
    state = build(guard, llm=RaisingLLM())
    with pytest.raises(RuntimeError):
        run(state)
    assert guard.cancelled  # 요청이 끝난 뒤 남는 작업이 없다(TU-18)


# --- 흐름: judge_output ---


def test_judge_output_gets_merged_category_and_joined_candidate():
    guard = FakeGuard("normal")
    run(build(guard))
    assert guard.judge_calls == [
        {"candidate": GOOD.text, "category": "normal"}  # category를 넘긴다(넘기지 않으면 nano +1)
    ]


def test_candidate_joins_sentences_with_single_space():
    guard = FakeGuard("normal")
    run(build(guard, out(sentences=[GOOD, EXTRA])))
    assert guard.judge_calls[0]["candidate"] == f"{GOOD.text} {EXTRA.text}"


def test_unsupported_span_deletes_that_sentence():
    verdict = OutputVerdict(False, ("틀린 주장이 하나 더",), True)
    guard = FakeGuard("normal", verdict)
    resp = run(build(guard, out(sentences=[GOOD, EXTRA])))
    assert resp["answer"] == GOOD.text
    assert resp["action"] == "answer"
    assert resp["citations"] == ["PY-01-01"]


def test_unsafe_verdict_replaces_answer_with_refuse():
    guard = FakeGuard("normal", OutputVerdict(True, (), False))
    resp = run(build(guard))
    assert resp["action"] == "refuse"
    assert resp["citations"] == []


def test_judge_failure_skips_judgement_only():
    guard = FakeGuard("normal", judge_error=RuntimeError("x"))
    resp = run(build(guard))
    assert resp["action"] == "answer"  # abstain으로 바꾸지 않는다
    assert resp["answer"] == GOOD.text


def test_judge_timeout_skips_judgement_only():
    guard = FakeGuard("normal", judge_delay=5.0)
    resp = run(build(guard, **{"llm.timeout_s.verify": 0.05}))
    assert resp["action"] == "answer"


def test_judge_is_skipped_when_remaining_time_is_below_judge_skip_s():
    guard = FakeGuard("normal")
    resp = run_soon(build(guard), 2.0)  # 남은 2초 < judge_skip_s(잠정 4초)
    assert guard.judge_calls == []
    assert resp["action"] == "answer"
    assert resp["answer"] == GOOD.text


def test_judge_skip_s_can_come_from_state():
    guard = FakeGuard("normal")
    state = build(guard)
    state.judge_skip_s = 0.5
    run_soon(state, 2.0)
    assert len(guard.judge_calls) == 1


def test_judge_skip_s_default_is_read_from_shared_runtime_config():
    # 통합 config/runtime.yaml의 judge_skip_s를 읽는다(코드에 값을 따로 두지 않는다)
    from integration.runtime import load_runtime
    from tutor.judge.guard_merge import JUDGE_SKIP_S

    assert load_runtime().judge_skip_s == JUDGE_SKIP_S


@pytest.mark.parametrize("mode", ["off", "self"])
def test_verify_other_than_guard_never_calls_judge_output(mode):
    guard = FakeGuard("normal")
    run(build(guard, **{"toggles.verify": mode}))
    assert guard.judge_calls == []


@pytest.mark.parametrize(
    ("category", "action"),
    [
        ("out_of_material", "abstain"),
        ("off_topic", "redirect"),
        ("jailbreak_injection", "refuse"),
        ("emotional_distress", "escalate"),
        ("low_signal_input", "clarify"),
        ("academic_dishonesty", "scaffold"),
    ],
)
def test_fixed_sentence_paths_are_not_judged(category, action):
    guard = FakeGuard("normal")
    run(build(guard, out(category=category, action=action, sentences=[GOOD])))
    assert guard.judge_calls == []


@pytest.mark.parametrize(
    ("category", "action"),
    [("normal", "answer"), ("pii_disclosure", "answer"), ("answer_seeking", "scaffold")],
)
def test_main_call_text_paths_are_judged(category, action):
    guard = FakeGuard("normal")
    run(build(guard, out(category=category, action=action, sentences=[GOOD])))
    assert len(guard.judge_calls) == 1
    assert guard.judge_calls[0]["category"] == category


def test_off_table_combination_with_main_text_is_judged():
    # 표 밖 조합(범주 off_topic인데 action answer): 주 호출 문장이 그대로 나가므로 판정한다
    guard = FakeGuard("normal")
    resp = run(build(guard, out(category="off_topic", action="answer", sentences=[GOOD])))
    assert resp["action"] == "answer"
    assert guard.judge_calls == [{"candidate": GOOD.text, "category": "off_topic"}]


def test_per_request_nano_calls_stay_within_three():
    # 정상 경로: 주 호출 1 + classify 1 + judge_output 1(category를 넘겨 classify 재호출 없음)
    guard = FakeGuard("normal")
    llm = FakeLLM(out(sentences=[GOOD]))
    run(build(guard, llm=llm))
    assert len(llm.calls) + len(guard.classify_calls) + len(guard.judge_calls) == 3


def test_tutor_never_calls_classify_or_judge_output_again_after_failure():
    # 재시도는 주 호출만(TU-13, 통합 절대 4회). classify·judge_output은 실패해도 다시 안 부른다
    guard = FakeGuard("normal", classify_error=RuntimeError("x"), judge_error=RuntimeError("y"))
    run(build(guard))
    assert len(guard.classify_calls) == 1
    assert len(guard.judge_calls) == 1


def test_no_state_between_requests():
    guard = FakeGuard("jailbreak_injection")
    state = build(guard)
    assert run(state)["action"] == "refuse"
    guard.category = "normal"
    assert run(state)["action"] == "answer"  # 앞 요청의 classify 결과가 남지 않는다
