"""E를 보내는 때(`toggles.oom_schedule`). LLM은 역할별 가짜다(실제 API를 부르지 않는다).

입력 문장은 지어낸 것이다(공개 세트 · KB 문구를 옮기지 않는다).
"""

import asyncio

import pytest
from pydantic import ValidationError

from tests.tutor.test_flow import out, state_with
from tests.tutor.test_oom_gate import ABSTAIN, RoleLLM, run
from tutor.judge import flow, intent, oom_gate
from tutor.runtime import config

SERIAL_ALL = {"toggles.oom_gate": "all", "toggles.intent_override": "serial"}


def after(mode="after_intent", **extra):
    return {**SERIAL_ALL, "toggles.oom_schedule": mode, **extra}


class SlowIntentLLM(RoleLLM):
    """T만 `intent_sleep`초 늦게 답한다."""

    def __init__(self, *a, intent_sleep=0.0, **kw):
        super().__init__(*a, **kw)
        self.intent_sleep = intent_sleep

    async def complete(self, role, messages, schema, **kw):
        if role == intent.ROLE and self.intent_sleep:
            await asyncio.sleep(self.intent_sleep)
        return await super().complete(role, messages, schema, **kw)


def test_unit_test_baseline_is_with_request():
    """튜터 단위 테스트 기준 설정은 동결 토글을 끈 값(conftest)이라 with_request다."""
    assert config.load_config().toggles.oom_schedule == "with_request"


def test_with_request_sends_e_before_t_and_main():
    llm = RoleLLM(out(), found=False, label="학습 질문")
    assert run(state_with(llm, **SERIAL_ALL)) == ABSTAIN
    assert llm.roles[0] == oom_gate.ROLE
    assert llm.roles.count(oom_gate.ROLE) == 1


@pytest.mark.parametrize("mode", ["after_intent", "after_intent_fresh"])
@pytest.mark.parametrize("label,action", [("유해-실행", "refuse"), ("위기", "escalate")])
def test_after_intent_skips_e_when_serial_t_ends_the_request(mode, label, action):
    llm = RoleLLM(out(), found=False, label=label)
    body = run(state_with(llm, **after(mode)))
    assert body["action"] == action
    assert oom_gate.ROLE not in llm.roles
    assert "main" not in llm.roles


@pytest.mark.parametrize("mode", ["after_intent", "after_intent_fresh"])
def test_after_intent_sends_e_after_t_and_before_main(mode):
    llm = RoleLLM(out(), found=False, label="학습 질문")
    assert run(state_with(llm, **after(mode))) == ABSTAIN
    assert llm.roles == [intent.ROLE, oom_gate.ROLE, "main"]


def test_after_intent_keeps_e_when_t_fails():
    llm = RoleLLM(out(), found=False, label=None)  # T 응답 없음(실패)
    assert run(state_with(llm, **after())) == ABSTAIN
    assert llm.roles.count(oom_gate.ROLE) == 1


@pytest.mark.parametrize("found,expected", [(True, "answer"), (None, "answer"), (False, "abstain")])
def test_after_intent_applies_e_only_like_with_request(found, expected):
    for mode in ("with_request", "after_intent"):
        llm = RoleLLM(out(), found=found, label="학습 질문")
        assert run(state_with(llm, **after(mode)))["action"] == expected


def test_after_intent_keeps_e_for_jailbreak_label_without_override_signal():
    """신호 없는 탈옥 이름표는 T가 덮지 않아 주 호출로 간다. 그 요청의 E는 그대로 부른다."""
    llm = RoleLLM(out(), found=False, label="탈옥")
    assert run(state_with(llm, **after())) == ABSTAIN
    assert llm.roles.count(oom_gate.ROLE) == 1


def test_after_intent_still_calls_e_for_serial_redirect_labels_that_ask_main():
    llm = RoleLLM(out(), found=False, label="제출물 대행")
    body = run(state_with(llm, **after()))
    assert llm.roles.count(oom_gate.ROLE) == 1
    assert body["action"] != "answer"  # T 대행 이름표는 그대로 덮고 E는 answer에만 쓴다


def test_after_intent_counts_e_deadline_from_request_start():
    """T가 E 마감(요청 시작부터)을 다 쓰면 E를 보내지 않는다(no_time)."""
    llm = SlowIntentLLM(out(), found=False, label="학습 질문", intent_sleep=0.2)
    state = state_with(llm, **after(**{"llm.timeout_s.oom_gate": 0.1}))
    assert run(state)["action"] == "answer"
    assert oom_gate.ROLE not in llm.roles
    assert state.oom_gate_counts["no_time"] == 1


def test_after_intent_fresh_counts_e_deadline_from_e_start():
    llm = SlowIntentLLM(out(), found=False, label="학습 질문", intent_sleep=0.2)
    state = state_with(llm, **after("after_intent_fresh", **{"llm.timeout_s.oom_gate": 0.1}))
    assert run(state) == ABSTAIN
    assert llm.kwargs[oom_gate.ROLE]["timeout_s"] == pytest.approx(0.1, abs=0.02)


def test_after_intent_fresh_never_passes_the_request_deadline():
    llm = SlowIntentLLM(out(), found=False, label="학습 질문", intent_sleep=0.05)
    state = state_with(llm, **after("after_intent_fresh"))
    run(state, seconds=0.3)
    limit = llm.kwargs[oom_gate.ROLE]["timeout_s"]
    assert 0 < limit <= 0.3


def test_after_intent_cancels_e_when_the_flow_raises(monkeypatch):
    llm = RoleLLM(out(), found=False, label="학습 질문", gate_sleep=5.0)

    async def boom(*_a, **_k):
        await asyncio.sleep(0.01)
        raise RuntimeError("main boom")

    monkeypatch.setattr(flow, "draft_answer", boom)
    with pytest.raises(RuntimeError):
        run(state_with(llm, **after()))
    assert llm.gate_cancelled


@pytest.mark.parametrize("mode", ["with_request", "after_intent", "after_intent_fresh"])
def test_nano_calls_stay_within_the_budget(mode):
    llm = RoleLLM(out(), found=True, label="학습 질문")
    run(state_with(llm, **after(mode)))
    assert len(llm.roles) <= 4
    assert llm.roles.count(oom_gate.ROLE) == 1


@pytest.mark.parametrize("value", ["later", True, "after"])
def test_invalid_schedule_fails_at_load(value):
    with pytest.raises(ValidationError):
        config.load_config(overrides={"toggles.oom_schedule": value})
