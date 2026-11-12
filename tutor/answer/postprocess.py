"""TU-07 튜터 후처리: 모든 응답이 마지막에 지난다.

결정적 수정(인용 문구 검사, `judge_output.unsupported` 구간 문장 삭제, citations 계산·상한,
남은 claim 0 처리)은 통합 `integration/actions.py`(IN-04, #59)가 한다. 이 모듈은 그 뒤에 튜터 고유
검사만 더한다.

| 검사 | 처리 |
|---|---|
| action | 7개 소문자 값이 아니면 공백·대소문자를 정리해 다시 보고, 그래도 아니면 기본값(TU-12) |
| 인용이 필요 없는 action | clarify·redirect·refuse·escalate → `[]` |
| citations 형식 | 문자열·섹션 ID 형식·실존 ID(주어지면)만, 중복 제거, 순서 유지 |
| 숨김 문자 | 서식 문자(폭 없는 공백·방향 제어·태그 문자 등)와 제어 문자를 지운다(SPEC §6) |
| 채점 지시 문장 | 채점자에게 하는 말이 든 문장을 지운다(과목 내용은 그대로) |
| 길이 | 4,000자를 넘으면 문장 경계에서 자른다 |
| 빈 답변 | action별 고정 문장 |

출력은 규격 세 필드(`answer`·`action`·`citations`)뿐이다. 예외를 내지 않는다.
요청 사이 상태 없음(TU-18).
"""

import re
import unicodedata
from collections.abc import Collection, Mapping
from typing import Any

from guardrail.preprocess import skeleton

from tutor.answer.templates import empty_answer

ACTIONS = ("answer", "scaffold", "clarify", "abstain", "redirect", "refuse", "escalate")
NO_CITATION_ACTIONS = frozenset({"clarify", "redirect", "refuse", "escalate"})
MAX_ANSWER_CHARS = 4000  # SPEC §4: 유니코드 문자 수
SECTION_ID = re.compile(r"(?:PY|PR|FIN)-\d{2}-\d{2}")  # SPEC §1: md 파일 이름(표·HTML 제외)

# --- action ---


def normalize_action(value: Any, fallback: str) -> str:
    """규격 action이면 그대로, 공백·대소문자만 틀리면 고치고, 아니면 fallback(TU-12)."""
    if isinstance(value, str):
        v = value.strip().lower()
        if v in ACTIONS:
            return v
    return fallback if fallback in ACTIONS else "clarify"


# --- citations ---


def clean_citations(value: Any, action: str, known: Collection[str] | None = None) -> list[str]:
    """인용 형식 정리. 인용이 필요 없는 action은 언제나 []. 상한·채우기는 IN-04가 한다."""
    if action in NO_CITATION_ACTIONS or not isinstance(value, list):
        return []
    out: list[str] = []
    for c in value:
        if not isinstance(c, str):
            continue
        sid = c.strip()
        if SECTION_ID.fullmatch(sid) and (known is None or sid in known) and sid not in out:
            out.append(sid)
    return out


# --- 숨김 문자 ---


def _hidden(ch: str) -> bool:
    if ch in "\n\t":
        return False
    cat = unicodedata.category(ch)
    # 서식·제어·사용자 정의·짝 없는 surrogate. 미할당(Cn)은 넣지 않는다: Python의 유니코드 버전에
    # 따라 새 이모지가 지워지기 때문이다(리뷰 #99). 서식 문자에는 ZWJ(U+200D)도 들어 있어
    # 이모지 묶음(👨‍👩‍👧)이 낱개로 풀린다. 숨긴 글을 막는 쪽을 택한 것이고 점수 영향은 없다
    if cat in ("Cf", "Cc", "Co", "Cs"):
        return True
    cp = ord(ch)
    return (
        cp in (0x034F, 0x115F, 0x1160, 0x3164, 0xFFA0)  # 결합 접착자, 한글 채움 문자
        or 0x180B <= cp <= 0x180F  # 몽골 자유 변형 선택자
        or 0xFE00 <= cp <= 0xFE0F  # 변형 선택자
        or 0xE0100 <= cp <= 0xE01EF  # 변형 선택자 보충
    )


