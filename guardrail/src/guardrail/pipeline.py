"""공개 API `Guard` (master-plan §4.2). S0.2는 무방비 모드 골격이다.

단계를 붙이는 슬라이스(S1.x·S2.x)가 check_toggles 등록과 함께 이 파일에 자기 단계를 넣는다.
지금은 가드 단계가 모두 꺼져 있어 튜터 답을 그대로 돌려주고, 단계 13개를 기록만 한다.
"""

import inspect
import time
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from guardrail.config import GuardConfig, check_toggles, load_config
from guardrail.llm import LLM
from guardrail.types import (
    Chunk,
    CrisisTier,
    Session,
    Stage,
    StageResult,
    Turn,
    TutorAdapter,
    Verdict,
)

INPUT_STAGES: tuple[Stage, ...] = ("normalize", "crisis", "rules", "pii", "jev", "input_judge")
OUTPUT_STAGES: tuple[Stage, ...] = ("output_code", "prefilter", "exchange_judge", "repair")


def _skipped(stage: Stage) -> StageResult:
    """설정으로 꺼진 단계. 실패(error)와 다르고 fallback_infra가 아니다."""
    return StageResult(stage, None, None, None, None, "disabled", 0.0, status="skipped")


def _ms(since: float) -> float:
    return (time.perf_counter() - since) * 1000.0


async def _run_stage(
    stage: Stage,
    fn: Callable[[], StageResult | Awaitable[StageResult]],
    *,
    t0: float,
    clock: Callable[[], float] = time.perf_counter,
) -> StageResult:
    """가드 단계 하나를 돌리고 시간을 잰다(S0.4). 실패 처리는 여기서 하지 않는다(GR-14 표 한 곳).

    예외면 status="error"이고 error에는 클래스 이름만 넣는다(메시지에 키가 섞여 trace로 새지 않게).
    CancelledError는 잡지 않는다(Exception이 아니다). 튜터 단계 retrieve·generate는 이 함수를 쓰지
    않고 예외를 올린다(생성 실패 처리는 S2.5).
    """
    started = clock()
    try:
        out = fn()
        result = await out if inspect.isawaitable(out) else out
    except Exception as e:  # noqa: BLE001 — 단계 하나의 예외로 파이프라인을 멈추지 않는다
        result = StageResult(
            stage, None, None, None, None, "stage_exception", 0.0,
            status="error", error=type(e).__name__,
        )  # fmt: skip
    result.started_ms = (started - t0) * 1000.0
    result.latency_ms = (clock() - started) * 1000.0
    return result


