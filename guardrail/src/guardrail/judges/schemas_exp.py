"""S3.13 실험용 판정 스키마(기본 끔). 필드 순서가 곧 생성 순서다.

`schemas.py`와 따로 둔다: 통합 테스트의 가짜 응답기가 `guardrail.judges.schemas`의 공개 모델을
전부 훑어 응답을 만드는데, 중첩 목록 모델은 아직 모른다. 채택하면 `schemas.py`로 옮기고 통합
담당에게 알린다.
"""

from typing import Literal

from guardrail.judges.schemas import _Out
from guardrail.schema import Category

AskKind = Literal["finished_work_to_submit", "offcourse_output", "learn", "other"]


class Ask(_Out):
    quote: str
    wants: str
    kind: AskKind


class RequestCheckOut(_Out):
    """S3.13 요청 재확인: 해 달라는 것을 나열하고(asks) 자료 밖 값을 묻는지 본다."""

    asks: list[Ask]
    evidence: str
    verdict: Literal["not_asked", "stated", "not_stated"]


# 실험 input.judge_mode: topdown(exp/guard-jamo-longinput). 무리를 먼저 고르고 그 안에서 범주를
# 고르게 한다. 판정(category)은 맨 마지막이고, decision은 지금처럼 범주 표로만 정한다(group은 쓰지
# 않는다)
Group = Literal["attack", "study", "off_course", "crisis", "other"]


class InputTopdownOut(_Out):
    """입력 판정 topdown: evidence(한 문장) → group → category."""

    evidence: str
    group: Group
    category: Category


class SentCheck(_Out):
    n: int
    kb_phrase: str
    verdict: Literal["matches", "conflicts", "not_in_material", "general"]


class OutputSentOut(_Out):
    """S3.13 문장별 근거 확인(output.judge_mode: sentences). checks를 먼저 쓰고 판정을 낸다."""

    checks: list[SentCheck]
    answer_quote: str
    kb_quote: str
    grounded: bool
    refuses: bool
