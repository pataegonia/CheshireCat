"""GR-13 trace: 요청별 JSONL 레코드와 쓰기 (master-plan §4.5).

- 평가 모드에만 원문(input_raw, displayed_output)을 둔다(학생 데이터 없음).
- 서비스 모드는 원문을 두지 않고 output_code 단계 detail["canary"]도 지운다. user_hmac·grade_band와
  보관 사유 필드는 S2.3이 채운다(session 인자는 서명을 뒤에 바꾸지 않으려고 지금 둔다).
- 키는 두 겹으로 막는다. ① 만들 때: StageResult.error에는 오류 코드·예외 클래스 이름만 넣는다.
  ② 쓸 때: 비밀 환경변수 값(8자 이상)과 Bearer·sk- 모양을 [REDACTED]로 바꾼다.
"""

import json
import os
import re
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType
from typing import Any

from guardrail.config import GuardConfig, config_hash
from guardrail.types import Session, Verdict

# §4.4 비밀 환경변수. 모델명(GEN_MODEL·JUDGE_MODEL)은 비밀이 아니고 trace에 남아야 하므로 뺀다
SECRET_ENV_PREFIXES: tuple[str, ...] = ("GUARD_LLM_", "JEV_", "TUTOR_")
SECRET_ENV_EXACT: frozenset[str] = frozenset({"GUARD_LOG_HMAC_KEY"})
_MIN_SECRET_LEN = 8
_REDACTED = "[REDACTED]"
# 따옴표·역슬래시에서 멈춰 JSON 줄을 깨지 않는다
_PATTERNS = (
    re.compile(r"(?i)bearer\s+[^\s\"\\]+"),
    re.compile(r"sk-[A-Za-z0-9_-]{16,}"),
)
_TUTOR_STAGES = frozenset({"retrieve", "generate"})


def make_record(
    request_id: str,
    verdict: Verdict,
    cfg: GuardConfig,
    *,
    session: Session | None = None,
    input_request_id: str | None = None,
    input_raw: str | None = None,
    displayed_output: str | None = None,
) -> dict[str, Any]:
    """요청 하나의 trace 레코드. 평가 모드의 request_id는 하네스의 평가 id다."""
    service = cfg.mode == "service"
    stages = [asdict(s) for s in verdict.stages]  # 깊은 복사라 Verdict는 바뀌지 않는다
    if service:
        for s in stages:
            if s["stage"] == "output_code":
                s["detail"].pop("canary", None)
    guard_stages = [s for s in verdict.stages if s.stage not in _TUTOR_STAGES]
    record: dict[str, Any] = {
        "request_id": request_id,
        "ts": datetime.now(UTC).isoformat(timespec="milliseconds"),
        "policy_version": verdict.policy_version,
        "config_hash": config_hash(cfg),
        "mode": cfg.mode,
        "final_action": verdict.action,
        "verdict": verdict.verdict,
        "crisis_tier": verdict.crisis_tier,
        "fallback_infra": verdict.fallback_infra,
        "latency_ms": verdict.latency_ms,
        # 가드 토큰만 센다. 튜터 단계(retrieve·generate)는 뺀다(§4.1)
        "tokens_in": sum(s.tokens_in for s in guard_stages),
        "tokens_out": sum(s.tokens_out for s in guard_stages),
        "stages": stages,
    }
    if service:
        if input_request_id is not None:
            record["input_request_id"] = input_request_id
    else:
        record["input_raw"] = input_raw
        record["displayed_output"] = displayed_output
    return record


def _secret_values() -> list[str]:
    values = {
        v
        for k, v in os.environ.items()
        if (k in SECRET_ENV_EXACT or k.startswith(SECRET_ENV_PREFIXES))
        and len(v) >= _MIN_SECRET_LEN
    }
    # 긴 값부터 바꿔 한 값이 다른 값의 일부일 때도 남지 않게 한다
    return sorted(values, key=len, reverse=True)


def _scrub(line: str, secrets: list[str]) -> str:
    for value in secrets:
        line = line.replace(value, _REDACTED)
        escaped = json.dumps(value, ensure_ascii=False)[1:-1]  # JSON 안에서 이스케이프된 모양
        if escaped != value:
            line = line.replace(escaped, _REDACTED)
    for pattern in _PATTERNS:
        line = pattern.sub(_REDACTED, line)
    return line


class TraceWriter:
    """JSONL 한 줄씩 덧붙이고 바로 flush한다. 비밀 값 목록은 만들 때 한 번 모은다."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._secrets = _secret_values()
        self._f = self.path.open("a", encoding="utf-8")

    def write(self, record: dict[str, Any]) -> None:
        line = json.dumps(record, ensure_ascii=False)
        self._f.write(_scrub(line, self._secrets) + "\n")
        self._f.flush()

    def close(self) -> None:
        self._f.close()

    def __enter__(self) -> "TraceWriter":
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()
