"""임베딩 혼합 검색(TU-11): RRF, 영어 판별, 기동 때 KB 임베딩, 질의 임베딩 실패 시 BM25 그대로.

운영진 API는 부르지 않는다. 임베딩은 글자 주머니 벡터를 내는 가짜로 바꾼다.
"""

import asyncio
import contextlib
import dataclasses
import importlib
import json
import logging
import math
import sys
from types import SimpleNamespace

import numpy as np
import pytest

from tests.tutor.tutor_testlib import fake_openai
from tutor.retrieval import default_index, retrieve
from tutor.retrieval import embedding as emb
from tutor.retrieval.index import KbIndex
from tutor.retrieval.kb import Section, bigrams
from tutor.runtime.config import ROOT, load_config

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
    assert search.matrix.shape == (len(texts), DIM)
    assert np.allclose(np.linalg.norm(search.matrix, axis=1), 1.0)  # 행마다 단위 벡터


def test_build_rejects_a_reply_with_missing_vectors():
    class Short(FakeEmbed):
        async def __call__(self, texts):
            return (await super().__call__(texts))[:-1]

    with pytest.raises(ValueError):
        build(fake=Short())


def test_build_rejects_batches_whose_counts_only_add_up():
    """묶음마다 수를 본다. 한 묶음이 하나 많고 다음 묶음이 하나 적으면 총수는 맞아도
    벡터가 조각과 어긋난다."""

    class Shifted(FakeEmbed):
        async def __call__(self, texts):
            vectors = await super().__call__(texts)
            return [*vectors, vectors[-1]] if len(self.calls) == 1 else vectors[:-1]

    index = small_index()
    assert math.ceil(len(index.chunks) / 3) == 2  # 묶음 두 개: 하나 많음 + 하나 적음
    with pytest.raises(ValueError):
        build(index, Shifted(), batch=3)


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


@pytest.mark.parametrize("query", [[1.0] * (DIM - 1), [math.nan] * DIM, [0.0] * DIM])
def test_ranking_rejects_a_query_vector_it_cannot_compare(query):
    with pytest.raises(ValueError):  # 차원이 다름·NaN·영벡터(rerank가 받아 BM25로 떨어진다)
        build().ranking(query)


def test_rerank_scores_on_the_loop_without_a_thread(monkeypatch):
    """유사도는 행렬곱 한 번이라 루프에서 바로 한다(순수 파이썬 루프는 스레드에서도 GIL을 쥔다)."""
    search = build()
    monkeypatch.setattr(emb.asyncio, "to_thread", lambda *_a, **_k: pytest.fail("스레드 없음"))
    text = "bank card limit"
    merged = run(search.rerank(text, ["A-01"], allowed=None))
    assert merged == emb.rrf(["A-01"], search.ranking(bag(text)))


def test_query_embedding_shares_the_gate_and_waits_within_its_time_limit():
    """질의 임베딩도 가드 전역 게이트를 함께 쓴다(같은 팀 키, TU-13). 게이트 대기도 시간 제한 안이다."""
    fake = FakeEmbed()
    search = build()

    async def go():
        held = asyncio.Semaphore(1)
        inside = []

        @contextlib.asynccontextmanager
        async def gate():
            async with held:
                inside.append(len(fake.calls))
                yield

        gated = dataclasses.replace(search, embed=fake, gate=gate, timeout_s=0.05)
        async with held:  # 다른 호출이 게이트를 쥐고 있으면 시간 제한 안에 못 얻는다
            blocked = await gated.rerank("bank card", ["B-01"], allowed=None)
        free = await gated.rerank("bank card", ["B-01"], allowed=None)
        return blocked, free, inside

    blocked, free, inside = run(go())
    assert blocked == ["B-01"] and free == emb.rrf(["B-01"], search.ranking(bag("bank card")))
    assert len(fake.calls) == 1 and inside == [0]  # 게이트를 얻은 뒤에만 보낸다


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


