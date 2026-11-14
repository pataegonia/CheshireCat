"""KB 색인·검색(TU-03).

R13 참조 구현과 같은 조각·점수, retrieve 계약, 요청 사이 상태 없음(TU-18).
"""

import asyncio
import contextlib
import importlib.util
import logging
import math
import re

import pytest
import yaml
from fastapi.testclient import TestClient
from guardrail import server as guard_server
from guardrail.config import load_config
from guardrail.kb import KbIndex as GuardKbIndex

import tutor
from tests.tutor.tutor_testlib import AUTH, COMPETITION, REPO, answer_openai, make_app, scoring
from tutor.judge import draft as draft_mod
from tutor.retrieval import KbIndex, Section, default_index, retrieve, search
from tutor.retrieval.kb import load_sections, normalize

KB = COMPETITION / "kb"
R13 = REPO / "guardrail" / "scripts" / "r13" / "common.py"
GUARD_DEFAULT = REPO / "guardrail" / "configs" / "default.yaml"
NGRAM_QUERY = "변수에 값을 저장하는 방법"  # 가드 설정 kb.ngram 2와 3에서 검색 1위가 다르다


def use_guard_config(tmp_path, monkeypatch, **changes):
    """가드 기본 설정에서 최상위 항목 몇 개만 바꾼 파일을 GUARD_CONFIG로 쓴다."""
    raw = yaml.safe_load(GUARD_DEFAULT.read_text("utf-8"))
    assert all(key in raw for key in changes)
    path = tmp_path / "guard.yaml"
    path.write_text(yaml.safe_dump({**raw, **changes}, allow_unicode=True), "utf-8")
    monkeypatch.setenv("GUARD_CONFIG", str(path))


@pytest.fixture(scope="module")
def r13():
    """가드 R13 연구 코드(TU-03이 옮기라고 한 원본). 같은 조각·점수인지 맞춰 본다."""
    spec = importlib.util.spec_from_file_location("r13_common", R13)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run(coro):
    return asyncio.run(coro)


def heading(sid: str) -> str:
    """섹션의 첫 `## ` 소제목 줄(질의 재료)."""
    return next(
        line for line in default_index().sections[sid].text.splitlines() if line.startswith("## ")
    )


def test_normalize_matches_the_grader(r13):
    texts = [s.full_text() for s in load_sections(KB).values()]
    texts += [m["content"] for row in r13.tutor_rows() for m in row["messages"]]
    texts += ["ＡＢＣ　가 나！", "“따옴표”—…·", "\t\r\n", ""]
    for text in texts:
        assert normalize(text) == scoring.normalize(text)


def test_sections_are_the_48_markdown_files_only():
    sections = load_sections(KB)
    md = sorted(KB.glob("*/*.md"))
    assert len(md) == 48
    assert sorted(sections) == sorted(p.stem for p in md)
    assert all(re.fullmatch(r"(PY|PR|FIN)-\d{2}-\d{2}", sid) for sid in sections)
    for path in md:
        section = sections[path.stem]
        assert section.text == path.read_text(encoding="utf-8")
        assert section.subject == path.parent.name
        assert section.names == ()
    html = list(KB.glob("*/*.html"))
    assert html  # 배포 자료에 HTML이 있지만
    assert all(p.stem not in sections for p in html)  # 섹션이 되지 않는다(HTML은 색인하지 않음)


def test_tables_are_attached_to_their_sections():
    sections = load_sections(KB)
    csvs = sorted(KB.glob("*/*-table*.csv"))
    assert len(csvs) == sum(len(s.tables) for s in sections.values()) == 23
    for path in csvs:
        assert path.read_text(encoding="utf-8") in sections[path.name.split("-table")[0]].tables
    for section in sections.values():
        assert section.full_text() == "\n\n".join([section.text, *section.tables])


def test_title_drops_the_hash_and_id():
    assert load_sections(KB)["PY-01-01"].title == "프로그래밍과 파이썬"


def test_chunks_match_the_r13_reference(r13):
    reference = [(sid, part) for sid, _head, part in r13.kb_chunks(r13.load_kb())]
    assert list(default_index().chunks) == reference


