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


SECRET = "비밀값ZQ9"  # noqa: S105  파일에 넣는 표식. 오류 메시지에 나오면 파일 내용이 샌 것이다


@pytest.mark.parametrize(
    "text",
    [
        f"judge_skip_s: [4\nfallback_action: {SECRET}\n",  # 닫히지 않은 괄호
        f"judge_skip_s: 4\n\tfallback_action: {SECRET}\n",  # 탭 들여쓰기
        f'judge_skip_s: 4\nfallback_action: "{SECRET}\n',  # 닫히지 않은 따옴표
        f"? [a, {SECRET}]\n: 1\njudge_skip_s: 4\nfallback_action: clarify\n",  # 목록을 키로
        f"judge_skip_s: 4\nfallback_action: clarify\n---\nnote: {SECRET}\n",  # 문서 두 개
        f"judge_skip_s: 4\nfallback_action: clarify\n---\njudge_skip_s: 4\nnote: {SECRET}\n",
    ],
)
def test_broken_or_multi_document_yaml_is_a_value_error_with_the_file_name_only(tmp_path, text):
    with pytest.raises(ValueError) as caught:
        load_runtime(write(tmp_path, text))
    assert str(caught.value) == "runtime.yaml: YAML 형식 오류"
    assert not isinstance(caught.value, yaml.YAMLError)
    # 원래 예외(파일 조각이 들어 있다)가 트레이스백에 따라오지 않는다
    assert caught.value.__cause__ is None and caught.value.__context__ is None


@pytest.mark.parametrize(
    ("text", "key"),
    [
        ("judge_skip_s: 4\njudge_skip_s: 5\nfallback_action: clarify\n", "judge_skip_s"),
        (
            "judge_skip_s: 4\nfallback_action: clarify\nfallback_action: clarify\n",
            "fallback_action",
        ),
        ("judge_skip_s: 4\nfallback_action: clarify\nfallback_action: refuse\n", "fallback_action"),
    ],
)
def test_duplicate_key_fails(tmp_path, text, key):
    with pytest.raises(ValueError) as caught:
        load_runtime(write(tmp_path, text))
    assert str(caught.value) == f"runtime.yaml: {key}: 같은 키가 두 번 나온다"


@pytest.mark.parametrize("value", ["0", "0.0", "4", "29.9", "30", "30.0"])
def test_judge_skip_s_up_to_the_request_limit_is_allowed(tmp_path, value):
    cfg = load_runtime(write(tmp_path, f"judge_skip_s: {value}\nfallback_action: clarify\n"))
    assert cfg.judge_skip_s == float(value)


@pytest.mark.parametrize("value", ["30.01", "31", "60", "1e9"])
def test_judge_skip_s_over_the_request_limit_fails(tmp_path, value):
    with pytest.raises(ValueError) as caught:
        load_runtime(write(tmp_path, f"judge_skip_s: {value}\nfallback_action: clarify\n"))
    assert str(caught.value).startswith("runtime.yaml: judge_skip_s: ")


@pytest.mark.parametrize(
    ("text", "said"),
    [
        (f"judge_skip_s: 4\nfallback_action: {SECRET}\n", "fallback_action"),
        (f"judge_skip_s: {SECRET}\nfallback_action: clarify\n", "judge_skip_s"),
        (f"judge_skip_s: 4\nfallback_action: clarify\nextra: {SECRET}\n", "extra"),
        (f"judge_skip_s: 4\nnote: {SECRET}\n", "fallback_action"),
        (f"- {SECRET}\n", "매핑"),
        (f"{SECRET}\n", "매핑"),
        (f"judge_skip_s: 4\nfallback_action: [{SECRET}]\n", "fallback_action"),
        (f"judge_skip_s: {{a: {SECRET}}}\nfallback_action: clarify\n", "judge_skip_s"),
    ],
)
def test_validation_error_names_the_key_but_not_the_content(tmp_path, text, said):
    with pytest.raises(ValueError) as caught:
        load_runtime(write(tmp_path, text))
    message = str(caught.value)
    assert message.startswith("runtime.yaml: ")
    assert said in message
    assert SECRET not in message and SECRET not in repr(caught.value)
    assert "input" not in message  # pydantic 기본 메시지(input_value=…)가 아니다
    assert caught.value.__cause__ is None and caught.value.__context__ is None


def test_validation_error_lists_every_problem(tmp_path):
    with pytest.raises(ValueError) as caught:
        load_runtime(write(tmp_path, "judge_skip_s: -1\nextra: 1\n"))
    message = str(caught.value)
    assert "judge_skip_s" in message and "fallback_action" in message and "extra" in message


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