@pytest.mark.parametrize(
    "missing, value",
    [
        ("EMBEDDING_API_KEY", None),
        ("EMBEDDING_MODEL", None),
        ("EMBEDDING_BASE_URL", None),  # 주소가 비면 SDK 기본 주소(공개 OpenAI)로 나간다
        ("EMBEDDING_BASE_URL", ""),
        ("EMBEDDING_BASE_URL", "  "),
        ("EMBEDDING_API_KEY", "  "),
    ],
)
def test_openai_embed_needs_a_key_a_model_and_an_address(monkeypatch, missing, value):
    made = []
    monkeypatch.setattr(emb.openai, "AsyncOpenAI", lambda **kwargs: made.append(kwargs))
    monkeypatch.setenv("EMBEDDING_API_KEY", "k")
    monkeypatch.setenv("EMBEDDING_MODEL", "m")
    monkeypatch.setenv("EMBEDDING_BASE_URL", "http://127.0.0.1:9")
    if value is None:
        monkeypatch.delenv(missing)
    else:
        monkeypatch.setenv(missing, value)
    with pytest.raises(emb.openai.OpenAIError):
        run(emb.openai_embed()(["a"]))
    assert made == []  # 클라이언트를 만들지 않는다(아무 데도 보내지 않음)


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
    assert on.tutor_emb.matrix.shape[0] == len(default_index().chunks)
    assert on.tutor_emb.gate is on.tutor_llm.gate  # 튜터 LLM과 같은 게이트(서버는 가드 slot)


def test_startup_uses_the_guard_gate_for_query_embeddings(monkeypatch):
    monkeypatch.setattr(startup, "openai_embed", FakeEmbed)
    slot = contextlib.nullcontext  # 가드 LLM의 slot() 자리
    state = SimpleNamespace(guard=SimpleNamespace(llm=SimpleNamespace(slot=slot, params={})))
    state.guard.llm.structured = "json_schema"
    startup.prepare(state, load_config(overrides={"toggles.embedding": True}))
    run(startup.build_embedding(state))
    assert state.tutor_emb.gate is slot


def test_startup_falls_back_to_bm25_when_the_kb_embedding_fails(monkeypatch, caplog):
    monkeypatch.setattr(startup, "openai_embed", lambda: FakeEmbed(fail=RuntimeError("본문")))
    state = _state(True)
    with caplog.at_level(logging.WARNING, logger="tutor.startup"):
        run(startup.build_embedding(state))
    assert state.tutor_emb is None
    assert "RuntimeError" in caplog.text and "본문" not in caplog.text  # 클래스 이름만


# --- 측정 경로(run_local)도 서버 기동과 같은 임베딩을 만든다(#124 리뷰 ①⑤③) ---


def test_counted_embed_counts_calls_and_failures_including_timeouts():
    from tutor.eval.recall import CountedEmbed

    async def go():
        ok = CountedEmbed(FakeEmbed())
        await ok(["a"])
        bad = CountedEmbed(FakeEmbed(fail=RuntimeError("본문")))
        with pytest.raises(RuntimeError):
            await bad(["a"])
        slow = CountedEmbed(FakeEmbed(delay=1.0))
        with pytest.raises(TimeoutError):
            async with asyncio.timeout(0.01):  # 시간 초과(취소)도 실패로 센다
                await slow(["a"])
        return ok, bad, slow

    ok, bad, slow = run(go())
    assert (ok.calls, ok.failures) == (1, 0)
    assert (bad.calls, bad.failures) == (1, 1) and (slow.calls, slow.failures) == (1, 1)


@pytest.mark.parametrize(
    "reply",
    [
        [],  # 수가 모자람(rerank의 vectors[0]이 IndexError)
        [[1.0] * (DIM - 1)],  # 차원이 처음과 다름
        [[math.nan] * DIM],  # NaN
        [[0.0] * DIM],  # 영벡터
    ],
)
def test_counted_embed_counts_unusable_vectors_as_failures(reply):
    """호출은 성공해도 EmbeddingSearch가 쓰지 못해 BM25로 떨어지는 벡터는 실패로 센다."""
    from tutor.eval.recall import CountedEmbed

    replies = iter([[bag("first")], reply])

    async def embed(_texts):
        return next(replies)

    counted = CountedEmbed(embed)
    run(counted(["first"]))  # 첫 벡터로 차원을 정한다
    run(counted(["second"]))
    assert (counted.calls, counted.failures) == (2, 1)


