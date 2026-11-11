"""I1.3 결정적 수정(PRD IN-04): 케이스 표 12개 + 공개 세트 정답 답변 119행 회귀."""

import importlib.util
import json
import time
from pathlib import Path

import pytest
from guardrail.schema import OutputVerdict

from integration import actions
from integration.actions import FinalizeConfig, action_for, finalize
from integration.contracts import Evidence, Section, Sentence, TutorResult

ROOT = Path(__file__).resolve().parents[2]
COMPETITION = ROOT / "competition"

# 문구 주인은 튜터다. 테스트는 아무 문자열이나 넣어 "받은 그대로 쓴다"만 확인한다.
NOTICE = "<<안내>>"
REFUSE = "<<거절>>"
CFG = FinalizeConfig(unverified_notice=NOTICE, refuse_text=REFUSE)

NORMAL = action_for("normal")
SCAFFOLD = action_for("answer_seeking")
ABSTAIN = action_for("out_of_material")
ACADEMIC = action_for("academic_dishonesty")
REFUSAL = action_for("jailbreak_injection")

OK = OutputVerdict(grounded=True, unsupported=(), safe=True)


def _load_scoring():
    spec = importlib.util.spec_from_file_location(
        "_grader_scoring", COMPETITION / "grader/scoring.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


scoring = _load_scoring()


def sec(sid: str, text: str, tables: list[str] | None = None) -> Section:
    return Section(id=sid, subject="s", title="t", text=text, tables=tables or [])


def claim(text: str, *evidence: tuple[str, str]) -> Sentence:
    return Sentence(
        text=text, kind="claim", evidence=[Evidence(sid=s, quote=q) for s, q in evidence]
    )


def plain(text: str) -> Sentence:
    return Sentence(text=text, kind="non_claim")


A = sec("PY-01-01", "야구 점수판은 3회까지의 점수를 기록한다. 점수는 리스트에 담는다.")
B = sec("PY-01-02", "변수는 값을 담는 이름이다.")
C = sec("FIN-02-03", "예산은 수입과 지출의 계획이다.", tables=["항목,금액\n간식,3000"])
RETRIEVED = [B, A, C]  # 검색 1위는 B


def _kb() -> dict[str, Section]:
    kb = {}
    for path in sorted((COMPETITION / "kb").glob("*/*.md")):
        tables = [
            t.read_text(encoding="utf-8")
            for t in sorted(path.parent.glob(f"{path.stem}-table*.csv"))
        ]
        kb[path.stem] = Section(
            id=path.stem,
            subject=path.parent.name,
            title=path.stem,
            text=path.read_text(encoding="utf-8"),
            tables=tables,
        )
    return kb


def _public_rows() -> list[dict]:
    with (COMPETITION / "public" / "tutor.jsonl").open(encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


KB = _kb()
ROWS = _public_rows()
EVIDENCE_ROWS = [r for r in ROWS if r["labels"]["evidence"]]


# ── 케이스 표 12개 (plan I1.3 "테스트 먼저") ──────────────────────────────


def test_01_normal_single_sentence():
    out = finalize(
        [claim("3회까지 기록해요.", ("PY-01-01", "3회까지의 점수를 기록한다"))],
        OK,
        RETRIEVED,
        NORMAL,
        CFG,
    )
    assert out == TutorResult(answer="3회까지 기록해요.", action="answer", citations=["PY-01-01"])


def test_02_quote_mismatch_drops_evidence_and_falls_back_to_top1():
    out = finalize(
        [claim("5회까지 기록해요.", ("PY-01-01", "5회까지의 점수를 기록한다"))],
        OK,
        RETRIEVED,
        NORMAL,
        CFG,
    )
    # evidence만 지운다. 문장은 남고(PRD IN-04-1), 인용은 검색 1위로 채운다.
    assert out.answer == "5회까지 기록해요."
    assert out.action == "answer"
    assert out.citations == ["PY-01-02"]


def test_03_unsupported_span_deletes_only_that_sentence():
    sentences = [
        claim("3회까지 기록해요.", ("PY-01-01", "3회까지의 점수를 기록한다")),
        claim("우승팀은 항상 홈팀이에요.", ("PY-01-01", "점수는 리스트에 담는다")),
        plain("더 궁금한 점이 있나요?"),
    ]
    verdict = OutputVerdict(grounded=False, unsupported=("우승팀은 항상 홈팀",), safe=True)
    out = finalize(sentences, verdict, RETRIEVED, NORMAL, CFG)
    assert out.answer == "3회까지 기록해요. 더 궁금한 점이 있나요?"
    assert out.action == "answer"
    assert out.citations == ["PY-01-01"]


def test_04_unsafe_discards_everything_and_uses_refuse_text():
    sentences = [
        claim("3회까지 기록해요.", ("PY-01-01", "3회까지의 점수를 기록한다")),
        plain("그리고…"),
    ]
    verdict = OutputVerdict(grounded=True, unsupported=(), safe=False)
    out = finalize(sentences, verdict, RETRIEVED, NORMAL, CFG)
    assert out.answer == REFUSE
    assert out.action == "refuse"
    assert out.citations == []  # refuse는 범주와 상관없이 인용하지 않는다


def test_05_all_claims_deleted_keeps_action_and_adds_notice():
    sentences = [
        plain("좋은 질문이에요."),
        claim("우승팀은 항상 홈팀이에요.", ("PY-01-01", "점수는 리스트에 담는다")),
    ]
    verdict = OutputVerdict(grounded=False, unsupported=("우승팀은 항상 홈팀이에요.",), safe=True)
    out = finalize(sentences, verdict, RETRIEVED, NORMAL, CFG)
    assert out.answer == f"좋은 질문이에요. {NOTICE}"
    assert out.action == "answer"  # abstain으로 바꾸지 않는다
    assert out.citations == ["PY-01-02"]  # 검색 1위


def test_06_judge_skipped_keeps_generated_answer():
    sentences = [
        claim("3회까지 기록해요.", ("PY-01-01", "3회까지의 점수를 기록한다")),
        claim("우승팀은 항상 홈팀이에요.", ("PY-01-01", "점수는 리스트에 담는다")),
    ]
    out = finalize(sentences, None, RETRIEVED, NORMAL, CFG)
    assert out.answer == "3회까지 기록해요. 우승팀은 항상 홈팀이에요."
    assert out.action == "answer"
    assert out.citations == ["PY-01-01"]


def test_07_answer_cut_to_4000_unicode_chars():
    text = "가" * 3999 + "😀" + "나"  # 4,001 유니코드 문자(바이트 수가 아니다)
    out = finalize([plain(text)], OK, RETRIEVED, REFUSAL, CFG)
    assert len(out.answer) == 4000
    assert out.answer == text[:4000]
    assert scoring.parse_tutor(out.model_dump()) is not None


def test_08_two_citations_capped_to_one():
    sentences = [
        claim("예산은 계획이에요.", ("FIN-02-03", "수입과 지출의 계획")),
        claim("3회까지 기록해요.", ("PY-01-01", "3회까지의 점수를 기록한다")),
        claim("리스트에 담아요.", ("PY-01-01", "점수는 리스트에 담는다")),
    ]
    out = finalize(sentences, OK, RETRIEVED, NORMAL, CFG)
    assert out.citations == ["PY-01-01"]  # claim 2개가 가리킨 섹션

    two = FinalizeConfig(unverified_notice=NOTICE, refuse_text=REFUSE, max_citations=2)
    assert finalize(sentences, OK, RETRIEVED, NORMAL, two).citations == ["PY-01-01", "FIN-02-03"]


def test_08b_citation_tie_broken_by_retrieval_rank():
    sentences = [
        claim("예산은 계획이에요.", ("FIN-02-03", "수입과 지출의 계획")),
        claim("3회까지 기록해요.", ("PY-01-01", "3회까지의 점수를 기록한다")),
    ]
    assert finalize(sentences, OK, RETRIEVED, NORMAL, CFG).citations == [
        "PY-01-01"
    ]  # A가 C보다 순위가 높다
    assert finalize(sentences, OK, [C, A], NORMAL, CFG).citations == ["FIN-02-03"]


@pytest.mark.parametrize(
    ("sid", "retrieved"),
    [
        ("PY-04-04", RETRIEVED),  # 실존하지만 검색 묶음에 없음 → 원문과 대조할 수 없다
        (
            "PY-09-01",
            [sec("PY-09-01", "3회까지의 점수를 기록한다"), *RETRIEVED],
        ),  # 실존하지 않는 ID
        ("PY-01-01\n", [*RETRIEVED]),  # 정규식 `$`가 통과시키는 꼴
        (
            "py-01-01",
            [sec("py-01-01", "3회까지의 점수를 기록한다")],
        ),  # 검색 1위도 형식이 틀리면 버린다
    ],
)
def test_09_unknown_sid_is_never_cited(sid, retrieved):
    out = finalize(
        [claim("3회까지 기록해요.", (sid, "3회까지의 점수를 기록한다"))], OK, retrieved, NORMAL, CFG
    )
    assert sid not in out.citations
    valid_top1 = [s.id for s in retrieved if s.id in ("PY-01-02", "PY-01-01", "FIN-02-03")][:1]
    assert out.citations == valid_top1
    assert out.answer == "3회까지 기록해요."


@pytest.mark.parametrize("rule", [ABSTAIN, ACADEMIC])
def test_10_template_rule_cites_the_section_it_used(rule):
    sentences = [
        plain("그 내용은 자료에 없어요."),
        claim("자료에는 3회까지 기록한다고 나와요.", ("PY-01-01", "3회까지의 점수를 기록한다")),
    ]
    out = finalize(sentences, OK, RETRIEVED, rule, CFG)
    assert out.answer == "그 내용은 자료에 없어요. 자료에는 3회까지 기록한다고 나와요."
    assert out.action == rule.action
    assert out.citations == ["PY-01-01"]


@pytest.mark.parametrize("rule", [ABSTAIN, ACADEMIC])
def test_11_template_rule_without_section_cites_nothing(rule):
    out = finalize([plain("그 내용은 자료에 없어요.")], OK, RETRIEVED, rule, CFG)
    assert out.answer == "그 내용은 자료에 없어요."  # 안내 문장을 붙이지 않는다
    assert out.action == rule.action
    assert out.citations == []  # 검색 1위로 채우지 않는다


def test_12_empty_sentences():
    out = finalize([], OK, RETRIEVED, NORMAL, CFG)
    assert out == TutorResult(answer=NOTICE, action="answer", citations=["PY-01-02"])
    assert finalize([], None, RETRIEVED, SCAFFOLD, CFG).action == "scaffold"
    assert finalize([], OK, RETRIEVED, REFUSAL, CFG) == TutorResult(
        answer="", action="refuse", citations=[]
    )


# ── plan에 없는 추가 조건(다른 트랙 PR에서 확정) ──────────────────────────


@pytest.mark.parametrize("rule", [NORMAL, SCAFFOLD])
def test_no_claims_and_empty_retrieved_cites_nothing(rule):
    out = finalize([plain("좋은 질문이에요.")], OK, [], rule, CFG)
    assert out.answer == "좋은 질문이에요."
    assert out.action == rule.action
    assert out.citations == []


def test_empty_retrieved_cannot_verify_any_quote():
    out = finalize(
        [claim("3회까지 기록해요.", ("PY-01-01", "3회까지의 점수를 기록한다"))], OK, [], NORMAL, CFG
    )
    assert out.citations == []
    assert out.answer == "3회까지 기록해요."


def test_unsafe_with_empty_retrieved_cites_nothing():
    verdict = OutputVerdict(grounded=True, unsupported=(), safe=False)
    out = finalize([plain("…")], verdict, [], NORMAL, CFG)
    assert out == TutorResult(answer=REFUSE, action="refuse", citations=[])


@pytest.mark.parametrize("action", [None, "answer", "redirect", "abstain"])
def test_unsafe_wins_over_the_chosen_action(action):
    verdict = OutputVerdict(grounded=True, unsupported=(), safe=False)
    sentences = [claim("3회까지 기록해요.", ("PY-01-01", "3회까지의 점수를 기록한다"))]
    out = finalize(sentences, verdict, RETRIEVED, NORMAL, CFG, action=action)
    assert out == TutorResult(answer=REFUSE, action="refuse", citations=[])


@pytest.mark.parametrize("rule", [NORMAL, SCAFFOLD, ABSTAIN, ACADEMIC, REFUSAL])
def test_unsafe_cites_nothing_whatever_the_rule(rule):
    verdict = OutputVerdict(grounded=True, unsupported=(), safe=False)
    sentences = [claim("3회까지 기록해요.", ("PY-01-01", "3회까지의 점수를 기록한다"))]
    out = finalize(sentences, verdict, RETRIEVED, rule, CFG)
    assert out == TutorResult(answer=REFUSE, action="refuse", citations=[])


def test_fixed_texts_come_only_from_cfg():
    other = FinalizeConfig(unverified_notice="N2", refuse_text="R2")
    assert finalize([], OK, RETRIEVED, NORMAL, other).answer == "N2"
    unsafe = OutputVerdict(grounded=True, unsupported=(), safe=False)
    assert finalize([], unsafe, RETRIEVED, NORMAL, other).answer == "R2"
    source = Path(actions.__file__).read_text(encoding="utf-8")
    assert "확인하지 못했" not in source


def test_integration_does_not_import_tutor():
    for path in (ROOT / "integration").glob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert "import tutor" not in text and "from tutor" not in text, path.name


def test_never_rule_ignores_evidence():
    out = finalize(
        [claim("3회까지 기록해요.", ("PY-01-01", "3회까지의 점수를 기록한다"))],
        OK,
        RETRIEVED,
        REFUSAL,
        CFG,
    )
    assert out.citations == []


# ── action 인자(주 호출 · 합치기 단계가 고른 action) ──────────────────────

HARMFUL = action_for("harmful_content")
VALID_CLAIM = ("PY-01-01", "3회까지의 점수를 기록한다")


def test_action_none_keeps_rule_action():
    sentences = [claim("3회까지 기록해요.", VALID_CLAIM)]
    assert finalize(sentences, OK, RETRIEVED, NORMAL, CFG, action=None) == finalize(
        sentences, OK, RETRIEVED, NORMAL, CFG
    )
    assert finalize(sentences, OK, RETRIEVED, HARMFUL, CFG).action == "refuse"


def test_action_is_keyword_only():
    with pytest.raises(TypeError):
        finalize([], OK, RETRIEVED, NORMAL, CFG, "answer")


def test_harmful_redirect_variant():
    sentences = [plain("그런 표현은 함께 쓰지 않아요."), claim("3회까지 기록해요.", VALID_CLAIM)]
    out = finalize(sentences, OK, RETRIEVED, HARMFUL, CFG, action="redirect")
    assert out.action == "redirect"
    assert out.answer == "그런 표현은 함께 쓰지 않아요. 3회까지 기록해요."
    assert out.citations == []


def test_normal_rule_with_abstain_is_if_used():
    """표에 없는 조합: claim이 실제로 쓴 섹션만 인용하고, 없으면 [](검색 1위 · 안내 문장 없음)."""
    sentences = [claim("3회까지 기록해요.", VALID_CLAIM)]
    used = finalize(sentences, OK, RETRIEVED, NORMAL, CFG, action="abstain")
    assert used == TutorResult(answer="3회까지 기록해요.", action="abstain", citations=["PY-01-01"])

    sentences = [plain("그 내용은 자료에 없어요.")]
    unused = finalize(sentences, OK, RETRIEVED, NORMAL, CFG, action="abstain")
    assert unused == TutorResult(answer="그 내용은 자료에 없어요.", action="abstain", citations=[])


@pytest.mark.parametrize("action", ["clarify", "redirect", "refuse", "escalate"])
@pytest.mark.parametrize("rule", [NORMAL, ABSTAIN, REFUSAL])
def test_no_citation_actions_always_cite_nothing(rule, action):
    sentences = [claim("3회까지 기록해요.", VALID_CLAIM)]
    out = finalize(sentences, OK, RETRIEVED, rule, CFG, action=action)
    assert out.action == action
    assert out.citations == []
    assert out.answer == "3회까지 기록해요."  # 안내 문장도 붙이지 않는다


def test_action_outside_the_table_is_not_blocked():
    # 표 강제는 튜터 설정 몫이다. 여기서는 받은 action을 그대로 낸다.
    out = finalize([plain("…")], OK, RETRIEVED, REFUSAL, CFG, action="answer")
    assert out.action == "answer"
    assert out.citations == []  # 표에 없는 조합 → if_used, 쓴 섹션 없음


def test_action_in_rule_actions_follows_rule_cite():
    sentences = [plain("힌트부터 볼까요?")]
    # answer_seeking + scaffold(표 값을 명시) → always: 남은 claim이 없으면 검색 1위로 채운다
    out = finalize(sentences, OK, RETRIEVED, SCAFFOLD, CFG, action="scaffold")
    assert out.answer == "힌트부터 볼까요?"
    assert (out.action, out.citations) == ("scaffold", ["PY-01-02"])
    # normal + scaffold(표에 없는 조합) → if_used
    other = finalize(sentences, OK, RETRIEVED, NORMAL, CFG, action="scaffold")
    assert other == TutorResult(answer="힌트부터 볼까요?", action="scaffold", citations=[])


def test_invalid_action_is_rejected():
    with pytest.raises(ValueError):
        finalize([], OK, RETRIEVED, NORMAL, CFG, action="allow")


# ── 4,000자: 뒤 문장부터 통째로 뺀다 ──────────────────────────────────────


def test_over_limit_drops_trailing_sentences_whole():
    sentences = [plain("가" * 1500 + "."), plain("나" * 1500 + "."), plain("다" * 1500 + ".")]
    out = finalize(sentences, OK, RETRIEVED, ABSTAIN, CFG)
    assert out.answer == "가" * 1500 + ". " + "나" * 1500 + "."
    assert len(out.answer) <= 4000


def test_exactly_at_limit_is_not_cut():
    sentences = [plain("가" * 1999), plain("나" * 2000)]  # 1999 + 공백 + 2000 = 4000
    out = finalize(sentences, OK, RETRIEVED, ABSTAIN, CFG)
    assert len(out.answer) == 4000
    assert out.answer.endswith("나")


def test_first_sentence_alone_over_limit_is_cut_by_chars():
    sentences = [plain("가" * 4500), plain("둘째.")]
    out = finalize(sentences, OK, RETRIEVED, ABSTAIN, CFG)
    assert out.answer == "가" * 4000


def test_citations_recomputed_from_sentences_that_remain():
    sentences = [
        claim("3회까지 기록해요." + "가" * 1000, VALID_CLAIM),
        claim("예산은 계획이에요." + "나" * 2000, ("FIN-02-03", "수입과 지출의 계획")),
        claim("금액은 표에 있어요." + "다" * 2000, ("FIN-02-03", "간식,3000")),
    ]
    roomy = FinalizeConfig(unverified_notice=NOTICE, refuse_text=REFUSE, max_answer_chars=10000)
    assert finalize(sentences, OK, RETRIEVED, NORMAL, roomy).citations == ["FIN-02-03"]
    out = finalize(sentences, OK, RETRIEVED, NORMAL, CFG)  # 셋째 문장이 빠진다
    assert out.answer == sentences[0].text + " " + sentences[1].text
    assert out.citations == ["PY-01-01"]  # 1 대 1 동률 → 검색 순위


def test_dropping_the_only_claim_adds_notice_and_top1():
    sentences = [plain("가" * 3000), claim("나" * 2000, VALID_CLAIM)]
    out = finalize(sentences, OK, RETRIEVED, NORMAL, CFG)
    assert out.answer == "가" * 3000 + " " + NOTICE
    assert out.action == "answer"
    assert out.citations == ["PY-01-02"]


def test_if_used_rule_loses_citation_when_its_claim_is_dropped():
    sentences = [plain("가" * 3000), claim("나" * 2000, VALID_CLAIM)]
    out = finalize(sentences, OK, RETRIEVED, ABSTAIN, CFG)
    assert out == TutorResult(answer="가" * 3000, action="abstain", citations=[])


def test_notice_that_does_not_fit_is_cut_by_chars():
    tight = FinalizeConfig(unverified_notice="안" * 50, refuse_text=REFUSE, max_answer_chars=20)
    verdict = OutputVerdict(grounded=False, unsupported=("틀린 말",), safe=True)
    out = finalize([plain("가" * 10), claim("틀린 말.")], verdict, RETRIEVED, NORMAL, tight)
    assert out.answer == "가" * 10 + " " + "안" * 9


def test_limit_counts_after_control_characters_are_removed():
    sentences = [plain("가" * 2000 + "\x00" * 50), plain("나" * 1999)]
    out = finalize(sentences, OK, RETRIEVED, ABSTAIN, CFG)
    assert out.answer == "가" * 2000 + " " + "나" * 1999


def test_empty_answer_is_returned_as_is():
    # 빈 답변을 action별 고정 문장으로 채우는 것은 튜터 후처리(TU-07) 몫이다.
    verdict = OutputVerdict(grounded=False, unsupported=("전부 틀린 말",), safe=True)
    out = finalize([plain("전부 틀린 말.")], verdict, RETRIEVED, ABSTAIN, CFG)
    assert out == TutorResult(answer="", action="abstain", citations=[])


# ── 짧은 unsupported 구간 무시(min_span_chars) ────────────────────────────


def test_min_span_chars_default_is_two():
    assert CFG.min_span_chars == 2


@pytest.mark.parametrize("span", ["요", " 요. ", "…요"])
def test_one_char_span_does_not_delete_anything(span):
    sentences = [claim("3회까지 기록해요.", VALID_CLAIM), plain("더 궁금한 점이 있나요?")]
    verdict = OutputVerdict(grounded=False, unsupported=(span,), safe=True)
    out = finalize(sentences, verdict, RETRIEVED, NORMAL, CFG)
    assert out.answer == "3회까지 기록해요. 더 궁금한 점이 있나요?"
    assert out.citations == ["PY-01-01"]


def test_two_char_span_still_deletes():
    sentences = [claim("3회까지 기록해요.", VALID_CLAIM), plain("더 궁금한 점이 있나요?")]
    verdict = OutputVerdict(grounded=False, unsupported=("해요",), safe=True)
    out = finalize(sentences, verdict, RETRIEVED, ABSTAIN, CFG)
    assert out.answer == "더 궁금한 점이 있나요?"


def test_short_span_is_skipped_but_others_still_apply():
    sentences = [plain("첫 문장이에요."), plain("둘째 문장이에요.")]
    verdict = OutputVerdict(grounded=False, unsupported=("요", "둘째 문장"), safe=True)
    assert finalize(sentences, verdict, RETRIEVED, ABSTAIN, CFG).answer == "첫 문장이에요."


def test_min_span_chars_is_configurable():
    sentences = [plain("첫 문장이에요."), plain("둘째 문장.")]
    verdict = OutputVerdict(grounded=False, unsupported=("요", "둘째"), safe=True)
    one = FinalizeConfig(unverified_notice=NOTICE, refuse_text=REFUSE, min_span_chars=1)
    # 1이면 "요"가 첫 문장을, "둘째"가 둘째 문장을 지운다.
    assert finalize(sentences, verdict, RETRIEVED, ABSTAIN, one).answer == ""
    three = FinalizeConfig(unverified_notice=NOTICE, refuse_text=REFUSE, min_span_chars=3)
    assert (
        finalize(sentences, verdict, RETRIEVED, ABSTAIN, three).answer
        == "첫 문장이에요. 둘째 문장."
    )


# ── 안내 문장을 붙이는 조건 ───────────────────────────────────────────────


@pytest.mark.parametrize("verdict", [None, OK], ids=["judge_skipped", "judge_ok"])
@pytest.mark.parametrize("rule", [NORMAL, SCAFFOLD])
def test_answer_without_any_claim_gets_no_notice(rule, verdict):
    sentences = [plain("좋은 질문이에요."), plain("같이 살펴볼까요?")]
    out = finalize(sentences, verdict, RETRIEVED, rule, CFG)
    assert out.answer == "좋은 질문이에요. 같이 살펴볼까요?"
    assert out.action == rule.action
    assert out.citations == ["PY-01-02"]  # 인용 채우기(검색 1위)는 그대로


def test_notice_when_judge_removed_the_last_claim():
    sentences = [plain("좋은 질문이에요."), claim("우승팀은 항상 홈팀이에요.")]
    verdict = OutputVerdict(grounded=False, unsupported=("항상 홈팀",), safe=True)
    out = finalize(sentences, verdict, RETRIEVED, NORMAL, CFG)
    assert out.answer == f"좋은 질문이에요. {NOTICE}"
    assert out.citations == ["PY-01-02"]


def test_no_notice_when_judge_removed_only_a_non_claim():
    sentences = [plain("좋은 질문이에요."), plain("우승팀은 항상 홈팀이에요.")]
    verdict = OutputVerdict(grounded=False, unsupported=("항상 홈팀",), safe=True)
    out = finalize(sentences, verdict, RETRIEVED, NORMAL, CFG)
    assert out.answer == "좋은 질문이에요."


def test_no_notice_while_a_claim_remains():
    sentences = [claim("3회까지 기록해요.", VALID_CLAIM), claim("우승팀은 항상 홈팀이에요.")]
    verdict = OutputVerdict(grounded=False, unsupported=("항상 홈팀",), safe=True)
    out = finalize(sentences, verdict, RETRIEVED, NORMAL, CFG)
    assert out.answer == "3회까지 기록해요."


def test_notice_only_when_nothing_remains():
    assert finalize([], None, RETRIEVED, NORMAL, CFG).answer == NOTICE
    # non_claim 한 문장뿐인 답이 판정으로 지워져도 남은 문장이 없으므로 안내만 낸다.
    verdict = OutputVerdict(grounded=False, unsupported=("전부 틀린 말",), safe=True)
    out = finalize([plain("전부 틀린 말.")], verdict, RETRIEVED, NORMAL, CFG)
    assert out == TutorResult(answer=NOTICE, action="answer", citations=["PY-01-02"])


def test_no_notice_when_length_cut_removed_only_non_claims():
    sentences = [plain("가" * 3000), plain("나" * 2000)]
    out = finalize(sentences, OK, RETRIEVED, NORMAL, CFG)
    assert out.answer == "가" * 3000


# ── 세부 동작 ─────────────────────────────────────────────────────────────


def test_normalize_equals_grader_normalize():
    samples = [p.read_text(encoding="utf-8") for p in sorted((COMPETITION / "kb").glob("*/*.md"))]
    samples += ["ＡＢＣ “따옴표” — 줄\n바꿈\t탭 · … ~ / \\ ①㈜ Ⅳ ﬁ", "", "  ", "İ ß"]
    samples += [r["labels"]["reference_answer"] for r in ROWS]
    samples += [m["content"] for r in ROWS for m in r["messages"]]
    assert len(samples) > 48 + 200
    for s in samples:
        assert actions._normalize(s) == scoring.normalize(s)


@pytest.mark.parametrize(
    "quote",
    [
        "3회까지의 점수를 기록한다",
        "3회까지의점수를기록한다",  # 공백 차이
        "3회까지의 점수를 기록한다.",  # 문장 부호
        "야구 점수판은 3회까지의 점수를 기록한다. 점수는",  # 문장 경계를 넘는 인용
        "３회까지의 점수를 기록한다",  # 전각 숫자(NFKC)
    ],
)
def test_quote_matches_under_grader_normalization(quote):
    out = finalize([claim("x", ("PY-01-01", quote))], OK, RETRIEVED, NORMAL, CFG)
    assert out.citations == ["PY-01-01"]


@pytest.mark.parametrize("quote", ["", " . ", "…"])
def test_quote_without_content_is_not_evidence(quote):
    out = finalize([claim("x", ("PY-01-01", quote))], OK, RETRIEVED, NORMAL, CFG)
    assert out.citations == ["PY-01-02"]


def test_quote_from_a_table_is_accepted():
    out = finalize(
        [claim("간식은 3,000원이에요.", ("FIN-02-03", "간식,3000"))], OK, RETRIEVED, NORMAL, CFG
    )
    assert out.citations == ["FIN-02-03"]


def test_quote_must_come_from_the_cited_section():
    out = finalize([claim("x", ("PY-01-02", "3회까지의 점수를 기록한다"))], OK, [A, B], NORMAL, CFG)
    assert out.citations == ["PY-01-01"]  # B에서 따온 문구가 아니므로 evidence 제거 → 검색 1위(A)


def test_one_valid_evidence_among_several_is_enough():
    s = claim("x", ("PY-01-02", "없는 문구"), ("PY-01-01", "점수는 리스트에 담는다"))
    assert finalize([s], OK, RETRIEVED, NORMAL, CFG).citations == ["PY-01-01"]


def test_unsupported_span_across_sentences_deletes_both():
    sentences = [
        plain("첫 문장이에요."),
        claim("둘째 문장."),
        claim("셋째 문장."),
        plain("넷째 문장."),
    ]
    verdict = OutputVerdict(grounded=False, unsupported=("둘째 문장. 셋째",), safe=True)
    out = finalize(sentences, verdict, RETRIEVED, ABSTAIN, CFG)
    assert out.answer == "첫 문장이에요. 넷째 문장."


def test_unsupported_span_matches_every_occurrence():
    sentences = [plain("같은 말."), plain("다른 말."), plain("같은 말.")]
    verdict = OutputVerdict(grounded=False, unsupported=("같은 말",), safe=True)
    assert finalize(sentences, verdict, RETRIEVED, ABSTAIN, CFG).answer == "다른 말."


@pytest.mark.parametrize("span", ["", " ", "…", "답변에 없는 말"])
def test_unsupported_span_not_in_answer_changes_nothing(span):
    verdict = OutputVerdict(grounded=False, unsupported=(span,), safe=True)
    out = finalize([plain("첫 문장이에요.")], verdict, RETRIEVED, ABSTAIN, CFG)
    assert out.answer == "첫 문장이에요."


def test_grounded_flag_alone_changes_nothing():
    verdict = OutputVerdict(grounded=False, unsupported=(), safe=True)
    out = finalize(
        [claim("3회까지 기록해요.", ("PY-01-01", "3회까지의 점수를 기록한다"))],
        verdict,
        RETRIEVED,
        NORMAL,
        CFG,
    )
    assert out == TutorResult(answer="3회까지 기록해요.", action="answer", citations=["PY-01-01"])


def test_control_characters_removed_but_newline_and_tab_kept():
    out = finalize(
        [plain("첫 줄\n둘째\t줄\x00\x07\x1b\x7f\x85끝\ud800")], OK, RETRIEVED, REFUSAL, CFG
    )
    assert out.answer == "첫 줄\n둘째\t줄끝"
    json.dumps(out.model_dump(), ensure_ascii=False).encode("utf-8")


def test_cut_is_applied_after_control_characters_are_removed():
    out = finalize([plain("\x00" * 10 + "가" * 4000)], OK, RETRIEVED, REFUSAL, CFG)
    assert out.answer == "가" * 4000


def test_empty_sentence_texts_are_skipped_when_joining():
    out = finalize([plain("앞."), plain(""), plain("뒤.")], OK, RETRIEVED, REFUSAL, CFG)
    assert out.answer == "앞. 뒤."


def test_duplicate_citations_removed():
    sentences = [claim("a", ("PY-01-01", "3회까지"), ("PY-01-01", "리스트에 담는다"))]
    two = FinalizeConfig(unverified_notice=NOTICE, refuse_text=REFUSE, max_citations=2)
    assert finalize(sentences, OK, RETRIEVED, NORMAL, two).citations == ["PY-01-01"]


def test_inputs_are_not_mutated():
    sentences = [claim("5회.", ("PY-01-01", "없는 문구")), plain("끝.")]
    before = [s.model_copy(deep=True) for s in sentences]
    retrieved = list(RETRIEVED)
    finalize(sentences, OutputVerdict(False, ("5회",), True), retrieved, NORMAL, CFG)
    assert sentences == before
    assert retrieved == RETRIEVED


def test_same_input_gives_same_output():
    sentences = [
        claim("3회까지 기록해요.", ("PY-01-01", "3회까지의 점수를 기록한다")),
        plain("끝."),
    ]
    assert finalize(sentences, OK, RETRIEVED, NORMAL, CFG) == finalize(
        sentences, OK, RETRIEVED, NORMAL, CFG
    )


@pytest.mark.parametrize(
    "bad",
    [{"max_citations": -1}, {"max_answer_chars": 0}, {"min_span_chars": 0}, {"extra": 1}],
)
def test_config_rejects_bad_values(bad):
    with pytest.raises(ValueError):
        FinalizeConfig(unverified_notice=NOTICE, refuse_text=REFUSE, **bad)


def test_config_requires_the_fixed_texts():
    with pytest.raises(ValueError):
        FinalizeConfig()


# ── 공개 세트 회귀 ────────────────────────────────────────────────────────


def _as_input(row: dict) -> tuple[list[Sentence], list[Section]]:
    """정답 답변을 Sentence 하나로 감싼다. 인용 문구는 정답 섹션 원문의 한 줄."""
    labels = row["labels"]
    if not labels["evidence"]:
        return [plain(labels["reference_answer"])], list(KB.values())[:3]
    sid = labels["evidence"][0]
    quote = next(line for line in KB[sid].text.splitlines() if len(actions._normalize(line)) >= 10)
    # 정답 섹션을 검색 1위가 아닌 자리에 둔다(인용이 claim에서 나와야 통과한다).
    others = [s for k, s in KB.items() if k != sid][:2]
    return [claim(labels["reference_answer"], (sid, quote))], [others[0], KB[sid], others[1]]


def test_public_set_has_119_rows_with_evidence():
    assert len(ROWS) == 200
    assert len(EVIDENCE_ROWS) == 119


@pytest.mark.parametrize("verdict", [None, OK], ids=["judge_skipped", "judge_ok"])
def test_reference_answers_pass_unchanged_with_citation_f1_1(verdict):
    changed, f1 = [], []
    for row in EVIDENCE_ROWS:
        labels = row["labels"]
        sentences, retrieved = _as_input(row)
        out = finalize(sentences, verdict, retrieved, action_for(labels["category"]), CFG)
        if out.answer != labels["reference_answer"] or out.action != labels["action"]:
            changed.append(row["id"])
        scored = scoring.score_tutor(
            {"labels": labels, "kind": "normal"},
            {"gold_facts": []},
            out.model_dump(),
            lambda *_: {"ok": True},
            {},
        )
        assert scored["valid"], row["id"]
        p, r = scored["citation_p"], scored["citation_r"]
        f1.append(2 * p * r / (p + r) if p + r else 0.0)
    assert changed == []
    assert f1 == [1.0] * 119


def test_rows_without_evidence_pass_unchanged_and_uncited():
    for row in ROWS:
        labels = row["labels"]
        if labels["evidence"]:
            continue
        sentences, retrieved = _as_input(row)
        rule = action_for(labels["category"])
        out = finalize(sentences, OK, retrieved, rule, CFG)
        assert out.answer == labels["reference_answer"], row["id"]
        assert out.citations == [], row["id"]
        assert out.action == rule.action, row["id"]


def test_all_200_rows_under_50ms():
    inputs = [(*_as_input(r), action_for(r["labels"]["category"])) for r in ROWS]
    best = float("inf")
    for _ in range(5):
        start = time.perf_counter()
        for sentences, retrieved, rule in inputs:
            finalize(sentences, OK, retrieved, rule, CFG)
        best = min(best, time.perf_counter() - start)
    assert best < 0.050, f"{best * 1000:.1f}ms"
