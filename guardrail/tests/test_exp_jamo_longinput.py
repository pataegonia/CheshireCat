"""실험 exp/guard-jamo-longinput: 자모 욕설 저신호 제외 · 입력 프롬프트 복사본 · 긴 입력 · topdown ·
입력 판정 medium · GUARD_OVERLAY 덮개. 모두 기본 끔이고 기본 설정은 지금과 같다. LLM은 가짜다.

테스트 문장은 팀이 직접 쓴 것이다(공개 세트 문구 없음).
"""

import json
from pathlib import Path

import pytest
from fakes import Ok, ServerError
from test_guard import DEFAULT, PING, calls, make, req

from guardrail.config import config_hash, load_config
from guardrail.judges.input_judge import build_messages
from guardrail.preprocess import clip
from guardrail.schema import Message, decision_of
from guardrail.signals import JAMO_PROFANITY, has_jamo_profanity, is_low_signal

pytestmark = pytest.mark.anyio

ROOT = Path(__file__).parents[1]
PROMPTS = ROOT / "prompts"
CONFIGS = ROOT / "configs"
OVERLAYS = ("exp-jamo", "exp-long", "exp-perf", "exp-all")
HISTORY = (
    Message("user", "반복문이 뭐예요?"),
    Message("assistant", "같은 일을 여러 번 하는 거예요."),
)
SWEAR = ["ㅅㅂ", "ㅂㅅ", "ㄲㅈ", "ㅄ", "ㅅ ㅂ", "ㅅㅂ!!"]
PLAIN = ["ㅠㅠ", "ㅋㅋ", "ㅇㅇ", "ㅁㄴㅇㄹ"]
MEDIUM = {"reasoning_effort": "medium", "max_completion_tokens": 6000, "read_s": 14, "stream": True}


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _low(text: str, *, on: bool, history: bool = True) -> bool:
    """가드 `_prepare`와 같은 규칙: 저신호면서 토글이 켜졌고 욕설 초성이 들면 저신호가 아니다."""
    low = is_low_signal(text, history, "")
    return False if low and on and has_jamo_profanity(text) else low


# --- 1. 자모 욕설 ---


@pytest.mark.parametrize("text", SWEAR)
def test_swear_jamo_is_not_low_signal_only_when_the_toggle_is_on(text):
    assert is_low_signal(text, True, "") is True  # 함수 자체(끔)는 지금 그대로
    assert _low(text, on=False) is True
    assert _low(text, on=True) is False


@pytest.mark.parametrize("text", PLAIN)
def test_other_jamo_stays_low_signal_either_way(text):
    assert _low(text, on=False) is _low(text, on=True) is True


@pytest.mark.parametrize("text", SWEAR + PLAIN)
def test_first_turn_is_never_low_signal_either_way(text):
    assert _low(text, on=False, history=False) is _low(text, on=True, history=False) is False


def test_profanity_list_is_one_constant():
    assert set(JAMO_PROFANITY) == {"ㅅㅂ", "ㅆㅂ", "ㅄ", "ㅂㅅ", "ㄲㅈ", "ㅈㄹ", "ㅗ"}
    assert has_jamo_profanity("ﾵﾲ") is True  # 반각 ㅅㅂ도 호환 자모로 접어 본다
    assert has_jamo_profanity("ㅇ ㅅ ㅂ ㅇ") is True


async def test_guard_sends_swear_jamo_to_nano_only_when_on():
    msgs = (*HISTORY, Message("user", "ㅅㅂ"))
    guard, fake = await make()
    off = await guard.judge(req(None, msgs, has=False))
    assert off.input_source == "low_signal" and not calls(fake, "input")
    guard, fake = await make(input__jamo_profanity_not_low=True)
    on = await guard.judge(req(None, msgs, has=False))
    assert on.input_source == "nano" and len(calls(fake, "input")) == 1
    plain = await guard.judge(req(None, (*HISTORY, Message("user", "ㅠㅠ")), has=False))
    assert plain.input_source == "low_signal"


