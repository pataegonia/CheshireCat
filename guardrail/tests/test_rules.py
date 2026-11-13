"""S2.4 숫자·리터럴 규칙 ② (GR-06 추출, GR-07 ②, C2-judges S2.4).

이식 원본 scripts/r13/q6_rule_fp.py(V2·merged_pm4_spans)와 토큰·구간·U/G/T 수치가 같은지 본다.
grader(scoring.normalize·span_f1)는 테스트에서만 import한다.
측정 수치는 capsys.disabled()로 출력한다(PR 표의 근거).
"""

import statistics
import sys
import time
from collections import Counter
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

from guardrail.config import load_config, resolve_path
from guardrail.kb import KbIndex
from guardrail.rules import (
    PUNCT,
    LiteralToken,
    extract_literals,
    literal_rule,
    normalize,
    rule_spans,
    unsupported_literals,
)

R13 = Path(__file__).resolve().parents[1] / "scripts" / "r13"
if str(R13) not in sys.path:
    sys.path.append(str(R13))
import common  # noqa: E402  (이식 원본. 테스트에서만 import)
import q5_ungrounded  # noqa: E402
import q6_rule_fp  # noqa: E402

CFG = load_config()
V2 = q6_rule_fp.VARIANTS["V2"]


def _cfg(**output):
    return load_config(overrides={f"output.{k}": v for k, v in output.items()})


@pytest.fixture(scope="module")
def index() -> KbIndex:
    return KbIndex.load(resolve_path(CFG.paths.kb), ngram=CFG.kb.ngram, k1=CFG.kb.k1, b=CFG.kb.b)


@pytest.fixture(scope="module")
def corpora():
    u, g, t, _ = q6_rule_fp.corpora()
    return {"U": u, "G": g, "T": t}


def _texts(answer: str, toks: list[LiteralToken]) -> list[str]:
    return [answer[t.start : t.end] for t in toks]


def _report(capsys, text: str) -> None:
    with capsys.disabled():
        print(f"\n[S2.4] {text}")  # noqa: T201  (테스트 측정 보고, 서버 로그 아님)


# --- normalize 복사 ---


def test_normalize_matches_grader():
    assert PUNCT == common.scoring.PUNCT
    for s in ["Ａ Ｂ, c!", "x² “인용” — 끝…", "  탭\t줄\n바꿈 ", "Ⅻ ﬁ", "1,000원 (PY-01-01)"]:
        assert normalize(s) == common.scoring.normalize(s)


@given(st.text(max_size=80))
def test_normalize_matches_grader_any_text(s):
    assert normalize(s) == common.scoring.normalize(s)


def test_normalize_matches_grader_on_public_answers(corpora):
    for rows in corpora.values():
        for _, q, a, _ in rows:
            assert normalize(a) == common.scoring.normalize(a)
            assert normalize(q) == common.scoring.normalize(q)


# --- 추출 ⓐ–ⓕ ---


@pytest.mark.parametrize(
    ("text", "kind", "token"),
    [
        ("가격은 5만 원이에요", "unit_number", "5만 원"),
        ("3개월 동안", "unit_number", "3개월"),
        ("정답률 50%야", "unit_number", "50%"),
        ("1.5배 빨라", "unit_number", "1.5배"),
        ("값은 1.3 정도", "decimal", "1.3"),
        ("총 1,000 정도", "decimal", "1,000"),
        ("42 나와", "multi_digit", "42"),
        ("리스트 [3, 7, 12] 봐", "num_list", "[3, 7, 12]"),
        ("호출 random.seed(42) 끝", "latin", "random.seed(42)"),
        ("이건 for loop 예시", "latin", "for loop"),
        ("두 번 돌려", "numeral", "두 번"),
        ("다섯 가지야", "numeral", "다섯 가지"),
    ],
)
def test_each_kind(text, kind, token):
    toks = extract_literals(text)
    assert [(t.kind, text[t.start : t.end]) for t in toks] == [(kind, token)]
    assert toks[0].value == normalize(token)
    assert V2(text) == [token]


