"""GR-12 설정: YAML 로드·검증, overrides, ${ENV} 치환, config_hash, 단계 켜기 규칙.

계약은 master-plan §4.4.

- 모든 모델이 extra="forbid"라 오타는 로드에서 잡힌다.
- 단계 토글은 기본값이 없다. 단계 상태는 YAML에서만 정해진다.
- 키·base URL은 설정에 두지 않는다(환경변수로만). ${VAR} 치환은 `models` 아래 값에만 한다.
"""

import hashlib
import json
import os
import re
from pathlib import Path
from typing import Annotated, Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from guardrail.types import Role

ROOT = Path(__file__).resolve().parents[2]  # guardrail/

Threshold = Annotated[float, Field(ge=0.0, le=1.01)]  # 1.01 = 막는 쪽 임계값에서 "막지 않고 점수만"
Positive = Annotated[float, Field(gt=0)]
FaultValue = Literal["timeout", "rate_limit", "server", "bad_json", "fake_ok"]

# 켜기 규칙을 지키는 곳은 check_toggles 하나다. 단계를 파이프라인에 붙이는 슬라이스가 여기에 토글
# 경로를 더하고, 같은 PR에서 default.yaml의 토글을 켠다(GP0 S0.2 표).
_IMPLEMENTED: frozenset[str] = frozenset()


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PipelineCfg(_Strict):
    mode: Literal["parallel", "serial"] = "parallel"


class InputStages(_Strict):
    rules: bool
    pii: bool
    judge: bool
    jev: bool


class SessionStage(_Strict):
    enabled: bool


class RetrievalStage(_Strict):
    enabled: bool


class OutputStages(_Strict):
    code: bool
    prefilter: bool
    judge: bool
    repair: bool
    r2: bool


class Stages(_Strict):
    crisis: bool
    input: InputStages
    session: SessionStage
    retrieval: RetrievalStage
    output: OutputStages


class Policy(_Strict):
    d1: Literal["s2", "s1_s2"] = "s2"


class Models(_Strict):
    # 이름은 게이트웨이 목록이 나오면 채운다. 판정은 생성과 다른 계열(ADR-0004)
    generator: str | None = None
    input_judge: str | None = None
    exchange_judge: str | None = None
    attack_judge: str | None = None
    jev: str | None = None


class Thresholds(_Strict):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    input_block: Threshold = 0.0
    tau_J: Threshold | None = None
    tau_m: Threshold | None = None
    theta: Threshold = 0.9
    G: Threshold = 0.7
    R_expand: Threshold = 0.5
    lambda_: Threshold = Field(0.9, alias="lambda")


class ContextTurns(_Strict):
    base: int = Field(4, ge=1)
    expanded: int = Field(8, ge=1)


class Deadlines(_Strict):
    input_judge: Positive = 2.5
    exchange_judge: Positive = 3.0
    generator: Positive = 20.0
    jev: Positive = 1.5
    attack_judge: Positive = 10.0
    tutor_retrieve: Positive = 5.0


class GateCfg(_Strict):
    initial: int = Field(4, ge=1)
    min: int = Field(1, ge=1)
    max: int = Field(16, ge=1)
    increase_after: int = Field(10, ge=1)
    max_wait_s: Positive = 30.0
    structured_output: Literal["json_schema", "json_object", "prompt"] = "json_schema"

    @model_validator(mode="after")
    def _bounds(self) -> "GateCfg":
        if not self.min <= self.initial <= self.max:
            raise ValueError(
                f"gate는 min ≤ initial ≤ max여야 한다({self.min}, {self.initial}, {self.max})"
            )
        return self


class Obligations(_Strict):
    cites: bool = True
    canary: bool = True
    wraps_chunks: bool = True
    non_streaming: bool = True
    regenerate: bool = True  # R2는 이것과 stages.output.r2가 둘 다 참일 때만


class TutorCfg(_Strict):
    adapter: Literal["mock", "real"] = "mock"
    obligations: Obligations = Obligations()


class ServiceLog(_Strict):
    retention_days: int = Field(30, ge=1)
    keep_raw_for: list[Literal["block", "escalation", "appeal", "sample"]] = Field(
        default_factory=lambda: ["block", "escalation", "appeal", "sample"]
    )
    sample_rate: float = Field(0.01, ge=0.0, le=1.0)
    path: str = "logs/service-trace.jsonl"  # 서비스 모드에서 Guard가 여는 trace 위치(S2.3)


class Paths(_Strict):
    prompts: str = "prompts"
    resources: str = "resources"


class GuardConfig(_Strict):
    policy_version: str
    mode: Literal["eval", "service"]
    pipeline: PipelineCfg = PipelineCfg()
    stages: Stages
    policy: Policy = Policy()
    models: Models = Models()
    thresholds: Thresholds = Thresholds()
    context_turns: ContextTurns = ContextTurns()
    deadlines_s: Deadlines = Deadlines()
    gate: GateCfg = GateCfg()
    tutor: TutorCfg = TutorCfg()
    service_log: ServiceLog = ServiceLog()
    faults: dict[Role, FaultValue] = Field(default_factory=dict)  # 평가 전용 장애 주입
    paths: Paths = Paths()

    @model_validator(mode="after")
    def _service_mode_rules(self) -> "GuardConfig":
        if self.mode != "service":
            return self
        problems = []
        if not self.stages.crisis:
            problems.append("stages.crisis가 꺼져 있다")
        if not self.stages.input.pii:
            problems.append("stages.input.pii가 꺼져 있다(마스킹 없이 LLM에 보내지 않는다, GR-03)")
        if self.faults:
            problems.append("faults가 비어 있지 않다(평가 전용)")
        if not os.environ.get("GUARD_LOG_HMAC_KEY"):
            # 변수 이름만 쓴다. 값은 어떤 메시지에도 넣지 않는다
            problems.append("환경변수 GUARD_LOG_HMAC_KEY가 없다")
        if problems:
            raise ValueError("서비스 모드 설정 오류: " + "; ".join(problems))
        return self


