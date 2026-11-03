"""평가 데이터: 형식 무관 로더, 원 요청(씨앗) 단위 분할, 중복 제거 (master-plan §4.6).

- JSONL 한 줄이 Item 하나다. CSV 같은 공개 세트는 매핑 YAML 하나로 같은 Item이 된다.
- 분할은 assign_split() 하나, 중복 제거는 dedupe() 하나만 쓴다(§4.6 규칙과 글자 그대로 같다).
- 주최측 원본은 S3.2가 이 형식의 JSONL로 한 번 변환하고 하네스는 그 파일을 읽는다.
"""

import copy
import csv
import hashlib
import json
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from guardrail.preprocess import normalize
from guardrail.types import Action, Category, CrisisTier

Split = Literal["dev", "holdout", "organizer_test", "ext"]
Kind = Literal[
    "attack", "benign", "benign_risky", "greeting", "crisis", "grounding", "citation",
    "multiturn", "injection",
]  # fmt: skip
Technique = Literal[
    "none", "jamo", "chosung", "fullwidth", "zerowidth", "digit_insert", "yamin", "codeswitch",
    "romaja", "roundtrip", "honorific", "roleplay", "encoding", "indirect",
]  # fmt: skip

_NEEDS_ACTION = {"attack", "benign", "benign_risky", "greeting", "multiturn"}
_GROUNDED = {"grounding", "citation"}
_CHUNK_KINDS = {"grounding", "citation", "injection"}
_SUBTYPES = {"crisis": {"direct", "euphemism", "evasion"}, "injection": {"direct", "indirect"}}
_MAX_REPORTED = 10


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TurnIn(_Strict):
    role: Literal["student", "tutor"]
    content: str = Field(min_length=1)  # 빈 글은 분할 사이 중복 비교에서 가짜로 겹친다


class ChunkIn(_Strict):
    sid: str
    source: str
    text: str


class Expected(_Strict):
    action: Action | None = None
    crisis_tier: CrisisTier | None = None
    violation: bool | None = None
    marker: str | None = None  # 간접·직접 인젝션에 심은 표지어(S2.7 채점)


class Item(_Strict):
    id: str = Field(min_length=1)
    seed_id: str = Field(min_length=1)
    split: Split
    subsplit: Literal["calibration", "report"] | None = None
    kind: Kind
    category: Category | None  # 필수. null은 organizer_test의 대응 없는 라벨만
    technique: Technique | None = None  # 필수. organizer_test에서 없으면 none
    subtype: str | None = None
    turns: list[TurnIn] = Field(min_length=1)
    expected: Expected = Field(default_factory=Expected)  # 검증기가 action을 채운다
    source: str
    license: str
    # 근거 세트 필드. 필수 검사와 labels 형식은 S1.1이 더한다
    chunks: list[ChunkIn] | None = None
    answer: str | None = None
    labels: dict[str, Any] | None = None

    @model_validator(mode="after")
    def _rules(self) -> "Item":
        organizer = self.split == "organizer_test"
        if self.turns[-1].role != "student":
            raise ValueError("turns의 마지막은 student여야 한다")
        if self.category is None and not organizer:
            raise ValueError("category는 필수다(null은 split=organizer_test에서만)")
        if self.technique is None:
            if not organizer:
                raise ValueError("technique는 필수다(organizer_test만 없으면 none)")
            self.technique = "none"
        exp = self.expected
        if exp.crisis_tier is not None:
            if exp.action not in (None, "crisis"):
                raise ValueError("expected.crisis_tier가 있으면 action은 crisis다")
            exp.action = "crisis"
        if self.kind in _SUBTYPES and self.subtype not in _SUBTYPES[self.kind]:
            allowed = sorted(_SUBTYPES[self.kind])
            raise ValueError(f"kind={self.kind}의 subtype은 {allowed} 중 하나다")
        if self.kind == "crisis" and exp.crisis_tier is None:
            raise ValueError("kind=crisis는 expected.crisis_tier가 필수다")
        if self.kind in _NEEDS_ACTION and exp.action is None:
            raise ValueError(f"kind={self.kind}는 expected.action이 필수다")
        if self.kind in _GROUNDED and exp.violation is None:
            raise ValueError(f"kind={self.kind}는 expected.violation이 필수다")
        if self.kind == "injection" and not exp.marker:
            raise ValueError("kind=injection은 expected.marker(빈 문자열 아님)가 필수다")
        if self.chunks is not None and self.kind not in _CHUNK_KINDS:
            raise ValueError(f"chunks는 {sorted(_CHUNK_KINDS)}에서만 쓴다")
        if self.kind == "injection" and self.subtype == "indirect" and not self.chunks:
            raise ValueError("간접 인젝션(subtype=indirect)은 chunks가 필수다")
        return self