def test_citation_numbers_are_not_tokens():
    a = "기록돼요. (PY-01-03) 그리고 FIN-02-11, PR-10-01 참고"
    assert extract_literals(a) == []
    assert V2(a) == []


def test_da_beon_is_not_numeral():
    assert extract_literals("모두 다 번갈아") == []
    assert extract_literals("다 개") == []
    assert [t.kind for t in extract_literals("다섯 개")] == ["numeral"]


def test_positions_map_back_to_raw_after_citation_removal():
    a = "기록돼요 (PY-01-01) 그래서 5개, 또 PR-02-03 뒤 12"
    toks = extract_literals(a)
    assert _texts(a, toks) == ["5개", "12"]
    assert [t.value for t in toks] == ["5개", "12"]


# V2와 토큰이 다른 곳은 수사 경계(ⓕ)가 뺀 이 다섯 매칭뿐이다(공개 답변 86·튜터 참조 200·KB 48섹션)
NUMERAL_BOUNDARY_DROPS = Counter(
    {"한 개": 1, "세 개": 1, "한 단계": 1, "한 배": 1, "두 개": 1}
)  # 불필요[한 개]인 · 두[세 개] 규칙 · 중요[한 단계]를 · 향[한 배]제 · [두 개]수를


def test_tokens_match_v2_except_numeral_boundary_drops(corpora, index):
    texts = [a for rows in corpora.values() for _, _, a, _ in rows]
    texts += [text for _, text in index.chunks]
    n, dropped = 0, Counter()
    for text in texts:
        toks = extract_literals(text)
        got, want = _texts(text, toks), V2(text)
        dropped += Counter(want) - Counter(got)
        assert not Counter(got) - Counter(want)
        assert [t.value for t in toks] == [normalize(x) for x in got]
        n += len(toks)
    assert dropped == NUMERAL_BOUNDARY_DROPS
    assert n > 1000


@pytest.mark.parametrize(
    "text",
    [
        "열두 번",
        "중요한 개념",
        "또한 번호",
        "세 개념",
        "두 배열",
        "한 개인 정보",
        "두 개수를 더한",
        "필요한 개발",
        "한 개체",
        "두 번역",
        "향한 배제",
        "한 배치",
        "두 배경",
    ],
)
def test_numeral_boundary_rejects_word_fragments(text):
    assert [t for t in extract_literals(text) if t.kind == "numeral"] == []


@pytest.mark.parametrize(
    ("text", "token"),
    [
        ("총 두 번", "두 번"),
        ("딱 한 개", "한 개"),
        ("세 개월", "세 개"),
        ("두 번째", "두 번"),
        ("한 개를", "한 개"),
        ("네 단계로", "네 단계"),
        ("두 배로", "두 배"),
    ],
)
def test_numeral_boundary_keeps_real_numerals(text, token):
    toks = [t for t in extract_literals(text) if t.kind == "numeral"]
    assert _texts(text, toks) == [token]


def test_numeral_class_is_exactly_han_du_se_ne_daseot():
    # 클래스에 수사를 더하면 이 단언이 깨진다(값 대조 대상이 늘어난다)
    assert [t for t in extract_literals("여섯 번") if t.kind == "numeral"] == []


def test_cite_prefix_is_limited_to_subjects():
    # 과목 접두를 [A-Z]+로 넓히면 다른 접두의 숫자가 인용으로 지워진다
    toks = extract_literals("(AB-01-01)")
    assert "01" in [t.value for t in toks]
    assert extract_literals("(PY-01-01)") == []


def test_token_is_frozen():
    tok = extract_literals("42")[0]
    with pytest.raises(AttributeError):
        tok.value = "x"  # type: ignore[misc]


# --- ablation rule_extra_tokens ---


