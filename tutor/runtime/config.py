"""튜터 설정(TU-14). 루트 config/tutor.yaml을 읽어 검증한다. 형식은 가드 GR-11과 같다.

- 모든 모델이 extra="forbid"·strict이고 필드 기본값이 없다. 오타·누락·느슨한 값(true → 1,
  "5" → 5)은 로드에서 잡힌다.
- 순서: 파일(TUTOR_CONFIG, 없으면 config/tutor.yaml) → ablation 깊은 병합 → overrides → 검증.
- 설정은 기동 때만 읽고(tutor.startup) 바꾸지 않는다(frozen). 요청 말고는 동작을 바꾸는 입력이 없다.
- 키·주소는 설정에 두지 않는다(환경변수로만). 요청 예산은 가드 설정
  budget_s에 이 설정의 budget_scale을 곱한 값이다.
"""

import copy
import os
from pathlib import Path
from typing import Annotated, Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, PositiveFloat, PositiveInt, model_validator

ROOT = Path(__file__).resolve().parents[2]  # 저장소 루트
DEFAULT_CONFIG = "config/tutor.yaml"
CONFIG_ENV = "TUTOR_CONFIG"
# 운영진 프록시는 스트리밍이 아닌 응답의 출력 상한을 2000토큰으로 묶는다(넘기면 400, 11/12 런박스)
PROXY_MAX_COMPLETION_TOKENS = 2000
# 재시도는 주 호출만 1회(TU-13). T·classify를 함께 켜면 0(tutor.judge.flow._complete)
MAIN_RETRIES = 1
# 요청당 nano 시도 상한의 위 끝(11/19 사용자 허용: 4회 초과 가능, 대회 규정은 30초·동시 64만).
# 후보 단독 실험의 관리 상한이라 대회 규정이 아니다. 올리려면 근거와 예상 부하를 먼저 문서화한다
REQUEST_CALLS_CEILING = 8


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
    # 영어 발화(한글 없이 라틴만)에는 임베딩 순위만으로 검색하고(BM25와 섞지 않음), 자료 밖 확인 E를
    # oom_gate 모드와 상관없이(off 제외) 임베딩 근거 묶음으로 부르며, 규칙 A 재검색에도 임베딩을
    # 쓴다. toggles.embedding이 꺼져 있어도 이 토글이 켜지면 기동 때 KB 임베딩을 만든다.
    # 측정 전이라 끔
    english_embedding: bool
    rerank: bool
    vote: bool
    scaffold_state_rule: bool
    # 대행 신호가 잡히면 answer·scaffold를 academic 고정 문장으로(숨김에서도, 주 호출 실패
    # 기본값 포함, TU-06). 기본 끔, 켜기 전 조건은 PRD TU-06
    delegation_override: bool
    # 주 호출 실패이고 신호가 없으면 clarify 대신 검색 1위 섹션의 자료 인용 답(TU-12). 기본 끔
    fallback_quote: bool
    # 최종 answer에 첫 인용 섹션에서 질문과 가장 겹치는 자료 문장(최대 2개)을 덧붙인다(TU-07).
    # LLM 0회. 기본 끔
    quote_append: bool
    # quote_append가 붙일 자료 문장을 첫 인용 대신 cite_top1이 뒤에 더한 1위 섹션에서
    # 고른다. 인용이 max_citations보다 많을 때만(그때 마지막 인용이 더한 1위다). LLM 0회.
    # max_citations 2에서는 모델이 하나만 인용하면 1위를 더해도 켜지지 않는다(놓치기만 하고 다른
    # 섹션을 고르지는 않는다). 동결 설정에서 켬(11/18)
    quote_from_top1: bool
    inline_cite: bool
    evidence_check: bool  # 주 호출이 범주 전에 근거 대조 칸을 쓴다(TU-06, #162)
    max_citations: Annotated[int, Field(ge=1, le=2)]  # Literal[1, 2]는 true·1.0도 받는다
    cite_top1: bool  # 인용에 검색 1위 섹션을 더한다. 켜면 max_citations + 1개까지(TU-07)
    # cite_top1이 더할 1위를 튜터 발화까지 넣은 질의(규칙 A)로 다시 검색해 정한다. 주 호출 근거
    # 묶음은 그대로. LLM 0회, 검색 1회 추가. cite_top1이 꺼져 있으면 아무것도 하지 않는다.
    # 동결 설정에서 켬(11/18)
    top1_dialog_query: bool
    # T 의도 판정(#209): nano 1회, 이름표로 action을 덮는다. parallel은 주 호출과 동시에,
    # serial은 T 먼저(refuse·위기 고정 문장 이름표면 주 호출 없이). 기본 끔(TU-06)
    intent_override: Literal["off", "parallel", "serial"]
    # 자료 밖 확인 E(#213 B2): 주 호출과 동시에 nano 1회, 최종 answer인데 E가 "근거에 없음"이면
    # 자료 밖 고정 문장. all은 모든 요청(영어 발화 빼고), named_term은 판단 대상에 KB에 없는 이름
    # 붙은 말(tutor.judge.unknown_terms)이 있을 때만. 기본 끔
    oom_gate: Literal["off", "all", "named_term"]
    # E를 언제 보내나(oom_gate가 켜졌을 때만 뜻이 있다). with_request는 요청 시작 때(T·주 호출과
    # 동시에). after_intent는 직렬 T가 주 호출 없이 끝낼 요청에는 보내지 않고, 주 호출 직전에
    # 보낸다. E 마감은 요청 시작부터 llm.timeout_s.oom_gate로 같다(호출 순서만 바꾼다).
    # after_intent_fresh는 같은 순서에 E 마감만 E 시작부터 센다(요청 마감보다 늦지 않게)
    oom_schedule: Literal["with_request", "after_intent", "after_intent_fresh"]
    # 한 요청 안에서 같은 질의 문자열의 임베딩(E·주 호출·규칙 A)을 한 번만 부르고 결과를 나눠 쓴다.
    # 요청 사이에는 나누지 않는다(요청이 끝나면 버린다). 실패한 결과는 나누지 않는다. 기본 끔
    share_query_embedding: bool

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
        if self.english_embedding and self.oom_gate == "off":
            # E 없이 영어에 임베딩만 켜면 영어 자료 밖 질문이 그대로 answer가 된다(11/18 측정)
            raise ValueError("toggles.english_embedding은 oom_gate가 off가 아닐 때만 켠다")
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
    main_hedge: PositiveFloat  # 주 호출을 겹쳐 보내기 시작하는 시간(main 이상이면 끔)
    intent_wait: PositiveFloat  # 직렬 T가 주 호출을 붙잡는 최대 시간(intent 이상이면 T 제한까지)
    classify: PositiveFloat
    verify: PositiveFloat
    embedding: PositiveFloat
    intent: PositiveFloat  # T 의도 판정 호출(toggles.intent_override)
    oom_gate: PositiveFloat  # 자료 밖 확인 E(toggles.oom_gate). E 시작(검색 직후)부터 센다


