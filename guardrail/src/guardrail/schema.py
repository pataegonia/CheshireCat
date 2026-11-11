"""요청·응답 타입과 범주 → decision 표 (master-plan §4.1, ADR-0012).

응답 형식의 기준은 docs/contract/guard.schema.json이다. decision은 저장하지 않고
decision_of()가 설정의 block_categories로 정한다. parse_request는 예외를 내지 않는다(GR-01, GR-09).
"""

import json
from dataclasses import dataclass
from typing import Any, Literal

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

MAX_RESPONSE_BYTES = 256 * 1024  # 응답 상한(미만이어야 함). 넘으면 unsupported를 비운다
_CANDIDATE_KEY = b'"candidate_answer"'


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


def _messages(raw: Any) -> tuple[Message, ...]:
    if not isinstance(raw, list):
        return ()
    return tuple(
        Message(m["role"] if isinstance(m.get("role"), str) else "", m["content"])
        for m in raw
        if isinstance(m, dict) and isinstance(m.get("content"), str)
    )


def parse_request(body: bytes) -> GuardRequest:
    """읽을 수 있는 만큼 읽는다. JSON이 아니거나 너무 깊으면 빈 요청이다."""
    try:
        obj = json.loads(body)
    except (ValueError, RecursionError):  # UnicodeDecodeError·JSONDecodeError는 ValueError
        obj = None
    if not isinstance(obj, dict):
        # 못 읽어도 키가 보이면 output을 낸다(GR-09). 남는 output은 채점기가 무시한다
        return GuardRequest((), None, has_candidate=_CANDIDATE_KEY in body)
    answer = obj.get("candidate_answer")
    return GuardRequest(
        messages=_messages(obj.get("messages")),
        candidate_answer=answer if isinstance(answer, str) else None,
        has_candidate="candidate_answer" in obj,
    )


@dataclass(frozen=True)
class OutputVerdict:
    grounded: bool
    unsupported: tuple[str, ...]
    safe: bool


def _is_span(s: str) -> bool:
    """계약: 빈 문자열 금지(minLength 1), UTF-8로 쓸 수 있어야 함(짝 없는 surrogate 제외)."""
    try:
        return bool(s) and bool(s.encode("utf-8"))
    except UnicodeEncodeError:
        return False


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
                "unsupported": [s for s in self.output.unsupported if _is_span(s)],
                "safe": self.output.safe,
                "category": self.category,
            }
            if len(json.dumps(body, ensure_ascii=False).encode()) >= MAX_RESPONSE_BYTES:
                body["output"]["unsupported"] = []
        return body
