"""I1.4 `config/runtime.yaml` 로더(PRD §3-1): 두 키, 엄격 검증, frozen."""

import pytest
import yaml

from integration import actions, runtime
from integration.contracts import ACTIONS
from integration.runtime import RuntimeConfig, load_runtime


def write(tmp_path, text: str):
    path = tmp_path / "runtime.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def test_repo_runtime_yaml_has_exactly_the_two_keys():
    raw = yaml.safe_load(runtime.RUNTIME_PATH.read_text(encoding="utf-8"))
    assert raw == {"judge_skip_s": 4, "fallback_action": "clarify"}
    assert "budget_s" not in raw  # 주인은 가드 설정


def test_load_runtime_default_path():
    cfg = load_runtime()
    assert isinstance(cfg, RuntimeConfig)
    assert cfg.judge_skip_s == 4.0
    assert cfg.fallback_action == "clarify"
    assert runtime.RUNTIME_PATH.name == "runtime.yaml"
    assert runtime.RUNTIME_PATH.parent.name == "config"


def test_load_runtime_explicit_path(tmp_path):
    cfg = load_runtime(write(tmp_path, "judge_skip_s: 2.5\nfallback_action: abstain\n"))
    assert (cfg.judge_skip_s, cfg.fallback_action) == (2.5, "abstain")
    assert (
        load_runtime(
            str(write(tmp_path, "judge_skip_s: 0\nfallback_action: refuse\n"))
        ).judge_skip_s
        == 0
    )


@pytest.mark.parametrize("action", ACTIONS)
def test_every_action_is_a_valid_fallback(tmp_path, action):
    assert (
        load_runtime(
            write(tmp_path, f"judge_skip_s: 4\nfallback_action: {action}\n")
        ).fallback_action
        == action
    )


@pytest.mark.parametrize(
    "text",
    [
        "judge_skip_s: 4\nfallback_action: clarify\nbudget_s: 20\n",  # 모르는 키(가드 소유)
        "judge_skip_s: 4\nfallback_action: clarify\nextra: 1\n",
        "judge_skip_s: 4\n",  # 빠진 키
        "fallback_action: clarify\n",
        "",  # 빈 파일
        "judge_skip_s: 4\nfallback_action: allow\n",  # action 7종이 아님
        "judge_skip_s: 4\nfallback_action: Clarify\n",
        "judge_skip_s: 4\nfallback_action: 3\n",
        "judge_skip_s: 4\nfallback_action: null\n",
        "judge_skip_s: -1\nfallback_action: clarify\n",  # 음수
        "judge_skip_s: -0.5\nfallback_action: clarify\n",
        'judge_skip_s: "4"\nfallback_action: clarify\n',  # 타입 불일치
        "judge_skip_s: true\nfallback_action: clarify\n",
        "judge_skip_s: null\nfallback_action: clarify\n",
        "judge_skip_s: .inf\nfallback_action: clarify\n",
        "judge_skip_s: .nan\nfallback_action: clarify\n",
        "- judge_skip_s: 4\n",  # 매핑이 아님
    ],
)
def test_load_runtime_rejects(tmp_path, text):
    with pytest.raises(ValueError):
        load_runtime(write(tmp_path, text))


def test_load_runtime_fails_when_file_is_missing(tmp_path):
    with pytest.raises(OSError):
        load_runtime(tmp_path / "nope.yaml")


def test_runtime_config_is_frozen():
    cfg = load_runtime()
    with pytest.raises(ValueError):
        cfg.judge_skip_s = 1.0
    with pytest.raises(ValueError):
        cfg.fallback_action = "refuse"


def test_actions_fallback_comes_from_runtime_yaml():
    assert actions.FALLBACK_ACTION == load_runtime().fallback_action
    assert actions.load_fallback_action() == load_runtime().fallback_action
    assert actions.RUNTIME_PATH == runtime.RUNTIME_PATH
    assert actions.action_for("모르는 범주").action == load_runtime().fallback_action


def test_actions_default_fallback_path_is_strict(monkeypatch, tmp_path):
    """경로를 주든 안 주든 같은 엄격한 로더를 거친다."""
    bad = write(tmp_path, "judge_skip_s: 4\nfallback_action: clarify\nextra: 1\n")
    monkeypatch.setattr(runtime, "RUNTIME_PATH", bad)
    with pytest.raises(ValueError):
        actions.load_fallback_action()
    with pytest.raises(ValueError):
        actions.load_fallback_action(bad)