# --- 2. 프롬프트 복사본 ---


def _read(name: str) -> str:
    return (PROMPTS / name).read_text(encoding="utf-8").replace("\r\n", "\n")


def test_jamo_prompt_is_the_original_plus_one_line_and_a_new_version():
    base, jamo = _read("input_judge_en.md"), _read("input_judge_en_jamo.md")
    line = (
        " Jamo-only swear words or insults (such as ㅅㅂ, ㅂㅅ, ㄲㅈ) are not low_signal_input;"
        " judge them as harmful_content."
    )
    assert 'version: "2.0"' in base and line not in base  # 원본은 그대로
    assert jamo == base.replace('version: "2.0"', 'version: "2.1"').replace(
        "it does not apply.\n\n## How", "it does not apply." + line + "\n\n## How", 1
    )


def test_topdown_prompts_ask_group_then_category():
    for name in ("input_judge_en_topdown.md", "input_judge_en_topdown_jamo.md"):
        text = _read(name)
        assert "`group`" in text and "first the group, then the category" in text
    assert "harmful_content." in _read("input_judge_en_topdown_jamo.md").split("## How")[0]


def test_all_prompt_versions_differ():
    versions = set()
    for name in (
        "input_judge_en.md",
        "input_judge_en_jamo.md",
        "input_judge_en_topdown.md",
        "input_judge_en_topdown_jamo.md",
    ):
        versions.add(_read(name).split("\n")[2])
    assert len(versions) == 4


# --- 3. 긴 입력 ---


def test_long_input_keeps_8000_chars_in_the_target_block():
    text = "가" * 3000 + "나" * 3000  # 공백·반복 줄이기에 걸리지 않게 서로 다른 글자로
    text = "".join(f"{i % 10}{ch}" for i, ch in enumerate(text))[:6000]
    assert clip(text, 2000) != text and clip(text, 8000) == text
    base = load_config(DEFAULT)
    long = load_config(DEFAULT, overrides={"input.max_chars": 8000})
    short = build_messages([Message("user", text)], text, [], base)[1]["content"]
    full = build_messages([Message("user", text)], text, [], long)[1]["content"]
    assert len(full) > len(short) and text in full and text not in short


# --- 4a. topdown ---


def _topdown(category="normal", group="study"):
    def respond(kw):
        if kw["messages"] == PING:
            return Ok()
        if kw["response_format"]["json_schema"]["name"].startswith("Input"):
            body = {"evidence": "e", "group": group, "category": category}
            return Ok(json.dumps(body))
        return Ok(
            json.dumps({"answer_quote": "", "kb_quote": "", "grounded": True, "refuses": False})
        )

    return respond


async def test_topdown_sends_the_topdown_schema_and_keeps_the_decision_table():
    guard, fake = await make(
        _topdown("off_topic", "off_course"),
        prompts__input_judge="input_judge_en_topdown.md",
        input__judge_mode="topdown",
    )
    j = await guard.judge(req(None, has=False))
    sent = calls(fake, "input")[0]
    schema = sent["response_format"]["json_schema"]
    assert schema["name"] == "InputTopdownOut"
    assert list(schema["schema"]["properties"]) == ["evidence", "group", "category"]
    block = frozenset(guard.cfg.block_categories)  # 범주 → decision 표는 그대로
    assert j.response.category == "off_topic" and decision_of("off_topic", block) == "block"


def test_topdown_needs_a_topdown_prompt_and_category_labels():
    with pytest.raises(ValueError, match="topdown"):
        load_config(DEFAULT, overrides={"input.judge_mode": "topdown"})
    with pytest.raises(ValueError, match="topdown"):
        load_config(
            DEFAULT,
            overrides={
                "input.judge_mode": "topdown",
                "prompts.input_judge": "input_judge_en_topdown.md",
                "input.label_mode": "act",
            },
        )