@pytest.mark.parametrize("answer", ["1,000원", "1000원"])
def test_value_norm_only_when_enabled(answer):
    ref = "한 잔에 천 원이에요"
    assert [t.value for t in unsupported_literals(answer, ref)] == [normalize(answer)]
    assert unsupported_literals(answer, ref, ("value_norm",)) == []


def test_value_norm_man_cheon_eok():
    assert unsupported_literals("5만 원", "50,000원", ("value_norm",)) == []
    assert unsupported_literals("1.5만 원", "15000원", ("value_norm",)) == []
    assert unsupported_literals("1,000원", "1000원") == []  # 콤마는 normalize가 이미 지운다
    assert len(unsupported_literals("5만 원", "50,000원")) == 1


@pytest.mark.parametrize(
    ("text", "want"),
    [
        ("만 원", "10000원"),
        ("천 원", "1000원"),
        ("1억 원", "100000000원"),
        ("1,500만 원", "15000000원"),
        ("1.2.3만 원", "1.2.3만원"),  # 잘못된 숫자는 바꾸지 않는다(예외 없음)
        ("3,5만 원", "3,5만원"),
        ("수천 원", "수천원"),
    ],
)
def test_value_norm_money_edge_cases(text, want):
    ref = f"참조 {text}"
    assert unsupported_literals(f"값 {want}", ref, ("value_norm",)) == []
    assert unsupported_literals(text, ref, ("value_norm",)) == []


def test_token_across_citation_covers_citation():
    a = "답 12(PY-01-01)34 끝"
    (tok,) = extract_literals(a)
    assert (tok.kind, tok.value, a[tok.start : tok.end]) == (
        "multi_digit",
        "1234",
        "12(PY-01-01)34",
    )


def test_fullwidth_digits_and_empty_text():
    a = "모두 ５개"
    (tok,) = extract_literals(a)
    assert (tok.value, a[tok.start : tok.end]) == ("5개", "５개")
    assert extract_literals("") == []


def test_units_extra_only_when_enabled():
    assert extract_literals("3주 동안") == []
    toks = extract_literals("3주 동안 7일 2년 10글자", ("units_extra",))
    assert _texts("3주 동안 7일 2년 10글자", toks) == ["3주", "7일", "2년", "10글자"]


def test_backtick_latin_keeps_only_latin_inside_backticks():
    a = "그냥 print 말고 `len(x)` 써요 ```\nfor i\n``` 42"
    assert _texts(a, extract_literals(a)) == ["print", "len(x)", "for i", "42"]
    assert _texts(a, extract_literals(a, ("backtick_latin",))) == ["len(x)", "for i", "42"]


def test_unknown_extra_token_rejected():
    with pytest.raises(ValueError):
        extract_literals("42", ("nope",))


def test_unmeasured_ablations_are_not_implemented():
    with pytest.raises(NotImplementedError):
        extract_literals("한 사람", ("bare_numeral",))


# --- 구간 pm4 ---


def _tok(answer: str, text: str, nth: int = 0) -> LiteralToken:
    i = -1
    for _ in range(nth + 1):
        i = answer.index(text, i + 1)
    return LiteralToken("unit_number", normalize(text), i, i + len(text))


def test_pm4_uses_first_occurrence_only():
    a = "처음엔 3개를 썼고 한참 뒤에도 다시 3개를 썼어요"
    assert rule_spans(a, [_tok(a, "3개", nth=1)]) == ("처음엔 3개를 썼고",)


def test_pm4_merges_overlapping_windows():
    a = "abcdefghijklmnop"
    toks = [LiteralToken("latin", "e", 4, 5), LiteralToken("latin", "g", 6, 7)]
    assert rule_spans(a, toks) == ("abcdefghijk",)
    assert rule_spans(a, toks) == tuple(q6_rule_fp.merged_pm4_spans(a, ["e", "g"]))


def test_pm4_same_token_twice_gives_one_span():
    a = "가나다라 7개 마바사아 7개 자차"
    assert rule_spans(a, [_tok(a, "7개"), _tok(a, "7개", nth=1)]) == ("나다라 7개 마바사",)


