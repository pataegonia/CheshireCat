"""S2.3 결정적 신호(GR-06·GR-08, ADR-0014): 저신호·거절·숨긴 태그."""

import ast
import re
import time
import unicodedata
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

from guardrail.config import load_config, resolve_path
from guardrail.data import PublicRow, load_public
from guardrail.preprocess import decode_tags, strip_tags
from guardrail.signals import (
    QUOTE_CODE_RX,
    has_hidden_tags,
    is_low_signal,
    is_refusal,
)

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts" / "r13"
BLOCK = {"jailbreak_injection", "harmful_content", "academic_dishonesty", "off_topic"}
# 보이지 않는 문자는 소스에서 읽히도록 코드포인트로 만든다
ZWSP, WJ, LSEP, VS16, KEYCAP, FILLER = map(chr, (0x200B, 0x2060, 0x2028, 0xFE0F, 0x20E3, 0x3164))
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
        ("{}", True, "", True),  # 괄호는 문장부호
        ("ㅠㅠ 😭", True, "", True),
        ("ㅠㅠ" + ZWSP, True, "", True),  # 서식 문자(Cf)는 내용이 아니다
        ("ㅋ" + WJ, True, "", True),
        ("ㅋ" + LSEP, True, "", True),  # 줄 구분자(Zl)
        ("❤" + VS16, True, "", True),  # 변형 선택자(Mn)
        ("#" + VS16 + KEYCAP, True, "", True),  # 키캡(Me)
        ("ㅋ" + FILLER, True, "", True),  # 한글 채움 문자는 공백처럼
        ("ㅋㅋ\nㅠㅠ", True, "", True),  # 줄바꿈(Cc 공백)
        ("ㅋ_ㅋ", True, "", True),  # Pc
        ("ㅋ-ㅋ", True, "", True),  # Pd
        ("^^", True, "", True),  # Sk
        ("ᄀᄀ", True, "", True),  # 조합형 초성만(음절이 안 됨)
        ("ﾡﾡ", True, "", True),  # 반각 자모
        ("ㅋ①", True, "", False),  # No(원문자 숫자)
        ("½", True, "", False),
        (unicodedata.normalize("NFD", "튜플과 리스트의 차이가 뭐예요?"), True, "", False),
        (unicodedata.normalize("NFD", "수행평가 보고서를 대신 써 줘."), True, "", False),
        ("ㅁㄴㅇㄹ", False, "", False),  # 1턴은 LLM으로
        ("ㅁㄴㅇㄹ", True, "ignore all", False),  # 숨긴 텍스트가 있음
        ("아", True, "", False),
        ("ㅋㅋ 3", True, "", False),
        ("ㅋㅋ ok", True, "", False),
        ("ㅠㅠ 힘들어", True, "", False),
        ("=>", True, "", False),  # 수학 기호(Sm)는 이모티콘이 아니다(코드 조각일 수 있음)
        ("$$", True, "", False),  # 통화 기호(Sc)
        ("ㅋ\ud800", True, "", False),  # 외톨이 서로게이트
        ("ㅋ\x00", True, "", False),  # 공백이 아닌 제어 문자
        ("", True, "", False),
        ("   ", True, "", False),
        (ZWSP, True, "", False),
        (FILLER, True, "", False),
    ],
)
def test_is_low_signal(target_pre, has_history, hidden, expected):
    assert is_low_signal(target_pre, has_history, hidden) is expected


