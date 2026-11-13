"""S1.1 설정: §4.4 모델 로드·GUARD_* 환경변수·토글 조합·ablation 병합·config_hash·load_prompt."""

import pytest
import yaml
from pydantic import ValidationError

from guardrail.config import ROOT, GuardConfig, config_hash, load_config, load_prompt

DEFAULT = ROOT / "configs" / "default.yaml"
BASELINE = ROOT / "configs" / "baseline.yaml"
FIXTURE_PROMPTS = ROOT / "tests" / "fixtures" / "prompts"
ENV_DIRS = {"GUARD_KB_DIR": "kb", "GUARD_PUBLIC_DIR": "public", "GUARD_PROMPTS_DIR": "prompts"}


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for name in ("GUARD_CONFIG", *ENV_DIRS):
        monkeypatch.delenv(name, raising=False)


def _raw() -> dict:
    return yaml.safe_load(DEFAULT.read_text(encoding="utf-8"))


def _write(tmp_path, raw: dict, name: str = "c.yaml"):
    path = tmp_path / name
    path.write_text(yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8")
    return path


def _leaves(d: dict, prefix: str = "") -> dict:
    """중첩 매핑을 {"a.b": 값}으로 편다. 빈 매핑은 잎으로 본다."""
    out = {}
    for k, v in d.items():
        key = f"{prefix}{k}"
        # prompts는 이름 → 파일 매핑이라 항목이 몇 개든 된다. 매핑 자체만 필수 키로 본다
        if isinstance(v, dict) and v and key != "prompts":
            out |= _leaves(v, key + ".")
        else:
            out[key] = v
    return out


def _diff(a: GuardConfig, b: GuardConfig) -> set[str]:
    la, lb = _leaves(a.model_dump(mode="json")), _leaves(b.model_dump(mode="json"))
    return {k for k in la.keys() | lb.keys() if la.get(k) != lb.get(k)}


# --- 로드 ---


def test_default_loads_section_4_4_model():
    cfg = load_config("configs/default.yaml")
    assert isinstance(cfg, GuardConfig)
    assert cfg.version == "6.0.1"
    assert cfg.budget_s == 20
    assert cfg.block_categories == [
        "jailbreak_injection",
        "harmful_content",
        "academic_dishonesty",
        "off_topic",
    ]
    assert cfg.prompts == {  # S2.6·S2.7
        "input_judge": "input_judge.md",
        "output_judge": "output_judge.md",
        "output_judge_baseline": "output_judge_baseline.md",
    }
    assert cfg.output.tier3_trust == "answer_quote" and cfg.output.quote_min_chars == 8
    assert cfg.toggles.knn_vote and not cfg.toggles.third_call
    assert cfg.output.mode == "v6" and cfg.output.rule_min_margin == 3.5


def test_model_keys_equal_yaml_keys():
    # 모델에 기본값이 없으므로 키 하나라도 빠지거나 더해지면 여기서 드러난다(§4.4 완전성)
    assert _leaves(load_config(DEFAULT).model_dump(mode="json")).keys() == _leaves(_raw()).keys()


@pytest.mark.parametrize("dotted", sorted(_leaves(_raw())))
def test_every_key_is_required(tmp_path, dotted):
    # 기본값을 하나라도 두면 그 키를 지운 설정이 통과해 여기서 걸린다
    raw = _raw()
    *parents, last = dotted.split(".")
    node = raw
    for key in parents:
        node = node[key]
    del node[last]
    with pytest.raises(ValidationError):
        load_config(_write(tmp_path, raw))


@pytest.mark.parametrize(
    ("dotted", "bad"),
    [
        ("output.mode", "v5"),
        ("output.failure_default", "maybe"),
        ("output.rule_span", "word"),
        ("output.rule_extra_tokens", ["nope"]),
        ("llm.structured_output", "prompt"),
        ("input.knn.analyzer", "bytes"),
        ("embedding.targets", ["web"]),
        ("llm.gate", 0),
        ("budget_s", 0),
        ("input.lr.ngram", [3, 1]),
        *[
            (k, v)
            for k in ("input.knn.max_examples", "input.knn.per_category", "input.knn_vote_k")
            for v in (0, -1)
        ],
        *[("input.lr.C", v) for v in (0, -1.0, float("nan"))],
    ],
)
def test_bad_value_rejected(dotted, bad):
    with pytest.raises(ValidationError):
        load_config(DEFAULT, overrides={dotted: bad})


def test_load_order_overrides_beat_env_beat_ablation(tmp_path, monkeypatch):
    abl = _write(tmp_path, {"paths": {"kb": "from_ablation", "public": "from_ablation"}}, "a.yaml")
    monkeypatch.setenv("GUARD_KB_DIR", "from_env")
    monkeypatch.setenv("GUARD_PUBLIC_DIR", "from_env")
    cfg = load_config(DEFAULT, ablation=abl, overrides={"paths.public": "from_override"})
    assert cfg.paths.kb == "from_env"
    assert cfg.paths.public == "from_override"


def test_baseline_differs_only_in_toggles_and_mode():
    base = load_config(BASELINE)
    assert not any(base.toggles.model_dump().values())
    assert len(base.toggles.model_dump()) == 11
    assert base.output.mode == "baseline"
    expected = {f"toggles.{t}" for t, on in load_config(DEFAULT).toggles if on} | {"output.mode"}
    assert _diff(base, load_config(DEFAULT)) == expected


@pytest.mark.parametrize(
    "mutate",
    [
        lambda r: r.update(typo=1),
        lambda r: r["toggles"].update(typo=True),
        lambda r: r["llm"]["timeout_s"].update(typo=1),
        lambda r: r.update(block_categories=["not_a_category"]),
    ],
)
def test_unknown_key_or_value_rejected(tmp_path, mutate):
    raw = _raw()
    mutate(raw)
    with pytest.raises(ValidationError):
        load_config(_write(tmp_path, raw))


def test_unknown_override_rejected():
    with pytest.raises(ValidationError):
        load_config(DEFAULT, overrides={"llm.nope": 1})


def test_override_into_scalar_rejected():
    with pytest.raises(ValueError, match="overrides"):
        load_config(DEFAULT, overrides={"budget_s.x": 1})


def test_top_level_must_be_mapping(tmp_path):
    path = tmp_path / "list.yaml"
    path.write_text("- 1\n", encoding="utf-8")
    with pytest.raises(ValueError, match="매핑"):
        load_config(path)


# --- 환경변수 ---


def test_guard_config_env_selects_file(tmp_path, monkeypatch):
    raw = _raw()
    raw["version"] = "6.0.0-env"
    monkeypatch.setenv("GUARD_CONFIG", str(_write(tmp_path, raw)))
    assert load_config().version == "6.0.0-env"


def test_guard_config_default_when_unset():
    assert load_config() == load_config(DEFAULT)


@pytest.mark.parametrize(("env", "field"), ENV_DIRS.items())
def test_guard_dir_env_overrides_paths(monkeypatch, tmp_path, env, field):
    base = load_config(DEFAULT)
    monkeypatch.setenv(env, str(tmp_path))
    cfg = load_config(DEFAULT)
    assert getattr(cfg.paths, field) == str(tmp_path)
    assert _diff(cfg, base) == {f"paths.{field}"}


def test_paths_from_file_when_env_unset():
    assert load_config(DEFAULT).paths.model_dump() == _raw()["paths"]


# --- 토글 조합 ---


@pytest.mark.parametrize(
    "toggles",
    [
        {"third_call": True, "knn_vote": False},
        {"third_call": True, "lr": False},
        {"nano_span": True, "literal_rule": False},
    ],
)
def test_forbidden_toggle_combinations(toggles):
    with pytest.raises(ValidationError):
        load_config(DEFAULT, overrides={f"toggles.{k}": v for k, v in toggles.items()})


@pytest.mark.parametrize(
    ("dotted", "bad", "msg"),
    [
        ("output.rule_extra_tokens", ["bare_numeral"], "bare_numeral"),
        ("output.rule_extra_tokens", ["value_norm", "bare_numeral"], "bare_numeral"),
        ("output.rule_require_same_kind", True, "rule_require_same_kind"),
    ],
)
def test_unimplemented_output_options_rejected_at_load(dotted, bad, msg):
    # rules.py는 요청 때 NotImplementedError를 내므로, 기동 때 막지 않으면 서버가 떠 있다가 깨진다
    with pytest.raises(ValidationError, match=msg):
        load_config(DEFAULT, overrides={dotted: bad})


def test_implemented_output_options_still_load():
    cfg = load_config(
        DEFAULT,
        overrides={"output.rule_extra_tokens": ["value_norm", "units_extra", "backtick_latin"]},
    )
    assert cfg.output.rule_extra_tokens == ["value_norm", "units_extra", "backtick_latin"]


def test_allowed_toggle_combinations():
    cfg = load_config(
        DEFAULT,
        overrides={"toggles.third_call": True, "toggles.nano_span": True},
    )
    assert cfg.toggles.third_call and cfg.toggles.nano_span


# --- ablation ---


def test_ablation_deep_merge_changes_only_overridden_key(tmp_path):
    abl = _write(tmp_path, {"toggles": {"lr": False}}, "no_lr.yaml")
    assert _diff(load_config(DEFAULT, ablation=abl), load_config(DEFAULT)) == {"toggles.lr"}


def test_ablation_nested_merge(tmp_path):
    abl = _write(tmp_path, {"input": {"knn": {"max_examples": 4}}}, "knn4.yaml")
    assert _diff(load_config(DEFAULT, ablation=abl), load_config(DEFAULT)) == {
        "input.knn.max_examples"
    }


def test_ablation_unknown_key_rejected(tmp_path):
    abl = _write(tmp_path, {"toggles": {"typo": True}}, "bad.yaml")
    with pytest.raises(ValidationError):
        load_config(DEFAULT, ablation=abl)


# --- 프롬프트·해시 ---


def _with_prompt(monkeypatch, prompts_dir) -> GuardConfig:
    monkeypatch.setenv("GUARD_PROMPTS_DIR", str(prompts_dir))
    return load_config(DEFAULT, overrides={"prompts": {"fx": "fx.md"}})


def test_config_hash_without_prompts_is_stable():
    assert config_hash(load_config(DEFAULT)) == config_hash(load_config(DEFAULT))
    assert len(config_hash(load_config(DEFAULT))) == 12


def test_config_hash_includes_prompt_version(monkeypatch, tmp_path):
    # 같은 폴더에서 version만 바꾼다(paths.prompts가 해시에 들어가므로 폴더를 고정)
    v1 = (FIXTURE_PROMPTS / "fx.md").read_text("utf-8")
    (tmp_path / "fx.md").write_text(v1, "utf-8")
    h1 = config_hash(_with_prompt(monkeypatch, tmp_path))
    v2 = v1.replace('version: "1"', 'version: "2"')
    (tmp_path / "fx.md").write_text(v2, "utf-8")
    h2 = config_hash(_with_prompt(monkeypatch, tmp_path))
    assert h1 != h2
    # 해시는 본문이 아니라 머리말 version을 본다(같은 폴더에서 본문만 바꾸면 그대로)
    (tmp_path / "fx.md").write_text(v2.replace("본문", "다른 본문"), "utf-8")
    assert config_hash(_with_prompt(monkeypatch, tmp_path)) == h2


def test_config_hash_changes_with_config_value():
    assert config_hash(load_config(DEFAULT)) != config_hash(
        load_config(DEFAULT, overrides={"budget_s": 19})
    )


def test_load_prompt_returns_version_and_body(monkeypatch):
    version, body = load_prompt(_with_prompt(monkeypatch, FIXTURE_PROMPTS), "fx")
    assert version == "1"
    assert body.strip() == "픽스처 프롬프트 본문."


@pytest.mark.parametrize(
    "text",
    [
        "본문만 있고 머리말이 없다\n",
        '---\nid: other\nversion: "1"\n---\n본문\n',
        "---\nid: fx\n---\n",
        '---\nid: fx\nversion: "1"\n---\n',  # 머리말만
        '---\nid: fx\nversion: "1"\n---\n  \n\t\n',  # 본문이 공백뿐
    ],
)
def test_load_prompt_rejects_bad_front_matter(monkeypatch, tmp_path, text):
    (tmp_path / "fx.md").write_text(text, encoding="utf-8")
    with pytest.raises(ValueError, match="프롬프트 fx") as e:
        load_prompt(_with_prompt(monkeypatch, tmp_path), "fx")
    assert str(tmp_path) not in str(e.value)  # 이름만, 경로 없이


def test_load_prompt_rejects_unmapped_name():
    with pytest.raises(ValueError, match="prompts"):
        load_prompt(load_config(DEFAULT), "not_registered")
