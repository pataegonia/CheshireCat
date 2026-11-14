"""TU-05 결정적 신호: 저신호 직접 판정과 판단 힌트 신호.

- 저신호 직접 판정(`direct_clarify`): LLM 없이 `clarify` 고정 문장을 낼지 정한다.
  가드 `is_low_signal`(GR-06)을 그대로 쓰고 튜터 고유 조건만 더한다. 빈 발화·공백뿐인 발화는
  1턴이어도 참이고, 튜터가 바로 앞에서 질문했으면 거짓이다. 1턴 자모 입력은 주 호출이 판단한다.
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
from collections.abc import Sequence
from enum import StrEnum
from types import MappingProxyType

from guardrail.preprocess import skeleton
from guardrail.signals import is_low_signal

from tutor.judge.conversation import Conversation, normalize_fast

# --- 저신호 ---

# 튜터가 대답을 청하는 끝말(물음표 없이 "1번과 2번 중 골라 주세요." 처럼 끝나도 질문이다)
_ASKING_END = re.compile(r"(?:주세요|줘요|줘|래요|할래|볼래|까요|나요)[.!。~\s]*$")


def _tutor_asked(conv: Conversation) -> bool:
    """판단 대상 바로 앞의 튜터 발화가 질문·선택 요청으로 끝나는가("예제 더 볼래요?" → "ㅇㅇ")."""
    before = [t for t in conv.window if not t.is_target and t.role == "assistant"]
    target_idx = next((t.index for t in conv.window if t.is_target), None)
    prev = [t for t in before if target_idx is None or t.index < target_idx]
    if not prev:
        return False
    last = prev[-1].text.rstrip()
    return last.endswith(("?", "？")) or bool(_ASKING_END.search(last))


def direct_clarify(conv: Conversation) -> bool:
    """LLM 없이 clarify 고정 문장을 낼 입력인가(TU-05).

    - 숨긴 텍스트가 있으면 거짓이다.
    - 빈 발화(보이지 않는 문자뿐 포함)는 1턴이어도 참이다(튜터 고유).
      판단은 `Conversation.is_empty` 하나로 한다.
    - 그 밖은 가드 `is_low_signal`(GR-06) 그대로다. 조건은 그 docstring에 있다.
    - 다만 튜터가 바로 앞에서 질문했다면 짧은 대답("ㅇㅇ")일 수 있으니 주 호출로 보낸다(튜터 고유,
      공개 세트 저신호 8행은 모두 해당 없음).
    """
    if conv.target_hidden:  # 가드도 거짓을 내지만, 가드 기준이 바뀌어도 튜터는 지킨다
        return False
    if conv.is_empty:
        return True
    low = is_low_signal(conv.target_raw, conv.has_history, conv.target_hidden)
    return low and not _tutor_asked(conv)


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


# 정서적 고통·고립(위기 신호의 두 번째 조건). 정도 부사를 열거하지 않고 핵심 말만 둔다.
# "혼자"는 바로 뒤에 고통 말이 붙을 때만 본다("매일 혼자 공부하는데 힘들어요"는 학습의 어려움)
_DISTRESS = (
    r"혼자(?:서)?\w{0,2}?(?:힘들|힘듦|버티|감당|외롭)|아무것도(?:하기|할)\w{0,4}(?:싫|없)"
    r"|희망이(?:안|없)|아무(?:한테|에게)도\w{0,6}(?:말|못)|누구(?:한테|에게)도\w{0,6}(?:말|못)"
    r"|우울|무기력|잠을?못자|살기싫"
)

# 자신을 해치려는 바람·생각(1인칭 서술). 단독으로 위기 신호다
# ("자해 방지 앱"처럼 주제로 말한 것은 아니다)
_SELF_HARM = _rx(
    r"죽고싶|죽어버리고싶|사라지고싶|없어지고싶|살기싫|살고싶지않|목숨을끊",
    r"자살(?:하고싶|할까|하려|생각|충동|했)",
    r"극단적(?:인)?선택(?:을)?(?:하고싶|할까|하려|생각|했)",
    r"자해(?:하고싶|했|하게|할까|하려|충동)",
)

# 지속(몇 주째·요즘 계속·매일) + 정서적 고통·고립. "버티기 힘들 정도로 어려워요" 같은
# 학습의 어려움만으로는 걸지 않는다
_SUSTAINED = _rx(
    r"(?:몇주|몇달|몇개월|한달|요즘계속|요새계속|매일|날마다|계속)\w{0,24}?(?:" + _DISTRESS + r")",
    r"(?:" + _DISTRESS + r")\w{0,24}?(?:몇주|몇달|몇개월|요즘계속|요새계속|매일|날마다)",
)

# 고통 말이 요청 대상(소설·노래·대사 등)을 꾸미면 학습자 자신의 서술이 아니다
# ("계속 우울한 이야기만 나오는 소설을 요약해줘")
_WORK_REQUEST = re.compile(
    r"(?:소설|이야기|예문|노래|가사|영화|드라마|기사|댓글|대사|주인공|등장인물|캐릭터|일기예시"
    r"|story|novel|poem|lyrics)\w{0,10}?"
    r"(?:요약|분석|설명|번역|정리|분류|읽어|써줘|써주|바꿔|고쳐|summar|analy|translat|rewrite)"
)

# 튜터 자신의 숨겨진 지시를 묻기(소유 표현 + 숨겨진 지시). 개념 질문 어미가 있어도 신호다
_OVERRIDE_DIRECTED = _rx(
    r"(?:네|너의|니|당신의|튜터의|너한테|your)\w{0,6}?"
    r"(?:숨겨진|숨긴|숨은|비밀|시스템|system|sys)(?:의)?(?:프롬프트|지시|지침|메시지|prompt)",
)


# 패턴은 skeleton 뷰(공백·기호·숫자 없음, 소문자) 기준이다. 숫자가 필요한 규칙만 _DIGIT_PATTERNS
_PATTERNS: dict[Signal, re.Pattern[str]] = {
    Signal.CRISIS: _rx(_SELF_HARM.pattern, _SUSTAINED.pattern),
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
        _OVERRIDE_DIRECTED.pattern,
        r"ignore\w{0,6}(?:previous|prior|above|earlier|all)\w{0,6}(?:instruction|rule|prompt)",
        r"(?:system|sys|hidden|secret)(?:prompt|instruction|message)\w{0,16}"
        r"(?:show|reveal|print|tell|what|copy|paste|repeat)",
        r"(?:show|reveal|print|tell|copy|paste|repeat)\w{0,16}"
        r"(?:system|sys|hidden|secret)(?:prompt|instruction|message)",
        r"(?:no|without)(?:restriction|rule|limit|filter)s?\w{0,12}(?:role|mode|ai|answer|act)",
        r"jailbreak|danmode|developermode",
    ),
    Signal.ANSWER_ONLY: _rx(
        # 정답(이나 정답을 가리키는 짧은 말) + 만 + 요청. "대답만"·"답변 만들어"·"만점"은 아니고,
        # "답만 알려주지 말고"처럼 뒤에서 부정하면 뜻이 반대다
        r"(?<!대)(?:정답|답)\w{0,3}?(?:만|하나만)(?![들점])(?!\w{0,8}?말고)\w{0,3}?"
        r"(?:골라|고르|알려|말해|써|적어|줘|주세|뭐)",
        r"(?:설명|풀이|과정|이유)(?:은|는|도)?(?:없이|빼고|생략|필요없)\w{0,16}?(?:정답|답)",
        r"(?:just|only)(?:give|tell)?(?:me)?(?:the)?(?:final)?answers?",
        r"(?:no|without|skip)(?:the|any)?(?:explanation|explaining|steps|working|reasoning)",
    ),
    Signal.DELEGATION: _rx(
        # 평가 제출물 + 대신 만들어 달라는 위임 표현("작성하는 방법", "어떻게 만들어요"는 아니다).
        # 과제·숙제가 주제인 프로그램("숙제 관리 앱", "리포트 자동 생성 함수")은 아니다
        r"(?:수행평가|숙제|과제|보고서|레포트|리포트|homework|assignment|report)"
        r"(?!\w{0,6}?(?:프로그램|앱|어플|함수|사이트|시스템|봇|관리|자동|program|app|function))"
        r"\w{0,40}?(?:대신|완성|써줘|써주|작성해줘|작성해주|만들어줘|만들어주|답안)",
        # 평가에 낼 것(제출 의도). 주제·방법을 묻는 것은 아니다("숙제로 낼 보고서 주제 추천해줘")
        r"(?:수행평가|숙제|과제)(?:에|로|으로|용)?(?:낼|내야할|제출할|넣을)"
        r"(?!\w{0,12}?(?:주제|추천|아이디어|방법|팁|조언|어떻게))",
        r"(?:제출할|제출용)\w{0,20}?(?:완성|전체|전부|대신|써|작성|만들어|답안)",
        # 답안(제출물)을 통째로 써 달라는 것.
        # "정답 전부"·작성 동사 없는 "답안 전부"는 제출 의도 없는
        # 정답 요구(answer_seeking)도 잡아 뺐다(#139 리뷰)
        r"답안(?:을|를)?(?:전체|전부|통째로)\w{0,6}?(?:써|작성|만들)",
        # 영어: 일반 어휘 조합(대행 동사 + 제출물, 제출물 + 나 대신). 공백을 지운 원문이라 `do`는
        # 단어 안(window·random)에서도 걸리니 목적어(this·my·the)를 붙여야만 본다
        r"(?:do(?:this|my|the)|(?:write|finish|complete|solve)(?:this|my|the|a)?)\w{0,12}?"
        r"(?:homework|assignment|essay|report|assessment)",
        r"(?:homework|assignment|essay|report|assessment)\w{0,24}?(?:forme|onmybehalf|tosubmit)",
        r"(?:answer|solution|essay|report|submission)\w{0,12}?for(?:my|a|the)(?:graded)?"
        r"(?:assessment|homework|assignment|exam|test|class)",
    ),
    Signal.SELF_PII: _rx(
        # 이름 값이 따라올 때만("내 이름은 어떻게 변수에 저장해?"는 아니다)
        r"(?:내|제|나의|저의)이름(?:은|는|이)(?!어떻게|뭐|무엇|왜|어디|변수|name|값)"
        r"\w{1,8}?(?:이에요|예요|이야|입니다|이고|이며|라고|이라고|야|고)",
        r"(?:나는|저는|난|전|나|저)\w{0,12}?(?:중학교|고등학교|초등학교|중|고|초)학년",
        r"(?:나는|저는|난|전|나|저)\w{0,12}?(?:중학교|고등학교|초등학교)(?:에)?(?:다녀|다니|학생)",
        r"mynameis|iamin(?:grade|middleschool|highschool)",
    ),
    Signal.GRADING: _rx(
        # 채점자에게 하는 판정 지시(호칭 + 판정 동사). "채점 기준을 알려줘", "평가자가 누구인지
        # 알려줘", "평가자 역할 프롬프트를 작성해줘"처럼 일반 요청 어미만 붙은 것은 아니다
        r"(?:채점자|채점관|채점기(?!준)|평가자|심사위원|grader|judge|evaluator)(?:님)?"
        r"(?:은|는|에게|께|여|야)?\w{0,24}?"
        r"(?:처리해|처리하세요|처리하라|인정해|인정하세요|판정해|판정하세요|매겨|통과시켜|무시해"
        r"|맞다고|만점으로|정답으로)",
        r"(?:정답|만점|통과|맞음)(?:으로|이라고|로)(?:처리|채점|인정|판정|표시|평가)",
        # 학습자 자신이 점수를 받는 말("신용점수를 높게 받게 하려면")은 아니다
        r"(?:만점|높은점수|점수를높게)(?:을|를)?(?:줘|주세요|주라|달라|매겨)",
        r"(?:markthis|gradethis|scorethis)\w{0,12}(?:correct|true|full)",
    ),
}
PATTERNS = MappingProxyType(_PATTERNS)

# 숫자가 필요한 규칙: 공백만 지운 정규화본(숫자 유지, 소문자)에 건다. "중2"·"고1"·"2학년"
_DIGIT_PATTERNS: dict[Signal, re.Pattern[str]] = {
    Signal.SELF_PII: _rx(r"(?:나는|저는|난|전|나|저)\w{0,12}?(?:(?:중|고|초)[1-6]|[1-6]학년)"),
}

# 지시 무력화 표현을 학습 대상으로 묻는 어미("~하면 어떻게 돼", "~하면 안 되는 이유").
# 이때 지시 무력화 신호는 올리지 않는다(PR 과목은 역할·시스템 프롬프트를 가르친다)
_ANALYSIS = re.compile(
    r"(?:하면|쓰면|넣으면|적으면|주면|시키면|맡기면|넣는|쓰는)\w{0,6}?"
    r"(?:어떻게|어떤|왜|안되|안돼|위험|문제|차이)|(?:안되는|위험한)이유|막으려면"
)

# 개념·방어를 묻는 어미("시스템 프롬프트가 뭔지", "이런 공격은 어떻게 막아?")
_CONCEPT = re.compile(
    r"뭔지|뭐야|뭐예요|뭐에요|뭐지|무엇인|무슨뜻|뜻이|차이|막아|막을|막는|막으|방어|대응|차단"
    r"|필터링|탐지|감지|구별|구분|예방|prevent|defend|defens|detect|protect|mitigat|filter"
)

# 공격 표현을 인용해 살펴보는 말("~하라는 문장", "역할 문장을 살펴보며", "왜 ~인지").
# 학습 대상으로 다루는 것이므로 신호를 올리지 않는다(명령형이 같이 있으면 예외)
_QUOTED_STUDY = re.compile(
    r"(?:라는|하라는|달라는)(?:문장|표현|요청|공격|지시|말)|(?:연습|예시|예제)문장"
    r"|(?:문장|표현|예시)(?:을|를)?살펴|어떻게다루|왜\w{0,16}?(?:인지|일까)"
)

# 튜터에게 하는 명령형. 분석·개념 어미가 있어도 이것이 같이 있으면 지시 무력화로 본다
# ("이전 지시를 무시하고 답하면 어떻게 되는지 직접 해봐", "지시 무시해 … 로 바뀌어")
_COMMAND = re.compile(
    r"직접\w{0,4}?(?:해|보여|실행|시연)|해봐|지금부터|이제부터|그대로(?:보여|출력|알려|말해|따라)"
    r"|(?:지시|지침|규칙|명령)(?:를|을)?(?:다)?무시해(?!야|도|서|보|면)"
    r"|(?:로|으로)바뀌어|(?:이|가)(?:돼서|되어서|되서|돼줘|되어줘)"
)
# 개념·방어 어미는 발화 끝부분(마지막 질문)에 있을 때만 학습 질문으로 본다
_CONCEPT_TAIL = 20


def _override(view: str) -> bool:
    if _OVERRIDE_DIRECTED.search(view):
        return True
    if not PATTERNS[Signal.OVERRIDE].search(view):
        return False
    learning = (
        _ANALYSIS.search(view)
        or _QUOTED_STUDY.search(view)
        or _CONCEPT.search(view[-_CONCEPT_TAIL:])
    )
    return not learning or bool(_COMMAND.search(view))


def _crisis(view: str) -> bool:
    if _SELF_HARM.search(view):
        return True
    return bool(_SUSTAINED.search(view)) and not _WORK_REQUEST.search(view)


def detect_text(text: str) -> tuple[Signal, ...]:
    """한 발화의 신호 목록(정의 순서). 없으면 ()."""
    view = skeleton(text)
    digits = re.sub(r"\s+", "", normalize_fast(text)).casefold()
    found = []
    for s, rx in PATTERNS.items():
        if s is Signal.CRISIS:
            hit = _crisis(view)
        elif s is Signal.OVERRIDE:
            hit = _override(view)
        else:
            hit = bool(rx.search(view)) or (
                s in _DIGIT_PATTERNS and bool(_DIGIT_PATTERNS[s].search(digits))
            )
        if hit:
            found.append(s)
    return tuple(found)


def detect(conv: Conversation) -> tuple[Signal, ...]:
    """판단 대상(마지막 학습자 발화, 숨긴 문자 표기 포함)에서만 신호를 찾는다(PRD §3)."""
    return detect_text(conv.target) if conv.target else ()


def labels(signals: Sequence[Signal]) -> list[str]:
    """주 호출 <신호>에 넣을 이름 목록."""
    return [LABEL[s] for s in signals]
