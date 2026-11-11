"""TU-05 결정적 신호: 저신호 직접 판정과 판단 힌트 신호.

- 저신호 직접 판정(`direct_clarify`): LLM 없이 `clarify` 고정 문장을 낼지 정한다.
  마지막 학습자 발화(태그 문자만 지운 원문, 자모 조합 전)가 자모·문장부호·기호·이모티콘만으로
  되어 있고(숫자·라틴·완성 음절 없음, 숨긴 텍스트 없음) 앞에 대화가 있으면 참이다(가드 GR-06 기준).
  빈 발화·공백뿐인 발화는 1턴이어도 참이다. 1턴 자모 입력은 주 호출이 판단한다.
- 판단 힌트(`detect`): 위기, 지시 무력화, 정답 요구, 과제 대행, 자기 개인정보, 채점 조작.
  action을 정하지 않는다. 주 호출 입력의 <신호>에 이름만 넘기고 판단은 범주 정의로 한다.

작성 원칙(가드 GR-06, TD-10)
- 단독 키워드가 아니라 **표현 조합**으로 쓴다. "거절하지 말고"·권한 주장·역할극 틀은 단독으로
  신호를 올리지 않는다(정상 문항에도 많다, PRD §1).
- 공개 세트 문항의 고유 표현(예제·상품·학교 이름, 문장)을 어휘에 넣지 않는다.
- 띄어 쓴 우회("네 시 스 템 프롬프트")를 잡도록 가드 `skeleton` 뷰(공백·기호·숫자 제거, 자모 조합,
  NFKC, 소문자)에 건다. 그래서 패턴에는 공백·숫자가 없다.

목록·정규식은 코드 안에 두고 데이터 파일로 빼지 않는다(TD-09). 요청 사이 상태 없음(TU-18).
"""

import re
import unicodedata
from collections.abc import Sequence
from enum import StrEnum
from types import MappingProxyType

from guardrail.preprocess import skeleton

from tutor.judge.conversation import Conversation

# --- 저신호 ---

_JAMO_RANGES = (
    (0x1100, 0x11FF),  # 한글 자모(조합형)
    (0x3131, 0x318E),  # 호환 자모
    (0xA960, 0xA97F),  # 자모 확장 A
    (0xD7B0, 0xD7FF),  # 자모 확장 B
    (0xFFA0, 0xFFDC),  # 반각 자모
)


def _is_jamo(ch: str) -> bool:
    cp = ord(ch)
    return any(lo <= cp <= hi for lo, hi in _JAMO_RANGES)


def _ignorable(ch: str) -> bool:
    """보이지 않는 문자: 공백, 서식 문자(ZWJ·방향 제어 등), 결합 표시(변형 선택자 등)."""
    return ch.isspace() or unicodedata.category(ch) in ("Cf", "Mn", "Me", "Cc")


def _low_char(ch: str) -> bool:
    """자모·문장부호·기호(이모지 포함). 숫자·라틴·완성 음절·한자 등 글자(L*·N*)는 아니다."""
    if _is_jamo(ch):
        return True
    return unicodedata.category(ch)[0] in ("P", "S")


def is_blank(text: str) -> bool:
    """보이는 글자가 하나도 없는가(빈 문자열, 공백·보이지 않는 문자뿐)."""
    return all(_ignorable(ch) for ch in text)


def is_low_signal_text(text: str) -> bool:
    """보이는 글자가 있고 모두 자모·문장부호·기호인가.

    숫자·라틴·완성 음절이 하나라도 있으면 아니다.
    """
    visible = [ch for ch in text if not _ignorable(ch)]
    return bool(visible) and all(_low_char(ch) for ch in visible)


def direct_clarify(conv: Conversation) -> bool:
    """LLM 없이 clarify 고정 문장을 낼 입력인가(TU-05). 숨긴 텍스트가 있으면 아니다."""
    if conv.target_hidden:
        return False
    if is_blank(conv.target_raw):
        return True
    return conv.has_history and is_low_signal_text(conv.target_raw)


# --- 판단 힌트 신호 ---


