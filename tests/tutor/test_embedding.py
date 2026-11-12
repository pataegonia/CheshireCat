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

from tutor.retrieval import default_index, retrieve
from tutor.retrieval import embedding as emb
from tutor.retrieval.index import KbIndex
from tutor.retrieval.kb import Section
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


def test_expected_embedding_calls_are_kb_batches_plus_english_queries_per_run():
    from tutor.eval.recall import english_queries, expected_embedding_calls, public_rows

    rows = public_rows()
    batches = math.ceil(len(default_index().chunks) / emb.BATCH)
    assert expected_embedding_calls(rows, runs=3) == batches + 3 * english_queries(rows)


EMBEDDING_ENV = {
    "EMBEDDING_API_KEY": "k",
    "EMBEDDING_MODEL": "m",
    "EMBEDDING_BASE_URL": "http://127.0.0.1:9",
}
BATCHES = math.ceil(len(default_index().chunks) / emb.BATCH)  # 기동 때 KB 묶음 호출 수
ON = ("--tutor-set", "toggles.embedding=true")


def _run_local(
    monkeypatch, tmp_path, fake, *argv, query=None, env=EMBEDDING_ENV, source=("--preset", "tutor")
):
    """run_local을 부르고 (종료 코드, 측정에 쓴 state)를 돌려준다.

    임베딩과 측정 실행을 가짜로 바꿔 튜터 모드와 상관없이 임베딩 배선만 본다. query가 있으면
    가짜 측정이 실행마다 그 질의로 임베딩 검색을 한 번 쓴다(v2 검색 자리).
    """
    from tutor.eval import run_local

    for name in EMBEDDING_ENV:
        if name in env:
            monkeypatch.setenv(name, env[name])
        else:
            monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(run_local, "openai_embed", lambda: fake)
    seen = {}

    def fake_runs(preset, rows, runs, state=None):
        seen["state"] = state
        for _ in range(runs):
            if query is not None and state is not None and state.tutor_emb is not None:
                run(state.tutor_emb.rerank(query, [], allowed=None))
        return [{row["id"]: None for row in rows} for _ in range(runs)]

    monkeypatch.setattr(run_local, "collect_runs", fake_runs)
    code = run_local.main([*source, *argv, "--out", str(tmp_path)])
    return code, seen.get("state")


def test_run_local_builds_and_uses_the_embedding_when_turned_on(monkeypatch, tmp_path, capsys):
    fake = FakeEmbed()
    code, state = _run_local(monkeypatch, tmp_path, fake, *ON, query="bank card limit")
    assert code == 0 and isinstance(state.tutor_emb, emb.EmbeddingSearch)  # 그 state로 잰다
    assert len(fake.calls) == BATCHES + 1  # 기동 묶음 + 질의 1회(실행 1회)
    out = capsys.readouterr().out
    assert "임베딩 예상 호출" in out
    assert f"임베딩 호출 {BATCHES + 1}회(기동 {BATCHES}회 + 질의 1회, 실패 0회)" in out


def test_run_local_flags_an_embedding_that_no_query_used(monkeypatch, tmp_path, capsys):
    """켜도 이 모드가 검색에 임베딩을 쓰지 않으면(베이스라인은 정의상 BM25) BM25 수치로 알린다."""
    code, _state = _run_local(monkeypatch, tmp_path, FakeEmbed(), *ON)
    assert code == 1 and "질의 임베딩이 한 번도 쓰이지 않았다" in capsys.readouterr().out


def test_run_local_without_the_embedding_neither_builds_nor_counts_it(
    monkeypatch, tmp_path, capsys
):
    fake = FakeEmbed()
    code, state = _run_local(monkeypatch, tmp_path, fake, query="bank card limit")
    assert code == 0 and fake.calls == [] and state is None  # 기본 state(tutor.handle)로 잰다
    assert "임베딩" not in capsys.readouterr().out


def test_run_local_stops_before_measuring_when_the_kb_embedding_fails(
    monkeypatch, tmp_path, capsys
):
    fake = FakeEmbed(fail=RuntimeError("본문"))
    with pytest.raises(SystemExit):  # 켰는데 못 만들면 BM25 수치를 임베딩 수치로 남기지 않는다
        _run_local(monkeypatch, tmp_path, fake, *ON)
    assert "BM25" in capsys.readouterr().err and not list(tmp_path.iterdir())


@pytest.mark.parametrize("missing", list(EMBEDDING_ENV))
def test_run_local_checks_the_embedding_env_before_building(monkeypatch, tmp_path, capsys, missing):
    fake = FakeEmbed()
    env = {k: v for k, v in EMBEDDING_ENV.items() if k != missing}
    with pytest.raises(SystemExit):  # 무엇이 빠졌는지 알리고, 만들기 전에 멈춘다
        _run_local(monkeypatch, tmp_path, fake, *ON, env=env)
    assert missing in capsys.readouterr().err and fake.calls == []


def test_run_local_counts_the_embedding_in_the_call_cap(monkeypatch, tmp_path):
    from tutor.eval import run_local
    from tutor.eval.recall import expected_embedding_calls

    rows = run_local.public_tutor_rows()
    tutor_calls = run_local.expected_calls(rows, preset="tutor", runs=1, judge=False)
    cap = ["--max-calls", str(tutor_calls + expected_embedding_calls(rows, runs=1) - 1)]
    assert _run_local(monkeypatch, tmp_path, FakeEmbed(), *cap)[0] == 0  # 끄면 들어간다
    with pytest.raises(SystemExit):  # 켜면 임베딩 몫까지 더해 상한을 넘는다
        _run_local(monkeypatch, tmp_path, FakeEmbed(), *cap, *ON)


def test_run_local_with_saved_responses_never_builds_the_embedding(monkeypatch, tmp_path, capsys):
    """--responses는 튜터를 부르지 않으므로, 설정 파일에서 임베딩을 켜 두어도 만들지 않는다."""
    from tutor.eval import run_local

    text = (ROOT / "config" / "tutor.yaml").read_text(encoding="utf-8")
    config = tmp_path / "tutor.yaml"
    config.write_text(text.replace("embedding: false", "embedding: true"), encoding="utf-8")
    monkeypatch.setenv("TUTOR_CONFIG", str(config))
    saved = tmp_path / "responses.jsonl"
    row_id = run_local.public_tutor_rows()[0]["id"]
    saved.write_text(json.dumps({"id": row_id, "response": None}) + "\n", encoding="utf-8")
    fake = FakeEmbed()
    code, state = _run_local(
        monkeypatch, tmp_path / "runs", fake, source=("--responses", str(saved))
    )
    assert code == 0 and fake.calls == [] and state is None
    assert "임베딩" not in capsys.readouterr().out


@pytest.mark.parametrize(
    "calls, failures, queries, ok, said",
    [
        (5, 0, 20, True, "임베딩 호출 5회(기동 4회 + 질의 1회, 실패 0회)"),
        (5, 1, 20, False, "BM25로 떨어졌다"),
        (4, 0, 20, False, "질의 임베딩이 한 번도 쓰이지 않았다"),
        (4, 0, 0, True, "질의 0회"),  # 영어 질의가 없는 행만 재면 질의 임베딩이 없는 게 맞다
    ],
)
def test_embedding_summary_judges_failures_and_unused_queries(
    capsys, calls, failures, queries, ok, said
):
    from tutor.eval import run_local

    counted = SimpleNamespace(calls=calls, failures=failures)
    assert run_local.embedding_summary(counted, sys.stdout, kb_batches=4, queries=queries) is ok
    out = capsys.readouterr().out
    assert said in out and "본문" not in out
