"""가드 공통 타입 (master-plan §4.1).

내부 기록은 dataclass로 둔다. LLM 출력 스키마와 설정 검증은 pydantic(config.py, llm.py)이다.
`Role`은 설정의 `faults` 키 검증에도 쓰므로 llm.py가 아니라 여기 둔다(순환 import 방지).
"""

from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, get_args

Action = Literal["allow", "redirect", "partial", "refuse", "crisis"]
Category = Literal[
    "S0", "S1", "S2", "S3", "S4", "S5", "S6", "S7", "S8", "S9", "S10", "S11", "S12", "S13", "S14"
]  # fmt: skip
CrisisTier = Literal["T1", "T2", "T3"]
VerdictKind = Literal["pass", "block", "modified"]
Role = Literal["input_judge", "exchange_judge", "generator", "attack_judge", "jev"]
Stage = Literal[
    "normalize", "crisis", "rules", "pii", "jev", "input_judge", "retrieve", "generate",
    "retrieval_guard", "output_code", "prefilter", "exchange_judge", "repair",
]  # fmt: skip
StageStatus = Literal["ok", "error", "skipped"]

STAGES: tuple[Stage, ...] = get_args(Stage)
ROLES: tuple[Role, ...] = get_args(Role)


@dataclass(frozen=True)
class Turn:
    role: Literal["student", "tutor"]
    content: str


@dataclass(frozen=True)
class Chunk:
    sid: str  # 답변의 [S#]와 같은 값 ("S1")
    source: str
    text: str


@dataclass
class Session:
    """대화 단위 상태. 하네스·튜터가 턴마다 같은 객체를 넘긴다."""

    user_id: str | None = None  # 서비스 모드에서만. trace에는 HMAC만 남는다
    grade_band: str | None = None
    risk: float = 0.0  # 세션 위험 R (S2.8)
    last_block_norm: str | None = None


@dataclass
class StageResult:
    """단계 하나의 기록. trace에 그대로 들어간다."""

    stage: Stage
    action: Action | None
    category: Category | None
    score: float | None
    rule_id: str | None
    reason_code: str  # 기계가 읽는 사유. 예: "crisis_lexicon", "canary_leak", "judge_timeout"
    latency_ms: float
    started_ms: float = 0.0  # 요청 시작 기준. 병렬 구간의 숨은 지연을 잰다
    attempts: int = 1
    tokens_in: int = 0
    tokens_out: int = 0
    model: str | None = None
    status: StageStatus = "ok"  # skipped = 설정으로 꺼짐(실패가 아님)
    error: str | None = None  # LLMResult.error 코드나 예외 클래스 이름만. 메시지는 넣지 않는다
    fallback_infra: bool = False
    detail: dict[str, Any] = field(default_factory=dict)  # 문장 번호·라벨·점수만. 원문 금지


@dataclass
class Verdict:
    """GR-11 공통 필드 + 가드 내부 필드."""

    verdict: (
        VerdictKind  # block = "최종, 생성하지 않음". 입력 전환·부분(instruction 있음)은 modified
    )
    action: Action
    category: Category
    confidence: float | None
    rule_id: str | None
    reason_code: str
    reason: str  # 내부 디버깅용. 학생에게 보이지 않는다
    latency_ms: float
    policy_version: str
    request_id: str  # Guard가 호출마다 만드는 uuid4
    response: str | None  # 학생에게 보일 최종 문구. check_input 허용이면 None
    crisis_tier: CrisisTier | None = None
    crisis_flag: bool = False  # ② 위기 어휘가 잡혔는지. check_input → check_output으로 넘어간다
    instruction: str | None = None  # check_input 전환·부분일 때 generate(instruction=)에 넘길 지시
    fallback_infra: bool = False
    stages: list[StageResult] = field(default_factory=list)

    def __post_init__(self) -> None:
        # 잘못된 조합은 만들 때 막는다(GP0 S0.2 불변식)
        if self.verdict != "pass" and self.response is None and self.instruction is None:
            raise ValueError(f"verdict={self.verdict}인데 response와 instruction이 둘 다 없다")
        if self.instruction is not None and (
            self.action not in ("redirect", "partial") or self.verdict != "modified"
        ):
            raise ValueError(
                "instruction은 modified·redirect/partial에만 있다"
                f"(지금 {self.verdict}·{self.action})"
            )
        if self.action == "crisis" and self.crisis_tier is None:
            raise ValueError("action=crisis인데 crisis_tier가 없다")


class TutorAdapter(Protocol):
    """튜터 쪽 연결 계약(GR-11).

    `system_prompt`는 선택 속성이다. 있으면 ⑦이 시스템 프롬프트 문장 유출을 이것과도 비교한다(S1.3).
    없는 어댑터도 받도록 가드는 `getattr(tutor, "system_prompt", None)`으로 읽는다.
    """

    system_prompt: str | None

    async def retrieve(self, question: str, history: list[Turn]) -> list[Chunk]: ...

    async def generate(
        self,
        question: str,
        history: list[Turn],
        chunks: list[Chunk],
        *,
        system_extra: str = "",
        instruction: str | None = None,
    ) -> str: ...
