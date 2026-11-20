"""검색 1위로 다시 받기(`toggles.top1_redraft`).

고른 답(answer)이 규칙 A 1위 섹션을 확인된 근거로 쓰지 않았으면, 근거 묶음을 그 섹션 하나로 줄인
주 호출을 한 번 더 받아(재시도 0) 조건이 맞을 때만 그 답·인용으로 바꾼다. LLM은 가짜(API 0회)이고
문장은 지어낸 것이다. 인용 문구는 실제 KB 섹션 본문에서 잘라 쓴다(원문 확인을 거치게).
"""

import asyncio

import pytest

from tests.tutor.test_flow import QUESTION, state_with
from tutor.judge import flow
from tutor.judge.main_call import EvidenceOut, MainCallOut, SentenceOut
from tutor.request import parse_request
from tutor.retrieval import retrieve
from tutor.runtime.config import load_config
from tutor.runtime.llm import Result

RULE_A = {"toggles.cite_top1": True, "toggles.top1_dialog_query": True}
ON = {**RULE_A, "toggles.top1_redraft": True}


def quote_of(section) -> str:
    """섹션 본문에서 원문 확인을 통과할 짧은 문구(굵게·표·코드 표시가 없는 줄의 앞부분)."""
    for line in section.text.splitlines():
        line = line.strip()
        if len(line) >= 12 and not any(c in line for c in "*#|`[]>-"):
            return line[:12]
    raise AssertionError(f"인용 문구를 못 골랐다: {section.id}")


def answer(text: str, section, *, quote: str | None = None, **kw) -> MainCallOut:
    ev = EvidenceOut(sid=section.id, quote=quote_of(section) if quote is None else quote)
    return MainCallOut(
        category=kw.get("category", "normal"),
        action=kw.get("action", "answer"),
        sentences=[SentenceOut(text=text, kind="claim", evidence=[ev])],
    )


class RoleLLM:
    """역할마다 정한 결과를 돌려준다(없으면 실패 결과). 부른 역할과 메시지를 남긴다."""

    def __init__(self, by_role):
        self.by_role = by_role
        self.calls = []

    async def complete(self, role, messages, schema, **kw):
        self.calls.append({"role": role, "messages": messages, **kw})
        parsed = self.by_role.get(role)
        return Result(
            parsed=parsed,
            error=None if parsed is not None else "timeout",
            attempts=1,
            latency_ms=1.0,
            finish_reason="stop",
            completion_tokens=10,
        )


@pytest.fixture
def sections():
    """(근거 묶음, 묶음 밖의 다른 섹션 B). B를 규칙 A 1위로 돌려주게 한다."""
    k = load_config().retrieval.k
    bundle = asyncio.run(retrieve(QUESTION, k=k))
    other = next(
        s
        for s in asyncio.run(retrieve("체크카드 사용 규칙", k=5))
        if s.id not in {b.id for b in bundle}
    )
    return bundle, other


@pytest.fixture
def rule_a_returns(monkeypatch):
    """규칙 A 검색(flow 모듈의 retrieve)만 정한 섹션을 1위로 돌려주게 한다.

    주 호출 근거 묶음은 tutor.judge.draft의 retrieve라 바뀌지 않는다. E(oom_gate)의 근거 검색도
    flow.retrieve를 쓰므로 E를 켠 테스트를 더하면 이 픽스처를 나눠야 한다(지금 기준 설정은 E 끔).
    """

    def use(top):
        async def fake(query, history=(), **kw):
            return [top]

        monkeypatch.setattr(flow, "retrieve", fake)

    return use


def run(state, *, deadline_left: float | None = None):
    body = parse_request({"messages": [{"role": "user", "content": QUESTION}]})

    async def go():
        loop = asyncio.get_running_loop()
        deadline = 1e12 if deadline_left is None else loop.time() + deadline_left
        return await flow.answer_v2(body, state, deadline=deadline)

    return asyncio.run(go())


class RaisingRedraft(RoleLLM):
    """다시 받는 호출(main_r)에서 프로그래밍 오류처럼 예외를 던진다."""

    async def complete(self, role, messages, schema, **kw):
        if role == flow.REDRAFT_ROLE:
            self.calls.append({"role": role})
            raise RuntimeError("redraft broke")
        return await super().complete(role, messages, schema, **kw)


def roles(llm) -> list[str]:
    return [c["role"] for c in llm.calls]


# --- 설정 ---


def test_repo_config_turns_it_on_for_the_submission_check():
    # 측정 판(tutor/t2.38-top1-on): 실제 제출 5대5로 가리려고 저장소 설정에서 켠다
    import yaml

    from tutor.runtime import config as tutor_config

    path = tutor_config.ROOT / tutor_config.DEFAULT_CONFIG
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert raw["toggles"]["top1_redraft"] is True
    assert load_config().toggles.top1_redraft is False  # 단위 테스트 기준 설정에서는 끈다


def test_needs_rule_a_to_load():
    with pytest.raises(ValueError, match="top1_redraft"):
        load_config(overrides={**ON, "toggles.top1_dialog_query": False})


