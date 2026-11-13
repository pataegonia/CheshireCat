"""결정적 신호 (GR-06, GR-08, ADR-0014). LLM 없이 돈다.

- is_low_signal: 저신호 직접 판정(입력). 자모 조합 전 뷰(`preprocess.strip_tags`)에 건다.
- is_refusal: 거절·대안 제시(출력 safe 규칙에만). 인용·코드를 뺀 원문에 건다.
- has_hidden_tags: 깃발 이모지 밖 태그 문자(출력 safe). 길이 상한으로 자르기 전 답변 전체에 건다.

정규식 출처(provenance "팀 작성"): QUOTE_CODE_RX는 `scripts/r13/q6_rule_fp.py` 그대로.
REFUSAL_RX는 `scripts/r13/extra_stats.py`를 다듬은 것(council D14, PR #130 리뷰):
거절 36건에 단독으로 걸리지 않고 오탐만 내던 `할 수 없`·`안 돼요`·`하지 않아요`·`하지 말`을 빼고,
토큰 사이 공백을 `\\s*`로 받는다(붙여쓰기·두 칸·NBSP·줄바꿈).
단 `못\\s+해요`는 붙여 쓴 "못해요"(못하다)를 받지 않는다.
공개 세트 튜터 거절 답변 36건을 보고 쓴 정규식이라 36/36은 표본 안 값이다.
"""

import re
import unicodedata
from dataclasses import dataclass

from guardrail.preprocess import FLAG_SEQUENCE, TAG_CHARS, normalize


@dataclass(frozen=True)
class Signals:
    low_signal: bool
    refusal: bool
    hidden_tags: bool


REFUSAL_RX = re.compile(
    r"수업과\s*(무관|관련이\s*없)|써\s*줄\s*수\s*없|수는\s*없|말아\s*주세요|계속\s*도와|"
    r"돌아가\s*볼게요|함께\s*(공부|살펴)|안\s*돼\b|도와줄\s*수\s*없|드릴\s*수\s*없|"
    r"알려\s*줄\s*수\s*없|맡을\s*수\s*없|않을게요|대신\s*.{0,20}(도와|알려|할\s*수)|"
    r"못\s+해요|공개하지\s*않"
)
# 따옴표(“” "" ‘’ '' 「」 『』) 안 인용과 백틱·코드 블록. 거절 정규식은 이것을 지운 뷰에 건다
QUOTE_CODE_RX = re.compile(
    r"```.*?```|`[^`]*`|\"[^\"]*\"|“[^”]*”|‘[^’]*’|'[^']*'|「[^」]*」|『[^』]*』", re.S
)
# 조합형 자모, 호환 자모, 반각 자모(채움 문자 U+115F·U+1160·U+3164·U+FFA0은 뺀다)
_JAMO = re.compile(r"[ᄀ-ᅞᅡ-ᇿㄱ-ㅣㅥ-ㆎﾡ-ￜ]")
_FILLERS = frozenset(map(chr, (0x115F, 0x1160, 0x3164, 0xFFA0)))
_SYLLABLE = re.compile(r"[가-힣]")
# 문장부호 전부와 이모티콘 쪽 기호(So, Sk). 수학(Sm)·통화(Sc) 기호는 코드 조각일 수 있어 뺀다
_SYMBOLIC_CATEGORIES = frozenset({"Pc", "Pd", "Ps", "Pe", "Pi", "Pf", "Po", "So", "Sk"})
_BLANK_CATEGORIES = frozenset({"Zs", "Zl", "Zp", "Cf", "Mn", "Me"})
# 자모만 모아 조합했을 때 이만큼 음절이 생기면 자모로 풀어 쓴 문장으로 본다(council D13).
# 1이면 스펙 예시 "ㅇㅏ"(→아)까지 LLM으로 가므로 2. 1음절 우회("ㅅㅣㅂ")는 감수한다.
_SPELLED_SYLLABLES = 2


def _is_symbolic(ch: str) -> bool:
    """자모·문장부호·이모티콘."""
    return bool(_JAMO.match(ch)) or unicodedata.category(ch) in _SYMBOLIC_CATEGORIES


def _is_blank(ch: str) -> bool:
    """공백·서식 문자·결합 부호(변형 선택자, 키캡 등)·한글 채움 문자."""
    return ch.isspace() or ch in _FILLERS or unicodedata.category(ch) in _BLANK_CATEGORIES


def _spelled_syllables(text: str) -> int:
    """자모만 남겨(공백·문장부호·기호를 지워) 조합했을 때 생기는 완성 음절 수.

    띄어 쓰거나 점으로 끊은 자모("ㅇ ㅣ ㅈ ㅓ ㄴ", "ㅇ.ㅣ.ㅈ.ㅓ.ㄴ")도 문장으로 잡는다.
    `normalize`의 이모티콘 규칙이 "ㅋㅋㅠㅠ" 같은 런은 조합하지 않는다.
    """
    jamo = "".join(c for c in text if _JAMO.match(c))
    return len(_SYLLABLE.findall(normalize(jamo)))


def is_low_signal(target_pre: str, has_history: bool, hidden: str) -> bool:
    """자모·문장부호·이모티콘만 있고(숫자·라틴·완성 음절·숨긴 텍스트 없음) 앞에 대화가 있음.

    target_pre는 태그 문자만 지운 원문(자모 조합 전). 정규화본을 쓰면 "ㅋㅋㅠㅠ"가 음절로 조합된다.
    hidden은 `preprocess.decode_tags`가 푼 숨긴 문자열. 1턴 입력은 False(LLM으로 보낸다).
    NFC만 먼저 건다: NFD로 쪼갠 음절(화면에는 보통 문장)은 음절로 되돌리고, 호환 자모(ㅋㅠ)는
    NFC가 조합하지 않으므로 "자모 조합 전" 뷰는 그대로다.
    자모로 풀어 쓴 문장(조합하면 음절 2개 이상)은 저신호가 아니다: 잘못 False면 nano 호출 1회,
    잘못 True면 입력 판정을 건너뛰므로 False 쪽으로 기운다.
    """
    if not has_history or hidden:
        return False
    text = unicodedata.normalize("NFC", target_pre)
    marks_only = any(_is_symbolic(c) for c in text) and all(
        _is_symbolic(c) or _is_blank(c) for c in text
    )
    return marks_only and _spelled_syllables(text) < _SPELLED_SYLLABLES


def is_refusal(answer_raw: str) -> bool:
    """후보 답변이 거절·대안 제시인가. 인용·코드 안 문구는 보지 않는다. safe 규칙에만 쓴다.

    `input.max_answer_chars`로 자른 답변을 넘긴다(닫히지 않은 따옴표 반복은 2차 시간).
    `reveal_hidden` 전 원문을 넘긴다. 알려진 한계: 영어 축약형 아포스트로피 두 개 사이는
    인용으로 보고 지운다.
    """
    return REFUSAL_RX.search(QUOTE_CODE_RX.sub(" ", answer_raw)) is not None


def has_hidden_tags(answer_raw: str) -> bool:
    """깃발 이모지(🏴 + 소문자·숫자 태그 1–7개 + 종료 태그) 밖에 태그 문자(U+E0000–E007F)가 있나."""
    return TAG_CHARS.search(FLAG_SEQUENCE.sub("", answer_raw)) is not None
