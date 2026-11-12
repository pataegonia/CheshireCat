"""통합 런타임 설정 `config/runtime.yaml` (통합 PRD §3-1).

가드 · 튜터 설정과 같은 방식이다: 기본값이 없고, 모르는 키 · 빠진 키 · 같은 키 두 번 · 느슨한 값
("4" → 4, true → 1)은 로드에서 실패한다. 읽은 뒤에는 바꾸지 않는다(frozen). `budget_s`는 가드
설정이 주인이라 여기 없다.

실패 메시지에는 파일 이름 · 키 이름 · 이유만 담는다. 파일 내용과 입력 값은 넣지 않는다.
"""

import re
from pathlib import Path
from typing import Annotated, Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from integration.contracts import Action

__all__ = ["JUDGE_SKIP_MAX_S", "RUNTIME_PATH", "RuntimeConfig", "load_runtime"]

RUNTIME_PATH = Path(__file__).resolve().parents[1] / "config" / "runtime.yaml"
JUDGE_SKIP_MAX_S = 30  # 요청 제한 30초(SPEC). 이보다 크면 판정을 언제나 건너뛰는 값이라 오타로 본다

_KEY_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,39}")
_REASONS = {
    "missing": "빠졌다",
    "extra_forbidden": "모르는 키",
    "literal_error": "action 7종이 아니다",
    "float_type": "숫자가 아니다",
    "greater_than_equal": "0보다 작다",
    "less_than_equal": f"{JUDGE_SKIP_MAX_S}보다 크다",
    "finite_number": "유한한 수가 아니다",
}


class RuntimeConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    # 남은 시간이 이보다 적으면 judge_output을 건너뛴다(초). 0이면 건너뛰지 않는다
    judge_skip_s: Annotated[float, Field(ge=0, le=JUDGE_SKIP_MAX_S, allow_inf_nan=False)]
    # 생성 실패 · 모르는 범주의 기본 action(주인은 튜터 TU-12)
    fallback_action: Action


class _DuplicateKey(Exception):
    def __init__(self, key: Any) -> None:
        super().__init__()
        self.key = key


class _UniqueKeyLoader(yaml.SafeLoader):
    """SafeLoader + 같은 키가 두 번 나오면 실패(기본 동작은 뒤의 값이 조용히 이긴다)."""

    def construct_mapping(self, node: yaml.MappingNode, deep: bool = False) -> dict[Any, Any]:
        seen = set()
        for key_node, _ in node.value:
            key = self.construct_object(key_node, deep=True)
            try:
                duplicate = key in seen
            except TypeError:  # 해시할 수 없는 키는 SafeLoader가 형식 오류로 막는다
                continue
            if duplicate:
                raise _DuplicateKey(key)
            seen.add(key)
        return super().construct_mapping(node, deep)


def _key(name: Any) -> str:
    """메시지에 넣을 키 이름. 이름답지 않은 것(긴 글, 다른 타입)은 넣지 않는다."""
    return name if isinstance(name, str) and _KEY_NAME.fullmatch(name) else "(이름 생략)"


def _problems(exc: ValidationError) -> str:
    parts = []
    for err in exc.errors(include_url=False, include_context=False, include_input=False):
        loc = err["loc"]
        reason = _REASONS.get(err["type"], "값이 틀렸다")
        parts.append(f"{_key(loc[0]) if loc else '(최상위)'}: {reason}")
    return ", ".join(sorted(parts))


def load_runtime(path: str | Path | None = None) -> RuntimeConfig:
    """설정을 읽어 검증한다. 파일이 없으면 OSError, 내용이 틀리면 ValueError.

    ValueError는 원래 예외를 달고 가지 않는다(yaml · pydantic 예외에는 파일 조각 · 입력 값이 있다).
    """
    path = RUNTIME_PATH if path is None else Path(path)
    text = path.read_text(encoding="utf-8")
    problem = None
    cfg = None
    try:
        raw = yaml.load(text, Loader=_UniqueKeyLoader)  # noqa: S506  SafeLoader의 하위 클래스다
        if isinstance(raw, dict):
            cfg = RuntimeConfig.model_validate(raw)
        else:
            problem = "최상위가 매핑이 아니다"
    except _DuplicateKey as exc:
        problem = f"{_key(exc.key)}: 같은 키가 두 번 나온다"
    except yaml.YAMLError:  # 깨진 YAML, 문서가 둘 이상
        problem = "YAML 형식 오류"
    except ValidationError as exc:
        problem = _problems(exc)
    if cfg is None:
        raise ValueError(f"{path.name}: {problem}")
    return cfg
