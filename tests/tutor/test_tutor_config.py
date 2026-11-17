"""튜터 설정(TU-14): 루트 config/tutor.yaml을 가드 GR-11과 같은 형식으로 읽는다."""

import asyncio
import types

import pytest
import yaml
from pydantic import ValidationError

import tutor
from tutor.eval import run_local
from tutor.runtime import config


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
    # TU-10 합치기 신호 조건 기본 끔(3회 비교 뒤 정함)
    assert toggles.jailbreak_needs_signal is False
    assert (toggles.embedding, toggles.rerank, toggles.vote) == (False, False, False)  # TU-13
    assert toggles.max_citations == 1  # TU-07 기본 1
    assert toggles.evidence_check is False  # #162 근거 대조 칸 기본 끔(3회 비교 뒤 정함)
    assert toggles.cite_top1 is True  # TU-07 검색 1위 인용 더하기(11/15 런박스 짝 비교로 켬)
    assert toggles.delegation_override is False  # TU-06 기본 끔, 동결 후보 측정에서 켠다
    assert toggles.fallback_quote is False  # TU-12 자료 인용 답 기본 끔(#201 A-2)
    assert toggles.intent_override == "off"  # T 의도 판정 기본 끔(#209, 측정용)
    assert toggles.anchor_unit is False  # TU-03 단원 닻 기본 끔(측정으로 켤지 정함)
    assert cfg.llm.reasoning_effort == "low"  # TU-13
    # 주 호출 상한 = 운영진 프록시 상한(11/14 실측: 1600은 length로 잘린 답이 기본값으로 나감)
    assert cfg.llm.max_completion_tokens == 2000  # 가드 판정 호출 상한과 따로
    assert cfg.eval.max_calls == 5000  # TU-14


def test_main_call_timeout_uses_the_budget_headroom():
    # TU-13: 주 호출 18초(11/15 점검 세트 실측: 15초에서 비교·계산 질문 4/98이 시간 초과로 기본값
    # clarify). 요청 예산(가드 설정 budget_s를 튜터가 실제로 쓰는 값) 안에서 검색·게이트 대기 몫
    # 2초 이상을 남긴다. 가드가 budget_s를 20 아래로 내리면 이 테스트가 알려 준다
    from guardrail.config import load_config as load_guard_config

    from tutor.runtime.budget import backstop_s, budget_s

    main = config.load_config().llm.timeout_s.main
    budget = budget_s(types.SimpleNamespace(cfg=load_guard_config()))
    assert main == 18
    assert main + 2 <= budget
    assert backstop_s(budget) < 30  # 안전망까지 SPEC §3 요청당 30초 안


@pytest.mark.parametrize(
    "change",
    [
        lambda d: d["toggles"].update(typo=True),
        lambda d: d.update(extra=1),
        lambda d: d["retrieval"].pop("k"),
        lambda d: d.pop("eval"),
        lambda d: d["toggles"].pop("anchor_unit"),
    ],
    ids=["unknown_nested", "unknown_top", "missing_nested", "missing_top", "missing_toggle"],
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
        ("toggles.cite_top1", "yes"),
        ("toggles.delegation_override", "yes"),
        ("toggles.fallback_quote", "yes"),
        ("toggles.anchor_unit", "yes"),
        ("toggles.anchor_unit", 1),
        ("toggles.signals", 1),
        ("toggles.intent_override", True),  # 옛 bool 값은 받지 않는다(off | parallel | serial)
        ("toggles.intent_override", "on"),
    ],
)
def test_invalid_values_fail_at_load(dotted, value):
    with pytest.raises(ValidationError):
        config.load_config(overrides={dotted: value})


@pytest.mark.parametrize("classify", ["serial", "parallel"])
def test_serial_intent_needs_classify_off(classify):
    overrides = {"toggles.intent_override": "serial", "toggles.guard_classify": classify}
    with pytest.raises(ValidationError, match="guard_classify off"):
        config.load_config(overrides=overrides)
    ok = {"toggles.intent_override": "parallel", "toggles.guard_classify": classify}
    assert config.load_config(overrides=ok).toggles.intent_override == "parallel"


@pytest.mark.parametrize(("dotted", "value"), [("toggles.vote", True), ("toggles.verify", "self")])
def test_toggles_that_were_not_built_fail_at_load(dotted, value):
    # #83 결정(11/14): 요청당 nano 절대 4회(TU-13)를 넘어 만들지 않았다. 켜도 끔처럼 돌지 않는다
    with pytest.raises(ValidationError, match="만들지 않았다"):
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
    cfg = config.load_config(overrides={"toggles.inline_cite": True, "retrieval.k": 8})
    assert cfg.toggles.inline_cite is True and cfg.retrieval.k == 8


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


def test_local_run_cap_comes_from_the_config(capsys):
    ap = run_local._parser()
    args = ap.parse_args(["--preset", "reference"])
    assert args.max_calls is None  # 인자 정의 때는 설정을 읽지 않는다(#107 리뷰: --help가 깨짐)
    run_local._inputs(ap, args)
    assert args.max_calls == config.load_config().eval.max_calls
    assert f"상한 {args.max_calls}회" in capsys.readouterr().out
