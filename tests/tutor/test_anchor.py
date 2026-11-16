"""단원 닻(TU-03, `toggles.anchor_unit`, #201 A-6 U): 상위 k에서 앞 대화 단원의 섹션을 앞으로.

검색은 main의 실제 KB 색인을 쓰고, 검색 횟수는 `KbIndex.search` 호출로 센다. LLM은 가짜다.
"""

import asyncio
import types

import pytest

from tutor.judge.conversation import tidy
from tutor.judge.draft import draft_answer
from tutor.judge.main_call import MainCallPrompt
from tutor.retrieval import KbIndex, anchor_first, default_index, find_anchor, retrieve
from tutor.retrieval.anchor import unit_of

TABLE = {"normal": ["answer"]}
QUESTION = "꿈틀 중학교 동아리 야구 점수판 예제는 몇 회까지 점수를 기록해?"
EARLIER = ("리스트가 뭐야", "리스트는 여러 값을 순서대로 담는 자료형이에요.")
# QUESTION을 EARLIER의 학습자 발화와 함께 검색한 상위 5와, 닻(PY-03)으로 다시 줄 세운 순서
PLAIN = ["PY-01-01", "PY-03-04", "PY-02-03", "PY-03-03", "PY-01-03"]
ANCHORED = ["PY-03-04", "PY-03-03", "PY-01-01", "PY-02-03", "PY-01-03"]


def u(text: str) -> dict:
    return {"role": "user", "content": text}


def a(text: str) -> dict:
    return {"role": "assistant", "content": text}


def run(coro):
    return asyncio.run(coro)


def ids(sections) -> list[str]:
    return [s.id for s in sections]


def fakes(*sids: str) -> tuple:
    return tuple(types.SimpleNamespace(id=sid) for sid in sids)


class FakeComplete:
    """주 호출 가짜. 근거 묶음만 보면 되므로 실패로 답한다."""

    def __init__(self):
        self.messages: list[list[dict]] = []

    async def __call__(self, messages, schema, *, deadline):
        self.messages.append(messages)
        return types.SimpleNamespace(parsed=None, error="timeout")


@pytest.fixture
def searches(monkeypatch):
    """`KbIndex.search`에 넘어간 질의(호출 순서). 길이가 검색 횟수다."""
    seen: list[str] = []
    real = KbIndex.search

    def spy(self, query, *, subject=None):
        seen.append(query)
        return real(self, query, subject=subject)

    monkeypatch.setattr(KbIndex, "search", spy)
    return seen


def draft(messages, *, anchor_unit, k=5, **kw):
    fake = FakeComplete()
    d = run(
        draft_answer(
            tidy(messages),
            prompt=MainCallPrompt.load(TABLE),
            complete=fake,
            deadline=1e9,
            k=k,
            index=default_index(),
            anchor_unit=anchor_unit,
            **kw,
        )
    )
    return d, fake


def bundle_order(fake: FakeComplete) -> list[str]:
    """주 호출에 넘긴 근거 묶음의 섹션 ID(나온 순서)."""
    body = fake.messages[0][1]["content"]
    return sorted(
        (sid for sid in default_index().sections if f'<섹션 id="{sid}">' in body),
        key=lambda sid: body.index(f'<섹션 id="{sid}">'),
    )


# --- 순수 함수 ---


@pytest.mark.parametrize(
    ("sid", "unit"), [("PY-03-02", "PY-03"), ("FIN-02-01", "FIN-02"), ("PR-04-03", "PR-04")]
)
def test_unit_is_the_first_two_parts_of_the_section_id(sid, unit):
    assert unit_of(sid) == unit


def test_anchor_first_moves_the_anchor_unit_forward_and_keeps_relative_order():
    given = fakes(*PLAIN)
    moved = anchor_first(given, "PY-03")
    assert ids(moved) == ANCHORED
    assert sorted(ids(moved)) == sorted(PLAIN)  # 순서만 바뀌고 섹션 집합은 같다
    assert ids(given) == PLAIN  # 입력을 바꾸지 않는다


@pytest.mark.parametrize("unit", [None, "FIN-01"], ids=["no_anchor", "unit_not_in_top_k"])
def test_anchor_first_keeps_the_order_otherwise(unit):
    assert ids(anchor_first(fakes(*PLAIN), unit)) == PLAIN


# --- 앞 대화(Conversation.anchor_turns) ---