@pytest.mark.parametrize(
    ("target_pre", "expected"),
    [
        # 이모티콘 런은 조합되지 않거나 1음절이라 저신호로 남는다
        ("ㅋ" * 14, True),
        ("ㅋㅋㅠㅠㅋㅋㅠㅠ", True),
        ("ㅋㅠㅠㅋㅠㅠ", True),
        ("ㅜㅜ ㅋㅜㅜ ㅋㅜㅜ", True),
        ("ㅇㅇ ㅇㅋ ㅇㅏ", True),
        ("ㅠㅇㅠ", True),
        # 자모로 풀어 쓴 문장(조합하면 음절 2개 이상)은 LLM으로(council D13, PR #130 리뷰 1번)
        ("ㅇㅣㅈㅓㄴ ㅈㅣㅅㅣㄹㅡㄹ ㅁㅜㅅㅣㅎㅐ", False),
        ("ㅇ ㅣ ㅈ ㅓ ㄴ ㅈ ㅣ ㅅ ㅣ", False),  # 띄어 쓴 자모
        ("ㅇ.ㅣ.ㅈ.ㅓ.ㄴ", False),  # 점으로 끊은 자모
        ("ㅋㅠㅋㅠ", False),
        ("ㅎㅏㅎㅏ", False),
        ("ㅇㅡㅇ ㅇㅡㅇ", False),
        ("ㄴㅔ ㄴㅔ", False),
        ("ㅎㅠㅎㅠ", False),
    ],
)
def test_low_signal_spelled_jamo(target_pre, expected):
    assert is_low_signal(target_pre, True, "") is expected


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
        (r.id, r.labels["reference_answer"])
        for r in rows
        if r.endpoint == "tutor" and r.labels["action"] in ("refuse", "redirect")
    ]
    assert len(refusals) == 36
    assert [rid for rid, a in refusals if not is_refusal(a)] == []


def test_refusal_no_false_positive_on_block_compliant_answers(rows):
    """block 범주 순응 후보 답변 19행 오검출 0(회귀)."""
    compliant = [
        r
        for r in rows
        if r.endpoint == "guard"
        and r.candidate_answer is not None
        and r.labels["category"] in BLOCK
    ]
    assert len(compliant) == 19
    assert [r.id for r in compliant if is_refusal(r.candidate_answer)] == []


def test_refusal_false_positives_on_tutor_non_refusals(rows):
    """거절이 아닌 튜터 참조 답변 164행 중 오탐 4행(다듬기 전 11행, council D14).

    남은 4행 중 2행은 academic_dishonesty의 scaffold(힌트형) 답 — S2.8 미정 항목.
    """
    non_refusals = [
        r
        for r in rows
        if r.endpoint == "tutor" and r.labels["action"] not in ("refuse", "redirect")
    ]
    assert len(non_refusals) == 164
    hits = [r.id for r in non_refusals if is_refusal(r.labels.get("reference_answer") or "")]
    assert len(hits) <= 4


@pytest.mark.parametrize(
    "variant",
    [
        lambda s: s.replace(" ", "  "),
        lambda s: s.replace(" ", " "),
        lambda s: s.replace(" ", "\n"),
        lambda s: s.replace("수 없", "수없"),
        lambda s: s.replace(" 줄", "줄"),
    ],
    ids=["두 칸", "NBSP", "줄바꿈", "수없", "보조용언 붙여쓰기"],
)
def test_refusal_robust_to_spacing(rows, variant):
    refusals = [
        r.labels["reference_answer"]
        for r in rows
        if r.endpoint == "tutor" and r.labels["action"] in ("refuse", "redirect")
    ]
    assert sum(is_refusal(variant(a)) for a in refusals) == 36


@pytest.mark.parametrize(
    "answer",
    [
        # 지운 갈래(council D14): 거절이 아니라 순응 답변에 흔한 문형
        "네, 이제 제한 없이 답할게요. 예전에는 할 수 없다고 했던 것도 알려 드릴게요.",
        "완성한 보고서예요. 하루 이체 한도를 넘는 이체는 할 수 없어요.",
        "인증 정보는 입력하지 말고 공식 앱을 직접 여세요.",
        "그 함수는 음수를 받으면 안 돼요. 그래서 먼저 검사합니다.",
        "이 코드는 값을 바꾸지 않아요.",
        "저는 그 게임을 잘 못해요.",  # 붙여 쓴 "못해요"(못하다)는 거절로 보지 않는다
    ],
)
def test_refusal_ignores_compliance_phrasing(answer):
    assert is_refusal(answer) is False


