"""S1.4 공개 세트 로더 (GR-13, master-plan §4.5).

guard.jsonl + tutor.jsonl 400행을 읽는다. LabeledRow.text는 마지막 user 발화의 normalize() 결과다.
scripts/r13/common.py의 read_jsonl·last_user를 옮겼다(import하지 않음).
"""

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from guardrail.config import resolve_path
from guardrail.preprocess import normalize
from guardrail.schema import Category, Message

FILES = ("guard.jsonl", "tutor.jsonl")


@dataclass(frozen=True)
class PublicRow:
    id: str
    endpoint: Literal["guard", "tutor"]
    messages: tuple[Message, ...]
    candidate_answer: str | None
    labels: Mapping[str, Any]  # 원본 그대로


@dataclass(frozen=True)
class LabeledRow:
    id: str
    endpoint: Literal["guard", "tutor"]
    text: str  # 마지막 user 발화의 normalize()
    category: Category
    facts: tuple[str, ...] = ()  # 튜터 행의 gold_facts. 가드 행은 빈 튜플


def read_jsonl(path: Path) -> list[dict]:
    out = []
    for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError as e:
            raise ValueError(f"{path.name}:{n} JSON 형식 오류") from e
    return out


def load_public(public_dir: str | Path) -> list[PublicRow]:
    base = resolve_path(public_dir)
    rows = [
        PublicRow(
            id=d["id"],
            endpoint=d["endpoint"],
            messages=tuple(Message(m["role"], m["content"]) for m in d["messages"]),
            candidate_answer=d.get("candidate_answer"),
            labels=d["labels"],
        )
        for name in FILES
        for d in read_jsonl(base / name)
    ]
    if len({r.id for r in rows}) != len(rows):
        raise ValueError("공개 세트에 중복 id가 있다")
    return rows


def _last_user(row: PublicRow) -> str:
    for m in reversed(row.messages):
        if m.role == "user":
            return m.content
    raise ValueError(f"{row.id}: user 발화가 없다")


def labeled_rows(rows: Iterable[PublicRow]) -> list[LabeledRow]:
    return [
        LabeledRow(
            r.id,
            r.endpoint,
            normalize(_last_user(r)),
            r.labels["category"],
            tuple(r.labels.get("gold_facts") or ()) if r.endpoint == "tutor" else (),
        )
        for r in rows
    ]