class Llm(_Strict):
    reasoning_effort: Literal["low", "medium"]  # minimal 금지(TU-13)
    # 튜터 주 호출 상한(가드 판정 호출과 따로). 프록시 상한을 넘으면 로드에서 실패한다
    max_completion_tokens: Annotated[int, Field(ge=1, le=PROXY_MAX_COMPLETION_TOKENS)]
    timeout_s: Timeouts
    # 요청당 nano 시도 상한(재시도·겹쳐 보내기·T·E·가드 단계 모두). 설정이 낼 수 있는 최악
    # (`request_calls`)이 이 값을 넘으면 로드에서 거부하고, 서버는 튜터 LLM 시도를 보내기 직전에
    # 요청별로 예약해 넘는 시도를 보내지 않는다(tutor.runtime.llm.RequestQuota). 임베딩은 따로 센다
    request_max_calls: Annotated[int, Field(ge=1, le=REQUEST_CALLS_CEILING)]


class EvalCfg(_Strict):
    max_calls: PositiveInt


class TutorConfig(_Strict):
    version: str
    mode: Literal["v2", "baseline"]
    # 튜터 요청 예산 = 가드 설정 budget_s × 이 값(TU-13). 가드 값은 로드 때 모르므로
    # 상한 26초 자르기는 tutor.runtime.budget이 한다
    budget_scale: Annotated[float, Field(ge=1.0, le=1.5)]
    toggles: Toggles
    fewshot: Fewshot
    retrieval: Retrieval
    answer: Answer
    llm: Llm
    eval: EvalCfg

    @model_validator(mode="after")
    def _calls_fit(self) -> "TutorConfig":
        normal, worst = request_calls(self)
        if worst > self.llm.request_max_calls:
            raise ValueError(
                f"요청당 nano 최악 {worst}회(정상 {normal}회)가 llm.request_max_calls"
                f" {self.llm.request_max_calls}회를 넘는다"
            )
        return self


def request_calls(cfg: "TutorConfig") -> tuple[int, int]:
    """(정상 경로, 최악) 요청당 nano 시도 수. 설정·검증·측정 예상 호출이 모두 이 값을 쓴다.

    정상 = 주 호출 1 + T + classify 1 + judge_output + E(켠 것만, 재시도·겹쳐 보내기 없이).
    최악 = 주 호출 1 + 재시도(MAIN_RETRIES, T와 classify를 함께 켜면 0) + 겹쳐 보낸 주 호출 1
    (main_hedge < main일 때, 재시도 없이) + T + classify 2(1차 탈옥이면 2차) + judge_output + E.
    겹쳐 보낸 주 호출이 나가도 처음 주 호출은 재시도할 수 있어 둘을 함께 센다. 베이스라인은 1·2.
    E는 named_term이면 일부 요청에만 나가지만 상한 쪽으로 센다. 임베딩은 세지 않는다.
    """
    if cfg.mode == "baseline":
        return 1, 1 + MAIN_RETRIES
    t = cfg.toggles
    t_on = t.intent_override != "off" and t.guard_classify != "serial"
    classify = t.guard_classify != "off"
    extra = int(t_on) + int(t.verify == "guard") + int(t.oom_gate != "off")
    retries = 0 if (t_on and classify) else MAIN_RETRIES
    hedge = int(cfg.llm.timeout_s.main_hedge < cfg.llm.timeout_s.main)
    return 1 + int(classify) + extra, 1 + retries + hedge + 2 * int(classify) + extra


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
