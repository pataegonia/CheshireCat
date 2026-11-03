"""S0.5 데이터: Item 스키마·로더(JSONL·CSV 매핑)·assign_split·dedupe·스모크 파일 검증."""

import json
import re

import pytest
from hypothesis import given
from hypothesis import strategies as st

from guardrail.config import ROOT
from guardrail.eval.datasets import (
    DatasetError,
    Item,
    _bucket,
    assign_split,
    dedupe,
    load,
)
from guardrail.preprocess import normalize

FIXTURES = ROOT / "tests" / "fixtures"
SEED = ROOT / "data" / "seed"
PHONE = re.compile(r"01[016789]-?\d{3,4}-?\d{4}")
RRN = re.compile(r"\d{6}-?[1-4]\d{6}")


def _item(**kw) -> dict:
    base = {
        "id": "x-0001",
        "seed_id": "x-0001",
        "split": "dev",
        "subsplit": None,
        "kind": "benign",
        "category": "S0",
        "technique": "none",
        "subtype": None,
        "turns": [{"role": "student", "content": "승화가 뭐야?"}],
        "expected": {"action": "allow"},
        "source": "self",
        "license": "CC-BY-4.0",
    }
    return base | kw


def _write(tmp_path, rows: list[dict], name="d.jsonl"):
    p = tmp_path / name
    p.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n", encoding="utf-8"
    )
    return p


MINIMAL_BY_KIND = {
    "attack": dict(kind="attack", category="S14", expected={"action": "refuse"}),
    "benign": {},
    "benign_risky": dict(kind="benign_risky"),
    "greeting": dict(kind="greeting"),
    "crisis": dict(kind="crisis", category="S5", subtype="direct", expected={"crisis_tier": "T1"}),
    "grounding": dict(kind="grounding", expected={"violation": False}),
    "citation": dict(kind="citation", expected={"violation": True}),
    "multiturn": dict(
        kind="multiturn",
        turns=[
            {"role": "student", "content": "안녕"},
            {"role": "tutor", "content": "안녕하세요"},
            {"role": "student", "content": "질문"},
        ],
    ),
    "injection": dict(
        kind="injection", category="S14", subtype="direct", expected={"marker": "ZEBRA"}
    ),
}


# --- 스키마 ---


@pytest.mark.parametrize("kind", list(MINIMAL_BY_KIND))
def test_minimal_item_per_kind_passes(kind):
    Item.model_validate(_item(**MINIMAL_BY_KIND[kind]))


def test_indirect_injection_with_chunks_passes():
    Item.model_validate(
        _item(
            kind="injection",
            category="S14",
            subtype="indirect",
            technique="indirect",
            expected={"marker": "ZEBRA"},
            chunks=[{"sid": "S1", "source": "sci-01", "text": "본문 ZEBRA"}],
        )
    )


def test_crisis_action_is_filled():
    it = Item.model_validate(_item(**MINIMAL_BY_KIND["crisis"]))
    assert it.expected.action == "crisis"


def test_grounding_without_grounding_fields_passes_in_s05():
    it = Item.model_validate(_item(kind="grounding", expected={"violation": False}))
    assert it.chunks is None and it.answer is None and it.labels is None


@pytest.mark.parametrize(
    ("override", "needle"),
    [
        (dict(turns=[]), "turns"),
        (dict(turns=[{"role": "tutor", "content": "x"}]), "student"),
        (dict(kind="weird"), "kind"),
        (dict(split="train"), "split"),
        (dict(technique="leet"), "technique"),
        (
            dict(kind="crisis", category="S5", subtype="direct", expected={"action": "crisis"}),
            "crisis_tier",
        ),
        (dict(kind="attack", expected={}), "action"),
        (dict(kind="grounding", expected={}), "violation"),
        (dict(kind="injection", subtype="direct", expected={}), "marker"),
        (dict(kind="injection", subtype="direct", expected={"marker": ""}), "marker"),
        (dict(kind="injection", subtype="indirect", expected={"marker": "Z"}), "chunks"),
        (dict(chunks=[{"sid": "S1", "source": "a", "text": "b"}]), "chunks"),
        (
            dict(kind="crisis", category="S5", subtype="sideways", expected={"crisis_tier": "T1"}),
            "subtype",
        ),
        (dict(expected={"action": "allow", "crisis_tier": "T1"}), "crisis"),
        (dict(category=None), "category"),
        (dict(extra_field=1), "extra_field"),
    ],
)
def test_schema_errors_report_line_number(tmp_path, override, needle):
    rows = [_item(id="ok-1", seed_id="ok-1"), _item(**override)]
    with pytest.raises(DatasetError) as err:
        load(_write(tmp_path, rows))
    msg = str(err.value)
    assert ":2:" in msg and needle in msg