# --- 4b. 입력 판정 medium ---


def _medium(**over):
    return {
        "llm__per_role": {"input_judge_medium": MEDIUM},
        "input__reasoning_effort": "medium",
        **over,
    }


async def test_medium_input_uses_the_stream_role_once():
    guard, fake = await make(**_medium())
    j = await guard.judge(req(None, has=False))
    sent = calls(fake, "input")
    assert len(sent) == 1 and j.input_source == "nano"
    kw = sent[0]
    assert kw["reasoning_effort"] == "medium" and kw["max_completion_tokens"] == 6000
    assert kw["stream"] is True
    assert guard.llm.calls == {"input_judge_medium": 1}


async def test_medium_input_failure_falls_back_without_retry():
    from test_guard import responder

    guard, fake = await make(responder(input_step=ServerError()), **_medium())
    j = await guard.judge(req(None, has=False))
    assert len(calls(fake, "input")) == 1  # 재시도 0
    assert j.input_source == "fallback" and j.errors == ("input_judge",)


def test_medium_needs_its_per_role_entry():
    with pytest.raises(ValueError, match="input_judge_medium"):
        load_config(DEFAULT, overrides={"input.reasoning_effort": "medium"})


# --- 5. 덮개 설정 ---


def test_default_config_keeps_current_behaviour():
    cfg = load_config(DEFAULT)
    i = cfg.input
    assert (i.jamo_profanity_not_low, i.judge_mode, i.reasoning_effort, i.max_chars) == (
        False,
        "flat",
        "low",
        2000,
    )
    assert cfg.prompts["input_judge"] == "input_judge_en.md"
    assert set(cfg.llm.per_role) == {"output_judge_medium"}


@pytest.mark.parametrize("base", ["default.yaml", "gate128.yaml"])
def test_overlays_load_on_each_base_with_distinct_versions_and_hashes(base, monkeypatch):
    seen = {}
    monkeypatch.setenv("GUARD_CONFIG", str(CONFIGS / base))
    monkeypatch.delenv("GUARD_OVERLAY", raising=False)
    plain = load_config()
    seen[plain.version] = config_hash(plain)
    for name in OVERLAYS:
        monkeypatch.setenv("GUARD_OVERLAY", str(CONFIGS / f"{name}.yaml"))
        cfg = load_config()
        assert cfg.llm.gate == plain.llm.gate  # 덮개는 gate를 건드리지 않는다
        seen[cfg.version] = config_hash(cfg)
    assert len(seen) == 5 and len(set(seen.values())) == 5


def test_overlay_contents(monkeypatch):
    monkeypatch.delenv("GUARD_CONFIG", raising=False)
    got = {}
    for name in OVERLAYS:
        monkeypatch.setenv("GUARD_OVERLAY", str(CONFIGS / f"{name}.yaml"))
        got[name] = load_config()
    assert got["exp-jamo"].input.jamo_profanity_not_low
    assert got["exp-jamo"].prompts["input_judge"] == "input_judge_en_jamo.md"
    assert got["exp-long"].input.max_chars == 8000
    perf = got["exp-perf"]
    assert (perf.input.judge_mode, perf.input.reasoning_effort) == ("topdown", "medium")
    assert set(perf.llm.per_role) == {"output_judge_medium", "input_judge_medium"}  # 깊은 병합
    a = got["exp-all"].input
    assert (a.jamo_profanity_not_low, a.max_chars, a.judge_mode, a.reasoning_effort) == (
        True,
        8000,
        "topdown",
        "medium",
    )


def test_overlay_is_ignored_when_a_path_is_given(monkeypatch):
    monkeypatch.setenv("GUARD_OVERLAY", str(CONFIGS / "exp-long.yaml"))
    assert load_config(DEFAULT).input.max_chars == 2000
