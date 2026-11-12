"""범주 → action 표 로더 (통합 PRD IN-03).

원본은 `config/actions.yaml` 하나다. 이 모듈은 import 시점에 표를 읽어 검증하므로, 범주가 빠졌거나
값이 틀리면 서버가 뜨지 않는다. 표는 설정이지 요청 사이 상태가 아니다(읽은 뒤 바뀌지 않는다).
"""

from pathlib import Path
from types import MappingProxyType
from typing import Any, Literal, get_args

import yaml
from pydantic import BaseModel, ConfigDict, ValidationError

from integration.contracts import ACTIONS, Action, Category

__all__ = [
    "ACTIONS_PATH",
    "DEFAULT_FALLBACK_ACTION",
    "FALLBACK_ACTION",
    "RUNTIME_PATH",
    "ActionRule",
    "ActionVariant",
    "action_for",
    "load_actions",
    "load_fallback_action",
]

CONFIG_DIR = Path(__file__).resolve().parents[1] / "config"
ACTIONS_PATH = CONFIG_DIR / "actions.yaml"
RUNTIME_PATH = CONFIG_DIR / "runtime.yaml"  # I1.4에서 생긴다. 없으면 기본값을 쓴다

# 생성 실패 · 모르는 범주의 기본 action. 주인은 튜터 TU-12(잠정 clarify, 11/18 측정으로 확정).
DEFAULT_FALLBACK_ACTION: Action = "clarify"

_CATEGORIES: tuple[str, ...] = get_args(Category)
_FROZEN = ConfigDict(frozen=True, extra="forbid")


class ActionVariant(BaseModel):
    """같은 범주 안에서 갈리는 다른 action(harmful_content의 redirect)."""

    model_config = _FROZEN

    action: Action
    when: str = ""


class ActionRule(BaseModel):
    """표의 한 줄. `category`가 None이면 모르는 범주에 쓴 기본값이다.

    고정 문장과 그 키는 튜터 소유라 여기 없다. 튜터가 (category, action)으로 고른다.
    """

    model_config = _FROZEN

    category: Category | None
    action: Action
    generate: Literal["llm", "template"]
    cite: Literal["always", "if_used", "never"]
    variants: tuple[ActionVariant, ...] = ()

    @property
    def actions(self) -> tuple[Action, ...]:
        """이 범주에서 허용되는 action 전부(기본값이 맨 앞)."""
        return (self.action, *(v.action for v in self.variants))


def _read_mapping(path: Path) -> dict[str, Any]:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"{path.name}: 최상위가 매핑이 아니다")
    return raw


def load_actions(path: Path | None = None) -> dict[str, ActionRule]:
    """표를 읽어 검증한다. 범주는 가드 `Category` 10개와 정확히 같아야 한다."""
    path = ACTIONS_PATH if path is None else path
    rules = _read_mapping(path).get("rules")
    if not isinstance(rules, dict):
        raise ValueError(f"{path.name}: rules 매핑이 없다")
    missing = [c for c in _CATEGORIES if c not in rules]
    unknown = [str(c) for c in rules if c not in _CATEGORIES]
    if missing or unknown:
        raise ValueError(f"{path.name}: 빠진 범주 {missing}, 모르는 범주 {unknown}")
    table: dict[str, ActionRule] = {}
    for category in _CATEGORIES:
        fields = rules[category]
        if not isinstance(fields, dict):
            raise ValueError(f"{path.name}: {category} 값이 매핑이 아니다")
        try:
            table[category] = ActionRule(category=category, **fields)
        except ValidationError as exc:
            raise ValueError(f"{path.name}: {category} 규칙이 틀렸다\n{exc}") from exc
    return table


def load_fallback_action(path: Path | None = None) -> Action:
    """`config/runtime.yaml`의 `fallback_action`. 파일이나 키가 없으면 기본값."""
    path = RUNTIME_PATH if path is None else path
    if not path.is_file():
        return DEFAULT_FALLBACK_ACTION
    value = _read_mapping(path).get("fallback_action", DEFAULT_FALLBACK_ACTION)
    if value not in ACTIONS:
        raise ValueError(f"{path.name}: fallback_action이 action 7종이 아니다")
    return value


_TABLE = MappingProxyType(load_actions())
FALLBACK_ACTION: Action = load_fallback_action()
_FALLBACK_RULE = ActionRule(
    category=None, action=FALLBACK_ACTION, generate="template", cite="never"
)


def action_for(category: object) -> ActionRule:
    """범주의 규칙. 모르는 범주(가드가 새 값을 냈거나 판정 실패)면 `fallback_action` 규칙."""
    if isinstance(category, str):
        return _TABLE.get(category, _FALLBACK_RULE)
    return _FALLBACK_RULE
