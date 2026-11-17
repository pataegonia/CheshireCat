"""튜터 설정(TU-14). 루트 config/tutor.yaml을 읽어 검증한다. 형식은 가드 GR-11과 같다.

- 모든 모델이 extra="forbid"·strict이고 필드 기본값이 없다. 오타·누락·느슨한 값(true → 1,
  "5" → 5)은 로드에서 잡힌다.
- 순서: 파일(TUTOR_CONFIG, 없으면 config/tutor.yaml) → ablation 깊은 병합 → overrides → 검증.
- 설정은 기동 때만 읽고(tutor.startup) 바꾸지 않는다(frozen). 요청 말고는 동작을 바꾸는 입력이 없다.
- 키·주소는 설정에 두지 않는다(환경변수로만). 공유 키 budget_s는 가드 설정에서 읽는다.
"""

import copy
import os
import re
from pathlib import Path
from typing import Annotated, Any, Literal

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    PositiveFloat,
    PositiveInt,
    field_validator,
    model_validator,
)

ROOT = Path(__file__).resolve().parents[2]  # 저장소 루트
DEFAULT_CONFIG = "config/tutor.yaml"
CONFIG_ENV = "TUTOR_CONFIG"
PROMPT_DIR = ROOT / "tutor" / "prompts"  # main_call.PROMPT_DIR와 같은 폴더(테스트가 맞춰 본다)
# 프롬프트 파일 이름: 폴더 구분자·상위 경로 없이 tutor/prompts/ 바로 아래 .md 파일 하나
PROMPT_NAME = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]*\.md")
# 운영진 프록시는 스트리밍이 아닌 응답의 출력 상한을 2000토큰으로 묶는다(넘기면 400, 11/12 런박스)
PROXY_MAX_COMPLETION_TOKENS = 2000


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class Toggles(_Strict):
    name_dict: bool
    fewshot: Literal["off", "fixed", "knn"]  # 판단 예시(TU-08). off는 따옴표로
    action_from_category: bool
    signals: bool
    verify: Literal["off", "self", "guard"]
    guard_classify: Literal["off", "serial", "parallel"]
    # classify가 jailbreak_injection이어도 지시 무력화 신호(TU-05)가 없으면 덮지 않는다(TU-10)
    jailbreak_needs_signal: bool
    embedding: bool
    rerank: bool
    vote: bool
    scaffold_state_rule: bool
    # 대행 신호가 잡히면 answer·scaffold를 academic 고정 문장으로(숨김에서도, 주 호출 실패
    # 기본값 포함, TU-06). 기본 끔, 켜기 전 조건은 PRD TU-06
    delegation_override: bool
    # 주 호출 실패이고 신호가 없으면 clarify 대신 검색 1위 섹션의 자료 인용 답(TU-12). 기본 끔
    fallback_quote: bool
    inline_cite: bool
    evidence_check: bool  # 주 호출이 범주 전에 근거 대조 칸을 쓴다(TU-06, #162)
    max_citations: Annotated[int, Field(ge=1, le=2)]  # Literal[1, 2]는 true·1.0도 받는다
    cite_top1: bool  # 인용에 검색 1위 섹션을 더한다. 켜면 max_citations + 1개까지(TU-07)
    anchor_unit: bool  # 상위 k에서 앞 대화 단원의 섹션을 앞으로(단원 닻, TU-03). API 호출 0회
    # 근거 확인(#201 B): 주 호출 answer 뒤 nano 1회, found=false면 자료 밖 고정 문장. 기본 끔
    oom_check: Literal["off", "after_answer"]
    # T 의도 판정(#209): nano 1회, 이름표로 action을 덮는다. parallel은 주 호출과 동시에,
    # serial은 T 먼저(고정 문장 이름표면 주 호출 없이). 기본 끔
    intent_override: Literal["off", "parallel", "serial"]

    @model_validator(mode="after")
    def _not_built(self) -> "Toggles":
        # #83 결정(11/14): 둘 다 요청당 nano 절대 4회(TU-13)를 넘어 동결 설정에 쓸 수 없고 범위 축소
        # 순서(PRD §15.4)에서 먼저 빠져 만들지 않았다. 켜도 조용히 끔처럼 돌지 않게 로드에서 막는다
        if self.vote:
            raise ValueError("toggles.vote는 만들지 않았다(#83, TU-13 호출 상한)")
        if self.verify == "self":
            raise ValueError("toggles.verify: self는 만들지 않았다(#83, TU-13 호출 상한)")
        if self.intent_override == "serial" and self.guard_classify != "off":
            # 직렬 T는 classify를 기다리지 않고 고정 문장을 내 classify 우선 규칙을 못 지킨다
            raise ValueError("toggles.intent_override: serial은 guard_classify off에서만 쓴다")
        return self


