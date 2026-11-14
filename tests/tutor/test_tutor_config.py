"""튜터 설정(TU-14): 루트 config/tutor.yaml을 가드 GR-11과 같은 형식으로 읽는다."""

import asyncio
import types

import pytest
import yaml
from pydantic import ValidationError

import tutor
from tutor.eval import run_local
from tutor.judge.main_call import MainCallPrompt
from tutor.runtime import config
from tutor.startup import prepare


def repo_raw():
    return yaml.safe_load((config.ROOT / config.DEFAULT_CONFIG).read_text(encoding="utf-8"))


def write(tmp_path, data, name="tutor.yaml"):
    path = tmp_path / name
    path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    return path


def test_repo_config_has_the_prd_defaults():
    cfg = config.load_config()
    toggles = cfg.toggles
    assert cfg.mode == "v2"
    assert toggles.action_from_category is False  # TU-06 기본 끔
    # TU-09·TU-10: 가드 judge_output·classify는 #170(S2.11) 뒤 런박스 켬/끔 비교 전까지 끔(11/14)
    assert toggles.verify == "off"
    assert toggles.guard_classify == "off"
    assert (toggles.embedding, toggles.rerank, toggles.vote) == (False, False, False)  # TU-13
    assert toggles.max_citations == 1  # TU-07 기본 1
    assert toggles.prompt_lang == "ko"  # T3.2 기본은 한국어 프롬프트
    assert cfg.llm.reasoning_effort == "low"  # TU-13
    assert cfg.llm.max_completion_tokens == 1600  # 주 호출 상한(가드 판정 호출과 따로)
    assert cfg.eval.max_calls == 5000  # TU-14


@pytest.mark.parametrize(
    "change",
    [
        lambda d: d["toggles"].update(typo=True),
        lambda d: d.update(extra=1),
        lambda d: d["retrieval"].pop("k"),
        lambda d: d.pop("eval"),
    ],
    ids=["unknown_nested", "unknown_top", "missing_nested", "missing_top"],
)
def test_unknown_or_missing_keys_fail_at_load(tmp_path, change):
    data = repo_raw()
    change(data)
    with pytest.raises(ValidationError):
        config.load_config(write(tmp_path, data))


@pytest.mark.parametrize(
    "dotted, value",
    [
        ("mode", "v3"),
        ("toggles.verify", "on"),
        ("toggles.guard_classify", "both"),
        ("toggles.max_citations", 3),
        ("llm.reasoning_effort", "minimal"),  # TU-13: minimal 금지
        ("llm.max_completion_tokens", 2001),  # 운영진 프록시 상한 2000(넘기면 400)
        ("llm.max_completion_tokens", 0),
        ("llm.max_completion_tokens", True),
        ("retrieval.k", 0),
        ("llm.timeout_s.main", 0),
        # 느슨한 값도 받지 않는다(strict)
        ("toggles.max_citations", True),
        ("toggles.max_citations", 1.0),
        ("retrieval.k", "5"),
        ("retrieval.k", 5.0),
        ("retrieval.k", True),
        ("toggles.signals", "yes"),
        ("toggles.signals", 1),
        ("toggles.prompt_lang", "fr"),
        ("toggles.prompt_lang", "EN"),
        ("toggles.prompt_lang", True),
    ],
)
def test_invalid_values_fail_at_load(dotted, value):
    with pytest.raises(ValidationError):
        config.load_config(overrides={dotted: value})


def test_output_cap_up_to_the_proxy_ceiling_loads():
    cfg = config.load_config(overrides={"llm.max_completion_tokens": 2000})
    assert cfg.llm.max_completion_tokens == 2000 == config.PROXY_MAX_COMPLETION_TOKENS


def test_unquoted_off_is_rejected(tmp_path):
    preset = tmp_path / "preset.yaml"
    preset.write_text("toggles:\n  verify: off\n", encoding="utf-8")  # YAML에서 off는 false
    with pytest.raises(ValidationError):
        config.load_config(ablation=preset)
    preset.write_text('toggles:\n  verify: "off"\n', encoding="utf-8")
    assert config.load_config(ablation=preset).toggles.verify == "off"