class DatasetError(ValueError):
    """줄 번호가 든 오류를 모아 한 번에 낸다(메시지에는 앞 10개)."""

    def __init__(self, errors: list[str]) -> None:
        self.errors = errors
        shown = errors[:_MAX_REPORTED]
        more = f"\n(외 {len(errors) - len(shown)}개)" if len(errors) > len(shown) else ""
        super().__init__("\n".join(shown) + more)


def _describe(e: ValidationError) -> str:
    parts = []
    for err in e.errors():
        loc = ".".join(str(x) for x in err["loc"])
        parts.append(f"{loc}: {err['msg']}" if loc else err["msg"])
    return "; ".join(parts)


Row = tuple[int, dict[str, Any]]


def _jsonl_rows(path: Path, errors: list[str]) -> list[Row]:
    rows = []
    # splitlines()는 JSON 문자열 안의 U+2028 등에서도 자르므로 \n에서만 나누는 파일 반복을 쓴다
    with path.open(encoding="utf-8", newline="\n") as f:
        for lineno, line in enumerate(f, start=1):
            if not line.strip():
                continue
            try:
                rows.append((lineno, json.loads(line)))
            except json.JSONDecodeError as e:
                errors.append(f"{path}:{lineno}: JSON 오류 {e.msg}")
    return rows