EMBEDDING_ENV = {
    "EMBEDDING_API_KEY": "k",
    "EMBEDDING_MODEL": "m",
    "EMBEDDING_BASE_URL": "http://127.0.0.1:9",
}
BATCHES = math.ceil(len(default_index().chunks) / emb.BATCH)  # 기동 때 KB 묶음 호출 수
ON = ("--tutor-set", "toggles.embedding=true")
QUERY = "bank card limit"


def test_expected_embedding_calls_are_kb_batches_plus_english_queries_per_run():
    from tutor.eval import run_local
    from tutor.eval.recall import (
        english_queries,
        expected_embedding_calls,
        kb_embedding_batches,
        public_rows,
    )

    rows = public_rows()
    assert kb_embedding_batches() == BATCHES == 4  # KB 조각 244개를 64개씩
    assert expected_embedding_calls(rows, runs=3) == 4 + 3 * english_queries(rows)
    assert expected_embedding_calls(run_local.public_tutor_rows(), runs=1) == 4 + 20  # 영어 20개


class Returns(FakeEmbed):
    """기동 묶음은 제대로 내고, 그 뒤 질의 호출에는 정해 둔 벡터를 낸다."""

    def __init__(self, reply):
        super().__init__()
        self.reply = reply

    async def __call__(self, texts):
        if len(self.calls) < BATCHES:
            return await super().__call__(texts)
        self.calls.append(list(texts))
        return self.reply


def _embedding_on_config(monkeypatch, tmp_path):
    """설정 파일에서 임베딩을 켠다(TUTOR_CONFIG). 정말 켜졌는지 확인한다."""
    text = (ROOT / "config" / "tutor.yaml").read_text(encoding="utf-8")
    config = tmp_path / "tutor.yaml"
    config.write_text(text.replace("embedding: false", "embedding: true"), encoding="utf-8")
    monkeypatch.setenv("TUTOR_CONFIG", str(config))
    assert load_config().toggles.embedding is True


def _run_local(
    monkeypatch,
    tmp_path,
    fake,
    *argv,
    query=None,
    blocked=False,
    env=EMBEDDING_ENV,
    source=("--preset", "tutor"),
):
    """run_local을 부르고 (종료 코드, 측정에 쓴 state)를 돌려준다.

    임베딩과 측정 실행을 가짜로 바꿔 튜터 모드와 상관없이 임베딩 배선만 본다. query가 있으면
    가짜 측정이 실행마다 그 질의로 임베딩 검색을 한 번 쓴다(v2 검색 자리). blocked면 튜터
    호출 하나가 상한에 막힌 것으로 둔다.
    """
    from tutor.eval import run_local

    fake_openai(monkeypatch)  # tutor 프리셋이 요구하는 OPENAI_*(가짜, 보내지 않는다)
    for name in EMBEDDING_ENV:
        if name in env:
            monkeypatch.setenv(name, env[name])
        else:
            monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(run_local, "openai_embed", lambda: fake)
    seen = {}

    def fake_timed(preset, rows, runs, state=None):
        seen["state"] = state
        for _ in range(runs):
            if query is not None and state is not None and state.tutor_emb is not None:
                run(state.tutor_emb.rerank(query, [], allowed=None))
        if blocked and state is not None:
            state.tutor_llm.blocked["main"] += 1
        responses = [{row["id"]: None for row in rows} for _ in range(runs)]
        return responses, [0.0] * (len(rows) * runs)  # 요청마다 걸린 시간(#122)

    monkeypatch.setattr(run_local, "collect_timed", fake_timed)
    code = run_local.main([*source, *argv, "--out", str(tmp_path)])
    return code, seen.get("state")


def _saved(tmp_path):
    return json.loads(next(tmp_path.glob("tutor-*.json")).read_text(encoding="utf-8"))


def test_run_local_builds_and_uses_the_embedding_when_turned_on(monkeypatch, tmp_path, capsys):
    from tutor.eval import run_local

    fake = FakeEmbed()
    code, state = _run_local(monkeypatch, tmp_path, fake, *ON, query=QUERY)
    assert code == 0 and isinstance(state.tutor_emb, emb.EmbeddingSearch)  # 그 state로 잰다
    assert len(fake.calls) == BATCHES + 1  # 기동 묶음 + 질의 1회(실행 1회)
    captured = capsys.readouterr()
    assert "임베딩 예상 호출 24회" in captured.out
    assert (
        f"임베딩 호출 {BATCHES + 1}회(기동 {BATCHES}회 + 질의 1회 / 영어 질의 20회" in captured.out
    )
    assert "영어 질의 20회 중 1회만" in captured.out  # 일부만 써도 실패는 아니고 알린다
    sample = run_local.public_tutor_rows()[0]["messages"][-1]["content"]
    assert QUERY not in captured.out + captured.err and sample not in captured.out  # 본문 없음
    saved = _saved(tmp_path)
    assert saved["tainted"] is False and saved["embedding"]["ok"] is True


