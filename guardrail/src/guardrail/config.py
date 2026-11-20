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
from pydantic import (
    BaseModel,
    ConfigDict,
    NonNegativeInt,
    PositiveFloat,
    PositiveInt,
    model_validator,
)

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


class RoleParams(_Strict):
    # S3.9 실험: 호출 하나(입력·출력·탈옥 2차)만 다른 값으로. None = 공통 값(llm.*)을 따른다
    # 기본 설정은 비워 둔다(`per_role: {}`). 쓰는 곳이 없는 실험 손잡이다
    reasoning_effort: str | None = None
    verbosity: str | None = None
    max_completion_tokens: int | None = None
    read_s: PositiveFloat | None = None  # 이 호출만 다른 read 한도(초). None = llm.timeout_s.read


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
    # 공통 값이 null이거나 probe()가 끈 파라미터는 여기 적어도 보내지 않는다
    per_role: dict[
        Literal[
            "input_judge",
            "output_judge",
            "output_judge_medium",
            "jb_check",
            "oom_check",
            "request_check",
        ],
        RoleParams,
    ]


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
    jb_check: bool  # S3.3 탈옥 2차 판정(1차가 jailbreak_injection일 때만)
    # S3.9: `/guard` 응답에서 nano가 normal·answer_seeking이라 한 자기소개 발화를 pii_disclosure로
    pii_intro_rule: bool
    # S3.10: nano가 normal이라 한 발화에 KB를 붙여 한 번 더 묻는다(수업 자료에 없는 값을 묻는가).
    # `/guard` 응답의 범주만 out_of_material로 바꾼다. 탈옥 2차 뒤에는 안 부른다(호출 3회 상한)
    oom_check: bool
    # S3.13 실험(기본 끔). request_check: 1차가 normal·answer_seeking일 때 요청을 나열해 다시 확인
    # (oom_check 칸을 대신 쓴다. offcourse_output만 off_topic으로 바꾼다). block_recheck: 1차가
    # off_topic일 때 다시 확인(learn만 있으면 normal).
    # 둘 다 `/guard` 응답만 바꾸고 입력 쪽 추가 호출은 jb_check·oom_check와 합쳐 최대 1회다
    request_check: bool
    block_recheck: bool


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
    # S3.2 선별용 변형(R14 B4·B5). yaml의 값이 v1 동작이다. 채택하면 master-plan §4.4에 올린다
    order: Literal["nearest_last", "nearest_first"]  # few-shot 제시 순서
    sublinear_tf: bool  # TF-IDF의 tf를 1 + log(tf)로


class LrCfg(Vectorizer):
    C: PositiveFloat


class InputCfg(_Strict):
    # S3.2 선별용 변형(R14 B3). category = nano가 범주를 고름(v1),
    # act = nano는 요청 유형을 고르고 코드가 범주로 대응
    # flags = 범주 앞 예/아니오 두 칸(S3.8 실험 변형, 기본 끔. results/2026-11-16-input-flags.md)
    label_mode: Literal["category", "act", "flags"]
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
    rule_span: Literal["pm4", "eojeol", "clause"]
    rule_extra_tokens: list[Literal["value_norm", "units_extra", "bare_numeral", "backtick_latin"]]
    rule_require_same_kind: bool
    # R14-D2·D4(ADR-0014 R14 수정). nano "근거 없음" 가운데 ③에 쓰는 범위, 인용 확인의 최소 글자 수,
    # 출력 nano에 주는 1위 섹션 전체의 길이 상한
    tier3_trust: Literal["all", "answer_quote", "both_quotes"]
    quote_min_chars: NonNegativeInt
    context_max_chars: PositiveInt
    # S3.10: 구간에서 섹션 인용·목록 기호를 떼고, 숫자가 든 구간 끝의 서술격 조사를 뗀다
    span_tidy: bool
    # S3.13 실험(기본 끔): quote = 인용 하나, sentences = 문장별 확인(prompts.output_judge_sent).
    # quote_repair = 검증에 실패한 인용을 답변에서 가장 비슷한 구간으로 고쳐 쓴다
    judge_mode: Literal["quote", "sentences"]
    quote_repair: bool
    # 출력 판정을 low와 medium(llm.per_role.output_judge_medium)으로 함께 보내고 medium이 성공하면
    # medium, 아니면 low를 쓴다(`/guard`의 judge만, baseline 모드 제외)
    medium_parallel: bool
    # medium_parallel에서 두 답을 합치는 규칙. medium_first = medium이 성공하면 medium(low는 버림).
    # or_ungrounded = medium이 "근거 있음"이면 low 답을 마감 − MEDIUM_MARGIN_S까지 기다려, low가
    # "근거 없음"이면 low를 쓴다(둘 중 하나라도 근거 없음이면 근거 없음, 둘 다면 medium)
    pair_rule: Literal["medium_first", "or_ungrounded"] = "medium_first"


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
        # rules.py는 요청 때 NotImplementedError를 낸다. 기동 때 막는다(rules를 import하면 순환)
        o = self.output
        if "bare_numeral" in o.rule_extra_tokens:
            raise ValueError("output.rule_extra_tokens의 bare_numeral은 미구현이다")
        if o.rule_require_same_kind:
            raise ValueError("output.rule_require_same_kind: true는 미구현이다")
        # shortcut: 파일 이름으로 판별. 순서를 말하는 프롬프트가 늘면 머리말 필드로
        if self.input.knn.order == "nearest_first" and self.prompts.get("input_judge") in (
            "input_judge.md",
            "input_judge_en.md",
            "input_judge_en_flags.md",
        ):
            raise ValueError(
                "input.knn.order: nearest_first는 input_judge.md·input_judge_en.md·"
                "input_judge_en_flags.md와 쓸 수 없다"
                "(프롬프트가 가장 비슷한 예시가 마지막에 있다고 적는다)"
            )
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
    if not text[m.end() :].strip():  # 빈 system 메시지로 판정이 나가 캐시되지 않게
        raise ValueError(f"프롬프트 {name}: 본문이 비었다")
    return str(head["version"]), text[m.end() :]


def config_hash(cfg: GuardConfig) -> str:
    """설정 값 + prompts 각 파일의 머리말 version. 프롬프트 본문은 넣지 않는다(version을 올린다)."""
    payload = {
        "config": cfg.model_dump(mode="json"),
        "prompts": {name: load_prompt(cfg, name)[0] for name in sorted(cfg.prompts)},
    }
    dumped = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(dumped.encode("utf-8")).hexdigest()[:12]
