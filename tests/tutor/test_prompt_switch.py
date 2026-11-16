"""주 호출 프롬프트 파일 전환(prompt.main_call, 측정 전용): 기본 그대로, 다른 파일, 틀린 이름."""

import types
from pathlib import Path

import pytest
from pydantic import ValidationError

from tutor.eval import run_local
from tutor.judge import flow, main_call
from tutor.judge.main_call import MainCallPrompt
from tutor.runtime import config

TABLE = {"normal": ["answer"], "out_of_material": ["abstain"]}
ALT = "main_call_v_test.md"
SOURCE = Path(main_call.PROMPT_DIR) / main_call.PROMPT_FILE


@pytest.fixture
def prompt_dir(tmp_path, monkeypatch):
    """기본 파일과 버전·본문 한 줄만 다른 ALT를 둔 프롬프트 폴더. 설정 검사도 이 폴더를 본다."""
    src = SOURCE.read_text(encoding="utf-8")
    (tmp_path / main_call.PROMPT_FILE).write_text(src, encoding="utf-8")
    head, sep, body = src.partition("\n---\n")
    lines = head.split("\n")
    head = "\n".join("version: alt-1" if ln.startswith("version:") else ln for ln in lines)
    (tmp_path / ALT).write_text(head + sep + "다른 판 표시 줄\n" + body, encoding="utf-8")
    monkeypatch.setattr(config, "PROMPT_DIR", tmp_path)
    return tmp_path


def test_prompt_dirs_agree():
    assert config.PROMPT_DIR == main_call.PROMPT_DIR


def test_default_config_keeps_the_current_prompt():
    cfg = config.load_config()
    assert cfg.prompt.main_call == main_call.PROMPT_FILE == "main_call.md"
    assert MainCallPrompt.from_rules() == MainCallPrompt.from_rules(file_name="main_call.md")
    state = types.SimpleNamespace(tutor_cfg=cfg)
    flow.prepare_judge(state)
    assert state.tutor_prompt == MainCallPrompt.from_rules()  # 파일을 고르기 전과 같은 프롬프트


def test_alternative_file_is_loaded(prompt_dir):
    default = MainCallPrompt.load(TABLE, prompt_dir)
    assert default == MainCallPrompt.load(TABLE)  # 폴더만 옮긴 기본 파일은 같다
    alt = MainCallPrompt.load(TABLE, prompt_dir, file_name=ALT)
    assert alt.version == "alt-1" and default.version != "alt-1"
    assert "다른 판 표시 줄" in alt.system and "다른 판 표시 줄" not in default.system


def test_prepare_judge_passes_the_configured_file(prompt_dir, monkeypatch):
    seen = {}

    class Spy:
        @classmethod
        def from_rules(cls, **kwargs):
            seen.update(kwargs)
            return "prompt"

    monkeypatch.setattr(flow, "MainCallPrompt", Spy)
    cfg = config.load_config(overrides={"prompt.main_call": ALT})
    state = types.SimpleNamespace(tutor_cfg=cfg)
    flow.prepare_judge(state)
    assert seen["file_name"] == ALT and state.tutor_prompt == "prompt"


@pytest.mark.parametrize(
    "name",
    [
        "missing_prompt.md",
        "MAIN_CALL.md",  # 대소문자가 다른 이름(런박스 Linux에서는 없는 파일)
        "main_call",
        "main_call.txt",
        "../main_call.md",
        "prompts/main_call.md",
        "..\\main_call.md",
        "C:/main_call.md",
        "/main_call.md",
        ".main_call.md",
        "",
        1,
    ],
)
def test_unknown_or_path_like_names_fail_at_load(name):
    with pytest.raises(ValidationError):
        config.load_config(overrides={"prompt.main_call": name})


@pytest.mark.parametrize("name", ["../main_call.md", "sub/main_call.md", "a\\main_call.md"])
def test_prompt_load_takes_only_a_file_name(name):
    with pytest.raises(ValueError):
        MainCallPrompt.load(TABLE, file_name=name)


def _args(*tutor_set):
    ap = run_local._parser()
    argv = ["--preset", "tutor"]
    for item in tutor_set:
        argv += ["--tutor-set", item]
    return ap, ap.parse_args(argv)


def test_tutor_set_picks_a_prompt_file(prompt_dir, monkeypatch):
    for name in ("OPENAI_BASE_URL", "OPENAI_MODEL", "OPENAI_API_KEY"):
        monkeypatch.setenv(name, "x")  # 자리만 채운다(이 검사는 아무것도 부르지 않는다)
    cfg, overrides = run_local._tutor_config(*_args(f'prompt.main_call="{ALT}"'))
    assert cfg.prompt.main_call == ALT and overrides == {"prompt.main_call": ALT}
    used = run_local._tutor_config_used(cfg, overrides)
    assert used["prompt"] == {"main_call": ALT}


def test_tutor_set_with_an_unknown_prompt_stops_before_any_call(capsys):
    with pytest.raises(SystemExit):
        run_local._tutor_config(*_args('prompt.main_call="nope.md"'))
    assert "--tutor-set 설정이 맞지 않다" in capsys.readouterr().err
