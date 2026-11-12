"""S2.2 KB 색인·검색 (GR-03, ADR-0010, C2-judges S2.2).

이식 원본 scripts/r13/common.py(kb_chunks·BM25)와 값이 같은지, Ranking 계약,
공개 세트 적중률·속도를 본다.
측정 수치는 capsys.disabled()로 출력한다(PR 표의 근거).
"""

import json
import statistics
import sys
import time
import tracemalloc
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

from guardrail.config import load_config, resolve_path
from guardrail.data import load_public
from guardrail.kb import Hit, KbIndex, Ranking, normalize

R13 = Path(__file__).resolve().parents[1] / "scripts" / "r13"
if str(R13) not in sys.path:
    sys.path.append(str(R13))
import common  # noqa: E402  (이식 원본. 테스트에서만 import)

CFG = load_config()
KB_DIR = resolve_path(CFG.paths.kb)
GOLD67 = json.loads((Path(__file__).parent / "fixtures" / "kb_gold67.json").read_text("utf-8"))


def _params() -> dict:
    return {"ngram": CFG.kb.ngram, "k1": CFG.kb.k1, "b": CFG.kb.b}


@pytest.fixture(scope="module")
def index() -> KbIndex:
    return KbIndex.load(KB_DIR, **_params())


@pytest.fixture(scope="module")
def ref_bm25() -> "common.BM25":
    return common.BM25([c[2] for c in common.kb_chunks(common.load_kb())])


@pytest.fixture(scope="module")
def public():
    return load_public(CFG.paths.public)


def _all_user(row) -> str:
    return "\n".join(m.content for m in row.messages if m.role == "user")


def _sections(ranking: Ranking) -> list[str]:
    return list(dict.fromkeys(h.section_id for h in ranking.hits))


def _report(capsys, text: str) -> None:
    with capsys.disabled():
        print(f"\n[S2.2] {text}")  # noqa: T201  (테스트 측정 보고, 서버 로그 아님)


# --- 조각 ---


def test_chunks_match_common_kb_chunks(index):
    expected = [(sid, text) for sid, _, text in common.kb_chunks(common.load_kb())]
    assert list(index.chunks) == expected
    assert len(index.chunks) == 244  # 현재 공개 KB 기준 회귀 고정(KB가 바뀌면 다시 잼)


def test_html_is_not_read(index):
    sections = {sid for sid, _ in index.chunks}
    assert len(sections) == 48
    assert "FIN-01" not in sections


def test_pr_02_01_splits_inside_code_block_and_keeps_title_chunk(index):
    parts = [text for sid, text in index.chunks if sid == "PR-02-01"]
    assert parts[0].startswith("# PR-02-01")
    assert all(p.startswith("## ") for p in parts[1:])
    # 자른 자리 앞까지 ``` 개수가 홀수면 코드 블록 안에서 자른 것
    inside = sum("\n".join(parts[:i]).count("```") % 2 for i in range(1, len(parts)))
    assert inside == 3


def test_csv_is_appended_to_last_chunk(index):
    csv = (KB_DIR / "finance" / "FIN-01-01-table1.csv").read_text("utf-8")
    parts = [text for sid, text in index.chunks if sid == "FIN-01-01"]
    assert parts[-1].endswith(csv)
    assert all(csv not in p for p in parts[:-1])


def test_section_text_is_all_chunks_plus_csv(index):
    kb = common.load_kb()
    for sid, text in kb.items():
        assert index.section_text(sid) == text
        assert "\n".join(t for s, t in index.chunks if s == sid) == text


def test_section_text_unknown_id_raises(index):
    with pytest.raises(KeyError):
        index.section_text("XX-99-99")


def test_max_chunk_length_report(index, capsys):
    sid, text = max(index.chunks, key=lambda c: len(c[1]))
    _report(capsys, f"최대 조각 길이(붙은 CSV 포함) {len(text)}자 ({sid})")
    assert len(text) > 0


# --- BM25 ---

FIXED_QUERIES = [
    "파이썬 리스트",
    "for for for for 반복문",
    "ㅋㅋㅋㅋㅋㅋ 이자 이자 이자",
    "Agents.md 지침 파일",
    "체크카드 하루 이체 한도",
    "!!!",
    "",
]


def test_bm25_scores_match_common(index, ref_bm25, public):
    queries = FIXED_QUERIES + [_all_user(r) for r in public[:40]]
    for q in queries:
        got, exp = index.scores(q), ref_bm25.scores(q)
        assert len(got) == len(exp)
        assert max((abs(a - b) for a, b in zip(got, exp, strict=True)), default=0) <= 1e-9, q


def test_query_gram_frequency_is_ignored():
    idx = KbIndex({"A-01-01": "## a\naa xyz", "B-01-01": "## b\nqrs tuv"})
    assert idx.scores("aaa") == idx.scores("aa")  # "aaa"의 2-gram은 aa 두 번
    assert idx.scores("aa")[0] > 0


