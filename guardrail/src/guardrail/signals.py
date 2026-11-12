"""결정적 신호 (GR-06, GR-08, ADR-0014). LLM 없이 돈다.

- is_low_signal: 저신호 직접 판정(입력). 자모 조합 전 뷰(`preprocess.strip_tags`)에 건다.
- is_refusal: 거절·대안 제시(출력 safe 규칙에만). 인용·코드를 뺀 원문에 건다.
- has_hidden_tags: 깃발 이모지 밖 태그 문자(출력 safe). 길이 상한으로 자르기 전 답변 전체에 건다.

정규식 두 개는 R13-A가 잰 것을 고치지 않고 옮겼다(council D6, provenance "팀 작성").
REFUSAL_RX = `scripts/r13/extra_stats.py`, QUOTE_CODE_RX = `scripts/r13/q6_rule_fp.py`.
공개 세트 튜터 거절 답변 36건을 보고 쓴 정규식이라 36/36은 표본 안 값이다.
"""

import re
import unicodedata
from dataclasses import dataclass

from guardrail.preprocess import FLAG_SEQUENCE, TAG_CHARS


@dataclass(frozen=True)
class Signals:
    low_signal: bool
    refusal: bool
    hidden_tags: bool


REFUSAL_RX = re.compile(
    r"수업과 (무관|관련이 없)|써 줄 수 없|수는 없|말아 주세요|계속 도와|돌아가 볼게요|"
    r"함께 (공부|살펴)|하지 말|할 수 없|안 돼요|안 돼\b|도와줄 수 없|드릴 수 없|"
    r"알려 줄 수 없|맡을 수 없|않을게요|하지 않아요|대신 .{0,20}(도와|알려|할 수)|못 해요|"
    r"공개하지 않"
)
# 따옴표(“” "" ‘’ '' 「」 『』) 안 인용과 백틱·코드 블록. 거절 정규식은 이것을 지운 뷰에 건다
QUOTE_CODE_RX = re.compile(
    r"```.*?```|`[^`]*`|\"[^\"]*\"|“[^”]*”|‘[^’]*’|'[^']*'|「[^」]*」|『[^』]*』", re.S
)
# 조합형 자모, 호환 자모, 반각 자모(채움 문자 U+115F·U+1160·U+3164·U+FFA0은 뺀다)
_JAMO = re.compile("[ᄀ-ᅞᅡ-ᇿㄱ-ㅣㅥ-ㆎﾡ-ￜ]")
_FILLERS = frozenset("ᅟᅠㅤﾠ")
# 문장부호 전부와 이모티콘 쪽 기호(So, Sk). 수학(Sm)·통화(Sc) 기호는 코드 조각일 수 있어 뺀다
_SYMBOLIC_CATEGORIES = frozenset({"Pc", "Pd", "Ps", "Pe", "Pi", "Pf", "Po", "So", "Sk"})
_BLANK_CATEGORIES = frozenset({"Zs", "Zl", "Zp", "Cf", "Mn", "Me"})


def _is_symbolic(ch: str) -> bool:
    """자모·문장부호·이모티콘."""
    return bool(_JAMO.match(ch)) or unicodedata.category(ch) in _SYMBOLIC_CATEGORIES


def _is_blank(ch: str) -> bool:
    """공백·서식 문자·결합 부호(변형 선택자, 키캡 등)·한글 채움 문자."""
    return ch.isspace() or ch in _FILLERS or unicodedata.category(ch) in _BLANK_CATEGORIES


def is_low_signal(target_pre: str, has_history: bool, hidden: str) -> bool:
    """자모·문장부호·이모티콘만 있고(숫자·라틴·완성 음절·숨긴 텍스트 없음) 앞에 대화가 있음.

    target_pre는 태그 문자만 지운 원문(자모 조합 전). 정규화본을 쓰면 "ㅋㅋㅠㅠ"가 음절로 조합된다.
    hidden은 `preprocess.decode_tags`가 푼 숨긴 문자열. 1턴 입력은 False(LLM으로 보낸다).
    NFC만 먼저 건다: NFD로 쪼갠 음절(화면에는 보통 문장)은 음절로 되돌리고, 호환 자모(ㅋㅠ)는
    NFC가 조합하지 않으므로 "자모 조합 전" 뷰는 그대로다.
    """
    if not has_history or hidden:
        return False
    text = unicodedata.normalize("NFC", target_pre)
    return any(_is_symbolic(c) for c in text) and all(_is_symbolic(c) or _is_blank(c) for c in text)


def is_refusal(answer_raw: str) -> bool:
    """후보 답변이 거절·대안 제시인가. 인용·코드 안 문구는 보지 않는다. safe 규칙에만 쓴다.

    `input.max_answer_chars`로 자른 답변을 넘긴다(닫히지 않은 따옴표 반복은 2차 시간).
    알려진 한계: 영어 축약형 아포스트로피 두 개 사이는 인용으로 보고 지운다.
    """
    return REFUSAL_RX.search(QUOTE_CODE_RX.sub(" ", answer_raw)) is not None


def has_hidden_tags(answer_raw: str) -> bool:
    """깃발 이모지(🏴 + 소문자·숫자 태그 1–7개 + 종료 태그) 밖에 태그 문자(U+E0000–E007F)가 있나."""
    return TAG_CHARS.search(FLAG_SEQUENCE.sub("", answer_raw)) is not None