def test_duplicate_id_in_file(tmp_path):
    with pytest.raises(DatasetError, match="x-0001"):
        load(_write(tmp_path, [_item(), _item()]))


def test_bad_json_line(tmp_path):
    p = tmp_path / "bad.jsonl"
    p.write_text(json.dumps(_item()) + "\n{nope\n", encoding="utf-8")
    with pytest.raises(DatasetError, match=":2:"):
        load(p)


def test_error_list_is_capped_at_10(tmp_path):
    rows = [_item(id=f"x-{i}", seed_id=f"x-{i}", kind="weird") for i in range(15)]
    with pytest.raises(DatasetError) as err:
        load(_write(tmp_path, rows))
    assert len(err.value.errors) == 15
    assert str(err.value).count("kind") <= 11


def test_organizer_test_allows_null_category_and_fills_technique():
    raw = _item(split="organizer_test", category=None)
    del raw["technique"]
    it = Item.model_validate(raw)
    assert it.category is None and it.technique == "none"


@pytest.mark.parametrize("split", ["dev", "holdout", "ext"])
def test_null_category_rejected_outside_organizer(tmp_path, split):
    with pytest.raises(DatasetError, match="category"):
        load(_write(tmp_path, [_item(split=split, category=None)]))


def test_missing_technique_rejected_outside_organizer(tmp_path):
    raw = _item()
    del raw["technique"]
    with pytest.raises(DatasetError, match="technique"):
        load(_write(tmp_path, [raw]))


# --- CSV 매핑 ---


def test_csv_mapping_gives_same_items_as_jsonl(tmp_path):
    items = load(FIXTURES / "mini.csv", mapping=FIXTURES / "mini_map.yaml")
    assert [i.id for i in items] == ["mini-00001", "mini-00002"]
    assert items[1].turns[0].content == "밀도, 질량, 부피의 관계는?"
    assert items[0].seed_id == items[0].id
    jsonl = _write(tmp_path, [i.model_dump(mode="json") for i in items])
    assert load(jsonl) == items


def test_csv_without_mapping_is_error():
    with pytest.raises(DatasetError, match="매핑"):
        load(FIXTURES / "mini.csv")


def test_jsonl_line_separator_inside_string_is_not_a_line_break(tmp_path):
    row = _item(turns=[{"role": "student", "content": "줄\u2028나눔\x0c문자"}])
    p = tmp_path / "u.jsonl"
    p.write_text(json.dumps(row, ensure_ascii=False) + "\n", encoding="utf-8")
    assert load(p)[0].turns[0].content == "줄\u2028나눔\x0c문자"


def test_bad_mapping_yaml_is_dataset_error(tmp_path):
    bad = tmp_path / "m.yaml"
    bad.write_text("columns: [unclosed\n", encoding="utf-8")
    with pytest.raises(DatasetError, match="매핑"):
        load(FIXTURES / "mini.csv", mapping=bad)


def test_undecodable_file_is_dataset_error(tmp_path):
    p = tmp_path / "x.jsonl"
    p.write_bytes(b"\xff\xfe\x00")
    with pytest.raises(DatasetError, match="읽지 못했다"):
        load(p)


def test_empty_student_turn_rejected(tmp_path):
    with pytest.raises(DatasetError, match="content"):
        load(_write(tmp_path, [_item(turns=[{"role": "student", "content": ""}])]))


def test_unknown_extension(tmp_path):
    p = tmp_path / "x.txt"
    p.write_text("a", encoding="utf-8")
    with pytest.raises(DatasetError, match="형식"):
        load(p)


# --- assign_split ---


