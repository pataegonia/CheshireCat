"""근거 불일치 답변 재작성(`toggles.repair`, 후보 D). LLM은 역할별 가짜다(실제 API를 부르지 않는다).

입력 문장은 지어낸 것이다. 검색 순위 전제는 test_flow의 cite_top1 테스트와 같다
(QUESTION의 1위 PY-01-01, 2위 PY-03-04).
"""

import asyncio

import pytest
from pydantic import ValidationError

from tests.tutor.test_flow import RANK2_CLAIM, FakeLLM, out, respond, state_with
from tutor.judge import repair
from tutor.judge.conversation import tidy
from tutor.judge.main_call import EvidenceOut, MainCallOut, SentenceOut
from tutor.retrieval import default_index
from tutor.runtime import config
from tutor.runtime.llm import Result

ON = {"toggles.cite_top1": True, "toggles.repair": "top1_mismatch"}
TOP1_CLAIM = SentenceOut(
    text="야구 점수판 예제는 3회까지의 점수를 기록해요.",
    kind="claim",
    evidence=[EvidenceOut(sid="PY-01-01", quote="3회까지의 점수를 기록해요")],
)


def _result(parsed=None, error=None, finish="stop"):
    return Result(parsed=parsed, error=error, attempts=1, latency_ms=1.0, finish_reason=finish,
                  completion_tokens=10)  # fmt: skip


class RoleFake:
    """주 호출은 2위 섹션만 인용한 답, 재작성은 `fixed`(None이면 실패)를 낸다."""

    def __init__(self, fixed=None, *, error=None, finish="stop"):
        self.fixed, self.error, self.finish = fixed, error, finish
        self.roles: list[str] = []
        self.messages: dict[str, list] = {}

    async def complete(self, role, messages, schema, **kw):
        self.roles.append(role)
        self.messages[role] = messages
        if role == repair.ROLE:
            return _result(self.fixed, self.error, self.finish)
        return _result(out(sentences=[RANK2_CLAIM]))


def test_off_never_calls_repair():
    llm = RoleFake(out(sentences=[TOP1_CLAIM]))
    resp = respond(state_with(llm, **{"toggles.cite_top1": True}))
    assert resp["citations"] == ["PY-03-04", "PY-01-01"]
    assert repair.ROLE not in llm.roles


def test_repair_replaces_the_answer_when_top1_was_added():
    llm = RoleFake(out(sentences=[TOP1_CLAIM]))
    resp = respond(state_with(llm, **ON))
    assert llm.roles.count(repair.ROLE) == 1
    assert resp["action"] == "answer"
    assert resp["answer"] == TOP1_CLAIM.text
    assert resp["citations"] == ["PY-01-01"]
    user = llm.messages[repair.ROLE][1]["content"]
    assert '<섹션 추가="예" id="PY-01-01">' in user
    assert "<초안" in user and RANK2_CLAIM.text in user


@pytest.mark.parametrize(
    "fixed,error,finish",
    [
        (None, "timeout", "stop"),  # 호출 실패
        (out(category="answer_seeking", sentences=[TOP1_CLAIM]), None, "stop"),  # 범주 바뀜
        (out(action="scaffold", sentences=[TOP1_CLAIM]), None, "stop"),  # action 바뀜
        (out(sentences=[TOP1_CLAIM]), None, "length"),  # 잘림
        (out(sentences=[]), None, "stop"),  # 빈 답
    ],
    ids=["fail", "category", "action", "length", "empty"],
)
def test_bad_repair_keeps_the_draft_answer(fixed, error, finish):
    llm = RoleFake(fixed, error=error, finish=finish)
    resp = respond(state_with(llm, **ON))
    assert resp["answer"] == RANK2_CLAIM.text
    assert resp["citations"] == ["PY-03-04", "PY-01-01"]


def test_unsupported_repair_keeps_the_draft_answer():
    # 고쳐 쓴 문장의 인용 문구가 원문에 없으면 결정적 수정이 지워 빈 답 안내가 된다 → 초안 유지
    bogus = SentenceOut(
        text="야구 점수판은 9회까지 기록해요.",
        kind="claim",
        evidence=[EvidenceOut(sid="PY-01-01", quote="자료에 없는 문구입니다")],
    )
    llm = RoleFake(out(sentences=[bogus]))
    resp = respond(state_with(llm, **ON))
    assert resp["answer"] == RANK2_CLAIM.text


def test_no_repair_when_the_model_already_cited_the_top_section():
    class TopFake(RoleFake):
        async def complete(self, role, messages, schema, **kw):
            self.roles.append(role)
            return _result(out(sentences=[TOP1_CLAIM]))

    llm = TopFake()
    resp = respond(state_with(llm, **ON))
    assert resp["citations"] == ["PY-01-01"]
    assert repair.ROLE not in llm.roles


def test_no_repair_for_fixed_sentences():
    llm = FakeLLM(out(category="jailbreak_injection", action="refuse"))
    respond(state_with(llm, **ON))
    assert all(role != repair.ROLE for role, *_ in llm.calls)


def test_rewrite_skips_when_too_little_time_is_left():
    llm = RoleFake(out(sentences=[TOP1_CLAIM]))
    conv = tidy([{"role": "user", "content": "점수판 예제 알려 주세요"}])
    sec = default_index().sections["PY-01-01"]

    async def go():
        now = asyncio.get_running_loop().time()
        return await repair.rewrite(
            llm, "s", [sec], sec, out(), conv, deadline=now + repair.MIN_LEFT_S - 0.5, now=now
        )

    assert asyncio.run(go()) is None
    assert llm.roles == []


def test_repair_call_has_no_retry_and_a_capped_timeout():
    seen = {}

    class Spy(RoleFake):
        async def complete(self, role, messages, schema, **kw):
            if role == repair.ROLE:
                seen.update(kw)
            return await super().complete(role, messages, schema, **kw)

    respond(state_with(Spy(out(sentences=[TOP1_CLAIM])), **ON))
    assert seen["retries"] == 0
    assert seen["timeout_s"] <= repair.TIMEOUT_S


def test_prompt_loads_without_the_source_line():
    system = repair.load_system()
    assert not system.startswith("<!--")
    assert "추가=" in system


@pytest.mark.parametrize("value", ["on", True, "all"])
def test_invalid_repair_value_fails_at_load(value):
    with pytest.raises(ValidationError):
        config.load_config(overrides={"toggles.repair": value})


def test_repo_config_keeps_repair_off():
    assert config.load_config().toggles.repair == "off"


def test_schema_is_the_main_call_schema():
    assert MainCallOut.model_fields.keys() == {"category", "action", "sentences"}