def _csv_rows(path: Path, mapping: Path) -> list[Row]:
    try:
        spec = yaml.safe_load(mapping.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as e:
        raise DatasetError([f"{mapping}: 매핑 YAML을 읽지 못했다({type(e).__name__})"]) from None
    columns: dict[str, str] = spec.get("columns", {})
    defaults: dict[str, Any] = spec.get("defaults", {})
    rows = []
    with path.open(encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        for n, record in enumerate(reader, start=1):
            raw = copy.deepcopy(defaults)
            for field, column in columns.items():
                value = record.get(column)
                if field == "text":
                    raw["turns"] = [{"role": "student", "content": value}]
                else:
                    raw[field] = value
            raw.setdefault("id", f"{raw.get('source', 'csv')}-{n:05d}")
            raw.setdefault("seed_id", raw["id"])
            rows.append((reader.line_num, raw))  # 셀 안 줄바꿈이 있어도 맞는 파일 줄 번호
    return rows


def load(path: str | Path, mapping: str | Path | None = None) -> list[Item]:
    """JSONL 또는 CSV(+매핑 YAML)를 Item 목록으로 읽는다. 오류는 줄 번호와 함께 모아 낸다."""
    path = Path(path)
    errors: list[str] = []
    try:
        if path.suffix == ".jsonl":
            rows = _jsonl_rows(path, errors)
        elif path.suffix == ".csv":
            if mapping is None:
                raise DatasetError([f"{path}: CSV에는 매핑 YAML이 필요하다"])
            rows = _csv_rows(path, Path(mapping))
        else:
            raise DatasetError([f"{path}: 모르는 형식 {path.suffix!r}(jsonl·csv만)"])
    except (OSError, UnicodeDecodeError, csv.Error) as e:
        raise DatasetError([f"{path}: 읽지 못했다({type(e).__name__})"]) from None
    items: list[Item] = []
    seen: dict[str, int] = {}
    for lineno, raw in rows:
        try:
            item = Item.model_validate(raw)
        except ValidationError as e:
            errors.append(f"{path}:{lineno}: {_describe(e)}")
            continue
        if item.id in seen:
            errors.append(f"{path}:{lineno}: id {item.id} 중복({seen[item.id]}번 줄)")
            continue
        seen[item.id] = lineno
        items.append(item)
    if errors:
        raise DatasetError(errors)
    return items


# --- 분할 ---


def _bucket(salt: str, seed_id: str) -> int:
    """씨앗만의 함수인 0–99 값. 근거 세트 subsplit(salt "cheshire-subsplit:")도 이것을 쓴다."""
    return int(hashlib.sha256((salt + seed_id).encode("utf-8")).hexdigest()[:8], 16) % 100


def assign_split(seed_id: str) -> Literal["dev", "holdout"]:
    """자체 제작 세트 전용. 순서와 무관하고, 항목을 더해도 기존 씨앗이 옮겨 가지 않는다."""
    return "holdout" if _bucket("cheshire-split:", seed_id) < 30 else "dev"


# --- 중복 제거 ---


def text(item: Item) -> str:
    """학생 턴마다 normalize한 것을 줄바꿈으로 이은 것."""
    return "\n".join(normalize(t.content) for t in item.turns if t.role == "student")


def _key(item: Item) -> tuple[str, ...]:
    key = (item.split, item.seed_id, item.technique or "none", text(item))
    if item.kind in _GROUNDED:
        # 한 질문에 정상·주입·인용 오류 답이 같이 있으므로 답 정규화본을 더한다
        key += (normalize(item.answer or ""),)
    return key


def dedupe(
    items: list[Item], fixed_dev: frozenset[str] = frozenset()
) -> tuple[list[Item], list[tuple[str, str]]]:
    """(남긴 것, [(지운 id, 남긴 id)]).

    같은 분할 안: 키(seed_id, technique, 정규화본[, answer])가 같으면 먼저 나온 하나만 남긴다.
    분할 사이(dev·holdout): 씨앗이나 정규화본 중 하나만 같아도 dev 쪽을 지운다. 예외로 겹친 dev
    항목이 dev 고정(fixed_dev, 이미 본 항목)이면 홀드아웃 쪽을 먼저 지운다.
    """
    removed: list[tuple[str, str]] = []
    first: dict[tuple[str, ...], int] = {}  # 키 → kept 안 위치
    kept: list[Item] = []
    for it in items:
        k = _key(it)
        if k not in first:
            first[k] = len(kept)
            kept.append(it)
            continue
        old = kept[first[k]]
        if it.id in fixed_dev and old.id not in fixed_dev:
            # dev 고정 항목을 대표로 남긴다. 아니면 고정 항목이 지워져 홀드아웃 쪽 겹침을 놓친다
            kept[first[k]] = it
            removed.append((old.id, it.id))
        else:
            removed.append((it.id, old.id))

    drop: set[str] = set()
    fixed = [i for i in kept if i.split == "dev" and i.id in fixed_dev]
    fixed_seed = {i.seed_id: i.id for i in fixed}
    fixed_text = {text(i): i.id for i in fixed}
    holdout = []
    for h in (i for i in kept if i.split == "holdout"):
        hit = fixed_seed.get(h.seed_id) or fixed_text.get(text(h))
        if hit:
            drop.add(h.id)
            removed.append((h.id, hit))
        else:
            holdout.append(h)

    hold_seed = {h.seed_id: h.id for h in holdout}
    hold_text = {text(h): h.id for h in holdout}
    for d in kept:
        if d.split != "dev" or d.id in fixed_dev:
            continue
        hit = hold_seed.get(d.seed_id) or hold_text.get(text(d))
        if hit:
            drop.add(d.id)
            removed.append((d.id, hit))
    return [i for i in kept if i.id not in drop], removed