def test_chunk_scores_match_the_r13_bm25(r13):
    bm25 = r13.BM25([part for _sid, _head, part in r13.kb_chunks(r13.load_kb())])
    queries = [r13.all_user(row) for row in r13.tutor_rows()[:40]] + ["변수", "", "ab", "!!"]
    for query in queries:
        ours, theirs = default_index().chunk_scores(query), bm25.scores(query)
        assert all(
            math.isclose(a, b, rel_tol=1e-9, abs_tol=1e-12)
            for a, b in zip(ours, theirs, strict=True)
        )


def test_index_scores_are_the_guard_kb_index_scores():
    """가드 KbIndex(GR-03, 가드 설정의 kb 값)와 조각·점수가 비트 단위로 같다(TD-03)."""
    kb = load_config().kb
    guard = GuardKbIndex.load(KB, ngram=kb.ngram, k1=kb.k1, b=kb.b)
    index = default_index()
    assert index.chunks == guard.chunks
    for query in [heading(sid) for sid in index.sections] + ["변수", "", "ab", "!!"]:
        assert index.chunk_scores(query) == guard.scores(query)


def test_guard_default_kb_settings_are_the_tu03_values():
    """튜터 검색은 가드 설정 kb·retrieval을 같이 쓴다(TD-03). 값이 바뀌면 튜터 순위·인용도 바뀐다.

    TU-03은 문자 2-gram BM25(k1 1.2, b 0.75), 질의 상한 4,000자다. 가드 기본 설정에서 이 값을
    바꾸려면 튜터 검색(recall·인용)도 다시 재고 두 트랙이 같이 정한 뒤 이 테스트를 고친다.
    """
    cfg = load_config(GUARD_DEFAULT)  # 셸의 GUARD_CONFIG와 상관없이 저장소의 가드 기본 설정
    assert (cfg.kb.ngram, cfg.kb.k1, cfg.kb.b) == (2, 1.2, 0.75)
    assert cfg.retrieval.max_query_chars == 4000


def test_bm25_settings_follow_the_guard_config(tmp_path, monkeypatch):
    query = heading("PY-01-01")
    before = GuardKbIndex.load(KB).scores(query)
    use_guard_config(tmp_path, monkeypatch, kb={"top_k": 3, "ngram": 3, "k1": 2.0, "b": 0.3})
    got = KbIndex(load_sections(KB)).chunk_scores(query)
    assert got == GuardKbIndex.load(KB, ngram=3, k1=2.0, b=0.3).scores(query)
    assert got != before


def test_query_limit_below_one_is_refused(tmp_path, monkeypatch):
    # 0이면 [-0:]가 질의를 통째로 남기고 음수면 앞만 버린다. 기동 때 막는다
    use_guard_config(tmp_path, monkeypatch, retrieval={"max_query_chars": 0})
    with pytest.raises(ValueError, match="max_query_chars"):
        KbIndex(load_sections(KB))


def test_query_limit_follows_the_guard_config(tmp_path, monkeypatch):
    """질의 상한은 가드 설정 retrieval.max_query_chars다. 넘으면 뒤쪽(최근 발화)을 남긴다."""
    tail, earlier = heading("PY-01-01"), [heading("FIN-01-01")]
    whole = [s.id for s in run(retrieve(tail, earlier))]
    assert whole != [s.id for s in run(retrieve(tail, []))]  # 상한 안이면 앞 발화도 질의에 든다
    use_guard_config(tmp_path, monkeypatch, retrieval={"max_query_chars": len(tail)})
    monkeypatch.setattr(search, "default_index", lambda: KbIndex.from_dir(KB))
    assert [s.id for s in run(retrieve(tail, earlier))] == [s.id for s in run(retrieve(tail, []))]


def test_search_folds_chunk_scores_to_the_section_max():
    index = default_index()
    query = "변수에 값을 저장하는 방법"
    best: dict[str, float] = {}
    for (sid, _text), score in zip(index.chunks, index.chunk_scores(query), strict=True):
        best[sid] = max(best.get(sid, 0.0), score)
    expected = sorted(((s, v) for s, v in best.items() if v > 0), key=lambda x: (-x[1], x[0]))
    assert index.search(query) == expected


