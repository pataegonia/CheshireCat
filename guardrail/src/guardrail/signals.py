"""결정적 신호 (GR-06, GR-08, ADR-0014). LLM 없이 돈다.

- is_low_signal: 저신호 직접 판정(입력). 자모 조합 전 뷰(`preprocess.strip_tags`)에 건다.
- is_refusal: 거절·대안 제시(출력 safe 규칙에만). 인용·코드를 뺀 원문에 건다.
- has_hidden_tags: 깃발 이모지 밖 태그 문자(출력 safe). 길이 상한으로 자르기 전 답변 전체에 건다.
- is_self_intro: 학습자가 자기 이름·학교·학년을 밝힌 줄(입력 범주 pii_disclosure 보정에만).

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

from guardrail.preprocess import FLAG_SEQUENCE, TAG_CHARS, _fold_width, normalize


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
# 자기소개(S3.9, 출처 "팀 작성"): 공개 세트 pii_disclosure 20건을 보고 쓴 정규식이라 20/20은
# 표본 안 값이다(results/2026-11-16-score-probes.md). 줄 첫머리의 1인칭 주어 뒤만 본다.
_ME = r"(?m:^)[ \t]*(?:\d+[.)][ \t]*)?(?:나는|난|나|저는|전)[ \t]+"
_IS = r"(?:이야|야|이에요|예요|임|입니다)(?=[\s.,!/]|$)"
# shortcut: 이름이 아닌 흔한 서술어("나는 초보야")는 목록으로만 뺀다. 오탐이 보이면 목록을 늘린다
_NOT_NAMES = (
    "학생|초보|초보자|개발자|관리자|운영자|선생님|선생|교사|튜터|사람|처음|바보|천재|반대|찬성|"
    "진짜|아니|진심|한국인|문과|이과|남자|여자|어른|고양이|고민|불만|걱정|코딩|질문|문제|괜찮"
)
_NAME = rf"(?!(?:{_NOT_NAMES})(?:이야|야|이에요|예요|임|입니다))[가-힣]{{1,5}}"
_GRADE = r"(?:[1-6][ \t]*학년|[초중고][ \t]*[1-6])"
SELF_INTRO_RX = re.compile(
    # 학교·학년: "나는 ○○ 중학교 2학년 ○○야", "저는 중학교 1학년이에요", "나는 중2 ○○야"
    _ME + rf"(?=[^\n.?!,]{{0,24}}?(?:초등학교|중학교|고등학교|{_GRADE}))"
    rf"(?:[가-힣]{{1,8}}[ \t]*)?(?:(?:초등|중|고등)학교[ \t]*)?(?:{_GRADE}[ \t]*)?(?:{_NAME})?"
    + _IS
    # 이름: "내 이름은 ○○야", "제 이름은 ○○고 …"
    + r"|(?:제|내)[ \t]*이름은[ \t]*[가-힣]{1,5}"
    r"(?:이야|야|이에요|예요|임|입니다|이고|고|이라고|라고)(?=[\s.,!/]|$)"
    # 이름만: "나는 ○○야"
     + "|" + _ME + rf"(?!(?:{_NOT_NAMES})(?:이야|야|이에요|예요|임|입니다))[가-힣]{{2,4}}" + _IS
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
# 자모 욕설 초성(실험 input.jamo_profanity_not_low, 출처 "팀 작성"): 팀이 직접 쓴 일반 규칙이다.
# 공개 세트·채점 문구에서 뽑지 않았다. 공백·기호를 뺀 자모열에 하나라도 들면 저신호가 아니다
# (nano 판정으로 보낸다). 쌍자음·겹자음 표기(ㅆ·ㅄ)도 따로 적는다
JAMO_PROFANITY = ("ㅅㅂ", "ㅆㅂ", "ㅄ", "ㅂㅅ", "ㄲㅈ", "ㅈㄹ", "ㅗ")


def _has_letter(ch: str) -> bool:
    """글자를 담은 문자인가: 원문자·괄호·사각 라틴·한글·한자, 지역 표시 문자, 점자, 결합 라틴 문자.

    이름에 LETTER·BRAILLE이 들거나(NFKC로 안 접히는 것까지), NFKC로 접은 글자 중 하나라도
    영숫자다(`㈎`→"(가)"처럼 문장부호가 섞여도). 이런 문자로 쓴 문장은 기호도 공백도 아니다.
    """
    name = unicodedata.name(ch, "")
    return (
        "LETTER" in name
        or "BRAILLE" in name
        or any(c.isalnum() for c in unicodedata.normalize("NFKC", ch))
    )


def _is_symbolic(ch: str) -> bool:
    """자모·문장부호·이모티콘. 글자를 담은 문자는 아니다."""
    if _JAMO.match(ch):
        return True
    return unicodedata.category(ch) in _SYMBOLIC_CATEGORIES and not _has_letter(ch)


def _is_blank(ch: str) -> bool:
    """공백·한글 채움 문자·서식 문자·결합 부호(변형 선택자, 키캡 등). 결합 라틴 문자는 아니다."""
    if ch.isspace() or ch in _FILLERS:
        return True
    return unicodedata.category(ch) in _BLANK_CATEGORIES and not _has_letter(ch)


def _spelled_syllables(text: str) -> int:
    """자모만 남겨(공백·문장부호·기호를 지워) 조합했을 때 생기는 완성 음절 수.

    띄어 쓰거나 점으로 끊은 자모("ㅇ ㅣ ㅈ ㅓ ㄴ", "ㅇ.ㅣ.ㅈ.ㅓ.ㄴ")도 문장으로 잡는다.
    `normalize`의 이모티콘 규칙이 "ㅋㅋㅠㅠ" 같은 런은 조합하지 않는다.
    """
    jamo = "".join(c for c in text if _JAMO.match(c))
    return len(_SYLLABLE.findall(normalize(jamo)))


def has_jamo_profanity(text: str) -> bool:
    """공백·기호를 뺀 자모열에 `JAMO_PROFANITY` 가운데 하나가 드는가. 반각 자모는 호환 자모로
    접는다(NFKC는 호환 자모를 조합형 자모로 바꿔 목록과 어긋나므로 쓰지 않는다)."""
    jamo = "".join(_fold_width(c) for c in unicodedata.normalize("NFC", text) if _JAMO.match(c))
    return any(word in jamo for word in JAMO_PROFANITY)


def is_low_signal(target_pre: str, has_history: bool, hidden: str) -> bool:
    """자모·문장부호·이모티콘만 있고(숫자·라틴·완성 음절·숨긴 텍스트 없음) 앞에 대화가 있음.

    target_pre는 태그 문자만 지운 원문(자모 조합 전). 정규화본을 쓰면 "ㅋㅋㅠㅠ"가 음절로 조합된다.
    hidden은 `preprocess.decode_tags`가 푼 숨긴 문자열. 1턴 입력은 False(LLM으로 보낸다).
    NFC만 먼저 건다: NFD로 쪼갠 음절(화면에는 보통 문장)은 음절로 되돌리고, 호환 자모(ㅋㅠ)는
    NFC가 조합하지 않으므로 "자모 조합 전" 뷰는 그대로다.
    자모로 풀어 쓴 문장(조합하면 음절 2개 이상)은 저신호가 아니다: 잘못 False면 nano 호출 1회,
    잘못 True면 입력 판정을 건너뛰므로 False 쪽으로 기운다.
    실험 `input.jamo_profanity_not_low`는 이 함수를 바꾸지 않고 부르는 쪽이 `has_jamo_profanity`로
    한 번 더 거른다(튜터와 맞춘 세 인자 계약 그대로, test_shared_api).
    """
    if not has_history or hidden:
        return False
    text = unicodedata.normalize("NFC", target_pre)
    marks_only = any(_is_symbolic(c) for c in text) and all(
        _is_symbolic(c) or _is_blank(c) for c in text
    )
    return marks_only and _spelled_syllables(text) < _SPELLED_SYLLABLES


def is_self_intro(target: str) -> bool:
    """마지막 학습자 발화에 자기 이름·학교·학년을 밝힌 줄이 있는가. `clip`한 글에 건다."""
    return SELF_INTRO_RX.search(target) is not None


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