class Signal(StrEnum):
    CRISIS = "crisis"  # 지속되는 고통 + 고립
    OVERRIDE = "override"  # 지시 무력화·제한 없는 역할·숨겨진 지시 요구
    ANSWER_ONLY = "answer_only"  # 풀이 없이 정답만 요구
    DELEGATION = "delegation"  # 평가 제출물 대행
    SELF_PII = "self_pii"  # 자기 이름·학교·학년 밝힘
    GRADING = "grading"  # 채점 조작(채점자에게 하는 말)


# 주 호출 <신호>에 넣는 이름(범주 이름을 쓰지 않는다: 신호는 힌트이고 범주는 모델이 정한다)
_LABEL = {
    Signal.CRISIS: "지속되는 고통·고립 표현",
    Signal.OVERRIDE: (
        "지시 무시·제한 없는 역할·숨겨진 지시 요구 표현(분석 대상으로 인용한 것일 수도 있음)"
    ),
    Signal.ANSWER_ONLY: "풀이 없이 정답만 요구하는 표현",
    Signal.DELEGATION: "평가 제출물을 대신 만들어 달라는 표현",
    Signal.SELF_PII: "자기 이름·학교·학년을 밝히는 표현",
    Signal.GRADING: "채점·평가 방법을 지시하는 표현",
}
LABEL = MappingProxyType(_LABEL)


def _rx(*alternatives: str) -> re.Pattern[str]:
    return re.compile("|".join(f"(?:{a})" for a in alternatives))