def test_retrieve_returns_top_k_sections_in_score_order():
    query = "파이썬에서 변수는 무엇인가요?"
    out = run(retrieve(query, []))
    assert [s.id for s in out] == [sid for sid, _ in default_index().search(query)[:5]]
    assert len(out) == 5 and all(isinstance(s, Section) for s in out)
    assert len(run(retrieve(query, [], k=2))) == 2


def test_retrieve_finds_a_section_from_its_heading():
    for sid in ("PY-01-01", "PR-02-01", "FIN-03-02"):
        assert run(retrieve(heading(sid), []))[0].id == sid


def test_subject_filter():
    out = run(retrieve(heading("FIN-01-01"), [], subject="finance"))
    assert out and all(s.subject == "finance" for s in out)
    assert run(retrieve(heading("FIN-01-01"), [], subject="history")) == []


@pytest.mark.parametrize("query", ["", "   ", "!", "a", "🙂"])
def test_query_without_bigrams_returns_nothing(query):
    assert run(retrieve(query, [])) == []


@pytest.mark.parametrize("k", [0, -1])
def test_non_positive_k_returns_nothing(k):
    assert run(retrieve("변수", [], k=k)) == []


def test_history_is_part_of_the_query():
    plain = run(retrieve("그거 다시 알려 줘", []))
    with_history = run(retrieve("그거 다시 알려 줘", [heading("FIN-01-01")]))
    assert with_history[0].id == "FIN-01-01"
    assert [s.id for s in plain] != [s.id for s in with_history]


def test_long_query_keeps_the_tail():
    tail = heading("PY-01-01")
    out = run(retrieve(tail, [heading("FIN-01-01") + " " * 5000]))
    assert [s.id for s in out] == [s.id for s in run(retrieve(tail, []))]


def test_queries_do_not_change_the_index():
    index = default_index()
    before = index.search(heading("PR-02-01"))
    for sid in list(index.sections)[:30]:
        run(retrieve(heading(sid), ["앞 대화"]))
    assert index.search(heading("PR-02-01")) == before
    with pytest.raises(TypeError):
        index.sections["PY-99-99"] = index.sections["PY-01-01"]  # type: ignore[index]


def test_retrieve_logs_nothing(caplog):
    caplog.set_level(logging.DEBUG)
    run(retrieve("변수가 뭐야", ["앞 대화"]))
    # asyncio.run 자체의 디버그 로그(이벤트 루프 종류)는 우리 코드가 아니므로 뺀다
    assert [r for r in caplog.records if not r.name.startswith("asyncio")] == []


def test_index_can_be_built_from_another_kb_dir(tmp_path):
    (tmp_path / "python").mkdir()
    (tmp_path / "python" / "PY-01-01.md").write_text(
        "# PY-01-01 제목\n## 변수\n변수는 이름표", "utf-8"
    )
    (tmp_path / "python" / "PY-01-01-table1.csv").write_text("이름,값\n점수,90\n", "utf-8")
    (tmp_path / "python" / "PY-01.html").write_text("<html>무시</html>", "utf-8")
    index = KbIndex.from_dir(tmp_path)
    assert list(index.sections) == ["PY-01-01"]
    assert (
        index.section_text("PY-01-01")
        == "# PY-01-01 제목\n## 변수\n변수는 이름표\n\n이름,값\n점수,90\n"
    )
    assert index.search("점수") == [("PY-01-01", index.search("점수")[0][1])]


def test_empty_kb_dir_fails_loudly(tmp_path):
    with pytest.raises(FileNotFoundError):
        KbIndex.from_dir(tmp_path)
    with pytest.raises(ValueError):
        KbIndex({})


def test_title_is_empty_without_a_heading_line(tmp_path):
    (tmp_path / "prompt").mkdir()
    (tmp_path / "prompt" / "PR-01-01.md").write_text("제목 줄 없음\n## 소제목\n본문", "utf-8")
    assert KbIndex.from_dir(tmp_path).sections["PR-01-01"].title == ""


def test_startup_builds_the_index():
    class State:
        pass

    state = State()
    assert run(tutor.startup(state)) is None
    assert state.tutor_kb is default_index()
    assert len(state.tutor_kb.sections) == 48


# --- 서버: 가드가 기동 때 읽은 설정(app.state.cfg)으로 색인을 만든다(TD-03) ---


def guard_cfg(**overrides):
    """저장소 가드 기본 설정에서 몇 값만 바꾼 설정 객체. 환경변수를 거치지 않는다."""
    return load_config(GUARD_DEFAULT, overrides=overrides)


