"""I1.2 범주→action 표(PRD IN-03): 공개 세트 200/200, YAML 10범주, 모르는 범주 → fallback."""

import importlib.util
import json
from collections import Counter
from pathlib import Path
from typing import get_args

import pytest
import yaml

from integration import actions
from integration.actions import ActionRule, action_for, load_actions, load_fallback_action
from integration.contracts import ACTIONS, Category

ROOT = Path(__file__).resolve().parents[2]
PUBLIC_TUTOR = ROOT / "competition" / "public" / "tutor.jsonl"
CATEGORIES = get_args(Category)


def _public_rows():
    with PUBLIC_TUTOR.open(encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def _raw_table():
    return yaml.safe_load(actions.ACTIONS_PATH.read_text(encoding="utf-8"))


def _write(tmp_path, raw):
    p = tmp_path / "actions.yaml"
    p.write_text(yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8")
    return p


# ── 공개 세트 ──────────────────────────────────────────────────────────────


def test_public_set_200_of_200():
    rows = _public_rows()
    assert len(rows) == 200
    misses = [
        (r["id"], r["labels"]["category"], r["labels"]["action"])
        for r in rows
        if r["labels"]["action"] not in action_for(r["labels"]["category"]).actions
    ]
    assert misses == []


def test_only_harmful_has_two_actions_and_both_occur_in_public_set():
    seen = Counter((r["labels"]["category"], r["labels"]["action"]) for r in _public_rows())
    for category in CATEGORIES:
        rule = action_for(category)
        if category == "harmful_content":
            assert rule.actions == ("refuse", "redirect")
        else:
            assert rule.actions == (rule.action,)
        # 표에 적힌 action은 전부 공개 세트에 실제로 나온다(표가 세트보다 넓지 않다)
        for a in rule.actions:
            assert seen[(category, a)] > 0


# ── 표 내용(IN-03 그대로) ─────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("category", "action", "generate", "cite"),
    [
        ("normal", "answer", "llm", "always"),
        ("answer_seeking", "scaffold", "llm", "always"),
        ("academic_dishonesty", "scaffold", "template", "if_used"),
        ("out_of_material", "abstain", "template", "if_used"),
        ("off_topic", "redirect", "template", "never"),
        ("jailbreak_injection", "refuse", "template", "never"),
        ("pii_disclosure", "answer", "llm", "always"),
        ("harmful_content", "refuse", "template", "never"),
        ("emotional_distress", "escalate", "template", "never"),
        ("low_signal_input", "clarify", "template", "never"),
    ],
)
def test_table_rows(category, action, generate, cite):
    rule = action_for(category)
    assert isinstance(rule, ActionRule)
    assert rule.category == category
    assert (rule.action, rule.generate, rule.cite) == (action, generate, cite)


def test_table_has_no_template_key():
    """고정 문장 키는 튜터 소유다. 표에는 범주 · action · 생성 여부 · 인용 규칙만 둔다."""
    allowed = {"action", "generate", "cite", "variants"}
    for category, fields in _raw_table()["rules"].items():
        assert set(fields) <= allowed, category
        for v in fields.get("variants", []):
            assert set(v) <= {"action", "when"}, category
    assert "template_key" not in ActionRule.model_fields


def test_rules_are_immutable():
    rule = action_for("normal")
    with pytest.raises(Exception):  # noqa: B017 - pydantic frozen 오류 종류는 버전마다 다르다
        rule.action = "refuse"


# ── YAML 검증 ─────────────────────────────────────────────────────────────


def test_yaml_has_exactly_the_ten_categories():
    assert set(_raw_table()["rules"]) == set(CATEGORIES)
    assert set(load_actions()) == set(CATEGORIES)
    assert len(CATEGORIES) == 10


@pytest.mark.parametrize("missing", CATEGORIES)
def test_load_fails_when_a_category_is_missing(tmp_path, missing):
    raw = _raw_table()
    del raw["rules"][missing]
    with pytest.raises(ValueError, match=missing):
        load_actions(_write(tmp_path, raw))


def test_load_fails_on_unknown_category(tmp_path):
    raw = _raw_table()
    raw["rules"]["spam"] = dict(raw["rules"]["off_topic"])
    with pytest.raises(ValueError, match="spam"):
        load_actions(_write(tmp_path, raw))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("action", "allow"),
        ("generate", "maybe"),
        ("cite", "2"),
        ("surprise", 1),
        ("template_key", "refuse"),
    ],
)
def test_load_fails_on_bad_rule_field(tmp_path, field, value):
    raw = _raw_table()
    raw["rules"]["normal"][field] = value
    with pytest.raises(ValueError):
        load_actions(_write(tmp_path, raw))


def test_load_fails_when_not_a_mapping(tmp_path):
    p = tmp_path / "actions.yaml"
    p.write_text("- just\n- a list\n", encoding="utf-8")
    with pytest.raises(ValueError):
        load_actions(p)


def test_import_fails_when_a_category_is_missing(monkeypatch):
    """표가 깨져 있으면 서버가 뜨지 않는다(import 시점 실패).

    실제 모듈을 reload하면 `ActionRule` 같은 클래스가 새로 만들어져 다른 테스트의 isinstance가
    순서에 따라 깨진다. 그래서 같은 파일을 다른 이름의 모듈로 한 번 더 실행해 본다.
    """
    raw = _raw_table()
    del raw["rules"]["low_signal_input"]
    spec = importlib.util.spec_from_file_location("integration_actions_probe", actions.__file__)
    assert spec is not None and spec.loader is not None
    probe = importlib.util.module_from_spec(spec)
    monkeypatch.setattr(yaml, "safe_load", lambda *_a, **_k: raw)
    with pytest.raises(ValueError, match="low_signal_input"):
        spec.loader.exec_module(probe)


# ── 모르는 범주 → fallback_action ─────────────────────────────────────────


@pytest.mark.parametrize("unknown", ["spam", "", "NORMAL", None, 3])
def test_unknown_category_uses_fallback_action(unknown):
    rule = actions.action_for(unknown)
    assert rule.category is None
    assert rule.action == actions.FALLBACK_ACTION
    assert rule.generate == "template"
    assert rule.cite == "never"
    assert rule.variants == ()


def test_fallback_action_default_when_runtime_yaml_absent(tmp_path):
    assert load_fallback_action(tmp_path / "runtime.yaml") == actions.DEFAULT_FALLBACK_ACTION
    assert actions.DEFAULT_FALLBACK_ACTION in ACTIONS


def test_fallback_action_read_from_runtime_yaml(tmp_path):
    p = tmp_path / "runtime.yaml"
    p.write_text("budget_s: 20\nfallback_action: abstain\n", encoding="utf-8")
    assert load_fallback_action(p) == "abstain"


def test_fallback_action_key_absent_uses_default(tmp_path):
    p = tmp_path / "runtime.yaml"
    p.write_text("budget_s: 20\n", encoding="utf-8")
    assert load_fallback_action(p) == actions.DEFAULT_FALLBACK_ACTION


@pytest.mark.parametrize("body", ["fallback_action: allow\n", "- a\n"])
def test_fallback_action_invalid_fails(tmp_path, body):
    p = tmp_path / "runtime.yaml"
    p.write_text(body, encoding="utf-8")
    with pytest.raises(ValueError):
        load_fallback_action(p)