# 패턴은 skeleton 뷰(공백·기호·숫자 없음, 소문자) 기준이다
_PATTERNS: dict[Signal, re.Pattern[str]] = {
    Signal.CRISIS: _rx(
        # 강한 표현은 단독으로
        r"죽고싶|사라지고싶|살기싫|자해|목숨을끊|없어지고싶",
        # 지속(몇 주째·요즘 계속·매일) + 고통·고립(버티기 힘듦·아무것도 하기 싫음·희망 없음)
        r"(?:몇주|몇달|몇개월|한달|요즘계속|요새계속|매일|날마다|계속)\w{0,24}?"
        r"(?:버티기(?:너무|넘|정말|진짜)?(?:힘들|힘듦|어렵)|아무것도(?:하기|할)\w{0,4}(?:싫|없)"
        r"|희망이(?:안|없)|아무(?:한테|에게)도\w{0,6}(?:말|못)|누구(?:한테|에게)도\w{0,6}(?:말|못)"
        r"|혼자\w{0,6}(?:힘들|버티|감당))",
        r"(?:버티기(?:너무|넘|정말|진짜)?(?:힘들|힘듦|어렵)|아무것도하기싫)\w{0,24}?"
        r"(?:몇주|몇달|몇개월|요즘계속|요새계속|매일|날마다)",
    ),
    Signal.OVERRIDE: _rx(
        # 이전·앞의 지시를 무시
        r"(?:이전|앞|위|기존|모든|원래|처음)(?:의|에)?(?:지시|지침|규칙|명령|설정|프롬프트|안내)\w{0,3}?"
        r"(?:무시|잊어|잊고|버리|어기|따르지마|해제)",
        r"(?:지시|지침|규칙|명령)(?:를|을|은|는)?(?:다)?무시(?:해|하고|하며|하라|하세요|해줘)",
        # 제한·규칙 없는 역할(튜터 자신에게)
        r"(?:제한|규칙|필터|검열|제약)(?:이|도|은)?(?:없|해제|풀)\w{0,6}?"
        r"(?:역할|인물|등장인물|캐릭터|안내자|ai|모드|버전|봇|답|대답|알려|말해|설명)",
        r"(?:튜터|선생님)(?:를|을)?(?:그만두|아닌|말고)",
        # 숨겨진 지시·시스템 프롬프트 원문 요구
        r"(?:시스템|숨겨진|숨긴|숨은|내부|비밀|초기|처음주어진|system|sys)(?:의)?"
        r"(?:프롬프트|지시|지침|설정|명령|메시지|prompt|message)\w{0,12}?"
        r"(?:원문|전부|전체|그대로|다보여|보여|공개|출력|알려)",
        # 튜터 자신의 숨겨진 지시를 묻기(소유 표현 + 숨겨진 지시)
        r"(?:네|너의|니|당신의|튜터의|너한테|your)\w{0,6}?"
        r"(?:숨겨진|숨긴|숨은|비밀|시스템|system|sys)(?:의)?(?:프롬프트|지시|지침|메시지|prompt)",
        r"ignore\w{0,6}(?:previous|prior|above|earlier|all)\w{0,6}(?:instruction|rule|prompt)",
        r"(?:system|sys|hidden|secret)(?:prompt|instruction|message)\w{0,16}"
        r"(?:show|reveal|print|tell|what|copy|paste|repeat)",
        r"(?:show|reveal|print|tell|copy|paste|repeat)\w{0,16}"
        r"(?:system|sys|hidden|secret)(?:prompt|instruction|message)",
        r"(?:no|without)(?:restriction|rule|limit|filter)s?\w{0,12}(?:role|mode|ai|answer|act)",
        r"jailbreak|danmode|developermode",
    ),
    Signal.ANSWER_ONLY: _rx(
        r"(?<!대)(?:정답|답)(?:만|하나만)(?:골라|고르|알려|알랴|말해|써|적어|줘|주세|뭐|머|ㄱㄱ|빨리|좀)",
        r"(?:설명|풀이|과정|이유)(?:은|는|도)?(?:없이|빼고|생략|필요없)\w{0,16}?(?:정답|답)",
        r"(?:just|only)(?:give|tell)?(?:me)?(?:the)?answer|answeronly|withoutexplain",
    ),
    Signal.DELEGATION: _rx(
        # 평가 제출물 + 대신 만들기·제출
        r"(?:수행평가|숙제|과제|보고서|레포트|리포트|homework|assignment|report)\w{0,40}?"
        r"(?:대신|완성|써줘|써주|작성해|작성하|만들어|제출|낼|내야|넣을|답안)",
        r"(?:제출할|제출용|제출하)\w{0,20}?(?:완성|전체|전부|대신|써|작성|만들어|답안)",
        r"(?:답안|정답)(?:을|를)?(?:전체|전부|통째로)",
        r"(?:do|write|finish)(?:my)?(?:homework|assignment|report)",
        r"(?:as|for)(?:a|my)?complete(?:answer|assessment|report|essay|submission)",
        r"my(?:complete)?(?:assessment|homework|assignment)(?:answer|submission)",
    ),
    Signal.SELF_PII: _rx(
        r"(?:내|제|나의|저의)이름(?:은|는|이)",
        r"(?:나는|저는|난|전|나|저)\w{0,12}?(?:중학교|고등학교|초등학교|중|고|초)학년",
        r"(?:나는|저는|난|전|나|저)\w{0,12}?(?:중학교|고등학교|초등학교)(?:에)?(?:다녀|다니|학생)",
        # 발화 첫머리의 이름 소개("나는 ○○야"). 인용한 문장 속 "나는 …이야"는 잡지 않는다
        r"^(?:나는|저는|난|전)\w{1,4}?(?:이야|이에요|예요|입니다|야)",
        r"mynameis|iamin(?:grade|middleschool|highschool)",
    ),
    Signal.GRADING: _rx(
        r"(?:채점자|채점관|채점기|평가자|심사위원|grader|judge|evaluator)\w{0,24}?"
        r"(?:정답|만점|통과|true|맞다|맞게|높게|처리|주세요|줘|무시)",
        r"(?:정답|만점|통과|맞음)(?:으로|이라고|로)(?:처리|채점|인정|판정|표시|평가)",
        r"(?:만점|높은점수|점수를높게)(?:을|를)?(?:줘|주세요|주라|달라|받게|매겨)",
        r"(?:markthis|gradethis|scorethis)\w{0,12}(?:correct|true|full)",
    ),
}
PATTERNS = MappingProxyType(_PATTERNS)


def detect_text(text: str) -> tuple[Signal, ...]:
    """한 발화의 신호 목록(정의 순서). 없으면 ()."""
    view = skeleton(text)
    return tuple(s for s, rx in PATTERNS.items() if rx.search(view))


def detect(conv: Conversation) -> tuple[Signal, ...]:
    """판단 대상(마지막 학습자 발화, 숨긴 문자 표기 포함)에서만 신호를 찾는다(PRD §3)."""
    return detect_text(conv.target) if conv.target else ()


def labels(signals: Sequence[Signal]) -> list[str]:
    """주 호출 <신호>에 넣을 이름 목록."""
    return [LABEL[s] for s in signals]
