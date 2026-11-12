"""임베딩 혼합 검색(TU-11): RRF, 영어 판별, 기동 때 KB 임베딩, 질의 임베딩 실패 시 BM25 그대로.

운영진 API는 부르지 않는다. 임베딩은 글자 주머니 벡터를 내는 가짜로 바꾼다.
"""

import asyncio
import dataclasses
import importlib
import logging
import math
from types import SimpleNamespace

import pytest

from tutor.retrieval import default_index, retrieve
from tutor.retrieval import embedding as emb
from tutor.retrieval.index import KbIndex
from tutor.retrieval.kb import Section
from tutor.runtime.config import load_config

DIM = 64
startup = importlib.import_module("tutor.startup")  # 패키지가 같은 이름의 함수 startup을 내보낸다


def bag(text):  # 테스트용 가짜 임베딩: 같은 글자가 많을수록 가깝다
    vec = [0.0] * DIM
    for ch in text.lower():
        if ch.isalnum():
            vec[ord(ch) % DIM] += 1.0
    return vec


class FakeEmbed:
    def __init__(self, *, fail=None, delay=0.0):
        self.calls = []
        self.fail = fail
        self.delay = delay

    async def __call__(self, texts):
        self.calls.append(list(texts))
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.fail is not None:
            raise self.fail
        return [bag(t) for t in texts]


def run(coro):
    return asyncio.run(coro)


def small_index():
    sections = {
        "A-01": Section("A-01", "a", "", "# A-01 apple\n## basket\napple basket fruit count"),
        "A-02": Section("A-02", "a", "", "# A-02 baseball\n## board\nbaseball score board"),
        "B-01": Section("B-01", "b", "", "# B-01 bank\n## card\nbank check card limit"),
    }
    return KbIndex(sections)


def build(index=None, fake=None, **kw):
    index = index or small_index()
    fake = fake or FakeEmbed()
    return run(emb.EmbeddingSearch.build(index, fake, timeout_s=kw.pop("timeout_s", 1.0), **kw))


def test_rrf_adds_reciprocal_ranks():
    # a: 1/61 + 1/62, c: 1/63 + 1/61, b: 1/62
    assert emb.rrf(["a", "b", "c"], ["c", "a"]) == ["a", "c", "b"]


@pytest.mark.parametrize(
    "text, english",
    [
        ("Explain what a variable is in Python", True),
        ("파이썬 변수 print 함수", False),
        ("Squirrel Teens 카드 rules", True),
        ("", False),
        ("123 !!", False),
    ],
)
def test_english_means_mostly_latin_letters(text, english):
    assert emb.is_english(text) is english


def test_build_embeds_every_chunk_once_in_batches():
    index, fake = small_index(), FakeEmbed()
    search = build(index, fake, batch=2)
    texts = [text for _sid, text in index.chunks]
    assert len(fake.calls) == math.ceil(len(texts) / 2)
    assert [t for call in fake.calls for t in call] == texts
    assert search.chunk_ids == tuple(sid for sid, _text in index.chunks)
    assert all(math.isclose(sum(x * x for x in v), 1.0) for v in search.vectors)  # 단위 벡터


def test_build_rejects_a_reply_with_missing_vectors():
    class Short(FakeEmbed):
        async def __call__(self, texts):
            return (await super().__call__(texts))[:-1]

    with pytest.raises(ValueError):
        build(fake=Short())


@pytest.mark.parametrize(
    "broken",
    [
        lambda vecs: [vecs[0][:-1], *vecs[1:]],  # 차원이 다른 벡터
        lambda vecs: [[math.nan] * DIM, *vecs[1:]],  # NaN
        lambda vecs: [[0.0] * DIM, *vecs[1:]],  # 영벡터
    ],
)
def test_build_rejects_vectors_it_cannot_compare(broken):
    class Bad(FakeEmbed):
        async def __call__(self, texts):
            return broken(await super().__call__(texts))

    with pytest.raises(ValueError):  # 기동 때 한 번 잡는다(요청마다 조용히 BM25로 떨어지지 않게)
        build(fake=Bad())


def test_ranking_folds_chunks_to_the_best_section():
    search = build()
    assert search.ranking(bag("baseball score board"))[0] == "A-02"


def test_rerank_mixes_the_query_embedding_with_bm25():
    fake = FakeEmbed()
    search = build(fake=fake)
    text = "bank card limit"
    merged = run(search.rerank(text, ["A-01"], allowed=None))
    assert merged == emb.rrf(["A-01"], search.ranking(bag(text)))
    assert fake.calls[-1] == [text]


def test_rerank_keeps_bm25_when_the_query_embedding_fails_or_is_slow(caplog):
    search = build()
    failing = dataclasses.replace(search, embed=FakeEmbed(fail=RuntimeError("본문")))
    with caplog.at_level(logging.WARNING, logger="tutor.retrieval"):
        assert run(failing.rerank("bank card", ["A-01", "B-01"], allowed=None)) == ["A-01", "B-01"]
    slow = dataclasses.replace(search, embed=FakeEmbed(delay=0.5), timeout_s=0.01)
    assert run(slow.rerank("bank card", ["A-01"], allowed=None)) == ["A-01"]
    # 왜 BM25로 떨어졌는지 클래스 이름만 남는다(본문·메시지 없음, TU-15)
    assert "RuntimeError" in caplog.text and "본문" not in caplog.text


