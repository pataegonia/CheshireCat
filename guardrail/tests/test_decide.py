"""S2.8 판정 조립의 순수 함수 (PRD GR-07 표 ①–④, GR-08, GR-09. C2 §S2.8)."""

from pathlib import Path
from typing import get_args

import pytest
from hypothesis import given
from hypothesis import strategies as st

from guardrail.config import load_config
from guardrail.decide import fallback_category, output_verdict, safe_of, trusted
from guardrail.schema import Category
from guardrail.signals import Signals

CFG = load_config(str(Path(__file__).parents[1] / "configs/default.yaml"))
BLOCK = frozenset(CFG.block_categories)
RULE = ("규칙 구간",)
QUOTE = "인용한 구절"


def toggles(**changes):
    return CFG.toggles.model_copy(update=changes)


def verdict(category="normal", rule_spans=None, nano_ungrounded=False, trust=True, **kw):
    """기본: non-block, 규칙 안 걸림, nano 근거 있음, 기본 토글, failure_default=grounded."""
    return output_verdict(
        category,
        BLOCK,
        rule_spans,
        nano_ungrounded,
        trust,
        kw.pop("toggles", CFG.toggles),
        kw.pop("failure_default", "grounded"),
        **kw,
    )


# --- GR-09 입력 실패 기본 범주 ---


@pytest.mark.parametrize(
    ("knn", "lr", "want"),
    [
        ("off_topic", "off_topic", "off_topic"),  # 같으면 그 범주
        ("off_topic", "normal", "normal"),  # 다르면 LR
        (None, "harmful_content", "harmful_content"),  # knn_vote 꺼짐
        ("pii_disclosure", None, "pii_disclosure"),  # lr 꺼짐
        (None, None, "normal"),
    ],
)
def test_fallback_category(knn, lr, want):
    assert fallback_category(knn, lr) == want


# --- R14-D2 인용 확인 범위 ---


@pytest.mark.parametrize(
    ("mode", "answer_ok", "kb_ok", "want"),
    [
        ("all", False, False, True),
        ("answer_quote", True, False, True),
        ("answer_quote", False, True, False),
        ("both_quotes", True, True, True),
        ("both_quotes", True, False, False),
    ],
)
def test_trusted(mode, answer_ok, kb_ok, want):
    assert trusted(mode, answer_ok, kb_ok) is want


def test_trusted_rejects_unknown_mode():
    with pytest.raises(ValueError, match="tier3_trust"):
        trusted("quotes", True, True)


# --- GR-07 표 ①–④ ---


@pytest.mark.parametrize("category", sorted(BLOCK))
def test_tier1_block_category_is_grounded_whatever_else_fired(category):
    got = verdict(category, rule_spans=RULE, nano_ungrounded=True, quote_span=QUOTE)
    assert got == (True, (), 1)


def test_tier1_applies_even_when_safe_rule_and_other_tiers_are_off():
    off = toggles(safe_rule=False, literal_rule=False, nano_tier3=False)
    assert verdict("off_topic", toggles=off, failure_default="ungrounded") == (True, (), 1)


def test_tier2_rule_emits_its_spans_before_nano():
    assert verdict(rule_spans=RULE, nano_ungrounded=True, quote_span=QUOTE) == (False, RULE, 2)


def test_tier2_rule_without_spans_is_false_empty():  # rule_emit_spans: false → 빈 튜플
    assert verdict(rule_spans=()) == (False, (), 2)


def test_tier2_is_skipped_when_literal_rule_is_off():
    off = toggles(literal_rule=False)
    assert verdict(rule_spans=RULE, toggles=off) == (True, (), 4)


def test_tier3_trusted_nano_ungrounded_is_false_empty():
    off = toggles(nano_span=False)
    assert verdict(nano_ungrounded=True, quote_span=QUOTE, toggles=off) == (False, (), 3)