def test_pm4_drops_span_empty_after_normalize():
    a = "!!!! ... !!!!"
    assert rule_spans(a, [LiteralToken("latin", "", 5, 8)]) == ()


@given(
    st.text(alphabet="ab 1개.", min_size=1, max_size=40),
    st.lists(st.tuples(st.integers(0, 39), st.integers(1, 6)), max_size=5),
)
def test_pm4_matches_merged_pm4_spans(answer, picks):
    toks = [
        LiteralToken("latin", normalize(answer[s : s + n]), s, s + n)
        for s, n in picks
        if s + n <= len(answer) and answer.find(answer[s : s + n]) == s  # 원본(find)과 같은 경우
    ]
    texts = [answer[t.start : t.end] for t in toks]
    want = [x for x in q6_rule_fp.merged_pm4_spans(answer, texts) if normalize(x)]
    got = rule_spans(answer, toks)
    assert got == tuple(want)
    assert len(set(got)) == len(got)  # 같은 구간 한 번
    assert all(x in answer for x in got)


def test_span_uses_first_token_with_that_text_not_a_find_hit_inside_another_token():
    # 원본(ans.find)은 "13개" 안의 "3개"에 창을 놓는다. 여기서는 실제 "3개" 토큰 위치를 쓴다
    a = "13개 총 3개"
    toks = extract_literals(a)
    assert _texts(a, toks) == ["13개", "3개"]
    assert rule_spans(a, toks) == ("13개 총 3개",)
    assert rule_spans(a, toks, "eojeol") == ("13개", "3개")


def test_rule_spans_is_linear_in_answer_length():
    a = " ".join(str(i) for i in range(100_000, 128_572))  # 서로 다른 6자리 수 약 20만 자
    toks = extract_literals(a)
    t0 = time.perf_counter()
    for mode in ("pm4", "eojeol"):
        assert rule_spans(a, toks, mode)
    assert len(a) > 190_000
    assert time.perf_counter() - t0 < 1.0


def test_eojeol_expands_to_whitespace_boundaries():
    a = "값은 42개입니다 그리고 끝"
    assert rule_spans(a, [_tok(a, "42개")], "eojeol") == ("42개입니다",)
    b = "총합=42개였다 끝"
    assert rule_spans(b, [_tok(b, "42개")], "eojeol") == ("총합=42개였다",)


def test_rule_spans_rejects_unknown_mode_and_skips_empty_token():
    with pytest.raises(ValueError):
        rule_spans("42", [], "words")
    assert rule_spans("42", [LiteralToken("latin", "", 1, 1)]) == ()


# --- literal_rule ---


def _tiny_kb() -> KbIndex:
    return KbIndex(
        {
            "PY-01-01": "# 리스트\n리스트 append 메서드는 원소를 3개까지 예시로 보인다",
            "PY-01-02": "# 딕셔너리\n딕셔너리 keys 메서드",
        }
    )


def test_literal_rule_fires_with_spans():
    kb = _tiny_kb()
    a = "리스트 append는 원소 7개를 넣어요"
    r = kb.rank("리스트 append 원소")
    assert r.hits[0].section_id == "PY-01-01"
    assert literal_rule(a, r.hits, r.margin, kb, _cfg(rule_min_margin=0.0)) == (" 원소 7개를 넣어",)


def test_literal_rule_none_when_all_tokens_in_section():
    kb = _tiny_kb()
    r = kb.rank("리스트 append 원소")
    a = "리스트 append는 원소 3개"
    assert literal_rule(a, r.hits, r.margin, kb, _cfg(rule_min_margin=0.0)) is None


def test_literal_rule_none_below_margin():
    kb = _tiny_kb()
    r = kb.rank("리스트 append 원소")
    a = "원소 7개"
    assert literal_rule(a, r.hits, r.margin, kb, _cfg(rule_min_margin=r.margin + 0.01)) is None
    assert literal_rule(a, r.hits, r.margin, kb, _cfg(rule_min_margin=r.margin)) is not None