def test_run_local_measures_the_real_v2_path_with_query_embeddings(monkeypatch, tmp_path, capsys):
    """측정 경로의 실제 v2 흐름(#97·da8c030)이 기동 때 만든 임베딩으로 영어 질의를 검색한다.

    주 호출은 가짜 전송이 실패로 돌려줘(밖으로 보내지 않는다) 답은 기본값이지만, 검색은 주 호출
    전에 끝난다. 검색까지 간 영어 질의(retrieve와 같은 조건)마다 질의 임베딩이 한 번씩 잡혀야
    한다. 검색 전에 끝나는 행이 생겨도(결정적 판정 등) 이 관계는 그대로다. 자기 state로 잴 때도
    실제 collect_timed가 요청마다 지연을 잰다(#122).
    """
    from tutor.eval import run_local
    from tutor.judge import draft as draft_mod

    searched = []
    real_retrieve = draft_mod.retrieve

    async def watched_retrieve(query, history=(), **kw):
        searched.append((emb.is_english(query) and bool(bigrams(query)), kw.get("embedding")))
        return await real_retrieve(query, history, **kw)

    sent = fake_openai(monkeypatch)  # 주 호출은 보내지 않고 auth 오류(다시 보내지 않는다)
    monkeypatch.setattr(draft_mod, "retrieve", watched_retrieve)
    monkeypatch.delenv("TUTOR_CONFIG", raising=False)  # 개발자 셸의 설정 경로가 새지 않게
    assert load_config().mode == "v2"
    for name, value in EMBEDDING_ENV.items():
        monkeypatch.setenv(name, value)
    fake = FakeEmbed()
    monkeypatch.setattr(run_local, "openai_embed", lambda: fake)
    code = run_local.main(["--preset", "tutor", *ON, "--out", str(tmp_path)])
    assert sent  # v2 주 호출까지 갔다(가짜 전송)
    english = [used for is_english, used in searched if is_english]
    assert english and all(isinstance(used, emb.EmbeddingSearch) for used in english)
    saved = _saved(tmp_path)
    assert len(english) <= saved["embedding"]["queries"]  # 넘으면 임베딩 호출 추정이 모자라다
    assert saved["embedding"]["calls"] == len(english) and saved["embedding"]["failures"] == 0
    assert len(fake.calls) == BATCHES + len(english)
    out = capsys.readouterr().out  # 자기 state 경로에서도 행마다 지연을 잰다
    assert f"(요청 {len(run_local.public_tutor_rows())}개)" in out
    assert all(len(texts) == 1 for texts in fake.calls[BATCHES:])  # 질의마다 한 문장씩
    assert code == 0 and saved["tainted"] is False


def test_run_local_reports_query_failures_over_several_runs(monkeypatch, tmp_path, capsys):
    """질의 임베딩이 실패하면(그 질의는 BM25) 종료 코드 1이고 결과 파일도 tainted다."""
    fake = Returns([[math.nan] * DIM])  # 기동은 되고 질의 벡터는 쓸 수 없다
    code, _state = _run_local(monkeypatch, tmp_path, fake, *ON, "--runs", "2", query=QUERY)
    out = capsys.readouterr().out
    assert code == 1 and len(fake.calls) == BATCHES + 2
    assert "임베딩 예상 호출 44회" in out and "질의 2회" in out and "실패 2회" in out
    assert "BM25로 떨어졌다" in out
    saved = _saved(tmp_path)
    assert saved["tainted"] is True and saved["embedding"]["failures"] == 2