def test_anchor_turns_drop_the_last_learner_utterance_and_keep_the_last_four():
    conv = tidy([u("u1"), a("a1"), u("u2"), a("a2"), u("u3"), a("a3"), u("질문")])
    assert conv.anchor_turns() == ("u2", "a2", "u3", "a3")


@pytest.mark.parametrize(
    "messages",
    [[], [u("질문")], [a("튜터만")]],
    ids=["empty", "single_turn", "no_learner"],
)
def test_anchor_turns_are_empty_without_earlier_turns(messages):
    assert tidy(messages).anchor_turns() == ()


def test_anchor_turns_skip_unknown_roles_and_turns_after_the_target():
    messages = [u("u1"), {"role": "system", "content": "s"}, a("a1"), u("질문"), a("뒤 발화")]
    assert tidy(messages).anchor_turns() == ("u1", "a1")


def test_anchor_turns_skip_the_button_phrase_like_the_main_search():
    from tutor.judge.conversation import is_button_phrase

    button = "자료를 요약해 줘"
    assert is_button_phrase(button)
    conv = tidy([u(button), a("a1"), u("u2"), a("a2"), u("질문")])
    assert conv.anchor_turns() == ("a1", "u2", "a2")


# --- 닻 찾기(find_anchor) ---


def test_find_anchor_is_the_unit_of_the_top_section_for_the_earlier_turns():
    assert ids(run(retrieve(EARLIER[-1], EARLIER[:-1], k=1)))[0].startswith("PY-03-")
    assert run(find_anchor(EARLIER)) == "PY-03"


@pytest.mark.parametrize("earlier", [(), ("!",), ("", "🙂")], ids=["none", "no_bigrams", "blank"])
def test_find_anchor_is_none_without_earlier_turns_or_results(earlier):
    assert run(find_anchor(earlier)) is None


# --- 초안 연결(draft_answer) ---


def test_premise_question_ranks_with_the_earlier_learner_turn():
    # 아래 테스트의 전제: 본 검색 1위(PY-01-01)는 닻 단원(PY-03) 밖이고 PY-03 섹션 둘이 상위 5 안
    assert ids(run(retrieve(QUESTION, EARLIER[:1], k=5))) == PLAIN


def test_anchor_off_searches_once_and_keeps_the_retrieval_order(searches):
    d, fake = draft([u(EARLIER[0]), a(EARLIER[1]), u(QUESTION)], anchor_unit=False)
    assert len(searches) == 1  # 닻 검색이 없다
    assert ids(d.sections) == PLAIN == bundle_order(fake)


def test_anchor_on_reorders_with_one_extra_search_over_the_earlier_turns(searches):
    d, fake = draft([u(EARLIER[0]), a(EARLIER[1]), u(QUESTION)], anchor_unit=True)
    assert len(searches) == 2
    assert searches[1] == "\n".join(EARLIER)  # 앞 대화(학습자 + 튜터)만, 판단 대상은 빠진다
    assert ids(d.sections) == ANCHORED == bundle_order(fake)  # 근거 묶음도 닻 순서


@pytest.mark.parametrize("last", [QUESTION, "그럼 딕셔너리는?"])
def test_anchor_query_ignores_the_last_learner_utterance(searches, last):
    draft([u(EARLIER[0]), a(EARLIER[1]), u(last)], anchor_unit=True)
    assert last not in searches[1] and searches[1] == "\n".join(EARLIER)


def test_anchor_on_single_turn_is_unchanged(searches):
    d, _ = draft([u(QUESTION)], anchor_unit=True)
    assert len(searches) == 1
    assert ids(d.sections) == ids(run(retrieve(QUESTION, [], k=5)))


class CountingEmbedding:
    """임베딩 검색 가짜: 부른 횟수를 세고 정해 둔 순위를 돌려준다."""

    def __init__(self):
        self.calls = 0

    async def rerank(self, text, ranked, *, allowed):
        self.calls += 1
        return ["PY-01-01", "PY-03-04", "PY-02-03"]


def test_anchor_search_never_calls_the_embedding(searches):
    # 임베딩을 켜도 닻 질의는 BM25만 쓴다(임베딩 API를 한 번 더 부르지 않는다)
    emb = CountingEmbedding()
    messages = [u("What is a list?"), a("A list keeps values in order."), u("How do I add one?")]
    d, _ = draft(messages, anchor_unit=True, embedding=emb)
    assert len(searches) == 2  # 닻 검색은 했다
    assert emb.calls == 1  # 본 검색에서만
    assert len(d.sections) == 3