class Fewshot(_Strict):
    """판단 예시 고르기(TU-08). 가드 GR-05 `input.knn`과 같은 값으로 시작한다."""

    # 문자 n-gram 범위(char_wb) [최소, 최대]. strict라 tuple 대신 길이 2 목록
    ngram: Annotated[list[PositiveInt], Field(min_length=2, max_length=2)]
    max_examples: PositiveInt  # knn 전체 상한(4–8)
    per_category: PositiveInt  # knn 범주당 상한
    with_answer: bool  # 예시에 정답 답변을 붙이는가(기본 끔, ablation)


class Retrieval(_Strict):
    k: PositiveInt


class Answer(_Strict):
    max_sentences: PositiveInt


class Timeouts(_Strict):
    main: PositiveFloat
    classify: PositiveFloat
    verify: PositiveFloat
    embedding: PositiveFloat
    oom_check: PositiveFloat  # 근거 확인 호출(toggles.oom_check)
    intent: PositiveFloat  # T 의도 판정 호출(toggles.intent_override)


class Llm(_Strict):
    reasoning_effort: Literal["low", "medium"]  # minimal 금지(TU-13)
    # 튜터 주 호출 상한(가드 판정 호출과 따로). 프록시 상한을 넘으면 로드에서 실패한다
    max_completion_tokens: Annotated[int, Field(ge=1, le=PROXY_MAX_COMPLETION_TOKENS)]
    timeout_s: Timeouts


class EvalCfg(_Strict):
    max_calls: PositiveInt


def prompt_file_problem(name: str) -> str | None:
    """프롬프트 파일 이름이 틀린 까닭(맞으면 None). 대소문자까지 같은 파일이 PROMPT_DIR에 있다."""
    if not PROMPT_NAME.fullmatch(name) or ".." in name:
        return "tutor/prompts/ 안 .md 파일 이름만 쓴다(경로 없이)"
    if name not in {p.name for p in PROMPT_DIR.iterdir() if p.is_file()}:
        return f"tutor/prompts/{name}이 없다"
    return None


class Prompt(_Strict):
    main_call: str  # 주 호출 프롬프트 파일 이름(tutor/prompts/ 안, 측정 비교용)

    @field_validator("main_call")
    @classmethod
    def _known_file(cls, name: str) -> str:
        problem = prompt_file_problem(name)
        if problem is not None:
            raise ValueError(problem)
        return name


class TutorConfig(_Strict):
    version: str
    mode: Literal["v2", "baseline"]
    prompt: Prompt
    toggles: Toggles
    fewshot: Fewshot
    retrieval: Retrieval
    answer: Answer
    llm: Llm
    eval: EvalCfg


def resolve_path(path: str | Path) -> Path:
    """상대 경로는 현재 위치에 없으면 저장소 루트 기준으로 푼다."""
    p = Path(path)
    if p.is_absolute() or p.exists():
        return p
    return ROOT / p


def _read_yaml(path: str | Path) -> dict[str, Any]:
    raw = yaml.safe_load(resolve_path(path).read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ValueError(f"설정 파일 최상위는 매핑이어야 한다: {path}")
    return raw


def _deep_merge(base: dict[str, Any], over: dict[str, Any]) -> dict[str, Any]:
    """over의 키만 base 위에 덮어쓴 새 매핑(가드 config와 같은 규칙)."""
    merged = copy.deepcopy(base)
    for key, value in over.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = copy.deepcopy(value)
    return merged


def _with_override(raw: dict[str, Any], dotted: str, value: Any) -> dict[str, Any]:
    """점으로 이은 경로의 값만 바꾼 새 매핑."""
    head, _, rest = dotted.partition(".")
    if not rest:
        return {**raw, head: value}
    inner = raw.get(head, {})
    if not isinstance(inner, dict):
        raise ValueError(f"overrides 경로 {dotted!r}: {head}는 값이라 더 들어갈 수 없다")
    return {**raw, head: _with_override(inner, rest, value)}


def load_config(
    path: str | Path | None = None,
    overrides: dict[str, Any] | None = None,
    ablation: str | Path | None = None,
) -> TutorConfig:
    raw = _read_yaml(path if path is not None else (os.environ.get(CONFIG_ENV) or DEFAULT_CONFIG))
    if ablation is not None:
        raw = _deep_merge(raw, _read_yaml(ablation))
    for dotted, value in (overrides or {}).items():
        raw = _with_override(raw, dotted, value)
    return TutorConfig.model_validate(raw)