def test_assign_split_fixed_values():
    # 고정 기준값(2026-11-03 계산). salt·비율·해시 자리수가 바뀌면 깨진다
    assert {s: assign_split(s) for s in ("atk-0001", "atk-0002", "ben-0001", "cri-0001")} == {
        "atk-0001": "dev",
        "atk-0002": "dev",
        "ben-0001": "dev",
        "cri-0001": "holdout",
    }
    assert (_bucket("cheshire-split:", "atk-0001"), _bucket("cheshire-split:", "cri-0001")) == (
        35,
        26,
    )


@given(
    st.lists(st.text(min_size=1, max_size=12), min_size=1, max_size=20, unique=True), st.randoms()
)
def test_assign_split_ignores_order_and_other_items(seeds, rnd):
    before = {s: assign_split(s) for s in seeds}
    shuffled = list(seeds) + ["새로 더한 씨앗"]
    rnd.shuffle(shuffled)
    after = {s: assign_split(s) for s in shuffled}
    assert all(after[s] == before[s] for s in seeds)


def test_assign_split_ratio_about_30_percent():
    n = sum(assign_split(f"seed-{i}") == "holdout" for i in range(4000))
    assert 0.27 < n / 4000 < 0.33


# --- dedupe ---


def _it(id_, seed, text, split="dev", technique="none", kind="benign", answer=None):
    extra = {}
    if kind == "grounding":
        extra = dict(kind="grounding", expected={"violation": False}, answer=answer)
    return Item.model_validate(
        _item(id=id_, seed_id=seed, split=split, technique=technique,
              turns=[{"role": "student", "content": text}], **extra)
    )  # fmt: skip


def test_dedupe_same_split_same_key_keeps_first():
    a, b = (
        _it("a", "s1", "승화가 뭐야?"),
        _it("b", "s1", "승화가 뭐야?\u200b"),
    )  # 보이지 않는 문자만 다름
    kept, removed = dedupe([a, b])
    assert kept == [a] and removed == [("b", "a")]


def test_dedupe_keeps_jamo_variant_of_same_seed():
    a = _it("a", "s1", "승화가 뭐야?")
    b = _it("a-jamo", "s1", "ㅅㅡㅇㅎㅘㄱㅏ ㅁㅝㅇㅑ?", technique="jamo")
    kept, removed = dedupe([a, b])
    assert len(kept) == 2 and removed == []


def test_dedupe_grounding_key_includes_answer():
    a = _it("g1", "s1", "승화가 뭐야?", kind="grounding", answer="답 하나 [S1]")
    b = _it("g2", "s1", "승화가 뭐야?", kind="grounding", answer="다른 답 [S1]")
    kept, removed = dedupe([a, b])
    assert len(kept) == 2 and removed == []


def test_dedupe_cross_split_same_seed_different_text_removes_dev():
    # ⓐ 번역본이 다르지만 씨앗이 같은 경우
    dev = _it("d", "s1", "밀도가 뭐야?")
    hold = _it("h", "s1", "밀도의 뜻을 알려 줘", split="holdout")
    kept, removed = dedupe([dev, hold])
    assert kept == [hold] and removed == [("d", "h")]


def test_dedupe_cross_split_same_text_different_seed_removes_dev():
    # ⓑ 씨앗은 다르고 정규화본이 같은 경우
    dev = _it("d", "s1", "밀도가 뭐야?")
    hold = _it("h", "s2", "밀도가 뭐야?", split="holdout")
    kept, removed = dedupe([hold, dev])
    assert kept == [hold] and removed == [("d", "h")]


def test_dedupe_fixed_dev_removes_holdout_instead():
    # ⓒ 겹친 dev 항목이 dev 고정이면 홀드아웃 쪽을 지운다
    dev = _it("smk-1", "s1", "밀도가 뭐야?")
    hold = _it("h", "s1", "다른 글", split="holdout")
    kept, removed = dedupe([dev, hold], fixed_dev=frozenset({"smk-1"}))
    assert kept == [dev] and removed == [("h", "smk-1")]


def test_dedupe_holdout_overlapping_fixed_and_plain_dev():
    # ⓓ 홀드아웃 하나가 dev 고정과 일반 dev에 함께 겹치면 홀드아웃만 지워지고 dev 둘은 남는다
    fixed = _it("smk-1", "s1", "밀도가 뭐야?")
    plain = _it("d2", "s9", "질량이 뭐야?")
    hold = _it("h", "s1", "질량이 뭐야?", split="holdout")
    kept, removed = dedupe([fixed, plain, hold], fixed_dev=frozenset({"smk-1"}))
    assert {i.id for i in kept} == {"smk-1", "d2"}
    assert removed == [("h", "smk-1")]


