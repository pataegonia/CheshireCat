"""검색 → 주 호출 초안(PRD §4 ③·⑤). 검색은 main의 실제 KB 색인(#70)을 쓴다."""

from dataclasses import dataclass

import pytest
from pydantic import BaseModel

from tutor.judge import draft as draft_mod
from tutor.judge.conversation import tidy
from tutor.judge.draft import draft_answer
from tutor.judge.main_call import EvidenceOut, MainCallOut, MainCallPrompt, SentenceOut

TABLE = {"normal": ["answer"], "harmful_content": ["refuse", "redirect"]}


@pytest.fixture
def anyio_backend():
    return "asyncio"


@dataclass
class Result:
    parsed: BaseModel | None
    error: str | None


class FakeComplete:
    def __init__(self, result: Result):
        self.result = result
        self.messages: list[list[dict]] = []

    async def __call__(self, messages, schema, *, deadline):
        self.messages.append(messages)
        return self.result


OUT = MainCallOut(
    category="normal",
    action="answer",
    sentences=[
        SentenceOut(
            text="야구 점수판에는 3회까지의 점수가 기록돼요.",
            kind="claim",
            evidence=[EvidenceOut(sid="PY-01-01", quote="3회까지")],
        )
    ],
)


def u(text: str) -> dict:
    return {"role": "user", "content": text}


def a(text: str) -> dict:
    return {"role": "assistant", "content": text}


def test_search_query_excludes_button_and_target_from_history():
    conv = tidy(
        [
            u("학습자료의 내용을 요약해 주세요."),
            a("네"),
            u("리스트가 뭐야"),
            a("답"),
            u("인덱스는?"),
        ]
    )
    assert conv.search_query() == ("인덱스는?", ("리스트가 뭐야",))


def test_search_query_empty_conversation():
    assert tidy([]).search_query() == ("", ())


@pytest.mark.anyio
async def test_draft_uses_real_retrieval_and_passes_bundle_to_main_call():
    conv = tidy([u("야구 점수판 예제는 몇 회까지 점수를 기록해?")])
    fake = FakeComplete(Result(OUT, None))
    d = await draft_answer(
        conv, prompt=MainCallPrompt.load(TABLE), complete=fake, deadline=1e9, k=3
    )
    assert 1 <= len(d.sections) <= 3
    assert d.out == OUT
    body = fake.messages[0][1]["content"]
    for s in d.sections:  # 검색 순위 순서대로 근거 묶음에 들어간다
        assert f'<섹션 id="{s.id}">' in body
    ids = [s.id for s in d.sections]
    assert [body.index(f'id="{i}"') for i in ids] == sorted(body.index(f'id="{i}"') for i in ids)


@pytest.mark.anyio
async def test_draft_main_call_failure_keeps_sections():
    conv = tidy([u("적금이 뭐야")])
    d = await draft_answer(
        conv,
        prompt=MainCallPrompt.load(TABLE),
        complete=FakeComplete(Result(None, "timeout")),
        deadline=1e9,
        k=5,
    )
    assert d.out is None
    assert d.sections  # IN-04가 남은 claim 0일 때 검색 1위를 쓸 수 있게 넘긴다


@pytest.mark.anyio
async def test_draft_sends_query_and_history(monkeypatch):
    seen = {}

    async def fake_retrieve(query, history=(), *, k=5, **_):
        seen.update(query=query, history=tuple(history), k=k)
        return []

    monkeypatch.setattr(draft_mod, "retrieve", fake_retrieve)
    conv = tidy(
        [
            u("힌트를 알려주세요."),
            a("어떤 문제요?"),
            u("새싹적금 문제요"),
            a("네"),
            u("최소 금액은?"),
        ]
    )
    d = await draft_answer(
        conv,
        prompt=MainCallPrompt.load(TABLE),
        complete=FakeComplete(Result(OUT, None)),
        deadline=1e9,
        k=2,
    )
    assert seen == {"query": "최소 금액은?", "history": ("새싹적금 문제요",), "k": 2}
    assert d.sections == ()


@pytest.mark.anyio
async def test_draft_passes_embedding_only_when_built(monkeypatch):
    # #124 retrieve(…, embedding=)와 맞물린다. 없을 때는 키를 넘기지 않아 지금 retrieve와도 맞다
    seen = []

    async def fake_retrieve(query, history=(), *, k=5, **kw):
        seen.append(kw)
        return []

    monkeypatch.setattr(draft_mod, "retrieve", fake_retrieve)
    conv = tidy([u("What is a list?")])
    kwargs = {
        "prompt": MainCallPrompt.load(TABLE),
        "complete": FakeComplete(Result(OUT, None)),
        "deadline": 1e9,
        "k": 3,
    }
    marker = object()
    await draft_answer(conv, **kwargs)
    await draft_answer(conv, **kwargs, embedding=marker)
    assert seen == [{}, {"embedding": marker}]


@pytest.mark.anyio
async def test_draft_without_embedding_works_with_current_retrieve():
    conv = tidy([u("What is a list?")])
    d = await draft_answer(
        conv,
        prompt=MainCallPrompt.load(TABLE),
        complete=FakeComplete(Result(OUT, None)),
        deadline=1e9,
        k=2,
        embedding=None,
    )
    assert d.out == OUT