class Guard:
    def __init__(self, cfg: GuardConfig, *, llm: LLM | None = None) -> None:
        check_toggles(cfg)  # 토글 검사를 Guard 안에 따로 두지 않는다
        self.cfg = cfg
        # 하네스는 LLM 하나를 Guard와 모의 튜터에 같이 넘겨 생성과 판정이 게이트 하나를 쓴다
        self.llm = llm if llm is not None else LLM(cfg)

    @classmethod
    def from_config(
        cls, path: str | Path, overrides: dict[str, Any] | None = None, *, llm: LLM | None = None
    ) -> "Guard":
        return cls(load_config(path, overrides), llm=llm)

    # --- 가드 문구 ---

    def new_canary(self) -> str | None:
        """output.code가 꺼졌거나 tutor.obligations.canary가 거짓이면 None. 카나리아는 S1.2."""
        return None

    def system_extra(self, canary: str | None = None) -> str:
        """켜진 단계의 가드 문구만 모은다.

        카나리아 지시는 output.code, datamarking 문구는 retrieval.enabled가 켜졌을 때만 넣는다.

        GP0에는 둘 다 없으므로 언제나 "". 무방비 베이스라인이 가드 문구 없이 돌게 한다.
        """
        parts: list[str] = []
        return "\n\n".join(parts)

    # --- 기본 경로: 구조 B2 전체 ---

    async def respond(
        self,
        message: str,
        history: list[Turn],
        tutor: TutorAdapter,
        session: Session | None = None,
    ) -> Verdict:
        request_id = str(uuid.uuid4())
        session = session if session is not None else Session()
        t0 = time.perf_counter()
        stages = [_skipped(s) for s in INPUT_STAGES]

        # 튜터 단계는 시간만 잰다. 예외는 올린다(하네스가 항목 단위로 잡는다, 생성 실패 처리는 S2.5)
        started = _ms(t0)
        chunks = await tutor.retrieve(message, history)
        stages.append(
            StageResult("retrieve", None, None, None, None, "ok", _ms(t0) - started, started)
        )

        guarded = self.guard_retrieval(chunks)
        stages.append(_skipped("retrieval_guard"))

        started = _ms(t0)
        answer = await tutor.generate(
            message, history, guarded, system_extra=self.system_extra(None), instruction=None
        )
        # 어댑터가 문자열만 돌려주므로 토큰은 0, 낭비 지표용으로 완성 답 길이만 남긴다(§4.1)
        stages.append(
            StageResult(
                "generate", None, None, None, None, "ok", _ms(t0) - started, started,
                detail={"chars": len(answer)},
            )
        )  # fmt: skip
        stages.extend(_skipped(s) for s in OUTPUT_STAGES)

        verdict = self._pass(request_id, answer, _ms(t0), stages)
        return self._finalize(verdict, verdict.crisis_flag, verdict.crisis_tier)

    # --- 튜터가 흐름을 쥘 때 (GR-11 함수, 방식 B) ---

    async def check_input(
        self, message: str, history: list[Turn], session: Session | None = None
    ) -> Verdict:
        request_id = str(uuid.uuid4())
        t0 = time.perf_counter()
        stages = [_skipped(s) for s in INPUT_STAGES]
        verdict = self._pass(request_id, None, _ms(t0), stages)
        return self._finalize(verdict, verdict.crisis_flag, verdict.crisis_tier)

    def guard_retrieval(self, chunks: list[Chunk]) -> list[Chunk]:
        """⑥ 검색 가드(S2.9). 꺼져 있으면 청크를 그대로 돌려준다."""
        return list(chunks)

    async def check_output(
        self,
        question: str,
        history: list[Turn],
        chunks: list[Chunk],
        answer: str,
        input_verdict: Verdict,
        regenerate: Callable[[str], Awaitable[str]] | None = None,
        canary: str | None = None,
        system_prompt: str | None = None,
    ) -> Verdict:
        request_id = str(uuid.uuid4())
        t0 = time.perf_counter()
        # 입력 단계 기록을 앞에 복사한다. S2.5의 _finalize가 그 안의 ②·⑤ 오류를 읽어 연락처를 붙인다
        stages = list(input_verdict.stages) + [_skipped(s) for s in OUTPUT_STAGES]
        verdict = self._pass(
            request_id, answer, _ms(t0), stages, crisis_flag=input_verdict.crisis_flag
        )
        # 위기 플래그·등급은 input_verdict에서 받는다(§4.2)
        return self._finalize(verdict, input_verdict.crisis_flag, input_verdict.crisis_tier)

    # --- 내부 ---

    def _pass(
        self,
        request_id: str,
        response: str | None,
        latency_ms: float,
        stages: list[StageResult],
        *,
        crisis_flag: bool = False,
    ) -> Verdict:
        # 필드는 생성자로만 채운다(사후 대입은 Verdict 불변식 검사를 건너뛴다)
        return Verdict(
            verdict="pass",
            action="allow",
            category="S0",
            confidence=None,
            rule_id=None,
            reason_code="stages_off",
            reason="",
            latency_ms=latency_ms,
            policy_version=self.cfg.policy_version,
            request_id=request_id,
            response=response,
            crisis_flag=crisis_flag,
            stages=stages,
        )

    def _finalize(
        self, verdict: Verdict, crisis_flag: bool, crisis_tier: CrisisTier | None
    ) -> Verdict:
        """위기 우선: 모든 응답의 마지막에 돈다. 위기 템플릿 치환·연락처 줄은 S2.1·S2.5가 채운다."""
        return verdict
