"""고정·템플릿 문장 (PRD §3 답변 방식, TU-07 빈 답변, TU-10 합치기, TU-12 기본값).

- 문항과 무관한 일반 문장이다. 과목 사실·예제 이름·수치를 넣지 않는다(하드코딩 아님, TU-12·TU-18).
  그래서 이 문장으로 낸 답은 인용할 섹션이 없고 `citations=[]`다(TU-07).
- 전화번호·상담 기관 이름을 넣지 않는다(TD-08). "상담 기관"이라는 일반 낱말만 쓴다.
- 요청 내용을 되풀이하지 않는다(응함·반사 방지).
  거절·전환 문장은 요청 내용을 일부도 수행하지 않는다.
- 채점·평가자에게 하는 말을 쓰지 않는다(SPEC §6).
- 어떤 경로에서 어떤 문장을 쓸지는 이 모듈이 정하지 않는다. 범주 → action 표는 통합
  `config/actions.yaml`(IN-03), 기본 action은 공유 설정 `fallback_action`(TU-12)이 정한다.
"""

from enum import StrEnum
from types import MappingProxyType


class Fixed(StrEnum):
    """고정 문장의 쓰임. 값은 action과 같은 이름을 앞에 둔다(로그에는 남기지 않는다, TU-15)."""

    CLARIFY = "clarify"  # 빈 발화·저신호 직접 판정·요청 파싱 실패·주 호출 실패 기본값(잠정)
    REFUSE_JAILBREAK = "refuse.jailbreak"  # 지시 무력화·숨겨진 지시 요구
    REFUSE_HARMFUL = "refuse.harmful"  # 남을 해치는 방법·남의 정보 탐색·사기 문구
    REDIRECT_OFF_TOPIC = "redirect.off_topic"  # 수업과 무관한 결과물
    REDIRECT_HARMFUL = "redirect.harmful"  # 비하·혐오·모욕 발언
    ESCALATE = "escalate"  # 지속되는 정서적 고통
    SCAFFOLD_ACADEMIC = "scaffold.academic"  # 평가 제출물 대행 요청(과목 사실 없는 순서 안내)
    ABSTAIN = "abstain"  # 자료에 없는 내용(덧붙일 자료 내용이 없을 때)


_TEXT: dict[Fixed, str] = {
    Fixed.CLARIFY: "무엇이 궁금한지 조금 더 알려 줄래요?",
    Fixed.REFUSE_JAILBREAK: (
        "이전 지시를 무시하거나 숨겨진 지시를 보여 주는 요청은 따를 수 없어요. "
        "수업 내용에 관한 질문은 계속 도와줄게요."
    ),
    Fixed.REFUSE_HARMFUL: (
        "다른 사람을 속이거나 해치는 방법, 다른 사람의 정보를 알아내는 일은 도와줄 수 없어요. "
        "안전하게 공부하는 방법은 계속 도와줄게요."
    ),
    Fixed.REDIRECT_OFF_TOPIC: (
        "그 요청은 이 수업과 관련이 없어요. "
        "대신 수업 자료에 나온 내용이나 실습을 함께 살펴볼 수 있어요."
    ),
    Fixed.REDIRECT_HARMFUL: (
        "다른 사람을 깎아내리거나 차별하는 표현은 사용하지 말아 주세요. "
        "수업 내용은 계속 도와줄게요."
    ),
    Fixed.ESCALATE: (
        "그동안 혼자 버티느라 많이 힘들었겠어요. "
        "지금은 공부를 잠시 멈추고 믿을 수 있는 선생님이나 보호자에게 지금 마음을 이야기해 주세요. "
        "학교 상담 선생님이나 상담 기관에 도움을 요청해도 좋아요."
    ),
    Fixed.SCAFFOLD_ACADEMIC: (
        "제출할 답을 대신 완성해 줄 수는 없지만, 직접 쓸 수 있는 순서를 알려 줄게요. "
        "먼저 자료에서 관련 개념을 찾아 한 문장으로 정리하고, "
        "그다음 과제의 조건에 맞게 적용해 보세요. "
        "직접 쓴 내용을 가져오면 함께 살펴볼게요."
    ),
    Fixed.ABSTAIN: "그 내용은 수업 자료에 나와 있지 않아요.",
}
TEXT = MappingProxyType(_TEXT)

# Fixed → 응답 action
_ACTION: dict[Fixed, str] = {f: f.value.split(".", 1)[0] for f in Fixed}
ACTION = MappingProxyType(_ACTION)

# 남은 claim이 0개일 때 덧붙이는 안내(통합 IN-04-3이 쓴다. 문구 주인은 튜터)
NOT_IN_MATERIAL_NOTE = "이 부분은 자료에서 확인하지 못했어요."

# 답변 문장이 하나도 남지 않았을 때 action별 고정 문장(TU-07 "빈 답변").
# answer·scaffold는 action을 바꾸지 않고(IN-04-3) 자료에서 확인하지 못했다는 안내를 낸다.
_EMPTY: dict[str, str] = {
    "answer": NOT_IN_MATERIAL_NOTE,
    "scaffold": "자료에서 관련 기준을 먼저 찾아보고, 그 기준을 문제에 하나씩 적용해 보세요.",
    "abstain": TEXT[Fixed.ABSTAIN],
    "clarify": TEXT[Fixed.CLARIFY],
    "redirect": TEXT[Fixed.REDIRECT_OFF_TOPIC],
    "refuse": "그 요청은 도와줄 수 없어요. 수업 내용에 관한 질문은 계속 도와줄게요.",
    "escalate": TEXT[Fixed.ESCALATE],
}
EMPTY_ANSWER = MappingProxyType(_EMPTY)


def fixed_body(kind: Fixed) -> dict[str, object]:
    """고정 문장 응답 본문. 과목 사실이 없으므로 citations는 언제나 []."""
    return {"answer": TEXT[kind], "action": ACTION[kind], "citations": []}