@contextlib.contextmanager
def server_with(monkeypatch, cfg):
    """가드 create_app이 기동 때 읽는 설정을 cfg로 바꿔 튜터를 붙인 앱을 띄운다."""
    monkeypatch.setattr(guard_server, "load_config", lambda: cfg)
    with TestClient(make_app()) as client:
        assert client.app.state.cfg is cfg
        yield client


def ranked(index, query):
    return [sid for sid, _score in index.search(query)]


def test_server_builds_the_tutor_index_from_the_guard_config_it_loaded(monkeypatch):
    cfg = guard_cfg(**{"kb.ngram": 3})
    guard = GuardKbIndex.load(KB, ngram=3, k1=cfg.kb.k1, b=cfg.kb.b)
    with server_with(monkeypatch, cfg) as c:
        index = c.app.state.tutor_kb
    assert index.chunks == guard.chunks
    for query in [NGRAM_QUERY, heading("PY-01-01"), "변수", ""]:
        assert index.chunk_scores(query) == guard.scores(query)
    assert ranked(index, NGRAM_QUERY)[0] != ranked(default_index(), NGRAM_QUERY)[0]


def test_server_index_with_the_default_guard_config_is_the_offline_index(monkeypatch):
    """설정이 같으면 서버 색인과 앱 state 없는 기본 색인(default_index)의 결과가 같다."""
    with server_with(monkeypatch, guard_cfg()) as c:
        index = c.app.state.tutor_kb
    offline = default_index()
    assert index.chunks == offline.chunks
    assert index.max_query_chars == offline.max_query_chars
    for query in [heading(sid) for sid in offline.sections] + [NGRAM_QUERY]:
        assert index.search(query) == offline.search(query)


def test_two_servers_with_different_guard_configs_search_their_own_index(monkeypatch):
    """한 프로세스의 두 앱이 서로의 색인을 쓰지 않고, 요청 처리의 검색도 자기 앱 색인을 쓴다."""
    answer_openai(monkeypatch)  # 주 호출은 보내지 않고 최소 정상 답
    seen = []
    real_retrieve = draft_mod.retrieve

    async def watched_retrieve(query, history=(), **kw):
        found = await real_retrieve(query, history, **kw)
        seen.append((kw.get("index"), [s.id for s in found]))
        return found

    monkeypatch.setattr(draft_mod, "retrieve", watched_retrieve)
    cfg3, cfg2 = guard_cfg(**{"kb.ngram": 3}), guard_cfg()
    message = {"role": "user", "content": NGRAM_QUERY}
    body = {"spec_version": "1.0", "session_id": "s", "messages": [message]}
    with server_with(monkeypatch, cfg3) as c3, server_with(monkeypatch, cfg2) as c2:
        assert (c3.app.state.cfg, c2.app.state.cfg) == (cfg3, cfg2)
        for client, cfg in ((c3, cfg3), (c2, cfg2), (c3, cfg3)):  # 뒤에 뜬 앱이 앞 앱을 안 바꾼다
            assert client.post("/tutor", json=body, headers=AUTH).status_code == 200
            index, top = seen.pop()
            assert index is client.app.state.tutor_kb
            k = client.app.state.tutor_cfg.retrieval.k
            assert top == ranked(KbIndex(load_sections(KB), cfg), NGRAM_QUERY)[:k]
    assert ranked(c3.app.state.tutor_kb, NGRAM_QUERY) != ranked(c2.app.state.tutor_kb, NGRAM_QUERY)
    # 앱 state가 없는 쪽(로컬 측정·테스트)의 기본 색인은 가드 기본 설정 그대로다
    guard2 = GuardKbIndex.load(KB, ngram=cfg2.kb.ngram, k1=cfg2.kb.k1, b=cfg2.kb.b)
    assert default_index().chunk_scores(NGRAM_QUERY) == guard2.scores(NGRAM_QUERY)


def test_server_refuses_a_guard_config_with_query_limit_below_one(monkeypatch):
    with (
        pytest.raises(ValueError, match="max_query_chars"),
        server_with(monkeypatch, guard_cfg(**{"retrieval.max_query_chars": 0})),
    ):
        pass
