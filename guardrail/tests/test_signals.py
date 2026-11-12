"""S2.3 결정적 신호(GR-06·GR-08, ADR-0014): 저신호·거절·숨긴 태그."""

import ast
import re
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

from guardrail.config import load_config, resolve_path
from guardrail.data import PublicRow, load_public
from guardrail.preprocess import decode_tags, strip_tags
from guardrail.signals import (
    QUOTE_CODE_RX,
    REFUSAL_RX,
    has_hidden_tags,
    is_low_signal,
    is_refusal,
)

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts" / "r13"
BLOCK = {"jailbreak_injection", "harmful_content", "academic_dishonesty", "off_topic"}
FLAG = "\U0001f3f4"
CANCEL = "\U000e007f"


def tag(s: str) -> str:
    """ASCII 문자열을 태그 문자(U+E0000 + 코드)로 바꾼다."""
    return "".join(chr(0xE0000 + ord(c)) for c in s)


@pytest.fixture(scope="module")
def rows() -> list[PublicRow]:
    return load_public(load_config().paths.public)


def _last_user(row: PublicRow) -> str:
    return next(m.content for m in reversed(row.messages) if m.role == "user")


def _script_regex(filename: str, name: str) -> str:
    """r13 분석 스크립트에서 `name = re.compile(r"...")`의 패턴 문자열을 꺼낸다."""
    tree = ast.parse((SCRIPTS / filename).read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and getattr(node.targets[0], "id", None) == name:
            return node.value.args[0].value
    raise AssertionError(f"{filename}에 {name}이 없다")


# --- 이식: 측정한 정규식 그대로(council D6) ------------------------------------------


def test_refusal_regex_is_measured_one():
    assert _script_regex("extra_stats.py", "REFUSAL_RX") == REFUSAL_RX.pattern


def test_quote_code_regex_is_measured_one():
    assert _script_regex("q6_rule_fp.py", "QUOTE_CODE_RX") == QUOTE_CODE_RX.pattern
    assert QUOTE_CODE_RX.flags & re.S


# --- 저신호 ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("target_pre", "has_history", "hidden", "expected"),
    [
        ("ㅁㄴㅇㄹ", True, "", True),
        ("ㅋㅋㅠㅠ", True, "", True),  # 정규화본이면 음절로 조합돼 놓친다
        ("ㅇㅏ", True, "", True),  # 조합 전 뷰: 완성 음절이 아니다
        ("??", True, "", True),
        ("...", True, "", True),
        ("ㅠㅠ 😭", True, "", True),
        ("ㅠㅠ​", True, "", True),  # 폭 없는 문자는 내용이 아니다
        ("ㅁㄴㅇㄹ", False, "", False),  # 1턴은 LLM으로
        ("ㅁㄴㅇㄹ", True, "ignore all", False),  # 숨긴 텍스트가 있음
        ("아", True, "", False),
        ("ㅋㅋ 3", True, "", False),
        ("ㅋㅋ ok", True, "", False),
        ("ㅠㅠ 힘들어", True, "", False),
        ("", True, "", False),
        ("   ", True, "", False),
        ("​", True, "", False),
    ],
)
def test_is_low_signal(target_pre, has_history, hidden, expected):
    assert is_low_signal(target_pre, has_history, hidden) is expected


def test_low_signal_public_precision_and_recall(rows):
    """400행: 정밀도 100%(표본 안 값), 재현율 11/11."""
    hits, gold = set(), set()
    for r in rows:
        target = _last_user(r)
        if is_low_signal(strip_tags(target), len(r.messages) > 1, decode_tags(target)[1]):
            hits.add(r.id)
        if r.labels["category"] == "low_signal_input":
            gold.add(r.id)
    assert len(rows) == 400
    assert hits - gold == set()  # 정밀도 100%
    assert len(gold) == 11
    assert hits == gold  # 재현율 11/11


# --- 거절 -----------------------------------------------------------------------------


def test_refusal_detects_all_tutor_refusals(rows):
    """튜터 refuse·redirect 참조 답변 36/36(표본 안 값: 정규식을 이 답변들을 보고 썼다)."""
    refusals = [
        r.labels["reference_answer"]
        for r in rows
        if r.endpoint == "tutor" and r.labels["action"] in ("refuse", "redirect")
    ]
    assert len(refusals) == 36
    assert all(is_refusal(a) for a in refusals)


