"""튜터 설정(TU-14): 루트 config/tutor.yaml을 가드 GR-11과 같은 형식으로 읽는다."""

import asyncio
import os
import types
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

import tutor
from tutor.eval import run_local
from tutor.runtime import config


def repo_raw():
    """테스트 기준 설정(conftest: 동결 토글을 끈 저장소 설정)의 원본 매핑."""
    return yaml.safe_load(Path(os.environ[config.CONFIG_ENV]).read_text(encoding="utf-8"))


def write(tmp_path, data, name="tutor.yaml"):
    path = tmp_path / name
    path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    return path


def test_repo_config_has_the_prd_defaults():
    cfg = config.load_config(config.ROOT / config.DEFAULT_CONFIG)  # 저장소 파일 그대로
    toggles = cfg.toggles
    assert cfg.mode == "v2"
    assert toggles.action_from_category is False  # TU-06 기본 끔
    # TU-09·TU-10: 가드 judge_output·classify는 #170(S2.11) 뒤 런박스 켬/끔 비교 전까지 끔(11/14)
    assert toggles.verify == "off"
    assert toggles.guard_classify == "off"
    # TU-10 합치기 신호 조건 기본 끔(3회 비교 뒤 정함)
    assert toggles.jailbreak_needs_signal is False
    assert (toggles.embedding, toggles.vote) == (False, False)  # TU-13
    # 실험(exp/tutor-rerank) 기본 끔. 값이 bool에서 "off" | context | top1로 바뀌었다
    assert toggles.rerank == "off"
    assert toggles.english_embedding is True  # 11/19 켬(실제 제출 5대5, #242 결과 문서 11절)
    assert toggles.max_citations == 1  # TU-07 기본 1
    assert toggles.evidence_check is False  # #162 근거 대조 칸 기본 끔(3회 비교 뒤 정함)
    assert toggles.cite_top1 is True  # TU-07 검색 1위 인용 더하기(11/15 런박스 짝 비교로 켬)
    assert toggles.top1_dialog_query is True  # 동결 설정(11/18): 규칙 A 질의로 1위 더하기 켬
    assert toggles.delegation_override is True  # 동결 설정(11/18): 대행 덮기 켬
    assert toggles.fallback_quote is True  # 동결 설정(11/18): 자료 인용 답 켬(T 관문)
    assert toggles.intent_override == "serial"  # 동결 설정(11/18): T serial
    assert toggles.quote_append is True  # 동결 설정(11/18): 자료 문장 덧붙이기 켬(#225)
    assert toggles.quote_from_top1 is True  # 동결 설정(11/18): 덧붙일 문장을 더한 1위 섹션에서
    assert toggles.oom_gate == "named_term"  # 동결 설정(11/18): 이름 붙은 말이 KB에 없을 때만 E
    assert toggles.oom_schedule == "after_intent_fresh"  # 동결 설정(11/18): E를 T 뒤에
    assert cfg.llm.reasoning_effort == "low"  # TU-13
    # 주 호출 상한 = 운영진 프록시 상한(11/14 실측: 1600은 length로 잘린 답이 기본값으로 나감)
    assert cfg.llm.max_completion_tokens == 2000  # 가드 판정 호출 상한과 따로
    assert cfg.eval.max_calls == 5000  # TU-14


def test_main_call_timeout_uses_the_budget_headroom():
    # TU-13: 주 호출 18초(11/15 점검 세트 실측: 15초에서 비교·계산 질문 4/98이 시간 초과로 기본값
    # clarify). 요청 예산(가드 설정 budget_s × 튜터 budget_scale) 안에서 검색·게이트 대기 몫
    # 2초 이상을 남긴다. 가드가 budget_s를 15 아래로 내리면 이 테스트가 알려 준다
    from guardrail.config import load_config as load_guard_config

    from tutor.runtime.budget import backstop_s, budget_s

    main = config.load_config().llm.timeout_s.main
    state = types.SimpleNamespace(cfg=load_guard_config(), tutor_cfg=config.load_config())
    budget = budget_s(state)
    assert main == 18
    assert main + 2 <= budget
    assert backstop_s(budget) < 30  # 안전망까지 SPEC §3 요청당 30초 안


def test_repo_config_keeps_the_tutor_budget_at_24s():
    from guardrail.config import load_config as load_guard_config

    from tutor.runtime.budget import budget_s

    cfg = config.load_config(config.ROOT / config.DEFAULT_CONFIG)
    assert cfg.budget_scale == 1.0  # 실험: 가드 budget_s 24 × 1.0
    guard = load_guard_config()
    assert guard.budget_s == 24
    assert budget_s(types.SimpleNamespace(cfg=guard, tutor_cfg=cfg)) == pytest.approx(24)
    scaled = config.load_config(overrides={"budget_scale": 1.2})  # 26초에서 자른다
    assert budget_s(types.SimpleNamespace(cfg=guard, tutor_cfg=scaled)) == pytest.approx(26)


@pytest.mark.parametrize(
    ("value", "ok"),
    [(1.0, True), (1.5, True), (0.99, False), (1.51, False), (0, False), (-1, False)],
)
def test_budget_scale_must_be_in_range(value, ok):
    if ok:
        assert config.load_config(overrides={"budget_scale": value}).budget_scale == value
    else:
        with pytest.raises(ValidationError):
            config.load_config(overrides={"budget_scale": value})


