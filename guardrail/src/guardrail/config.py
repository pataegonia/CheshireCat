"""GR-11 설정: YAML 로드·검증, ablation 깊은 병합, GUARD_* 환경변수, overrides, config_hash.

계약은 master-plan §4.4.

- 모든 모델이 extra="forbid"이고 필드 기본값이 없다. 오타·누락은 로드에서 잡힌다.
- 키·주소는 설정에 두지 않는다(환경변수로만). 오류 메시지에 환경변수 값을 넣지 않는다.
"""

import copy
import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, PositiveFloat, PositiveInt, model_validator

from guardrail.schema import Category

ROOT = Path(__file__).resolve().parents[2]  # guardrail/
DEFAULT_CONFIG = "configs/default.yaml"
PATH_ENV = {"kb": "GUARD_KB_DIR", "public": "GUARD_PUBLIC_DIR", "prompts": "GUARD_PROMPTS_DIR"}


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Paths(_Strict):
    kb: str
    public: str
    prompts: str


class Timeouts(_Strict):
    connect: float
    read: float
    write: float
    pool: float


class Llm(_Strict):
    # None = 파라미터를 보내지 않음(probe()가 거절된 것만 끈다, S1.3)
    reasoning_effort: str | None
    verbosity: str | None
    max_completion_tokens: int | None
    structured_output: Literal["json_schema", "json_object"]
    timeout_s: Timeouts
    retries: int
    retry_jitter_s: tuple[float, float]
    gate: PositiveInt  # 0이면 세마포어가 영영 안 열린다


class Toggles(_Strict):
    knn_fewshot: bool
    knn_vote: bool
    lr: bool
    third_call: bool
    low_signal_direct: bool
    literal_rule: bool
    nano_tier3: bool
    nano_span: bool
    safe_rule: bool
    oom_kb_snippet: bool
    embedding: bool


class Vectorizer(_Strict):
    analyzer: Literal["char", "char_wb", "word"]
    ngram: tuple[PositiveInt, PositiveInt]

    @model_validator(mode="after")
    def _ngram_order(self) -> "Vectorizer":
        if self.ngram[0] > self.ngram[1]:
            raise ValueError("ngram은 [작은 값, 큰 값]이어야 한다")
        return self


class KnnCfg(Vectorizer):
    max_examples: PositiveInt
    per_category: PositiveInt


class LrCfg(Vectorizer):
    C: PositiveFloat


class InputCfg(_Strict):
    window_last: int
    max_chars: int
    max_answer_chars: int
    knn: KnnCfg
    knn_vote_k: PositiveInt
    lr: LrCfg
    oom_snippet_chars: int


class KbCfg(_Strict):
    top_k: int
    ngram: int
    k1: float
    b: float


class RetrievalCfg(_Strict):
    max_query_chars: int


class OutputCfg(_Strict):
    mode: Literal["v6", "baseline"]
    failure_default: Literal["grounded", "ungrounded"]
    call_nano_when_unused: bool
    rule_min_margin: float
    rule_emit_spans: bool
    rule_span: Literal["pm4", "eojeol"]
    rule_extra_tokens: list[Literal["value_norm", "units_extra", "bare_numeral", "backtick_latin"]]
    rule_require_same_kind: bool


class EmbeddingCfg(_Strict):
    targets: list[Literal["kb", "knn"]]
    weight: float


class EvalCfg(_Strict):
    max_calls: int


class GuardConfig(_Strict):
    version: str
    paths: Paths
    prompts: dict[str, str]  # 이름 → paths.prompts 아래 파일(id·version 머리말 필수)
    budget_s: PositiveFloat
    block_categories: list[Category]
    llm: Llm
    toggles: Toggles
    input: InputCfg
    kb: KbCfg
    retrieval: RetrievalCfg
    output: OutputCfg
    embedding: EmbeddingCfg
    eval: EvalCfg

    @model_validator(mode="after")
    def _toggle_combinations(self) -> "GuardConfig":
        t = self.toggles
        if t.third_call and not (t.knn_vote and t.lr):
            raise ValueError("toggles.third_call은 knn_vote·lr이 둘 다 켜졌을 때만 켤 수 있다")
        if t.nano_span and not t.literal_rule:
            raise ValueError("toggles.nano_span은 literal_rule이 켜졌을 때만 켤 수 있다")
        return self


# --- 로드 ---


def _read_yaml(path: str | Path) -> dict:
    raw = yaml.safe_load(resolve_path(path).read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ValueError(f"설정 파일 최상위는 매핑이어야 한다: {path}")
    return raw


def _deep_merge(base: dict, over: dict) -> dict:
    """over의 키만 base 위에 덮어쓴 새 매핑. 양쪽이 매핑이면 안으로 들어간다."""
    merged = copy.deepcopy(base)
    for key, value in over.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = copy.deepcopy(value)
    return merged


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


def load_config(
    path: str | Path | None = None,
    overrides: dict[str, Any] | None = None,
    ablation: str | Path | None = None,
) -> GuardConfig:
    """설정을 읽어 검증한다. 순서: 파일 → ablation 깊은 병합 → GUARD_*_DIR → overrides → 검증.

    path가 없으면 GUARD_CONFIG(없으면 configs/default.yaml). 덮어쓴 값은 모두 검증 전에 들어가므로
    모르는 키는 extra="forbid"로 실패하고, 덮어쓴 값이 config_hash에 들어간다.
    """
    raw = _read_yaml(path if path is not None else os.environ.get("GUARD_CONFIG", DEFAULT_CONFIG))
    if ablation is not None:
        raw = _deep_merge(raw, _read_yaml(ablation))
    for field, env in PATH_ENV.items():
        value = os.environ.get(env)
        if value:
            _apply_override(raw, f"paths.{field}", value)
    for dotted, value in (overrides or {}).items():
        _apply_override(raw, dotted, value)
    return GuardConfig.model_validate(raw)


# --- 프롬프트 ---

_FRONT = re.compile(r"\A---\n(?P<head>.*?)\n---\n", re.S)


def load_prompt(cfg: GuardConfig, name: str) -> tuple[str, str]:
    """설정 prompts[name] 파일의 머리말(--- / id / version / ---)을 읽어 (version, 본문)을 낸다."""
    if name not in cfg.prompts:
        raise ValueError(f"프롬프트 {name}: 설정 prompts 매핑에 없다")
    path = resolve_path(Path(cfg.paths.prompts) / cfg.prompts[name])
    text = path.read_text(encoding="utf-8")
    m = _FRONT.match(text)
    if not m:
        raise ValueError(f"프롬프트 {name}: 버전 머리말(---/id/version/---)이 없다")
    head = yaml.safe_load(m.group("head"))
    if not isinstance(head, dict) or head.get("id") != name or not head.get("version"):
        raise ValueError(f"프롬프트 {name}: 머리말 id·version이 맞지 않다")
    return str(head["version"]), text[m.end() :]


def config_hash(cfg: GuardConfig) -> str:
    """설정 값 + prompts 각 파일의 머리말 version. 프롬프트 본문은 넣지 않는다(version을 올린다)."""
    payload = {
        "config": cfg.model_dump(mode="json"),
        "prompts": {name: load_prompt(cfg, name)[0] for name in sorted(cfg.prompts)},
    }
    dumped = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(dumped.encode("utf-8")).hexdigest()[:12]
