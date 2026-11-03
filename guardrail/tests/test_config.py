"""S0.2 설정: 로드·검증·overrides·${ENV} 치환·config_hash·check_toggles·load_prompt."""

import pytest
import yaml
from pydantic import BaseModel, ValidationError

from guardrail import config as config_mod
from guardrail.config import (
    ROOT,
    GuardConfig,
    check_toggles,
    config_hash,
    load_config,
    load_prompt,
)
from guardrail.pipeline import Guard
from guardrail.types import ROLES

DEFAULT = ROOT / "configs" / "default.yaml"
UNGUARDED = ROOT / "configs" / "unguarded.yaml"

ALL_TOGGLES = (
    "crisis",
    "input.rules",
    "input.pii",
    "input.judge",
    "input.jev",
    "session.enabled",
    "retrieval.enabled",
    "output.code",
    "output.prefilter",
    "output.judge",
    "output.repair",
    "output.r2",
)


def _raw() -> dict:
    return yaml.safe_load(DEFAULT.read_text(encoding="utf-8"))


def _write(tmp_path, raw: dict):
    path = tmp_path / "c.yaml"
    path.write_text(yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8")
    return path


def _service(raw: dict) -> dict:
    """서비스 모드 검증을 통과하는 최소 모양(위기·PII 켬, faults 없음)."""
    raw["mode"] = "service"
    raw["stages"]["crisis"] = True
    raw["stages"]["input"]["pii"] = True
    raw["faults"] = {}
    return raw


@pytest.fixture
def hmac_env(monkeypatch):
    # 값은 런타임에 만든다(소스에 비밀 모양 문자열을 두지 않는다)
    monkeypatch.setenv("GUARD_LOG_HMAC_KEY", "t" * 4 + "-hmac")


# --- 로드: default·unguarded는 같은 코드로, 단계 상태는 YAML에서만 ---


@pytest.mark.parametrize("path", [DEFAULT, UNGUARDED])
def test_shipped_configs_load_and_pass_toggles(path):
    cfg = load_config(path)
    check_toggles(cfg)
    assert cfg.mode == "eval"
    assert cfg.deadlines_s.attack_judge == 10.0
    assert cfg.faults == {}


@pytest.mark.parametrize("path", [DEFAULT, UNGUARDED])
def test_gp0_configs_have_every_stage_off(path):
    # GP0에는 머지된 가드 단계가 없다(GP0 §4 점검 항목)
    cfg = load_config(path)
    assert config_mod.enabled_toggles(cfg) == []


def test_default_matches_contract_values():
    cfg = load_config(DEFAULT)
    assert cfg.pipeline.mode == "parallel"
    assert cfg.policy.d1 == "s2"
    assert cfg.gate.initial == 4 and cfg.gate.min == 1 and cfg.gate.max == 16
    assert cfg.gate.increase_after == 10 and cfg.gate.max_wait_s == 30
    assert cfg.gate.structured_output == "json_schema"
    assert cfg.thresholds.lambda_ == 0.9
    assert cfg.thresholds.tau_J is None
    assert cfg.context_turns.base == 4 and cfg.context_turns.expanded == 8
    assert cfg.tutor.adapter == "mock"
    assert all(cfg.tutor.obligations.model_dump().values())
    assert cfg.service_log.keep_raw_for == ["block", "escalation", "appeal", "sample"]
    assert cfg.models.jev == "typesafe-ai/jev-1.14.0"


def test_relative_path_resolves_from_package_root(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    assert load_config("configs/default.yaml").policy_version == load_config(DEFAULT).policy_version


# --- 검증 실패 ---


def test_unknown_top_level_key_rejected(tmp_path):
    raw = _raw()
    raw["unknown_key"] = 1
    with pytest.raises(ValidationError, match="unknown_key"):
        load_config(_write(tmp_path, raw))


def test_unknown_nested_key_rejected(tmp_path):
    raw = _raw()
    raw["stages"]["output"]["codee"] = True
    with pytest.raises(ValidationError, match="codee"):
        load_config(_write(tmp_path, raw))


@pytest.mark.parametrize(
    ("dotted", "value"),
    [
        ("mode", "prod"),
        ("pipeline.mode", "fast"),
        ("policy.d1", "s3"),
        ("gate.structured_output", "xml"),
        ("tutor.adapter", "other"),
        ("service_log.keep_raw_for", ["forever"]),
        ("service_log.sample_rate", 1.5),
        ("thresholds.G", 1.2),
        ("thresholds.theta", -0.1),
        ("faults.input_judge", "explode"),
        ("faults.nobody", "timeout"),
        ("gate.initial", 20),
    ],
)
def test_bad_values_rejected(dotted, value):
    with pytest.raises(ValidationError):
        load_config(DEFAULT, overrides={dotted: value})


def test_stage_toggles_are_required(tmp_path):
    # 단계 상태는 YAML에서만 정해진다: 토글을 빼면 기본값으로 메우지 않고 실패한다
    raw = _raw()
    del raw["stages"]["output"]["code"]
    with pytest.raises(ValidationError, match="code"):
        load_config(_write(tmp_path, raw))


def test_threshold_upper_bound_is_1_01():
    cfg = load_config(DEFAULT, overrides={"thresholds.input_block": 1.01})
    assert cfg.thresholds.input_block == 1.01


def test_service_mode_valid_with_hmac(tmp_path, hmac_env):
    cfg = load_config(_write(tmp_path, _service(_raw())))
    assert cfg.mode == "service"


@pytest.mark.parametrize(
    ("dotted", "value", "needle"),
    [
        ("stages.crisis", False, "crisis"),
        ("stages.input.pii", False, "pii"),
        ("faults", {"input_judge": "timeout"}, "faults"),
    ],
)
def test_service_mode_rejects_unsafe(tmp_path, hmac_env, dotted, value, needle):
    path = _write(tmp_path, _service(_raw()))
    with pytest.raises(ValidationError, match=needle):
        load_config(path, overrides={dotted: value})


def test_service_mode_requires_hmac_env_and_names_only(tmp_path, monkeypatch):
    monkeypatch.delenv("GUARD_LOG_HMAC_KEY", raising=False)
    with pytest.raises(ValidationError, match="GUARD_LOG_HMAC_KEY"):
        load_config(_write(tmp_path, _service(_raw())))


def test_eval_mode_allows_crisis_off_and_faults():
    cfg = load_config(DEFAULT, overrides={"faults": {"generator": "fake_ok"}})
    assert cfg.faults == {"generator": "fake_ok"}


# --- overrides ---


def test_override_applies_before_validation_and_changes_hash():
    base = load_config(DEFAULT)
    changed = load_config(DEFAULT, overrides={"thresholds.G": 0.6})
    assert changed.thresholds.G == 0.6
    assert config_hash(changed) != config_hash(base)


def test_override_lambda_by_alias():
    assert load_config(DEFAULT, overrides={"thresholds.lambda": 0.5}).thresholds.lambda_ == 0.5


def test_override_unknown_path_fails():
    with pytest.raises(ValidationError, match="nope"):
        load_config(DEFAULT, overrides={"nope.x": 1})


def test_override_through_scalar_fails():
    with pytest.raises(ValueError, match="mode"):
        load_config(DEFAULT, overrides={"mode.x": 1})


# --- ${ENV} 치환 (models만) ---


def test_env_substitution_in_models(monkeypatch):
    monkeypatch.setenv("GEN_MODEL", "gen-a")
    monkeypatch.setenv("JUDGE_MODEL", "judge-b")
    cfg = load_config(DEFAULT)
    assert cfg.models.generator == "gen-a"
    assert cfg.models.input_judge == cfg.models.exchange_judge == "judge-b"
    assert cfg.models.attack_judge == "judge-b"


def test_env_missing_gives_none(monkeypatch):
    monkeypatch.delenv("GEN_MODEL", raising=False)
    assert load_config(DEFAULT).models.generator is None


def test_env_substitution_only_in_models(tmp_path, monkeypatch):
    monkeypatch.setenv("X_VERSION", "9.9.9")
    raw = _raw()
    raw["policy_version"] = "${X_VERSION}"
    assert load_config(_write(tmp_path, raw)).policy_version == "${X_VERSION}"


def test_env_embedded_in_model_name(tmp_path, monkeypatch):
    monkeypatch.setenv("GEN_MODEL", "mini")
    raw = _raw()
    raw["models"]["generator"] = "vendor/${GEN_MODEL}-v1"
    assert load_config(_write(tmp_path, raw)).models.generator == "vendor/mini-v1"


# --- config_hash ---


def test_config_hash_stable_and_short(monkeypatch):
    monkeypatch.setenv("GEN_MODEL", "gen-a")
    a, b = load_config(DEFAULT), load_config(DEFAULT)
    assert config_hash(a) == config_hash(b)
    assert len(config_hash(a)) == 12


def test_config_hash_ignores_yaml_key_order(tmp_path):
    raw = _raw()
    shuffled = dict(reversed(list(raw.items())))
    shuffled["gate"] = dict(reversed(list(raw["gate"].items())))
    assert config_hash(load_config(_write(tmp_path, shuffled))) == config_hash(load_config(DEFAULT))


def test_every_config_model_forbids_extra():
    models = [GuardConfig]
    seen = set()
    while models:
        m = models.pop()
        if m in seen:
            continue
        seen.add(m)
        assert m.model_config.get("extra") == "forbid", m.__name__
        for f in m.model_fields.values():
            for t in (f.annotation, *getattr(f.annotation, "__args__", ())):
                if isinstance(t, type) and issubclass(t, BaseModel):
                    models.append(t)
    assert len(seen) >= 15


@pytest.mark.parametrize("role", ROLES)
def test_faults_accept_every_role(role):
    assert load_config(DEFAULT, overrides={f"faults.{role}": "timeout"}).faults == {role: "timeout"}


def test_lambda_python_name_is_not_a_yaml_key():
    with pytest.raises(ValidationError):
        load_config(DEFAULT, overrides={"thresholds.lambda_": 0.5})


def test_config_hash_includes_substituted_model(monkeypatch):
    monkeypatch.setenv("GEN_MODEL", "gen-a")
    h1 = config_hash(load_config(DEFAULT))
    monkeypatch.setenv("GEN_MODEL", "gen-b")
    assert config_hash(load_config(DEFAULT)) != h1


# --- check_toggles ---


def test_unimplemented_toggle_raises_with_name():
    cfg = load_config(DEFAULT, overrides={"stages.input.judge": True})
    with pytest.raises(NotImplementedError, match=r"input\.judge"):
        check_toggles(cfg)


def test_two_unimplemented_toggles_both_named():
    cfg = load_config(DEFAULT, overrides={"stages.input.judge": True, "stages.output.code": True})
    with pytest.raises(NotImplementedError) as err:
        check_toggles(cfg)
    assert "input.judge" in str(err.value) and "output.code" in str(err.value)


@pytest.mark.parametrize("toggle", ALL_TOGGLES)
def test_every_toggle_is_guarded(toggle):
    # 자기 단계 이름이 없는 session.enabled·output.r2도 막는다
    cfg = load_config(DEFAULT, overrides={f"stages.{toggle}": True})
    with pytest.raises(NotImplementedError, match=toggle.replace(".", r"\.")):
        check_toggles(cfg)


def test_implemented_toggle_passes(monkeypatch):
    monkeypatch.setattr(config_mod, "_IMPLEMENTED", frozenset({"input.judge"}))
    check_toggles(load_config(DEFAULT, overrides={"stages.input.judge": True}))


def test_guard_construction_uses_check_toggles():
    cfg = load_config(DEFAULT, overrides={"stages.input.judge": True})
    with pytest.raises(NotImplementedError, match=r"input\.judge"):
        Guard(cfg)


def test_jev_requires_pii(monkeypatch):
    monkeypatch.setattr(config_mod, "_IMPLEMENTED", frozenset({"input.jev"}))
    cfg = load_config(DEFAULT, overrides={"stages.input.jev": True})
    with pytest.raises(ValueError, match=r"input\.pii") as err:
        check_toggles(cfg)
    assert "input.jev" in str(err.value)
    with pytest.raises(ValueError, match=r"input\.pii"):
        Guard(cfg)


def test_jev_with_pii_passes(monkeypatch):
    monkeypatch.setattr(config_mod, "_IMPLEMENTED", frozenset({"input.jev", "input.pii"}))
    check_toggles(
        load_config(DEFAULT, overrides={"stages.input.jev": True, "stages.input.pii": True})
    )


def test_check_toggles_reads_no_env(monkeypatch):
    # 순수 함수: 환경변수가 하나도 없어도 같은 결과
    for name in ("GEN_MODEL", "JUDGE_MODEL", "GUARD_LLM_API_KEY", "GUARD_LLM_BASE_URL"):
        monkeypatch.delenv(name, raising=False)
    check_toggles(load_config(UNGUARDED))


# --- load_prompt ---


def test_load_prompt_reads_front_matter(tmp_path):
    (tmp_path / "demo.md").write_text(
        "---\nid: demo\nversion: 1.2.3\n---\n본문 첫 줄\n", encoding="utf-8"
    )
    cfg = load_config(DEFAULT, overrides={"paths.prompts": str(tmp_path)})
    assert load_prompt(cfg, "demo") == ("1.2.3", "본문 첫 줄\n")


@pytest.mark.parametrize(
    "text",
    [
        "본문만\n",
        "---\nid: demo\n---\n본문\n",
        "---\nid: other\nversion: 1.0.0\n---\n본문\n",
        "---\nid: demo\nversion: 1.0.0\n본문(닫는 줄 없음)\n",
        "---\n- id\n- version\n---\n본문\n",
    ],
)
def test_load_prompt_rejects_bad_header(tmp_path, text):
    (tmp_path / "demo.md").write_text(text, encoding="utf-8")
    cfg = load_config(DEFAULT, overrides={"paths.prompts": str(tmp_path)})
    with pytest.raises(ValueError, match="demo"):
        load_prompt(cfg, "demo")


def test_guard_config_type_exported():
    assert isinstance(load_config(DEFAULT), GuardConfig)