def test_dedupe_fixed_dev_after_plain_duplicate_still_protects_holdout():
    # 같은 키의 일반 dev가 먼저 나와도 dev 고정 항목이 대표로 남아 홀드아웃 쪽이 지워진다
    plain = _it("d", "s1", "밀도가 뭐야?")
    fixed = _it("smk-1", "s1", "밀도가 뭐야?")
    hold = _it("h", "s1", "다른 글", split="holdout")
    kept, removed = dedupe([plain, fixed, hold], fixed_dev=frozenset({"smk-1"}))
    assert [i.id for i in kept] == ["smk-1"]
    assert removed == [("d", "smk-1"), ("h", "smk-1")]


def test_dedupe_fixed_dev_by_text_only():
    fixed = _it("smk-1", "s1", "밀도가 뭐야?")
    hold = _it("h", "s7", "밀도가 뭐야?", split="holdout")
    kept, removed = dedupe([hold, fixed], fixed_dev=frozenset({"smk-1"}))
    assert kept == [fixed] and removed == [("h", "smk-1")]


def test_dedupe_two_holdouts_one_fixed_one_plain():
    fixed = _it("smk-1", "s1", "밀도가 뭐야?")
    plain = _it("d2", "s2", "질량이 뭐야?")
    h1 = _it("h1", "s1", "x", split="holdout")
    h2 = _it("h2", "s2", "y", split="holdout")
    kept, removed = dedupe([fixed, plain, h1, h2], fixed_dev=frozenset({"smk-1"}))
    assert {i.id for i in kept} == {"smk-1", "h2"}
    assert removed == [("h1", "smk-1"), ("d2", "h2")]


def test_dedupe_cross_split_ignores_answer_for_grounding():
    dev = _it("g1", "s1", "승화가 뭐야?", kind="grounding", answer="답 A")
    hold = _it("g2", "s9", "승화가 뭐야?", split="holdout", kind="grounding", answer="답 B")
    kept, removed = dedupe([dev, hold])
    assert kept == [hold] and removed == [("g1", "g2")]


def test_dedupe_other_splits_untouched():
    a = _it("a", "s1", "밀도가 뭐야?", split="organizer_test")
    b = _it("b", "s1", "밀도가 뭐야?", split="holdout")
    kept, removed = dedupe([a, b])
    assert len(kept) == 2 and removed == []


# --- 스모크 파일 ---


def test_smoke_benign_is_valid_and_committed_shape():
    items = load(SEED / "smoke_benign.jsonl")
    assert len(items) == 50
    assert all(i.expected.action == "allow" for i in items)
    assert {i.kind for i in items} == {"benign", "benign_risky", "greeting"}
    assert sum(i.technique in ("chosung", "codeswitch") for i in items) >= 4


@pytest.mark.parametrize(
    ("name", "minimum"),
    [("smoke_benign.jsonl", 50), ("smoke_attack.jsonl", 50), ("smoke_crisis.jsonl", 30)],
)
def test_smoke_files_ids_prefix_and_no_pii(name, minimum):
    path = SEED / name
    if not path.exists():
        pytest.skip(f"{name}은 로컬 전용(커밋하지 않음)")
    items = load(path)
    assert len(items) >= minimum
    assert all(i.id.startswith("smk-") and i.seed_id.startswith("smk-") for i in items)
    assert all(i.split == "dev" for i in items)
    text = path.read_text(encoding="utf-8")
    assert not PHONE.search(text) and not RRN.search(text)


def test_smoke_attack_jamo_variants_normalize_to_seed():
    path = SEED / "smoke_attack.jsonl"
    if not path.exists():
        pytest.skip("공격 스모크는 로컬 전용")
    items = {i.id: i for i in load(path)}
    jamo = [i for i in items.values() if i.technique == "jamo"]
    assert jamo
    for v in jamo:
        seed = items[v.seed_id]
        assert normalize(v.turns[-1].content) == normalize(seed.turns[-1].content), v.id