def test_literal_rule_empty_tuple_when_spans_off():
    kb = _tiny_kb()
    r = kb.rank("리스트 append 원소")
    cfg = _cfg(rule_min_margin=0.0, rule_emit_spans=False)
    assert literal_rule("원소 7개", r.hits, r.margin, kb, cfg) == ()


def test_literal_rule_none_without_hits():
    assert literal_rule("원소 7개", (), 99.0, _tiny_kb(), _cfg(rule_min_margin=0.0)) is None


def test_require_same_kind_not_implemented():
    kb = _tiny_kb()
    r = kb.rank("리스트 append 원소")
    out = CFG.output.model_copy(update={"rule_min_margin": 0.0, "rule_require_same_kind": True})
    cfg = CFG.model_copy(
        update={"output": out}
    )  # load_config는 이 값을 막는다. 직접 호출 방어만 본다
    with pytest.raises(NotImplementedError):
        literal_rule("원소 7개", r.hits, r.margin, kb, cfg)


# --- 공개 세트: V2 목표값 (q6_rule_fp.py) ---


def _run(corpora, index, cfg):
    fired, spans = {}, {}
    for name, rows in corpora.items():
        for rid, q, a, _ in rows:
            r = index.rank(q + "\n" + a)
            out = literal_rule(a, r.hits, r.margin, index, cfg)
            if out is not None:
                fired.setdefault(name, []).append(rid)
                spans[rid] = out
                ref = index.section_text(r.hits[0].section_id)
                want = q6_rule_fp.merged_pm4_spans(a, q6_rule_fp.missing("V2", a, ref))
                assert out == tuple(want), rid
                assert all(s in a for s in out), rid  # 원문 부분 문자열 100%
    return {n: len(fired.get(n, [])) for n in corpora}, spans


def test_v2_counts_margin_0(corpora, index, capsys):
    counts, spans = _run(corpora, index, _cfg(rule_min_margin=0.0))
    _report(capsys, f"margin 0: U {counts['U']}/35 · G {counts['G']}/32 · T {counts['T']}/119")
    assert counts == {"U": 18, "G": 0, "T": 6}
    gold = {rid: r["labels"]["unsupported"] for rid, _, _, r in corpora["U"]}
    f1 = [common.scoring.span_f1(list(spans[rid]), gold[rid]) for rid in gold if rid in spans]
    _report(capsys, f"구간 F1 (BM25 1위) {statistics.mean(f1):.3f} · {len(f1)}행")
    assert len(f1) == 18
    assert statistics.mean(f1) == pytest.approx(0.651, abs=0.005)


def test_v2_counts_margin_3_5(corpora, index, capsys):
    counts, _ = _run(corpora, index, _cfg(rule_min_margin=3.5))
    _report(capsys, f"margin 3.5: U {counts['U']}/35 · G {counts['G']}/32 · T {counts['T']}/119")
    assert counts == {"U": 16, "G": 0, "T": 3}


def test_default_margin_is_3_5():
    assert CFG.output.rule_min_margin == 3.5


def test_span_f1_gold_sections(corpora, capsys):
    kb = common.load_kb()
    f1 = []
    for rid, _, a, r in corpora["U"]:
        sids = q5_ungrounded.sections_with(kb, q5_ungrounded.ANN[rid][0])
        toks = unsupported_literals(a, "\n".join(kb[s] for s in sids))
        if toks:
            spans = rule_spans(a, toks)
            f1.append(common.scoring.span_f1(list(spans), r["labels"]["unsupported"]))
    _report(capsys, f"구간 F1 (정답 섹션) {statistics.mean(f1):.3f} · {len(f1)}행")
    assert len(f1) == 16
    assert statistics.mean(f1) == pytest.approx(0.638, abs=0.005)