@pytest.mark.parametrize("value", [True, "1.2"])
def test_budget_scale_is_strict(value):
    with pytest.raises(ValidationError):
        config.load_config(overrides={"budget_scale": value})


def test_budget_scale_key_is_required_and_budget_s_is_gone(tmp_path):
    data = repo_raw()
    del data["budget_scale"]
    with pytest.raises(ValidationError):
        config.load_config(write(tmp_path, data))
    data["budget_scale"] = 1.2
    data["budget_s"] = 24  # 튜터 전용 절대 예산 키는 없다(모르는 키)
    with pytest.raises(ValidationError):
        config.load_config(write(tmp_path, data))


def test_repo_config_has_the_hedge_and_intent_wait_limits():
    timeouts = config.load_config(config.ROOT / config.DEFAULT_CONFIG).llm.timeout_s
    assert (timeouts.main_hedge, timeouts.intent_wait) == (12, 8)
    assert timeouts.main_hedge < timeouts.main  # 겹쳐 보내기는 켬(11/19, 실제 제출 판과 맞춤)
    assert timeouts.intent_wait >= timeouts.intent  # T 대기 상한은 끔(11/18 모의 부하: 가드 지연)


@pytest.mark.parametrize("key", ["main_hedge", "intent_wait"])
def test_hedge_and_intent_wait_are_required_and_positive(tmp_path, key):
    data = repo_raw()
    del data["llm"]["timeout_s"][key]
    with pytest.raises(ValidationError):
        config.load_config(write(tmp_path, data))
    for bad in (0, -1):
        with pytest.raises(ValidationError):
            config.load_config(overrides={f"llm.timeout_s.{key}": bad})


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
        ("toggles.cite_top1", "yes"),
        ("toggles.top1_dialog_query", "yes"),
        ("toggles.english_embedding", "yes"),
        ("toggles.english_embedding", 1),
        ("toggles.quote_from_top1", "yes"),
        ("toggles.delegation_override", "yes"),
        ("toggles.fallback_quote", "yes"),
        ("toggles.intent_override", True),  # off | parallel | serial만 받는다
        ("toggles.intent_override", "on"),
        ("toggles.signals", 1),
    ],
)
def test_invalid_values_fail_at_load(dotted, value):
    with pytest.raises(ValidationError):
        config.load_config(overrides={dotted: value})


def test_top1_dialog_query_key_is_required_and_loads_both_values(tmp_path):
    data = repo_raw()
    for value in (True, False):
        data["toggles"]["top1_dialog_query"] = value
        loaded = config.load_config(write(tmp_path, data))
        assert loaded.toggles.top1_dialog_query is value
    del data["toggles"]["top1_dialog_query"]
    with pytest.raises(ValidationError):
        config.load_config(write(tmp_path, data))


def test_english_embedding_key_is_required_and_loads_both_values(tmp_path):
    data = repo_raw()
    data["toggles"]["oom_gate"] = "named_term"  # english_embedding은 E가 켜져 있어야 한다
    for value in (True, False):
        data["toggles"]["english_embedding"] = value
        loaded = config.load_config(write(tmp_path, data))
        assert loaded.toggles.english_embedding is value
    del data["toggles"]["english_embedding"]
    with pytest.raises(ValidationError):
        config.load_config(write(tmp_path, data))


@pytest.mark.parametrize("classify", ["serial", "parallel"])
def test_serial_intent_needs_classify_off(classify):
    overrides = {"toggles.intent_override": "serial", "toggles.guard_classify": classify}
    with pytest.raises(ValidationError, match="guard_classify off"):
        config.load_config(overrides=overrides)
    ok = {"toggles.intent_override": "parallel", "toggles.guard_classify": classify}
    assert config.load_config(overrides=ok).toggles.intent_override == "parallel"


def test_intent_with_classify_and_verify_exceeds_the_call_limit():
    # 주 호출 1(재시도 끔) + classify 최대 2 + T + judge_output = 5회 > TU-13 절대 4회
    over = {
        "toggles.intent_override": "parallel",
        "toggles.guard_classify": "parallel",
        "toggles.verify": "guard",
    }
    with pytest.raises(ValidationError, match="TU-13"):
        config.load_config(overrides=over)
    for drop in ("toggles.guard_classify", "toggles.verify"):
        ok = {**over, drop: "off"}
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
    monkeypatch.delenv(config.CONFIG_ENV)  # 테스트 기준 설정(conftest) 대신 저장소 기본 파일
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


def test_english_embedding_needs_the_oom_gate():
    on = {"toggles.english_embedding": True}
    gated = config.load_config(overrides={**on, "toggles.oom_gate": "named_term"})
    assert gated.toggles.english_embedding is True
    with pytest.raises(ValueError, match="english_embedding"):
        config.load_config(overrides={**on, "toggles.oom_gate": "off"})


def test_oom_schedule_key_is_required_and_loads_all_values(tmp_path):
    data = repo_raw()
    for value in ("with_request", "after_intent", "after_intent_fresh"):
        data["toggles"]["oom_schedule"] = value
        assert config.load_config(write(tmp_path, data)).toggles.oom_schedule == value
    del data["toggles"]["oom_schedule"]
    with pytest.raises(ValidationError):
        config.load_config(write(tmp_path, data))
