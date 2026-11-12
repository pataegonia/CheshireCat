"""KB 색인·검색(TU-03).

R13 참조 구현과 같은 조각·점수, retrieve 계약, 요청 사이 상태 없음(TU-18).
"""

import asyncio
import importlib.util
import logging
import math
import re

import pytest

import tutor
from tests.tutor.tutor_testlib import COMPETITION, REPO, scoring
from tutor.retrieval import KbIndex, Section, default_index, retrieve
from tutor.retrieval.kb import load_sections, normalize

KB = COMPETITION / "kb"
R13 = REPO / "guardrail" / "scripts" / "r13" / "common.py"


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