def test_run_local_flags_an_embedding_that_no_query_used(monkeypatch, tmp_path, capsys):
    """켜도 이 모드가 검색에 임베딩을 쓰지 않으면 BM25 수치로 알리고 tainted로 남긴다."""
    code, _state = _run_local(monkeypatch, tmp_path, FakeEmbed(), *ON)
    assert code == 1 and "질의 임베딩이 한 번도 쓰이지 않았다" in capsys.readouterr().out
    assert _saved(tmp_path)["tainted"] is True


def test_run_local_blocked_calls_taint_the_result_even_with_a_good_embedding(
    monkeypatch, tmp_path, capsys
):
    code, state = _run_local(monkeypatch, tmp_path, FakeEmbed(), *ON, query=QUERY, blocked=True)
    saved = _saved(tmp_path)
    assert code == 1 and saved["tainted"] is True and saved["embedding"]["ok"] is True
    out = capsys.readouterr().out  # 튜터 상한에서 임베딩 몫도 뺐다고 알린다
    assert "상한에 막혀 보내지 못한 튜터 호출 1회" in out
    assert f"(튜터 상한 {state.tutor_llm.max_calls}회" in out  # 실제로 건 상한과 같다
    assert f"- 임베딩 몫 {BATCHES + 20})" in out  # 기동 묶음 + 영어 질의 20개
    marks = ["튜터 LLM 호출:", "튜터 요청 지연", "상한에 막혀 보내지 못한", "임베딩 호출 "]
    at = [out.index(mark) for mark in marks]
    assert at == sorted(at)  # 호출 → 지연(#122) → 상한 막힘 → 임베딩 요약 순


def test_run_local_without_the_embedding_neither_builds_nor_counts_it(
    monkeypatch, tmp_path, capsys
):
    fake = FakeEmbed()
    code, state = _run_local(monkeypatch, tmp_path, fake, query=QUERY)
    assert code == 0 and fake.calls == [] and state is None  # 기본 state(tutor.handle)로 잰다
    assert "임베딩" not in capsys.readouterr().out
    saved = _saved(tmp_path)
    assert saved["tainted"] is False and "embedding" not in saved


def test_run_local_other_overrides_alone_do_not_build_the_embedding(monkeypatch, tmp_path, capsys):
    fake = FakeEmbed()
    code, state = _run_local(
        monkeypatch, tmp_path, fake, "--tutor-set", "retrieval.k=3", query=QUERY
    )
    assert code == 0 and state.tutor_emb is None and fake.calls == []  # 자기 state, 임베딩 없음
    assert "임베딩" not in capsys.readouterr().out


def test_run_local_stops_before_measuring_when_the_kb_embedding_fails(
    monkeypatch, tmp_path, capsys
):
    fake = FakeEmbed(fail=RuntimeError("본문"))
    with pytest.raises(SystemExit):  # 켰는데 못 만들면 BM25 수치를 임베딩 수치로 남기지 않는다
        _run_local(monkeypatch, tmp_path, fake, *ON)
    err = capsys.readouterr().err
    assert "KB 임베딩" in err and "BM25" in err and "본문" not in err
    assert not list(tmp_path.iterdir())


def test_run_local_does_not_build_the_embedding_for_the_baseline(monkeypatch, tmp_path, capsys):
    """베이스라인은 정의상 BM25만 쓰므로(TU-14) 켜도 만들지 않고 멈춘다(기동 호출 낭비 방지)."""
    for name, value in (("OPENAI_API_KEY", "k"), ("OPENAI_MODEL", "m")):
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("OPENAI_BASE_URL", "http://127.0.0.1:9")
    fake = FakeEmbed()
    with pytest.raises(SystemExit):
        _run_local(monkeypatch, tmp_path, fake, "--tutor-set", "mode=baseline", *ON)
    assert "베이스라인" in capsys.readouterr().err and fake.calls == []


@pytest.mark.parametrize(
    "missing, value", [*((name, None) for name in EMBEDDING_ENV), ("EMBEDDING_BASE_URL", "  ")]
)
def test_run_local_checks_the_embedding_env_before_building(
    monkeypatch, tmp_path, capsys, missing, value
):
    fake = FakeEmbed()
    env = {k: v for k, v in EMBEDDING_ENV.items() if k != missing}
    if value is not None:
        env[missing] = value  # 공백뿐인 값도 없는 것으로 본다
    with pytest.raises(SystemExit):  # 무엇이 빠졌는지 알리고, 만들기 전에 멈춘다
        _run_local(monkeypatch, tmp_path, fake, *ON, env=env)
    assert missing in capsys.readouterr().err and fake.calls == []