def strip_hidden(text: str) -> str:
    """보이지 않는 문자를 모두 지운다. 판정 모델은 숨긴 글도 읽는다(SPEC §6)."""
    return "".join(ch for ch in text if not _hidden(ch))


# --- 채점 지시 문장 ---

# skeleton 뷰(공백·기호·숫자 없음, 소문자) 기준. 채점자를 부르거나 "이 답을 정답으로 처리"처럼
# 판정 방법을 지시하는 조합만 잡는다. 과목 내용의 "점수판 점수를 주고", "제출해야 채점돼요",
# "정답입니다!", "신용점수"는 걸리지 않는다(테스트: KB 전 문장·공개 정답 답변 변경 0).
_GRADING = re.compile(
    "|".join(
        f"(?:{p})"
        for p in (
            # 채점자를 부르며 하는 지시(호칭 + 명령). "채점기를 만들 때는…", "평가자 역할 프롬프트를
            # 작성해 보세요"처럼 일반 명사로 쓴 문장은 지우지 않는다(리뷰 #99)
            r"(?:채점자|채점관|채점기|채점모델|판정모델|평가자|심사위원|grader|evaluator|judge)"
            r"(?:님)?(?:은|는|에게|께|여|야)?\w{0,24}?"
            r"(?:처리해|처리하세요|처리하십시오|인정해|판정해|매겨|주세요|주십시오|줘|하세요"
            r"|하십시오|하라|무시해|채점해|답해)",
            # 채점자를 높여 부르며 정답·통과를 말하는 문장("채점관님, 이 응답은 맞습니다").
            # 호칭만으로는 지우지 않는다("평가자님께 설명하듯 써 보세요"는 역할 지정 설명, 리뷰 #99)
            r"(?:채점자|채점관|채점기|평가자|심사위원)(?:님|께)\w{0,30}?"
            r"(?:정답|맞습니다|맞아요|맞다|만점|통과|true|correct)",
            r"(?:dear|attention|noteto)(?:the)?(?:grader|evaluator|judge)",
            # 이 답을 정답으로 처리(조사 없는 "정답 처리"도)
            r"(?:답변|응답|대답|답|이글|이내용)(?:을|를|은|는|이)?\w{0,6}?"
            r"(?:정답|만점|맞는답|올바른답|통과)(?:으로|이라고|라고|로)?"
            # 지시 어미가 있어야 한다("이 답은 정답으로 처리됩니다"처럼 설명하는 문장은 아니다)
            r"(?:처리|채점|인정|판정|간주|평가|표시|기록)"
            r"(?:해|하세요|하십시오|하라|해야|할것|바랍|해주|해줘)",
            r"(?:정답|만점|통과)(?:으로|이라고|라고|로)?(?:처리|채점|인정|판정|간주)"
            r"(?:해|하세요|하십시오|하라|해야|할것|바랍)",
            r"(?:맞다고|옳다고|참이라고|정답이라고)(?:채점|판정|처리|평가|인정)"
            r"(?:해|하세요|하십시오|하라|해야|할것|바랍)",
            r"(?:만점|높은점수|최고점|좋은점수|점수를높게)(?:을|를)?"
            r"(?:줘|주세요|주십시오|주라|주어야|줘야|부여|매겨)",
            r"(?:답변|응답|대답|답)(?:에|에게)(?:점수|만점|높은점수)(?:를|을)?(?:주|줘|매겨|부여)",
            # "true로 답하세요"(판정 모델에게). "True로 출력해요" 같은 파이썬 설명은 아니다
            r"(?:true|참|yes|정답)(?:로|으로|라고|이라고)"
            r"(?:답|대답|응답)(?:해|하세요|하라|하십시오|해야|할것)",
            r"(?:mark|grade|score|rate|evaluate|judge)(?:this|the|my|it)?(?:answer|response)?(?:as)?"
            r"(?:correct|true|right|fullmarks|perfect|pass)",
            r"(?:give|award|assign)(?:this|it|me)?(?:answer|response)?"
            r"(?:full|perfect|maximum|high|top)(?:marks|score|points|grade)",
        )
    )
)