def test_rerank_keeps_bm25_for_a_zero_query_vector():
    class Zero(FakeEmbed):
        async def __call__(self, texts):
            return [[0.0] * DIM for _ in texts]

    zero = dataclasses.replace(build(), embed=Zero())
    assert run(zero.rerank("bank card", ["B-01"], allowed=None)) == ["B-01"]


def test_query_embedding_is_not_kept_between_requests():  # TU-18
    fake = FakeEmbed()
    search = build(fake=fake)
    before = len(fake.calls)
    run(search.rerank("bank card", [], allowed=None))
    run(search.rerank("bank card", [], allowed=None))
    assert len(fake.calls) == before + 2


def test_rerank_drops_sections_outside_the_allowed_set():
    search = build()
    merged = run(search.rerank("bank card limit", [], allowed={"A-01", "A-02"}))
    assert set(merged) <= {"A-01", "A-02"}


@pytest.fixture(scope="module")
def kb_search():
    return asyncio.run(emb.EmbeddingSearch.build(default_index(), FakeEmbed(), timeout_s=1.0))


def test_retrieve_embeds_only_english_queries(kb_search):
    fake = FakeEmbed()
    search = dataclasses.replace(kb_search, embed=fake)
    assert len(run(retrieve("Squirrel Teens check card rules", [], embedding=search))) == 5
    assert len(fake.calls) == 1
    run(retrieve("체크카드 사용 규칙", [], embedding=search))
    run(retrieve("a", [], embedding=search))  # 2-gram이 없는 질의는 임베딩도 부르지 않는다
    assert len(fake.calls) == 1


def test_retrieve_with_embedding_keeps_the_subject_filter(kb_search):
    found = run(retrieve("bank check card", [], subject="finance", k=8, embedding=kb_search))
    assert found and all(section.subject == "finance" for section in found)


def test_openai_embed_reads_the_env_and_orders_by_index(monkeypatch):
    seen = {}

    class FakeClient:
        def __init__(self, **kwargs):
            seen["init"] = kwargs
            self.embeddings = SimpleNamespace(create=self.create)

        async def create(self, *, model, input):
            seen["create"] = (model, input)
            data = [SimpleNamespace(index=i, embedding=[float(i)]) for i in range(len(input))]
            return SimpleNamespace(data=list(reversed(data)))

    monkeypatch.setattr(emb.openai, "AsyncOpenAI", FakeClient)
    monkeypatch.setenv("EMBEDDING_API_KEY", "k")
    monkeypatch.setenv("EMBEDDING_MODEL", "text-embedding-3-small")
    monkeypatch.setenv("EMBEDDING_BASE_URL", "http://localhost/v1")
    embed = emb.openai_embed()
    assert run(embed(["a", "b"])) == [[0.0], [1.0]]
    assert seen["create"] == ("text-embedding-3-small", ["a", "b"])
    assert seen["init"]["max_retries"] == 0 and seen["init"]["base_url"] == "http://localhost/v1"
    first = seen["init"]
    run(embed(["c"]))  # 다른 이벤트 루프에서는 클라이언트를 새로 만든다(연결이 루프에 묶임)
    assert seen["init"] is not first


def test_openai_embed_needs_a_key_and_a_model(monkeypatch):
    monkeypatch.delenv("EMBEDDING_API_KEY", raising=False)
    monkeypatch.setenv("EMBEDDING_MODEL", "m")
    with pytest.raises(emb.openai.OpenAIError):
        run(emb.openai_embed()(["a"]))
    monkeypatch.setenv("EMBEDDING_API_KEY", "k")
    monkeypatch.delenv("EMBEDDING_MODEL", raising=False)
    with pytest.raises(emb.openai.OpenAIError):
        run(emb.openai_embed()(["a"]))


def _state(embedding_on):
    state = SimpleNamespace()
    startup.prepare(state, load_config(overrides={"toggles.embedding": embedding_on}))
    return state


def test_startup_builds_the_kb_embedding_only_when_turned_on(monkeypatch):
    monkeypatch.setattr(startup, "openai_embed", FakeEmbed)
    off = _state(False)
    run(startup.build_embedding(off))
    assert off.tutor_emb is None
    on = _state(True)
    run(startup.build_embedding(on))
    assert isinstance(on.tutor_emb, emb.EmbeddingSearch)
    assert len(on.tutor_emb.vectors) == len(default_index().chunks)


def test_startup_falls_back_to_bm25_when_the_kb_embedding_fails(monkeypatch, caplog):
    monkeypatch.setattr(startup, "openai_embed", lambda: FakeEmbed(fail=RuntimeError("본문")))
    state = _state(True)
    with caplog.at_level(logging.WARNING, logger="tutor.startup"):
        run(startup.build_embedding(state))
    assert state.tutor_emb is None
    assert "RuntimeError" in caplog.text and "본문" not in caplog.text  # 클래스 이름만