def test_allows_one_extra_call_over_the_four_call_rule():
    # 리더 승인(11/20): 다시 받기 1회만 TU-13(4회)의 예외 → E·직렬 T를 켠 설정에서도 로드된다
    cfg = load_config(
        overrides={**ON, "toggles.oom_gate": "named_term", "toggles.intent_override": "serial"}
    )
    assert cfg.toggles.top1_redraft is True


# --- 흐름 ---


def test_off_never_redrafts(sections, rule_a_returns):
    bundle, other = sections
    rule_a_returns(other)
    llm = RoleLLM({flow.MAIN_ROLE: answer("묶음 섹션으로 답한 문장이에요.", bundle[0])})
    run(state_with(llm, **RULE_A))
    assert roles(llm) == [flow.MAIN_ROLE]


def test_redrafts_with_only_the_top_section_and_uses_it(sections, rule_a_returns):
    bundle, other = sections
    rule_a_returns(other)
    llm = RoleLLM(
        {
            flow.MAIN_ROLE: answer("묶음 섹션으로 답한 문장이에요.", bundle[0]),
            flow.REDRAFT_ROLE: answer("1위 섹션으로 다시 답한 문장이에요.", other),
        }
    )
    resp = run(state_with(llm, **ON))
    assert roles(llm) == [flow.MAIN_ROLE, flow.REDRAFT_ROLE]
    sent = llm.calls[1]
    assert sent["retries"] == 0
    assert sent["deadline"] == pytest.approx(1e12 - flow.REDRAFT_MARGIN_S)  # 요청 마감 − 3초
    user = sent["messages"][-1]["content"]
    assert other.id in user and bundle[0].id not in user  # 근거 묶음은 1위 섹션 하나
    assert "1위 섹션으로 다시 답한 문장이에요." in resp["answer"]
    assert resp["action"] == "answer"
    assert resp["citations"] == [other.id]


def test_keeps_the_answer_that_already_uses_the_top_section(sections, rule_a_returns):
    bundle = sections[0]
    rule_a_returns(bundle[0])
    llm = RoleLLM({flow.MAIN_ROLE: answer("묶음 1위로 답한 문장이에요.", bundle[0])})
    resp = run(state_with(llm, **ON))
    assert roles(llm) == [flow.MAIN_ROLE]
    assert "묶음 1위로 답한 문장이에요." in resp["answer"]


@pytest.mark.parametrize(
    "redraft",
    [
        "fail",
        "abstain",
        "other_category",
        "quote_not_in_top",
    ],
)
def test_keeps_the_first_answer_when_the_redraft_does_not_qualify(
    sections, rule_a_returns, redraft
):
    bundle, other = sections
    rule_a_returns(other)
    outs = {
        "fail": None,
        "abstain": answer("자료에 없어요.", other, action="abstain"),
        "other_category": answer("1위로 답했어요.", other, category="pii_disclosure"),
        "quote_not_in_top": answer("1위로 답했어요.", other, quote="이 문구는 원문에 없다"),
    }
    llm = RoleLLM(
        {
            flow.MAIN_ROLE: answer("묶음 섹션으로 답한 문장이에요.", bundle[0]),
            flow.REDRAFT_ROLE: outs[redraft],
        }
    )
    resp = run(state_with(llm, **ON))
    assert roles(llm) == [flow.MAIN_ROLE, flow.REDRAFT_ROLE]
    assert "묶음 섹션으로 답한 문장이에요." in resp["answer"]


def test_keeps_the_first_answer_when_the_redraft_raises(sections, rule_a_returns):
    bundle, other = sections
    rule_a_returns(other)
    llm = RaisingRedraft({flow.MAIN_ROLE: answer("묶음 섹션으로 답한 문장이에요.", bundle[0])})
    resp = run(state_with(llm, **ON))
    assert roles(llm) == [flow.MAIN_ROLE, flow.REDRAFT_ROLE]
    assert "묶음 섹션으로 답한 문장이에요." in resp["answer"]  # 기본값이 아니라 처음 답
    assert resp["action"] == "answer"


def test_skips_when_too_little_time_is_left(sections, rule_a_returns):
    bundle, other = sections
    rule_a_returns(other)
    llm = RoleLLM(
        {
            flow.MAIN_ROLE: answer("묶음 섹션으로 답한 문장이에요.", bundle[0]),
            flow.REDRAFT_ROLE: answer("1위 섹션으로 다시 답한 문장이에요.", other),
        }
    )
    resp = run(state_with(llm, **ON), deadline_left=flow.REDRAFT_MARGIN_S + 1.0)
    assert roles(llm) == [flow.MAIN_ROLE]
    assert "묶음 섹션으로 답한 문장이에요." in resp["answer"]


def test_only_for_answers(sections, rule_a_returns):
    bundle, other = sections
    rule_a_returns(other)
    scaffold = answer("직접 풀어 보도록 순서를 안내해요.", bundle[0], action="scaffold")
    llm = RoleLLM(
        {
            flow.MAIN_ROLE: scaffold.model_copy(update={"category": "answer_seeking"}),
            flow.REDRAFT_ROLE: answer("1위 섹션으로 다시 답한 문장이에요.", other),
        }
    )
    run(state_with(llm, **ON))
    assert roles(llm) == [flow.MAIN_ROLE]
