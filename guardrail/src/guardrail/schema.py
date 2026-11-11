"""요청·응답 타입과 범주 → decision 표 (master-plan §4.1, ADR-0012).

응답 형식의 기준은 docs/contract/guard.schema.json이다. decision은 저장하지 않고
decision_of()가 설정의 block_categories로 정한다. 요청 파싱(parse_request)은 S1.2.
"""

from dataclasses import dataclass
from typing import Literal

Category = Literal[
    "normal",
    "answer_seeking",
    "academic_dishonesty",
    "off_topic",
    "out_of_material",
    "jailbreak_injection",
    "pii_disclosure",
    "harmful_content",
    "emotional_distress",
    "low_signal_input",
]
Decision = Literal["allow", "block"]


def decision_of(category: Category, block: frozenset[Category]) -> Decision:
    return "block" if category in block else "allow"


@dataclass(frozen=True)
class Message:
    role: str  # user가 아니면 학습자 발화로 보지 않는다
    content: str


@dataclass(frozen=True)
class GuardRequest:
    messages: tuple[Message, ...]  # 못 읽으면 ()
    candidate_answer: str | None  # 키가 없거나 문자열이 아니면 None
    has_candidate: bool  # 키가 있었는가(값을 못 읽어도 True) → output 필수


@dataclass(frozen=True)
class OutputVerdict:
    grounded: bool
    unsupported: tuple[str, ...]
    safe: bool


@dataclass(frozen=True)
class GuardResponse:
    category: Category
    output: OutputVerdict | None

    def to_json(self, block: frozenset[Category]) -> dict:
        body: dict = {
            "input": {"decision": decision_of(self.category, block), "category": self.category}
        }
        if self.output is not None:
            body["output"] = {
                "grounded": self.output.grounded,
                "unsupported": list(self.output.unsupported),
                "safe": self.output.safe,
                "category": self.category,
            }
        return body