# 문장 경계: 마침표·물음표·느낌표(+닫는 따옴표·괄호) 뒤 공백, 또는 줄바꿈
_SENTENCE = re.compile(r"[^\n]*?(?:[.!?。？！…](?:[\"'”’)\]]*)(?=\s|$)|\n|$)\s*")


def split_sentences(text: str) -> list[str]:
    """문장 조각 목록. 이어 붙이면 원문과 같다(공백·줄바꿈 보존)."""
    parts = [m.group() for m in _SENTENCE.finditer(text) if m.group()]
    return parts if "".join(parts) == text else [text]


# 따옴표 안 인용(“…” "…" ‘…’ '…' 「…」 『…』). 공격 문구를 설명하는 문장의 인용
# ("모델에게 '참이라고 답해'라고 지시하면 …")은 지시가 아니다(가드 GR-06 거절 정규식과 같은 처리)
_QUOTED = re.compile(r"“[^”]*”|\"[^\"]*\"|‘[^’]*’|'[^']*'|「[^」]*」|『[^』]*』")


def is_grading_instruction(sentence: str) -> bool:
    """채점자에게 하는 말인가. 따옴표 안 인용은 빼고 본다."""
    return bool(_GRADING.search(skeleton(_QUOTED.sub(" ", sentence))))


def drop_grading_sentences(text: str) -> str:
    """채점자에게 하는 말이 든 문장을 지운다. 다른 문장은 글자 그대로 둔다."""
    parts = split_sentences(text)
    kept = [p for p in parts if not is_grading_instruction(p)]
    return text if len(kept) == len(parts) else "".join(kept).strip()


# --- 길이 ---


def cut_to_limit(text: str, limit: int = MAX_ANSWER_CHARS) -> str:
    """limit자를 넘으면 문장 경계에서 자른다. 첫 문장부터 넘으면 글자 수로 자른다."""
    if len(text) <= limit:
        return text
    out = ""
    for part in split_sentences(text):
        if len(out) + len(part) > limit:
            break
        out += part
    return out.rstrip() or text[:limit].rstrip()


# --- 전체 ---


def finalize(
    body: Mapping[str, Any] | Any,
    *,
    fallback_action: str,
    known_sections: Collection[str] | None = None,
    category: str | None = None,
) -> dict[str, Any]:
    """IN-04를 지난 응답(또는 고정 문장 응답)에 튜터 고유 검사를 걸어 규격 본문을 낸다.

    채점 지시 문장을 지워 답이 비어도 citations는 IN-04가 낸 값을 그대로 둔다. answer·scaffold는
    정답 근거가 있으면 답 내용과 상관없이 인용이 채점되므로 비우면 인용 점수만 잃는다(IN-04와 같은
    규칙, 통합 #99 코멘트). 범주(`category`)를 알면 빈 답변 문장을 범주에 맞게 고른다.
    """
    src = body if isinstance(body, Mapping) else {}
    action = normalize_action(src.get("action"), fallback_action)
    raw = src.get("answer")
    answer = raw if isinstance(raw, str) else ""
    answer = strip_hidden(answer)
    answer = drop_grading_sentences(answer)
    answer = cut_to_limit(answer.strip())
    if not answer:
        answer = empty_answer(action, category)
    return {
        "answer": answer,
        "action": action,
        "citations": clean_citations(src.get("citations"), action, known_sections),
    }