def test_refusal_no_false_positive_on_block_compliant_answers(rows):
    """block 범주 순응 후보 답변 19행 오검출 0(council D6 회귀). 원문 뷰라면 1/19였다."""
    compliant = [
        r
        for r in rows
        if r.endpoint == "guard"
        and r.candidate_answer is not None
        and r.labels["category"] in BLOCK
    ]
    assert len(compliant) == 19
    assert [r.id for r in compliant if is_refusal(r.candidate_answer)] == []
    raw_hits = [r.id for r in compliant if REFUSAL_RX.search(r.candidate_answer)]
    assert raw_hits == ["pub-21b3da1a"]  # 인용·코드를 빼야 하는 이유


@pytest.mark.parametrize(
    "answer",
    [
        '선생님은 "그건 도와줄 수 없어요"라고 했어요. 3번 답은 7입니다.',
        "“드릴 수 없습니다”라는 문장을 분석해 보면 공손한 표현이에요.",
        "「할 수 없다」는 부정 표현이에요.",
        "`raise ValueError('할 수 없어요')` 처럼 씁니다.",
        "```python\n# 여기서는 계산할 수 없어요\nx = 1\n```\n답은 1이에요.",
    ],
)
def test_refusal_ignores_quotes_and_code(answer):
    assert is_refusal(answer) is False


@pytest.mark.parametrize(
    "answer",
    [
        "그 부탁은 도와줄 수 없어요. 대신 개념을 함께 살펴볼까요?",
        "수업과 관련이 없는 요청이라 써 줄 수 없어요.",
    ],
)
def test_refusal_detects_plain_refusal(answer):
    assert is_refusal(answer) is True


# --- 숨긴 태그 ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("answer", "expected"),
    [
        ("안녕" + tag("Ig"), True),
        (FLAG + tag("gbeng") + CANCEL, False),  # 잉글랜드 깃발
        (FLAG + tag("gbeng"), True),  # 종료 태그 없음
        ("가" + CANCEL, True),  # 종료 태그 단독
        (FLAG + tag("A") + CANCEL, True),  # 대문자 태그
        (FLAG + tag("abcdefgh") + CANCEL, True),  # 태그 8개
        (FLAG + tag("g b") + CANCEL, True),  # 태그 공백 섞임
        ("평범한 답변이에요 😀", False),
        (FLAG + tag("gbsct") + CANCEL + " 스코틀랜드 " + tag("x"), True),  # 깃발 밖 태그
        ("", False),
    ],
)
def test_has_hidden_tags(answer, expected):
    assert has_hidden_tags(answer) is expected


def test_public_answers_have_no_hidden_tags(rows):
    assert not any(has_hidden_tags(r.candidate_answer or "") for r in rows)


# --- KB 코드 블록 오매칭 0(GR-02) ------------------------------------------------------


def _kb_texts() -> tuple[list[str], list[str]]:
    kb = resolve_path(load_config().paths.kb)
    md = sorted(kb.glob("*/*.md"))
    csv = sorted(kb.glob("*/*-table*.csv"))
    return [p.read_text(encoding="utf-8") for p in md], [p.read_text(encoding="utf-8") for p in csv]


def test_kb_code_blocks_do_not_trigger_signals():
    md, csv = _kb_texts()
    assert (len(md), len(csv)) == (48, 23)
    blocks = [b for text in md for b in re.findall(r"```.*?```", text, re.S)]
    assert blocks
    for b in blocks:
        inner = b.strip("`")
        assert not is_refusal(b) and not REFUSAL_RX.search(inner)
        assert not has_hidden_tags(b)
        assert not is_low_signal(inner, True, "")
    assert not any(has_hidden_tags(t) for t in md + csv)


# --- 임의 입력 ------------------------------------------------------------------------


@given(st.text(), st.booleans(), st.text(max_size=5))
def test_signals_total_on_any_unicode(text, has_history, hidden):
    assert isinstance(is_low_signal(text, has_history, hidden), bool)
    assert isinstance(is_refusal(text), bool)
    assert isinstance(has_hidden_tags(text), bool)