def test_empty_env_falls_back_to_the_default_file(monkeypatch):
    monkeypatch.setenv(config.CONFIG_ENV, "")
    assert config.load_config() == config.load_config(config.ROOT / config.DEFAULT_CONFIG)


def test_ablation_preset_merges_over_the_file(tmp_path):
    preset = {"mode": "baseline", "retrieval": {"k": 3}, "toggles": {"signals": False}}
    cfg = config.load_config(ablation=write(tmp_path, preset, "preset.yaml"))
    base = config.load_config()
    assert (cfg.mode, cfg.retrieval.k, cfg.toggles.signals) == ("baseline", 3, False)
    assert cfg.toggles.fewshot == base.toggles.fewshot and cfg.llm == base.llm  # 나머지는 그대로


def test_overrides_apply_last():
    cfg = config.load_config(overrides={"toggles.vote": True, "retrieval.k": 8})
    assert cfg.toggles.vote is True and cfg.retrieval.k == 8


def test_override_cannot_go_inside_a_value():
    with pytest.raises(ValueError):
        config.load_config(overrides={"mode.inner": 1})


def test_env_picks_the_config_file(tmp_path, monkeypatch):
    data = repo_raw()
    data["retrieval"]["k"] = 8
    monkeypatch.setenv(config.CONFIG_ENV, str(write(tmp_path, data)))
    assert config.load_config().retrieval.k == 8


def test_default_file_is_found_from_another_directory(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)  # 저장소 루트가 아닌 곳에서 띄워도 config/tutor.yaml을 찾는다
    assert config.load_config() == config.load_config(config.ROOT / config.DEFAULT_CONFIG)


def test_top_level_must_be_a_mapping(tmp_path):
    path = tmp_path / "list.yaml"
    path.write_text("- a\n- b\n", encoding="utf-8")
    with pytest.raises(ValueError):
        config.load_config(path)


def test_config_is_read_only():
    cfg = config.load_config()
    with pytest.raises(ValidationError):
        cfg.toggles.vote = True  # 기동 때 읽은 설정을 요청 처리 중 바꾸지 않는다(TU-18)


def test_startup_reads_the_config_once():
    state = types.SimpleNamespace()
    asyncio.run(tutor.startup(state))
    assert state.tutor_cfg == config.load_config()


@pytest.fixture
def api_names(monkeypatch):
    # tutor 프리셋은 운영진 API 환경변수가 있어야 설정 확인을 넘는다. API는 부르지 않는다
    monkeypatch.setenv("OPENAI_API_KEY", "k")
    monkeypatch.setenv("OPENAI_MODEL", "m")
    monkeypatch.setenv("OPENAI_BASE_URL", "http://127.0.0.1:9")


def test_tutor_set_switches_the_prompt_language(api_names):
    # T3.2 비교 명령: run_local --preset tutor --tutor-set toggles.prompt_lang=en
    ap = run_local._parser()
    args = ap.parse_args(["--preset", "tutor", "--tutor-set", "toggles.prompt_lang=en"])
    cfg, overrides = run_local._tutor_config(ap, args)
    assert cfg.toggles.prompt_lang == "en" and run_local._own_state(cfg, overrides)
    state = types.SimpleNamespace()
    prepare(state, cfg)  # run_local이 덮은 설정으로 만드는 state와 같은 길(_tutor_state)
    assert state.tutor_prompt == MainCallPrompt.from_rules(lang="en")


def test_tutor_set_rejects_an_unknown_prompt_language(api_names):
    ap = run_local._parser()
    args = ap.parse_args(["--preset", "tutor", "--tutor-set", "toggles.prompt_lang=fr"])
    with pytest.raises(SystemExit):
        run_local._tutor_config(ap, args)


def test_local_run_cap_comes_from_the_config(capsys):
    ap = run_local._parser()
    args = ap.parse_args(["--preset", "reference"])
    assert args.max_calls is None  # 인자 정의 때는 설정을 읽지 않는다(#107 리뷰: --help가 깨짐)
    run_local._inputs(ap, args)
    assert args.max_calls == config.load_config().eval.max_calls
    assert f"상한 {args.max_calls}회" in capsys.readouterr().out
