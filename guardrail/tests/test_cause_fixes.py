"""S3.10 원인 수정: 구간 다듬기(span_tidy), 자료 밖 확인(oom_check), 후보 프롬프트 두 개."""

import json

import pytest
from fakes import Ok, Timeout
from test_guard import ANSWER, MSGS, PING, make, req

from guardrail import guard as guard_module
from guardrail.config import load_config, load_prompt
from guardrail.eval.run import calls_per_row
from guardrail.rules import tidy_span
from guardrail.schema import Message, OutputVerdict

pytestmark = pytest.mark.anyio
GROUNDED = {"answer_quote": "", "kb_quote": "", "grounded": True, "refuses": False}


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def no_rule(monkeypatch):
    """규칙 ②가 안 걸린 것으로 둔다(규칙 자체는 test_rules가 본다)."""
    monkeypatch.setattr(guard_module, "literal_rule", lambda *a, **k: None)


# --- 구간 다듬기 ---


@pytest.mark.parametrize(
    ("span", "want"),
    [
        ("① 연필 한 자루 값은 300원이에요.", "연필 한 자루 값은 300원"),
        ("줄 수는 4 이하의 짝수로 적어요. [PY-02-04]", "줄 수는 4 이하의 짝수로 적어요."),
        ("높이는 2층부터 9층까지예요.", "높이는 2층부터 9층까지"),
        ("이자율은 2%이므로", "이자율은 2%"),
        ("2. 기간은 24개월입니다", "기간은 24개월"),
        ("(FIN-01-02) 한도는 3만 원", "한도는 3만 원"),
    ],
)
def test_tidy_span_strips_cites_list_marks_and_copula_after_numbers(span, want):
    assert tidy_span(span) == want


@pytest.mark.parametrize(
    "span",
    [
        "이 글을 읽을 사람은 동네 빵집 주인이에요.",  # 숫자가 없으면 끝을 건드리지 않는다
        "2권과 5권 중에서 골라요",  # 서술격 조사가 아닌 동사
        "1씩 빼고",
        "math.floor(7.5)",
        "x = 'PY-01-01' + 1",  # 코드 문자열 안의 식별자
        "코드 FIN-01-02-03",  # 더 긴 식별자의 일부
    ],
)
def test_tidy_span_leaves_other_spans(span):
    assert tidy_span(span) == span


def test_tidy_span_keeps_the_span_when_nothing_would_remain():
    assert tidy_span("[PY-01-01]") == "[PY-01-01]"
    assert tidy_span("3예요") == "3"


async def test_span_tidy_applies_to_the_quote_span_only_when_on(no_rule):
    answer = "① 연필 한 자루 값은 300원이에요. 더 사면 깎아 줘요."
    quote = "① 연필 한 자루 값은 300원이에요."
    out = {"answer_quote": quote, "kb_quote": "", "grounded": False, "refuses": False}
    off, _ = await make(_responder(output=out), output__span_tidy=False)
    on, _ = await make(_responder(output=out))
    a, b = await off.judge(req(answer)), await on.judge(req(answer))
    assert a.response.output == OutputVerdict(False, (quote,), True)
    assert b.response.output == OutputVerdict(False, ("연필 한 자루 값은 300원",), True)


# --- 자료 밖 확인 ---


class _CountingKb:
    """`rank` 호출만 세고 나머지는 원래 색인에 넘긴다."""

    def __init__(self, kb, calls):
        self._kb, self._calls = kb, calls

    def rank(self, query):
        self._calls.append(query)
        return self._kb.rank(query)

    def __getattr__(self, name):
        return getattr(self._kb, name)


def _role(kw) -> str:
    name = kw["response_format"]["json_schema"]["name"]
    for prefix, role in (("JbCheck", "jb_check"), ("OomCheck", "oom_check"), ("Input", "input")):
        if name.startswith(prefix):
            return role
    return "output"


def _responder(category="normal", oom="not_stated", jb="study", output=GROUNDED):
    def respond(kw):
        if kw["messages"] == PING:
            return Ok()
        role = _role(kw)
        if role == "input":
            return Ok(json.dumps({"evidence": "e", "category": category}))
        if role == "oom_check":
            return oom if not isinstance(oom, str) else _verdict(oom)
        if role == "jb_check":
            return _verdict(jb)
        return Ok(json.dumps(output, ensure_ascii=False))

    return respond


def _verdict(v: str) -> Ok:
    return Ok(json.dumps({"evidence": "e", "verdict": v}))


def _calls(fake, role):
    return [r for r in fake.requests if r["messages"] != PING and _role(r) == role]


async def test_oom_check_can_be_turned_off(no_rule):
    guard, fake = await make(_responder())
    j = await guard.judge(req())
    assert j.response.category == "normal" and j.calls == 2
    assert not _calls(fake, "oom_check")


