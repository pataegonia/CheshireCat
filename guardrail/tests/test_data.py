"""S1.4 데이터: 공개 세트 로더와 학습자 발화 정규화 (master-plan §4.5)."""

import json

import pytest

from guardrail.config import load_config
from guardrail.data import LabeledRow, PublicRow, labeled_rows, load_public, read_jsonl
from guardrail.preprocess import normalize


@pytest.fixture(scope="module")
def rows():
    return load_public(load_config().paths.public)


def test_loads_400_rows_split_by_endpoint(rows):
    assert len(rows) == 400
    assert sum(r.endpoint == "guard" for r in rows) == 200
    assert sum(r.endpoint == "tutor" for r in rows) == 200


def test_candidate_answer_only_on_86_guard_rows(rows):
    with_cand = [r for r in rows if r.candidate_answer is not None]
    assert len(with_cand) == 86
    assert all(r.endpoint == "guard" for r in with_cand)


def test_labels_are_kept_as_is(rows):
    guard = next(r for r in rows if r.endpoint == "guard" and r.candidate_answer)
    assert {"decision", "category", "grounded", "unsupported", "safe"} <= set(guard.labels)
    assert all("category" in r.labels for r in rows)


def test_labeled_rows_use_normalized_last_user_utterance(rows):
    labeled = labeled_rows(rows)
    assert len(labeled) == 400
    for src, row in zip(rows, labeled, strict=True):
        last_user = [m for m in src.messages if m.role == "user"][-1]
        assert row == LabeledRow(
            id=src.id,
            endpoint=src.endpoint,
            text=normalize(last_user.content),
            category=src.labels["category"],
        )


def test_last_user_skips_trailing_non_user_messages(tmp_path):
    item = {
        "id": "x",
        "endpoint": "guard",
        "messages": [
            {"role": "user", "content": "첫 질문"},
            {"role": "assistant", "content": "답"},
        ],
        "labels": {"category": "normal"},
    }
    (tmp_path / "guard.jsonl").write_text(json.dumps(item) + "\n\n", encoding="utf-8")
    (tmp_path / "tutor.jsonl").write_text("", encoding="utf-8")
    (row,) = labeled_rows(load_public(tmp_path))
    assert row.text == "첫 질문"


def test_missing_user_message_is_an_error():
    row = PublicRow(id="x", endpoint="guard", messages=(), candidate_answer=None, labels={})
    with pytest.raises(ValueError, match="x"):
        labeled_rows([row])


def test_read_jsonl_rejects_bad_line(tmp_path):
    p = tmp_path / "a.jsonl"
    p.write_text("{bad\n", encoding="utf-8")
    with pytest.raises(ValueError, match="a.jsonl"):
        read_jsonl(p)


def test_duplicate_ids_are_rejected(tmp_path):
    item = {"id": "x", "endpoint": "guard", "messages": [], "labels": {"category": "normal"}}
    line = json.dumps(item) + "\n"
    (tmp_path / "guard.jsonl").write_text(line, encoding="utf-8")
    (tmp_path / "tutor.jsonl").write_text(line, encoding="utf-8")
    with pytest.raises(ValueError, match="중복"):
        load_public(tmp_path)
