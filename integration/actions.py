"""범주 → action 표 로더(통합 PRD IN-03)와 결정적 수정 `finalize`(IN-04).

표의 원본은 `config/actions.yaml` 하나다. 이 모듈은 import 시점에 표를 읽어 검증하므로, 범주가
빠졌거나 값이 틀리면 서버가 뜨지 않는다. 표는 설정이지 요청 사이 상태가 아니다.

`finalize`는 LLM을 부르지 않는 마지막 단계다. 고정 문장 문구는 튜터 소유라 여기 적지 않고
`FinalizeConfig`로 받는다. 요청 사이에 아무것도 저장하지 않는다.
"""

import unicodedata
from collections.abc import Sequence
from pathlib import Path
from types import MappingProxyType
from typing import Any, Literal, get_args

import yaml
from guardrail.schema import OutputVerdict
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from integration.contracts import (
    ACTIONS,
    SECTION_IDS,
    SID_PATTERN,
    Action,
    Category,
    Section,
    Sentence,
    TutorResult,
)

__all__ = [
    "ACTIONS_PATH",
    "DEFAULT_FALLBACK_ACTION",
    "FALLBACK_ACTION",
    "RUNTIME_PATH",
    "ActionRule",
    "ActionVariant",
    "FinalizeConfig",
    "action_for",
    "finalize",
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


# ── 결정적 수정 (IN-04) ────────────────────────────────────────────────────

# competition/grader/scoring.py의 PUNCT · normalize와 같은 값이다. 서버가 채점 폴더에 기대지 않도록
# 옮겨 적었고, 같은지는 테스트가 교재 48개 섹션으로 확인한다.
_PUNCT_TABLE = str.maketrans("", "", ".,!?;:'\"“”‘’`()[]{}<>«»·…~-–—/\\")
# 최종 action이 이 넷이면 범주와 상관없이 인용하지 않는다.
_NO_CITE_ACTIONS: frozenset[str] = frozenset({"clarify", "redirect", "refuse", "escalate"})
_KEPT_CONTROLS = frozenset("\n\t")


class FinalizeConfig(BaseModel):
    """`finalize` 설정. 고정 문장 두 개는 튜터가 넘긴다(기본값 없음)."""

    model_config = _FROZEN

    unverified_notice: str  # 남은 claim이 0일 때 덧붙이는 안내
    refuse_text: str  # safe=false일 때 답변 전체를 대신하는 거절 문장
    max_citations: int = Field(default=1, ge=0)
    max_answer_chars: int = Field(default=4000, ge=1)
    # 정규화한 길이가 이보다 짧은 unsupported 구간은 무시한다(한 글자가 답변 전체를 지우지 않게)
    min_span_chars: int = Field(default=2, ge=1)
    # 정규화한 길이가 이보다 짧은 인용 문구는 검증된 근거로 치지 않는다(한 글자는 어디에나 있다)
    min_quote_chars: int = Field(default=2, ge=1)


def _normalize(text: str) -> str:
    text = unicodedata.normalize("NFKC", str(text)).lower()
    # split()은 str.isspace()와 같은 공백 정의로 자른다. 글자마다 도는 것보다 빠르다.
    return "".join(text.split()).translate(_PUNCT_TABLE)


def _is_real_sid(sid: str) -> bool:
    return SID_PATTERN.fullmatch(sid) is not None and sid in SECTION_IDS


def _verified_sids(sentence: Sentence, retrieved: Sequence[Section], min_chars: int) -> list[str]:
    """인용 문구가 그 섹션 원문(본문 + 표)에 실제로 있는 evidence의 sid.

    정규화한 길이가 `min_chars`보다 짧은 문구는 보지 않는다.
    """
    out: list[str] = []
    for ev in sentence.evidence:
        if not _is_real_sid(ev.sid):
            continue
        section = next((s for s in retrieved if s.id == ev.sid), None)
        quote = _normalize(ev.quote)
        if section is None or len(quote) < min_chars:
            continue
        if quote in _normalize("\n".join([section.text, *section.tables])):
            out.append(ev.sid)
    return out


def _unsupported_indexes(
    sentences: Sequence[Sentence], spans: Sequence[str], min_chars: int
) -> set[int]:
    """unsupported 구간이 걸친 문장 번호. 구간이 문장 경계를 넘으면 걸친 문장 전부.

    정규화한 길이가 `min_chars`보다 짧은 구간은 보지 않는다.
    """
    parts = [_normalize(s.text) for s in sentences]
    joined = "".join(parts)
    bounds: list[tuple[int, int]] = []
    start = 0
    for part in parts:
        bounds.append((start, start + len(part)))
        start += len(part)
    hit: set[int] = set()
    for span in spans:
        needle = _normalize(span)
        if len(needle) < min_chars:
            continue
        at = joined.find(needle)
        while at != -1:
            end = at + len(needle)
            hit.update(i for i, (lo, hi) in enumerate(bounds) if lo < end and at < hi)
            at = joined.find(needle, at + 1)
    return hit


def _rank_citations(
    sid_lists: Sequence[Sequence[str]], retrieved: Sequence[Section], *, fill: bool, limit: int
) -> list[str]:
    """많이 인용된 섹션 먼저, 같으면 검색 순위. `fill`이면 비었을 때 검색 1위로 채운다."""
    rank = {s.id: i for i, s in reversed(list(enumerate(retrieved)))}
    counts: dict[str, int] = {}
    for sids in sid_lists:
        for sid in dict.fromkeys(sids):
            counts[sid] = counts.get(sid, 0) + 1
    ordered = sorted(counts, key=lambda sid: (-counts[sid], rank[sid]))
    if not ordered and fill:
        ordered = [s.id for s in retrieved if _is_real_sid(s.id)][:1]
    return ordered[:limit]


def _clean(text: str) -> str:
    """제어 문자(줄바꿈 · 탭 제외)와 짝 없는 surrogate를 지운다."""
    return "".join(
        ch for ch in text if ch in _KEPT_CONTROLS or unicodedata.category(ch) not in ("Cc", "Cs")
    )


def _join(texts: Sequence[str]) -> str:
    return _clean(" ".join(t for t in texts if t))


def _cite_policy(rule: ActionRule, action: str) -> str:
    """최종 action 기준의 인용 규칙: always | if_used | never."""
    if action in _NO_CITE_ACTIONS:
        return "never"
    if action in rule.actions:
        return rule.cite
    return "if_used"  # 표에 없는 조합: claim이 실제로 쓴 섹션만


def finalize(
    sentences: Sequence[Sentence],
    verdict: OutputVerdict | None,
    retrieved: Sequence[Section],
    rule: ActionRule,
    cfg: FinalizeConfig,
    *,
    action: Action | None = None,
) -> TutorResult:
    """주 호출 문장 목록을 규격 응답으로 만든다(PRD IN-04). 입력을 바꾸지 않는다.

    - `verdict`가 None(판정 생략 · 실패)이면 판정 단계만 건너뛴다.
    - `retrieved`는 검색 순위 순서이고 비어 있을 수 있다.
    - `action`은 주 호출 · 합치기 단계가 고른 값이다(harmful_content의 redirect 등). None이면
      `rule.action`. 표와 다른 값이 와도 막지 않는다(표 강제는 튜터 설정).
    - `safe=false`면 답변을 버리고 `cfg.refuse_text` + refuse를 낸다.
    - 인용은 최종 action으로 정한다: clarify · redirect · refuse · escalate는 `[]`, 표에 있는
      조합이면 `rule.cite`, 그 밖에는 if_used.
    - 길이 상한을 넘으면 뒤 문장부터 통째로 빼고, 인용은 남은 문장으로 계산한다. 첫 문장 하나로도
      넘을 때만 문자 수로 자른다.
    - `cfg.unverified_notice`는 인용 규칙이 always일 때, claim 문장이 판정이나 길이 자르기로
      지워져 남은 claim이 0이거나 남은 문장이 하나도 없을 때만 붙인다. 처음부터 claim이 없던
      답은 그대로 둔다(인용은 검색 1위로 채운다).
    - 빈 답변은 그대로 돌려준다. action별 고정 문장으로 채우는 것은 튜터 후처리(TU-07) 몫이다.
    """
    limit = cfg.max_answer_chars
    if verdict is not None and not verdict.safe:
        return TutorResult(answer=_clean(cfg.refuse_text)[:limit], action="refuse", citations=[])

    final: Action = rule.action if action is None else action
    policy = _cite_policy(rule, final)
    fill = policy == "always"
    dropped: set[int] = set()
    if verdict is not None:
        dropped = _unsupported_indexes(sentences, verdict.unsupported, cfg.min_span_chars)
    kept = [i for i in range(len(sentences)) if i not in dropped]

    had_claim = any(s.kind == "claim" for s in sentences)
    while True:
        texts = [sentences[i].text for i in kept]
        # 안내는 claim이 판정 · 길이 자르기로 지워져 0이 됐거나, 남은 문장이 없을 때만 붙인다.
        # 처음부터 claim이 없던 답은 그대로 둔다.
        lost_all_claims = had_claim and not any(sentences[i].kind == "claim" for i in kept)
        if fill and (lost_all_claims or not kept):
            texts.append(cfg.unverified_notice)
        answer = _join(texts)
        if len(answer) <= limit or len(kept) <= 1:
            break
        kept.pop()

    citations: list[str] = []
    if policy != "never":
        sids = [
            _verified_sids(sentences[i], retrieved, cfg.min_quote_chars)
            for i in kept
            if sentences[i].kind == "claim"
        ]
        citations = _rank_citations(sids, retrieved, fill=fill, limit=cfg.max_citations)
    return TutorResult(answer=answer[:limit], action=final, citations=citations)