async def test_not_stated_turns_normal_into_out_of_material_and_keeps_output(no_rule):
    quote = ANSWER[:12]
    out = {"answer_quote": quote, "kb_quote": "", "grounded": False, "refuses": False}
    on, fake = await make(_responder(output=out), toggles__oom_check=True)
    off, _ = await make(_responder(output=out))
    a, b = await on.judge(req()), await off.judge(req())
    assert (a.response.category, b.response.category) == ("out_of_material", "normal")
    assert a.response.output == b.response.output and a.output_tier == b.output_tier
    assert (a.input_source, a.calls, a.errors) == ("nano", 3, ())
    assert [len(_calls(fake, r)) for r in ("input", "oom_check", "output")] == [1, 1, 1]
    block = frozenset(on.cfg.block_categories)
    assert a.response.to_json(block)["input"]["decision"] == "allow"
    ranked = []
    on.kb = _CountingKb(on.kb, ranked)
    assert await on.classify(MSGS) == "normal"  # 튜터 공유 메서드는 부르지 않는다
    assert ranked == []  # 자료 밖 확인용 KB 검색도 하지 않는다
    assert len(_calls(fake, "oom_check")) == 1


@pytest.mark.parametrize("verdict", ["stated", "not_asked"])
async def test_other_verdicts_keep_normal(no_rule, verdict):
    guard, _ = await make(_responder(oom=verdict), toggles__oom_check=True)
    j = await guard.judge(req())
    assert (j.response.category, j.calls, j.errors) == ("normal", 3, ())


@pytest.mark.parametrize("category", ["answer_seeking", "harmful_content", "pii_disclosure"])
async def test_oom_check_runs_only_for_normal(no_rule, category):
    guard, fake = await make(_responder(category), toggles__oom_check=True)
    j = await guard.judge(req())
    assert j.response.category == category and not _calls(fake, "oom_check")


async def test_oom_check_is_skipped_after_the_jailbreak_second_call(no_rule):
    # 탈옥 2차가 study라 normal이 됐어도 더 부르지 않는다(요청당 nano 3회 상한)
    guard, fake = await make(_responder("jailbreak_injection"), toggles__oom_check=True)
    j = await guard.judge(req())
    assert (j.response.category, j.calls) == ("normal", 3)
    assert not _calls(fake, "oom_check")


async def test_oom_check_failure_keeps_normal_and_is_counted(no_rule):
    guard, _ = await make(_responder(oom=Timeout()), toggles__oom_check=True)
    j = await guard.judge(req())
    assert j.response.category == "normal" and "oom_check" in j.errors and j.calls == 3


async def test_self_intro_wins_over_not_stated(no_rule):
    intro = (Message("user", "나는 하늘빛 중학교 1학년 민서야. 이 통장 최대 금액이 얼마야"),)
    guard, _ = await make(_responder(), toggles__oom_check=True)
    assert (await guard.judge(req(messages=intro))).response.category == "pii_disclosure"


async def test_oom_check_request_carries_conversation_target_and_kb(no_rule):
    guard, fake = await make(_responder(), toggles__oom_check=True)
    await guard.judge(req(None, has=False))
    (sent,) = _calls(fake, "oom_check")
    system, user = (m["content"] for m in sent["messages"])
    assert system == load_prompt(guard.cfg, "oom_check")[1]
    assert "<target>" in user and "<conversation>" in user and '<kb id="' in user
    assert "<examples>" not in user


def test_calls_per_row_counts_one_extra_call_at_most():
    on = load_config(overrides={"toggles.oom_check": True, "output.medium_parallel": False})
    neither = load_config(
        overrides={
            "toggles.oom_check": False,
            "toggles.jb_check": False,
            "toggles.request_check": False,
            "toggles.block_recheck": False,
            "output.medium_parallel": False,
        }
    )
    assert calls_per_row(on, True) == 3 and calls_per_row(on, False) == 2
    assert calls_per_row(neither, True) == 2 and calls_per_row(neither, False) == 1


# --- 후보 프롬프트 ---


@pytest.mark.parametrize(
    ("name", "file", "version", "phrase"),
    [
        ("jb_check", "jb_check_en_v2.md", "1.2", "quoted, numbered or pasted material"),
        ("output_judge", "output_judge_en_v2.md", "1.4", "8. Lists and named steps"),
        ("oom_check", "oom_check_en.md", "1.1", "`not_stated`"),
        ("input_judge", "input_judge_en_v3a.md", "2.2", "which category definition"),
        ("input_judge", "input_judge_en_v3.md", "2.3", "in no meaningful order"),
    ],
)
def test_candidate_prompts_load(name, file, version, phrase):
    got, body = load_prompt(load_config(overrides={f"prompts.{name}": file}), name)
    assert got == version and phrase in body and body.startswith("You ")


def test_v3_input_prompt_allows_nearest_first_order():
    # 2.3은 예시 순서를 말하지 않으므로 가장 비슷한 예시를 맨 앞에 둘 수 있다
    over = {"prompts.input_judge": "input_judge_en_v3.md", "input.knn.order": "nearest_first"}
    assert load_config(overrides=over).input.knn.order == "nearest_first"
    with pytest.raises(ValueError, match="nearest_first"):
        load_config(overrides={"input.knn.order": "nearest_first"})


def test_default_config_turns_the_adopted_fixes_on():
    cfg = load_config()
    assert cfg.output.span_tidy and cfg.toggles.oom_check
    assert not load_config(ablation="configs/ablation/no_oom_check.yaml").toggles.oom_check
    assert cfg.prompts["output_judge"] == "output_judge_en_v2.md"