# --- 로드 ---

_ENV_REF = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def _substitute_env(value: Any) -> Any:
    """`${VAR}`를 환경변수 값으로 바꾼다. 참조한 변수가 하나라도 없으면 None(오프라인 테스트용)."""
    if not isinstance(value, str) or not _ENV_REF.search(value):
        return value
    names = _ENV_REF.findall(value)
    if any(os.environ.get(n) is None for n in names):
        return None
    return _ENV_REF.sub(lambda m: os.environ[m.group(1)], value)


def _apply_override(raw: dict, dotted: str, value: Any) -> None:
    keys = dotted.split(".")
    node = raw
    for i, key in enumerate(keys[:-1]):
        nxt = node.setdefault(key, {})
        if not isinstance(nxt, dict):
            raise ValueError(
                f"overrides 경로 {dotted!r}: {'.'.join(keys[: i + 1])}는 값이라 더 들어갈 수 없다"
            )
        node = nxt
    node[keys[-1]] = value


def resolve_path(path: str | Path) -> Path:
    """상대 경로는 현재 위치에 없으면 guardrail/ 기준으로 푼다."""
    p = Path(path)
    if p.is_absolute() or p.exists():
        return p
    return ROOT / p


def load_config(path: str | Path, overrides: dict[str, Any] | None = None) -> GuardConfig:
    """YAML을 읽고 overrides(점 경로)를 검증 **전에** 덮어쓴 뒤 검증한다.

    그래서 모르는 경로는 extra="forbid"로 실패하고, 덮어쓴 값이 config_hash에 들어간다.
    """
    raw = yaml.safe_load(resolve_path(path).read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ValueError(f"설정 파일 최상위는 매핑이어야 한다: {path}")
    for dotted, value in (overrides or {}).items():
        _apply_override(raw, dotted, value)
    if isinstance(raw.get("models"), dict):
        raw["models"] = {k: _substitute_env(v) for k, v in raw["models"].items()}
    return GuardConfig.model_validate(raw)


def config_hash(cfg: GuardConfig) -> str:
    """치환 뒤 값으로 계산하므로 실제 모델명이 해시에 들어간다."""
    dumped = json.dumps(
        cfg.model_dump(mode="json", by_alias=True), sort_keys=True, ensure_ascii=False
    )
    return hashlib.sha256(dumped.encode("utf-8")).hexdigest()[:12]


# --- 단계 켜기 규칙 ---


def enabled_toggles(cfg: GuardConfig) -> list[str]:
    """켜진 단계 토글의 경로("output.code" 형식), 선언 순서."""
    st = cfg.stages
    flags = {
        "crisis": st.crisis,
        "input.rules": st.input.rules,
        "input.pii": st.input.pii,
        "input.judge": st.input.judge,
        "input.jev": st.input.jev,
        "session.enabled": st.session.enabled,
        "retrieval.enabled": st.retrieval.enabled,
        "output.code": st.output.code,
        "output.prefilter": st.output.prefilter,
        "output.judge": st.output.judge,
        "output.repair": st.output.repair,
        "output.r2": st.output.r2,
    }
    return [name for name, on in flags.items() if on]


def check_toggles(cfg: GuardConfig) -> None:
    """켰지만 구현되지 않은 단계가 있으면 멈춘다(조용히 건너뛰지 않는다).

    순수 함수다. 설정만 보고 Guard·LLM·튜터·환경변수를 쓰지 않는다. Guard.__init__과 하네스
    --estimate(S0.7)가 이 함수를 부른다.
    """
    missing = [t for t in enabled_toggles(cfg) if t not in _IMPLEMENTED]
    if missing:
        raise NotImplementedError("켰지만 아직 구현되지 않은 단계: " + ", ".join(missing))
    if cfg.stages.input.jev and not cfg.stages.input.pii:
        # Jev에는 마스킹한 글만 간다(ADR-0008)
        raise ValueError("stages.input.jev는 stages.input.pii가 켜졌을 때만 켤 수 있다")


# --- 프롬프트 ---

_FRONT = re.compile(r"\A---\n(?P<head>.*?)\n---\n", re.S)


def load_prompt(cfg: GuardConfig, name: str) -> tuple[str, str]:
    """`prompts/<name>.md`의 앞머리(--- / id: / version: / ---)를 읽어 (version, 본문)을 낸다."""
    path = resolve_path(Path(cfg.paths.prompts) / f"{name}.md")
    text = path.read_text(encoding="utf-8")
    m = _FRONT.match(text)
    if not m:
        raise ValueError(f"프롬프트 {name}: 버전 앞머리(---/id/version/---)가 없다")
    head = yaml.safe_load(m.group("head")) or {}
    if head.get("id") != name or not head.get("version"):
        raise ValueError(f"프롬프트 {name}: 앞머리 id·version이 맞지 않다({head})")
    return str(head["version"]), text[m.end() :]