def test_normalize_matches_grader():
    for s in ["Ａ Ｂ, c!", "x² “인용” — 끝…", "  탭\t줄\n바꿈 ", "Ⅻ ﬁ"]:
        assert normalize(s) == common.norm(s)


@given(st.text(max_size=60))
def test_normalize_matches_grader_any_text(s):
    assert normalize(s) == common.norm(s)


# --- Ranking 계약 ---


def _three_way_kb() -> KbIndex:
    return KbIndex(
        {
            "A-01-01": "## a1\n사과 사과 바나나\n## a2\n사과 바나나 포도\n## a3\n사과 바나나 키위",
            "B-01-01": "## b1\n사과 수박 참외 멜론 자두",
            "C-01-01": "## c1\n고양이 강아지",
        }
    )


def test_margin_folds_same_section_and_sees_beyond_top_k():
    idx = _three_way_kb()
    r = idx.rank("사과 바나나")
    assert [h.section_id for h in r.hits[:3]] == ["A-01-01"] * 3  # 다른 섹션은 top_k 밖
    best = {}
    for h in r.hits:
        best[h.section_id] = max(best.get(h.section_id, 0.0), h.score)
    assert best["B-01-01"] > 0
    assert r.margin == pytest.approx(best["A-01-01"] - best["B-01-01"])
    assert r.margin != pytest.approx(r.hits[0].score - r.hits[1].score)


def test_hits_are_all_chunks_sorted_desc(index):
    r = index.rank("파이썬 리스트 인덱스")
    assert isinstance(r, Ranking) and all(isinstance(h, Hit) for h in r.hits)
    assert len(r.hits) == len(index.chunks)
    scores = [h.score for h in r.hits]
    assert scores == sorted(scores, reverse=True)


def test_rank_computes_scores_once(index, monkeypatch):
    calls = 0
    real = KbIndex.scores

    def counting(self, query):
        nonlocal calls
        calls += 1
        return real(self, query)

    monkeypatch.setattr(KbIndex, "scores", counting)
    r = index.rank("이자 계산")
    assert calls == 1
    calls = 0
    assert index.search("이자 계산", k=3) == list(r.hits[:3])
    assert calls == 1


def test_empty_query_gives_zero_margin(index):
    r = index.rank("?!  ")
    assert r.margin == 0.0
    assert all(h.score == 0.0 for h in r.hits)


def test_single_section_margin_is_top_score():
    idx = KbIndex({"A-01-01": "## a\n사과 바나나"})
    r = idx.rank("사과")
    assert r.margin == r.hits[0].score > 0


# --- 입력 검증 ---


def test_load_rejects_missing_dir(tmp_path):
    with pytest.raises(FileNotFoundError):
        KbIndex.load(tmp_path / "nope")


def test_load_rejects_empty_kb(tmp_path):
    (tmp_path / "x").mkdir()
    with pytest.raises(ValueError, match="KB"):
        KbIndex.load(tmp_path)


@pytest.mark.parametrize(
    "params",
    [
        {"ngram": 0},
        {"ngram": 2.0},
        {"ngram": True},
        {"k1": -0.1},
        {"k1": float("nan")},
        {"k1": float("inf")},
        {"b": 1.5},
        {"b": float("nan")},
    ],
)
def test_rejects_bad_params(params):
    with pytest.raises(ValueError):
        KbIndex({"A-01-01": "## a\n사과"}, **params)


def test_load_rejects_duplicate_section_id(tmp_path):
    for subject in ("a", "b"):
        (tmp_path / subject).mkdir()
        (tmp_path / subject / "X-01-01.md").write_text("## x\n사과", encoding="utf-8")
    with pytest.raises(ValueError, match="X-01-01"):
        KbIndex.load(tmp_path)


def test_search_rejects_k_below_one():
    idx = KbIndex({"A-01-01": "## a\n사과"})
    with pytest.raises(ValueError):
        idx.search("사과", k=0)


def test_ties_keep_chunk_order():
    idx = KbIndex({"B-01-01": "## b\n사과", "A-01-01": "## a\n사과"})
    r = idx.rank("사과")
    assert r.hits[0].score == r.hits[1].score
    assert [h.section_id for h in r.hits] == ["B-01-01", "A-01-01"]
    assert r.margin == 0.0


def test_rejects_kb_without_grams():
    with pytest.raises(ValueError):
        KbIndex({"A-01-01": "!!!"})


# --- 공개 세트 적중률·속도 ---


def test_margin_matches_common_on_public_queries(index, ref_bm25, public):
    """rule_min_margin이 걸리는 값: 원본 점수로 따로 접은 margin과 1e-9 안에서 같다."""
    sids = [c[0] for c in common.kb_chunks(common.load_kb())]
    for r in public:
        q = _all_user(r) + ("\n" + r.candidate_answer if r.candidate_answer else "")
        best: dict[str, float] = {}
        for sid, s in zip(sids, ref_bm25.scores(q), strict=True):
            best[sid] = max(best.get(sid, 0.0), s)
        top = max(best, key=best.get)
        exp = best[top] - max(v for sid, v in best.items() if sid != top)
        assert abs(index.rank(q).margin - exp) <= 1e-9, r.id