def test_run_local_counts_the_embedding_in_the_call_cap(monkeypatch, tmp_path, capsys):
    from tutor.eval import run_local

    rows = run_local.public_tutor_rows()
    assert (
        run_local.expected_calls(rows, preset="tutor", runs=1, judge=False) == 660
    )  # 3 × 200 × 1.1
    exact = 660 + 24  # 튜터 + 임베딩(기동 4 + 영어 질의 20)
    code, state = _run_local(
        monkeypatch, tmp_path, FakeEmbed(), "--max-calls", str(exact), *ON, query=QUERY
    )
    assert code == 0 and state.tutor_llm.max_calls == exact - 24  # 실행 중 튜터 상한에서도 뺀다
    capsys.readouterr()
    with pytest.raises(SystemExit) as stop:  # 하나 모자라면 시작 전에 멈춘다
        _run_local(monkeypatch, tmp_path, FakeEmbed(), "--max-calls", str(exact - 1), *ON)
    assert stop.value.code == 2 and "상한" in capsys.readouterr().err
    code, state = _run_local(monkeypatch, tmp_path, FakeEmbed(), "--max-calls", str(exact - 1))
    assert code == 0 and state is None  # 끄면 임베딩 몫이 없어 들어간다


def test_run_local_with_saved_responses_never_builds_the_embedding(monkeypatch, tmp_path, capsys):
    """--responses는 튜터를 부르지 않으므로, 설정 파일에서 임베딩을 켜 두어도 만들지 않는다."""
    from tutor.eval import run_local

    _embedding_on_config(monkeypatch, tmp_path)
    saved = tmp_path / "responses.jsonl"
    row_id = run_local.public_tutor_rows()[0]["id"]
    saved.write_text(json.dumps({"id": row_id, "response": None}) + "\n", encoding="utf-8")
    fake = FakeEmbed()
    code, state = _run_local(
        monkeypatch, tmp_path / "runs", fake, env={}, source=("--responses", str(saved))
    )
    assert code == 0 and fake.calls == [] and state is None  # 환경변수도 요구하지 않는다
    out = capsys.readouterr().out
    assert "임베딩" not in out and "튜터 요청 지연" not in out  # 튜터를 부르지 않는다


def test_reference_preset_never_builds_the_embedding(monkeypatch, tmp_path, capsys):
    _embedding_on_config(monkeypatch, tmp_path)
    fake = FakeEmbed()
    code, state = _run_local(
        monkeypatch, tmp_path / "runs", fake, env={}, source=("--preset", "reference")
    )
    assert code == 0 and fake.calls == [] and state is None
    assert "임베딩" not in capsys.readouterr().out


def test_compare_refuses_a_tainted_baseline(tmp_path):
    from tutor.eval import run_local

    path = tmp_path / "old.json"
    data = {"judge": False, "tainted": True, "report": {"points": {}}}
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ValueError, match="tainted"):  # 기본값·BM25가 섞인 결과는 기준이 아니다
        run_local.load_previous(path, judge=False)


@pytest.mark.parametrize(
    "calls, failures, queries, ok, said",
    [
        (20, 0, 20, True, "임베딩 호출 24회(기동 4회 + 질의 20회 / 영어 질의 20회, 실패 0회)"),
        (20, 1, 20, False, "BM25로 떨어졌다"),
        (0, 0, 20, False, "질의 임베딩이 한 번도 쓰이지 않았다"),
        (0, 0, 0, True, "질의 0회"),  # 영어 질의가 없는 행만 재면 질의 임베딩이 없는 게 맞다
        (5, 0, 20, True, "영어 질의 20회 중 5회만"),  # 검색 전에 끝난 행이 있을 수 있다
        (25, 0, 20, True, "예상(20회)보다 많다"),
    ],
)
def test_embedding_summary_judges_failures_and_unused_queries(
    capsys, calls, failures, queries, ok, said
):
    from tutor.eval import run_local

    use = run_local.EmbeddingUse(build=4, calls=calls, failures=failures, queries=queries)
    assert use.ok is ok
    run_local.embedding_summary(use, sys.stdout)
    assert said in capsys.readouterr().out