def test_tier3_emits_quote_only_when_nano_span_is_on():
    on = toggles(nano_span=True)
    assert verdict(nano_ungrounded=True, quote_span=QUOTE, toggles=on) == (False, (QUOTE,), 3)
    assert verdict(nano_ungrounded=True, quote_span=None, toggles=on) == (False, (), 3)


@pytest.mark.parametrize("default", ["grounded", "ungrounded"])
def test_tier4_nano_grounded_is_true_whatever_failure_default(default):
    assert verdict(nano_ungrounded=False, failure_default=default) == (True, (), 4)


@pytest.mark.parametrize(("default", "grounded"), [("grounded", True), ("ungrounded", False)])
@pytest.mark.parametrize(
    "case",
    [
        {"nano_ungrounded": None},  # 출력 nano 실패·호출 안 함
        {"nano_ungrounded": True, "trust": False},  # 근거 없음이지만 인용 조건 미달
        {"nano_ungrounded": True, "toggles": toggles(nano_tier3=False)},  # ③ 꺼짐
        {"nano_ungrounded": False, "toggles": toggles(nano_tier3=False)},  # ③ 꺼짐
    ],
)
def test_tier4_uses_failure_default_when_nano_verdict_is_unused(case, default, grounded):
    assert verdict(failure_default=default, **case) == (grounded, (), 4)


def test_refusal_answer_on_non_block_row_is_not_tier1():
    # 거절 답변이어도 범주가 block이 아니면 ①이 아니다(거절 신호는 safe에만 쓴다)
    assert verdict("answer_seeking", nano_ungrounded=True)[2] == 3


# --- GR-08 safe ---


@pytest.mark.parametrize(
    ("category", "sig", "want"),
    [
        ("normal", Signals(False, False, False), True),
        ("harmful_content", Signals(False, False, False), False),  # block에 응함
        ("harmful_content", Signals(False, True, False), True),  # block이지만 거절
        ("normal", Signals(False, False, True), False),  # 숨긴 태그
        ("harmful_content", Signals(False, True, True), False),  # 거절이어도 숨긴 태그면 false
    ],
)
def test_safe_of(category, sig, want):
    assert safe_of(category, BLOCK, sig, CFG.toggles) is want


def test_safe_rule_off_is_always_safe():
    off = toggles(safe_rule=False)
    assert safe_of("harmful_content", BLOCK, Signals(False, False, True), off) is True


# --- 속성 ---

categories = st.sampled_from(get_args(Category))
spans = st.none() | st.lists(st.text(min_size=1, max_size=8), max_size=3).map(tuple)
all_toggles = st.builds(
    lambda rule, tier3, span: toggles(literal_rule=rule, nano_tier3=tier3, nano_span=span),
    st.booleans(),
    st.booleans(),
    st.booleans(),
)


@given(
    category=categories,
    rule_spans=spans,
    nano=st.none() | st.booleans(),
    trust=st.booleans(),
    tg=all_toggles,
    default=st.sampled_from(["grounded", "ungrounded"]),
    quote=st.none() | st.text(min_size=1, max_size=8),
)
def test_verdict_is_always_well_formed(category, rule_spans, nano, trust, tg, default, quote):
    grounded, unsupported, tier = output_verdict(
        category, BLOCK, rule_spans, nano, trust, tg, default, quote
    )
    assert isinstance(grounded, bool) and isinstance(unsupported, tuple) and tier in (1, 2, 3, 4)
    allowed = set(rule_spans or ()) | ({quote} if quote else set())
    assert set(unsupported) <= allowed  # 받은 구간만 낸다
    if grounded:
        assert unsupported == ()
    if category in BLOCK:
        assert (grounded, unsupported, tier) == (True, (), 1)
    if tier == 2:
        assert tg.literal_rule and rule_spans is not None and unsupported == rule_spans
    if tier == 3:
        assert tg.nano_tier3 and nano is True and trust
        assert unsupported in ((), (quote,)) and (unsupported == () or tg.nano_span)
