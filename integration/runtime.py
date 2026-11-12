"""통합 런타임 설정 `config/runtime.yaml` (통합 PRD §3-1).

가드 · 튜터 설정과 같은 방식이다: 기본값이 없고, 모르는 키 · 빠진 키 · 느슨한 값("4" → 4,
true → 1)은 로드에서 실패한다. 읽은 뒤에는 바꾸지 않는다(frozen). `budget_s`는 가드 설정이 주인이라
여기 없다.
"""

from pathlib import Path
from typing import Annotated

import yaml
from pydantic import BaseModel, ConfigDict, Field

from integration.contracts import Action

__all__ = ["RUNTIME_PATH", "RuntimeConfig", "load_runtime"]

RUNTIME_PATH = Path(__file__).resolve().parents[1] / "config" / "runtime.yaml"


class RuntimeConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    # 남은 시간이 이보다 적으면 judge_output을 건너뛴다(초). 0이면 건너뛰지 않는다
    judge_skip_s: Annotated[float, Field(ge=0, allow_inf_nan=False)]
    # 생성 실패 · 모르는 범주의 기본 action(주인은 튜터 TU-12)
    fallback_action: Action


def load_runtime(path: str | Path | None = None) -> RuntimeConfig:
    """설정을 읽어 검증한다. 파일이 없으면 OSError, 내용이 틀리면 ValueError."""
    path = RUNTIME_PATH if path is None else Path(path)
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"{path.name}: 최상위가 매핑이 아니다")
    return RuntimeConfig.model_validate(raw)
