"""세 트랙이 같이 쓰는 계약 타입 (통합 PRD IN-01).

가드 · 튜터 · 하네스는 이 모듈을 import만 한다(타입 중복 0). 범주(`Category`)는 가드가 주인이라
여기서는 다시 내보내기만 한다.
"""

import re
from typing import Literal, get_args

from guardrail.schema import Category
from pydantic import BaseModel, Field, field_validator

__all__ = [
    "ACTIONS",
    "SECTION_IDS",
    "SID_PATTERN",
    "Action",
    "Category",
    "Evidence",
    "Section",
    "Sentence",
    "TutorResult",
]

Action = Literal["answer", "scaffold", "clarify", "abstain", "redirect", "refuse", "escalate"]
ACTIONS: tuple[Action, ...] = get_args(Action)

# `$`는 끝 줄바꿈 앞에서도 맞으므로 검사는 fullmatch로 한다.
SID_PATTERN = re.compile(r"^(PY|PR|FIN)-\d{2}-\d{2}$")

# 교재 섹션 48개: 3과목 × 4단원 × 4섹션. competition/kb 파일 목록과 같은지는 테스트가 확인한다.
SECTION_IDS: frozenset[str] = frozenset(
    f"{subject}-{unit:02d}-{section:02d}"
    for subject in ("PY", "PR", "FIN")
    for unit in range(1, 5)
    for section in range(1, 5)
)


class Section(BaseModel):
    """교재 섹션 하나. 검색(`retrieve`)이 돌려주고 판정 · 결정적 수정이 원문으로 쓴다."""

    id: str
    subject: str
    title: str
    text: str
    tables: list[str] = Field(default_factory=list)
    names: list[str] = Field(default_factory=list)


class Evidence(BaseModel):
    """문장 하나의 근거: 섹션 ID와 그 섹션 원문에서 따온 문구."""

    sid: str
    quote: str


class Sentence(BaseModel):
    """튜터 주 호출 출력의 문장 단위. claim = 과목 사실, non_claim = 일반 설명 · 격려 · 되묻기."""

    text: str
    kind: Literal["claim", "non_claim"]
    evidence: list[Evidence] = Field(default_factory=list)


class TutorResult(BaseModel):
    """`POST /tutor` 응답 본문."""

    answer: str
    action: Action
    citations: list[str]

    @field_validator("citations")
    @classmethod
    def _citations_exist(cls, value: list[str]) -> list[str]:
        for sid in value:
            if SID_PATTERN.fullmatch(sid) is None or sid not in SECTION_IDS:
                raise ValueError("citations에 형식이 틀리거나 없는 섹션 ID가 있다")
        return value
