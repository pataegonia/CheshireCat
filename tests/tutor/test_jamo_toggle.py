"""가드 설정 `input.jamo_profanity_not_low`(실험, exp/guard-jamo-longinput)가 튜터에 닿는 길.

- 기본(끔)은 지금과 같다: direct_clarify 값, T 프롬프트(intent.md), 응답.
- 켜면 자모 욕설 초성이 든 발화는 LLM 없이 clarify로 끝내지 않고 주 호출로 간다. T 프롬프트는
  저신호 정의에 한 줄을 더한 복사본(intent_jamo.md)이다.
테스트 문장은 직접 지었다.
"""

import importlib
import types

import pytest

from tests.tutor.test_flow import FakeLLM, ask, out, state_with
from tutor.judge import flow, intent
from tutor.judge.conversation import tidy
from tutor.judge.signals import direct_clarify
from tutor.runtime.config import load_config

startup = importlib.import_module("tutor.startup")  # tutor.startup은 같은 이름의 함수도 내보낸다
HISTORY = [
    {"role": "user", "content": "반복문이 뭐예요?"},
    {"role": "assistant", "content": "같은 일을 여러 번 하는 거예요."},
]
SWEAR = ["ㅅㅂ", "ㅂㅅ", "ㄲㅈ", "ㅄ", "ㅅ ㅂ", "ㅅㅂ!!"]
PLAIN = ["ㅠㅠ", "ㅋㅋ", "ㅇㅇ", "ㅁㄴㅇㄹ"]


def conv(text):
    return tidy([*HISTORY, {"role": "user", "content": text}])


@pytest.mark.parametrize("text", SWEAR)
def test_swear_jamo_goes_past_direct_clarify_only_when_on(text):
    assert direct_clarify(conv(text)) is True
    assert direct_clarify(conv(text), jamo_profanity_not_low=False) is True
    assert direct_clarify(conv(text), jamo_profanity_not_low=True) is False


@pytest.mark.parametrize("text", PLAIN)
def test_other_jamo_is_unchanged(text):
    assert direct_clarify(conv(text)) is direct_clarify(conv(text), jamo_profanity_not_low=True)
    assert direct_clarify(conv(text)) is True


def test_flow_default_keeps_the_clarify_fixed_sentence():
    llm = FakeLLM(parsed=out())
    texts = ["반복문이 뭐예요?", "같은 일을 여러 번 하는 거예요.", "ㅅㅂ"]
    body = ask(state_with(llm), *texts)
    assert body["action"] == "clarify" and llm.calls == []


def test_flow_on_sends_swear_jamo_to_the_main_call():
    llm = FakeLLM(parsed=out(category="harmful_content", action="redirect", sentences=[]))
    state = state_with(llm)
    state.tutor_jamo_not_low = True
    ask(state, "반복문이 뭐예요?", "같은 일을 여러 번 하는 거예요.", "ㅅㅂ")
    assert [c["role"] for c in llm.calls] == ["main"]


def test_prepare_reads_the_guard_toggle_default_off(monkeypatch):
    monkeypatch.delenv("GUARD_OVERLAY", raising=False)
    state = types.SimpleNamespace()
    startup.prepare(state)
    assert state.tutor_jamo_not_low is False


def test_prepare_follows_the_overlay(monkeypatch):
    from guardrail.config import ROOT as GUARD_ROOT

    monkeypatch.setenv("GUARD_OVERLAY", str(GUARD_ROOT / "configs" / "exp-jamo.yaml"))
    state = types.SimpleNamespace()
    startup.prepare(state)
    assert state.tutor_jamo_not_low is True


@pytest.mark.parametrize("on, path", [(False, intent.PROMPT_PATH), (True, intent.JAMO_PROMPT_PATH)])
def test_intent_prompt_follows_the_toggle(on, path):
    state = types.SimpleNamespace(
        tutor_cfg=load_config(overrides={"toggles.intent_override": "serial"}),
        tutor_kb=state_with(FakeLLM()).tutor_kb,
        tutor_jamo_not_low=on,
    )
    flow.prepare_judge(state)
    assert state.tutor_intent_prompt == intent.load_system(path)


def test_intent_jamo_is_the_original_plus_one_sentence():
    base = intent.load_system(intent.PROMPT_PATH)
    jamo = intent.load_system(intent.JAMO_PROMPT_PATH)
    added = " 자모로만 쓴 욕설·모욕(ㅅㅂ, ㅂㅅ, ㄲㅈ 같은 것)은 저신호가 아니라 유해-비하다."
    assert added not in base
    assert jamo == base.replace("때도 저신호다.\n", "때도 저신호다." + added + "\n", 1)