def test_gold67_fixture_regenerates_from_q5():
    """픽스처는 scripts/r13/q5_ungrounded.py의 배정 함수로 만든 값이다(생성 절차 고정)."""
    import q5_ungrounded as q5

    kb = common.load_kb()
    gen = {}
    for r in common.guard_rows():
        if "candidate_answer" not in r or r["labels"]["category"] in common.BLOCK_CATS:
            continue
        if not r["labels"]["grounded"]:
            gen[r["id"]] = q5.sections_with(kb, q5.ANN[r["id"]][0])
            continue
        sids, _ = q5.gold_sections_grounded(kb, r["candidate_answer"])
        fact = q5.GROUNDED_FACTS.get(r["id"])
        if fact:
            fin = {k for k in kb if k.startswith("FIN")}
            sids = sorted(set(sids) | set(q5.sections_with(kb, fact)) & fin)
        gen[r["id"]] = sids
    assert gen == GOLD67


def test_gold67_top1_and_top3(index, public, capsys):
    rows = {r.id: r for r in public if r.endpoint == "guard"}
    groups = {
        "전체": [],
        "ungrounded(수작업 사실)": [],
        "grounded(답변 4-gram)": [],
        "정답 1개": [],
    }
    for rid, gold in GOLD67.items():
        r = rows[rid]
        ranked = _sections(index.rank(_all_user(r) + "\n" + r.candidate_answer))
        res = (ranked[0] in gold, bool(set(gold) & set(ranked[:3])))
        groups["전체"].append(res)
        groups[
            "grounded(답변 4-gram)" if r.labels["grounded"] else "ungrounded(수작업 사실)"
        ].append(res)
        if len(gold) == 1:
            groups["정답 1개"].append(res)
    for name, res in groups.items():
        n = len(res)
        top1, top3 = sum(a for a, _ in res), sum(b for _, b in res)
        _report(capsys, f"가드 후보 {name} {n}행 정답 섹션 1위 {top1}/{n}, 3위 {top3}/{n}")
    top1, top3 = (sum(x[i] for x in groups["전체"]) for i in (0, 1))
    assert len(GOLD67) == 67
    assert top1 >= 64
    assert top3 >= 66


def test_tutor_evidence_recall(index, public, capsys):
    rows = [r for r in public if r.endpoint == "tutor" and r.labels.get("evidence")]
    rec = {1: [], 3: []}
    hit = {1: 0, 3: 0}
    for r in rows:
        ranked = _sections(index.rank(_all_user(r)))
        ev = set(r.labels["evidence"])
        for k in rec:
            got = ev & set(ranked[:k])
            rec[k].append(len(got) / len(ev))
            hit[k] += bool(got)
    n = len(rows)
    _report(
        capsys,
        f"튜터 evidence {n}행(질의=모든 user 발화) recall@1 {statistics.mean(rec[1]):.3f}"
        f" recall@3 {statistics.mean(rec[3]):.3f}, 적중@1 {hit[1]}/{n} 적중@3 {hit[3]}/{n}",
    )
    # 회귀 고정: 도입 시 실측 0.824·0.916(결정적 계산이라 값이 바뀌면 검색이 바뀐 것)
    assert n == 119
    assert statistics.mean(rec[1]) >= 0.82
    assert statistics.mean(rec[3]) >= 0.91


def test_rank_p95_under_20ms(index, public, capsys):
    guard = [r for r in public if r.endpoint == "guard" and r.candidate_answer is not None]
    tutor = [r for r in public if r.endpoint == "tutor"]
    queries = [_all_user(r) + "\n" + r.candidate_answer for r in guard]
    queries += [_all_user(r) for r in tutor]
    assert (len(guard), len(tutor)) == (86, 200)
    index.rank(queries[0])  # 첫 호출 준비 비용 제외
    times = []
    for q in queries:
        t0 = time.perf_counter()
        index.rank(q)
        times.append((time.perf_counter() - t0) * 1000)
    p95 = statistics.quantiles(times, n=20)[-1]
    _report(capsys, f"rank p95 {p95:.2f}ms, 최대 {max(times):.2f}ms ({len(queries)}질의)")
    assert p95 <= 20


def test_index_time_and_memory_report(capsys):
    t0 = time.perf_counter()
    KbIndex.load(KB_DIR, **_params())
    secs = time.perf_counter() - t0
    tracemalloc.start()
    idx = KbIndex.load(KB_DIR, **_params())
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    _report(
        capsys,
        f"색인 {secs * 1000:.0f}ms, 색인 중 최대 할당 {peak / 1e6:.1f}MB(tracemalloc),"
        f" 조각 {len(idx.chunks)}",
    )
    assert secs < 10