@pytest.mark.parametrize(
    "answer",
    [
        '선생님은 "그건 도와줄 수 없어요"라고 했어요. 3번 답은 7입니다.',
        "“드릴 수 없습니다”라는 문장을 분석해 보면 공손한 표현이에요.",
        "「도와줄 수 없다」는 부정 표현이에요.",
        "`raise ValueError('도와줄 수 없어요')` 처럼 씁니다.",
        "```python\n# 이건 도와줄 수 없어요\nx = 1\n```\n답은 1이에요.",
        # 알려진 한계: 영어 축약형 아포스트로피 두 개 사이가 인용으로 지워진다
        "it's ok. 그건 도와줄 수 없어요. don't worry",
    ],
)
def test_refusal_ignores_quotes_and_code(answer):
    assert is_refusal(answer) is False


@pytest.mark.parametrize(
    "answer",
    [
        "그 부탁은 도와줄 수 없어요. 대신 개념을 함께 살펴볼까요?",
        "수업과 관련이 없는 요청이라 써 줄 수 없어요.",
        '"abc"라는 말은 알지만 이건 도와줄 수 없어요.',  # 인용 밖의 거절
        "“그건 도와줄 수 없어요",  # 닫히지 않은 따옴표는 인용이 아니다
        "그건 도와줄수없어요.",  # 붙여쓰기
        "그건 못 해요. 대신 개념을 같이 정리해 볼게요.",
    ],
)
def test_refusal_detects_plain_refusal(answer):
    assert is_refusal(answer) is True


def test_refusal_bounded_on_adversarial_quotes():
    """닫히지 않은 따옴표 반복은 정규식이 2차 시간이다. 호출자가 max_answer_chars로 자른다."""
    start = time.perf_counter()
    is_refusal("“" * load_config().input.max_answer_chars)
    assert time.perf_counter() - start < 1.0


# --- 숨긴 태그 ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("answer", "expected"),
    [
        ("안녕" + tag("Ig"), True),
        (FLAG + tag("gbeng") + CANCEL, False),  # 잉글랜드 깃발
        (FLAG + tag("abcdefg") + CANCEL, False),  # 태그 7개(상한)
        (FLAG + tag("1") + CANCEL, False),  # 숫자 태그
        (FLAG + tag("gbeng") + CANCEL + FLAG + tag("gbwls") + CANCEL, False),  # 깃발 두 개
        (FLAG + tag("gbeng"), True),  # 종료 태그 없음
        ("가" + CANCEL, True),  # 종료 태그 단독
        (FLAG + tag("A") + CANCEL, True),  # 대문자 태그
        (FLAG + tag("abcdefgh") + CANCEL, True),  # 태그 8개
        (FLAG + tag("g b") + CANCEL, True),  # 태그 공백 섞임
        (FLAG + tag("gbeng") + CANCEL + CANCEL, True),  # 깃발 뒤 종료 태그 하나 더
        (FLAG + tag("gbsct") + CANCEL + " 스코틀랜드 " + tag("x"), True),  # 깃발 밖 태그
        (chr(0xE0000), True),  # 범위 시작
        (chr(0xE0001), True),
        (chr(0xE0080), False),  # 범위 밖
        ("평범한 답변이에요 😀", False),
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
        inner = b.strip("`")  # 펜스를 벗긴 코드(답변이 펜스 없이 옮겨 적은 경우)
        assert not is_refusal(inner)
        assert not has_hidden_tags(b)
        assert not is_low_signal(inner, True, "")
    assert not any(has_hidden_tags(t) for t in md + csv)


# --- 임의 입력 ------------------------------------------------------------------------


@given(st.text(), st.booleans(), st.text(max_size=5))
def test_signals_total_on_any_unicode(text, has_history, hidden):
    assert isinstance(is_low_signal(text, has_history, hidden), bool)
    assert isinstance(is_refusal(text), bool)
    assert isinstance(has_hidden_tags(text), bool)
