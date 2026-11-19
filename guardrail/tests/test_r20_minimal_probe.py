"""scripts/r20 실험 도구: 가짜 전송으로 run을, 지어낸 기록으로 분석 함수를 확인한다(API 0회)."""

import html
import importlib.util
import json
import re
import sys
from pathlib import Path
from typing import get_args

import pytest
from fakes import BadRequest, FakeLLM, Ok, RateLimited
from test_prompts_en import PUBLIC, _windows

R20 = Path(__file__).resolve().parents[1] / "scripts" / "r20"


def _load(name: str):
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, R20 / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod  # minimal_probe가 같은 이름으로 다시 import한다
    spec.loader.exec_module(mod)
    return mod


pp = _load("probe_prompts")
pa = _load("probe_analysis")
mp = _load("minimal_probe")

BLOCK = frozenset({"jailbreak_injection", "harmful_content", "academic_dishonesty", "off_topic"})
JB, NORMAL, AD = "jailbreak_injection", "normal", "academic_dishonesty"


# --- 프롬프트 ---


@pytest.mark.parametrize("name", sorted(pp.ALL))
def test_prompts_do_not_overlap_public_rows(name):
    mine = _windows(pp.ALL[name])
    seen = set()
    for path in PUBLIC.glob("*.jsonl"):
        for line in path.read_text(encoding="utf-8").splitlines():
            row = json.loads(line)
            texts = [m["content"] for m in row.get("messages", [])]
            texts.append(row.get("candidate_answer") or "")
            for text in texts:
                seen |= _windows(text) & mine
    assert not seen, sorted(seen)[:3]


def test_prompts_carry_no_row_ids():
    ids = {x for v in pa.PERSISTENT.values() for x in v}
    for name, text in pp.ALL.items():
        assert "pub-" not in text and not any(i in text for i in ids), name


def test_guide_is_long_enough_to_cache_and_shared_byte_for_byte():
    assert len(pp.GUIDE) / 4 >= 1100  # 영어 글은 토큰당 약 4자. 자동 캐시는 1024토큰부터
    shared = [p for p in mp.PROBES.values() if p.group in ("rubric", "panel", "chair")]
    assert len(shared) == 2 + 13 + 2
    assert {p.system for p in shared} == {pp.GUIDE}
    assert {p.schema for p in shared if p.group == "panel"} == {mp.SpecOut}  # 스키마도 하나


def test_specialist_values_match_between_prompt_schema_and_analysis():
    assert list(pp.SPECIALISTS) == list(pa.SPECIALISTS) and set(pa.SPEC_TARGET) == set(
        pa.SPECIALISTS
    )
    allowed = set(get_args(mp.SpecOut.model_fields["verdict"].annotation))
    for role, values in pa.SPECIALISTS.items():
        for v in values:
            assert f"`{v}`" in pp.SPECIALISTS[role], (role, v)
            assert v in allowed, (role, v)
        assert pa.SPEC_TARGET[role][0] <= set(values)


def test_select_probes_groups_and_unknown():
    names = [p.name for p in mp.select_probes("panel", "low")]
    assert names == [*pp.SPECIALISTS, "chair_low"]
    assert [p.name for p in mp.select_probes("rubric,override", "low")] == [
        "rubric_full", "rubric_steps", "override",
    ]  # fmt: skip
    assert len(mp.select_probes("battery,output", "low")) == 12
    with pytest.raises(ValueError, match="nope"):
        mp.select_probes("nope", "low")


def test_medium_uses_2000_tokens_and_20s_read_and_effort_on_common_block():
    llm = mp.effort_config("medium", 8).llm
    assert (llm.reasoning_effort, llm.max_completion_tokens, llm.timeout_s.read) == (
        "medium",
        2000,
        20,
    )
    assert llm.per_role == {} and llm.gate == 8
    assert mp.effort_config("minimal", 8).llm.reasoning_effort == "minimal"


# --- run(가짜 전송) ---


def _lines(out: Path) -> list[dict]:
    return [json.loads(x) for x in (out / "probes.jsonl").read_text("utf-8").splitlines()]


def test_run_panel_fake_stores_every_call_then_resumes(tmp_path, capsys):
    argv = [
        "run",
        "--out",
        str(tmp_path),
        "--probes",
        "panel",
        "--limit",
        "2",
        "--reps",
        "2",
        "--fake",
    ]
    assert mp.main(argv) == 0
    lines = _lines(tmp_path)
    assert len(lines) == 2 * 2 * 14
    chairs = [r for r in lines if r["probe"] == "chair_low"]
    specs = [r for r in lines if r["probe"] != "chair_low"]
    assert len(chairs) == 4 and {r["effort"] for r in chairs} == {"low"}
    assert {r["effort"] for r in specs} == {"minimal"}
    assert max(r["seq"] for r in specs) < min(r["seq"] for r in chairs)  # 의장은 전문가 뒤
    first = min(lines, key=lambda r: r["seq"])
    assert all(r["t_s"] >= first["t_s"] for r in lines)  # 첫 호출이 혼자 먼저 나간다
    assert all(r["prompt_tokens"] > 1100 and r["cached_tokens"] == 1024 for r in specs)
    assert set(lines[0]) >= {"id", "probe", "rep", "answer", "error", "finish_reason", "latency_ms"}
    meta = json.loads((tmp_path / "meta.json").read_text("utf-8"))
    assert meta["llm"]["minimal"]["params"]["reasoning_effort"] == "minimal"
    assert mp.main(argv) == 0  # 다시 돌리면 남은 칸이 없다
    assert len(_lines(tmp_path)) == len(lines)
    assert "보낼 칸이 없다" in capsys.readouterr().out


def test_run_without_yes_sends_nothing(tmp_path, capsys):
    assert mp.main(["run", "--out", str(tmp_path / "x"), "--limit", "1"]) == 0
    assert not (tmp_path / "x").exists()
    assert "--yes" in capsys.readouterr().out


def test_run_refuses_to_mix_efforts_in_one_folder(tmp_path):
    base = ["run", "--out", str(tmp_path), "--probes", "override", "--limit", "1", "--reps", "1"]
    assert mp.main([*base, "--fake"]) == 0
    assert mp.main([*base, "--fake", "--effort", "low"]) == mp.EXIT_USAGE


def test_battery_and_output_probes_run_on_candidate_rows(tmp_path):
    argv = ["run", "--out", str(tmp_path), "--limit", "1", "--reps", "1", "--fake"]
    assert mp.main(argv) == 0
    assert {r["probe"] for r in _lines(tmp_path)} == {*pp.BATTERY, "category", *pp.OUTPUT}


def test_start_aborts_when_proxy_rejects_the_effort(tmp_path, monkeypatch, capsys):
    fake = FakeLLM(respond=lambda k: BadRequest() if "reasoning_effort" in k else Ok())
    monkeypatch.setattr(mp, "fake_send", fake.send)
    argv = ["run", "--out", str(tmp_path), "--probes", "override", "--limit", "1", "--fake"]
    assert mp.main(argv) == mp.EXIT_START
    assert "reasoning_effort=minimal" in capsys.readouterr().out
    assert not (tmp_path / "probes.jsonl").exists()


def test_run_stops_after_a_streak_of_429(tmp_path, monkeypatch, capsys):
    calls = {"n": 0}

    def respond(_kwargs):
        calls["n"] += 1
        return Ok() if calls["n"] <= 5 else RateLimited()  # 시작 점검 5회만 통과

    monkeypatch.setattr(mp, "fake_send", FakeLLM(respond=respond).send)
    argv = ["run", "--out", str(tmp_path), "--probes", "battery", "--limit", "8", "--fake",
            "--concurrency", "1"]  # fmt: skip
    assert mp.main(argv) == mp.EXIT_STOPPED
    lines = _lines(tmp_path)
    assert len(lines) == mp.STREAK and {r["error"] for r in lines} == {"rate_limit"}
    assert "중단" in capsys.readouterr().out


def test_truncated_reply_is_recorded_as_length(tmp_path, monkeypatch):
    calls = {"n": 0}

    def respond(_kwargs):
        calls["n"] += 1
        return Ok() if calls["n"] <= 5 else Ok(content='{"answ', finish_reason="length")

    monkeypatch.setattr(mp, "fake_send", FakeLLM(respond=respond).send)
    argv = ["run", "--out", str(tmp_path), "--probes", "override", "--limit", "1", "--reps", "1",
            "--fake"]  # fmt: skip
    assert mp.main(argv) == 0
    (line,) = _lines(tmp_path)
    assert (line["error"], line["finish_reason"], line["answer"]) == ("bad_json", "length", None)
    assert pa._rates([line])["trunc"] == 1.0


# --- 분석(순수 함수) ---


def test_short_strips_the_pub_prefix():
    assert pa.short("pub-4bdbbc40") == "4bdbbc40" and pa.short("4bdbbc40ffff") == "4bdbbc40"


def test_read_baseline_accepts_flat_nested_and_votes():
    lines = [
        '{"id": "pub-aaaaaaaa", "category": "normal"}',
        '{"id": "bbbbbbbb", "response": {"input": {"decision": "block", "category": "off_topic"}}}',
        '{"row_id": "pub-cccccccc", "input": {"category": "jailbreak_injection"}}',
        '{"id": "pub-cccccccc", "pred_category": "normal"}',
        '{"id": "pub-cccccccc", "pred": "normal"}',
        '{"id": "pub-dddddddd", "decision": "block"}',  # 범주가 없으면 결정으로 채운다
        "not json",
        "",
    ]
    base, info = pa.read_baseline(lines, pa.ABBR)
    assert base == {"aaaaaaaa": NORMAL, "bbbbbbbb": "off_topic", "cccccccc": NORMAL, "dddddddd": JB}
    assert (info["lines"], info["used"], info["skipped"], info["multi"]) == (7, 6, 1, 1)


def test_auc_and_points_and_metrics():
    assert pa.auc([1, 1], [0, 0]) == 1.0 and pa.auc([0.5], [0.5]) == 0.5 and pa.auc([], [1]) is None
    assert pa.points(JB, JB, BLOCK) == 2 and pa.points("harmful_content", JB, BLOCK) == 1
    assert pa.points(NORMAL, NORMAL, BLOCK) == 1 and pa.points(JB, NORMAL, BLOCK) == 0
    gold = {"a": JB, "b": NORMAL, "c": NORMAL, "d": "answer_seeking"}
    m = pa.metrics({"a": JB, "b": JB, "c": NORMAL, "d": NORMAL}, gold, BLOCK)
    assert m == {"f1": 2 / 3, "cat": 0.5, "block_err": 1}


def _rec(i, probe, rep, **answer):
    return {"id": i, "probe": probe, "rep": rep, "effort": "minimal", "answer": answer,
            "error": None, "finish_reason": "stop", "latency_ms": 100.0 + rep}  # fmt: skip


def _spec(i, role, verdict, conf=5, reps=(0, 1, 2)):
    return [_rec(i, role, r, reason="x", verdict=verdict, confidence=conf) for r in reps]


def test_run_keeps_the_last_successful_line_and_ignores_errors():
    recs = [
        _rec("a", "override", 0, answer="no"),
        {**_rec("a", "override", 0), "answer": None, "error": "timeout"},
        _rec("a", "override", 0, answer="yes"),
    ]
    run = pa.Run(recs)
    assert run.get("override", "answer", "a", 0) == "yes"
    assert pa._rates(run.lines["override"])["timeout"] == pytest.approx(1 / 3)


def test_veto_device_and_majority():
    recs = [
        _rec("a", "override", 0, answer="no"), _rec("a", "override", 1, answer="yes"),
        _rec("a", "override", 2, answer="no"),
    ]  # fmt: skip
    run = pa.Run(recs)
    base = {"a": JB, "b": JB}
    fam = pa.device_families(run, base)["veto: 기준선 jailbreak → normal"]
    one, many = fam["no(override)"], fam["no(override)/maj"]
    assert [one("a", r) for r in range(3)] == [NORMAL, JB, NORMAL]
    assert [many("a", r) for r in range(3)] == [NORMAL] * 3
    assert one("b", 0) == JB  # 답이 없으면 기준선 그대로


def test_evaluate_counts_fixed_and_broken_per_rep():
    base = {"a": JB, "b": JB}
    gold = {"a": NORMAL, "b": JB}
    e = pa.evaluate(
        lambda i, rep: NORMAL if rep == 0 else base[i], ["a", "b"], [0, 1], base, gold, BLOCK
    )
    assert (e["fixed"], e["broken"], e["net"]) == (0.5, 0.5, 0.0)
    assert not pa.worth(e)
    assert pa.worth({"fixed": 4, "broken": 1, "net": 3}) and not pa.worth(
        {"fixed": 5, "broken": 3, "net": 2}
    )


def test_crossfit_picks_on_the_other_folds_only():
    ids = ["a", "b", "c", "d"]
    fold = {"a": 0, "b": 0, "c": 1, "d": 1}
    base = dict.fromkeys(ids, JB)
    gold = {"a": NORMAL, "b": NORMAL, "c": JB, "d": JB}
    cands = {"(안 씀)": lambda i, _r: base[i], "풀기": lambda i, _r: NORMAL}
    fn, chosen = pa.crossfit(cands, ids, [0], fold, base, gold, BLOCK)
    assert chosen == {0: "(안 씀)", 1: "풀기"}  # 0겹은 1겹만 보고(풀면 손해), 1겹은 0겹만 본다
    assert [fn(i, 0) for i in ids] == [JB, JB, NORMAL, NORMAL]


def test_half_is_deterministic_and_splits():
    ids = [f"pub-{n:08x}" for n in range(200)]
    parts = dict(pa.subsets(ids))
    assert set(parts["design"]) | set(parts["eval"]) == set(ids)
    assert not set(parts["design"]) & set(parts["eval"]) and 60 < len(parts["design"]) < 140
    assert pa.half("pub-4bdbbc40") == pa.half("pub-4bdbbc40")


def test_stratified_halves_balance_every_category():
    gold = {
        f"pub-{n:08x}": {"category": JB if n % 5 == 0 else NORMAL, "endpoint": "guard"}
        for n in range(101)
    }
    halves = pa.stratified_halves(gold)
    assert halves == pa.stratified_halves(dict(reversed(gold.items())))  # 입력 순서와 무관
    for cat in (JB, NORMAL):
        got = [halves[i] for i in gold if gold[i]["category"] == cat]
        assert 0 <= got.count("design") - got.count("eval") <= 1


def test_read_baseline_falls_back_to_decision_and_reports_keys():
    lines = [
        '{"id": "pub-aaaaaaaa", "pred_decision": "block"}',
        '{"id": "pub-bbbbbbbb", "blocked": false}',
        '{"id": "pub-cccccccc", "pred_cat": "off_topic", "pred_decision": "allow"}',
        '{"id": "pub-dddddddd", "score": 1}',
    ]
    base, info = pa.read_baseline(lines, pa.ABBR)
    assert base == {"aaaaaaaa": JB, "bbbbbbbb": NORMAL, "cccccccc": "off_topic"}  # 범주가 먼저
    assert info["decision_only"] == 2 and info["skipped"] == 1
    assert info["keys"] == ["blocked", "id", "pred_cat", "pred_decision", "score"]


def test_panel_prio_priority_thresholds_and_release():
    recs = [
        *_spec("a", "spec_injection", "attack", conf=3),
        *_spec("a", "spec_ghostwrite", "deliverable"),
        *_spec("a", "spec_tone", "tone_only"),
        *_spec("b", "spec_scope", "in_subject"),
        *_spec("c", "spec_harmful", "yes"),  # 역할 밖의 값
    ]
    run = pa.Run(recs)
    assert pa.panel_prio(run, BLOCK)("a", 0) == JB  # injection이 integrity보다 먼저
    assert pa.panel_prio(run, BLOCK, c_inj=4)("a", 0) == AD  # 확신 3 < 4면 다음 전문가
    assert pa.panel_prio(run, BLOCK, veto="tone")("a", 0) == NORMAL
    assert pa.panel_prio(run, BLOCK)("b", 0) == NORMAL
    assert pa.panel_prio(run, BLOCK)("c", 0) is None and pa.panel_prio(run, BLOCK)("zz", 0) is None


def _error_map_run():
    return pa.Run([
        *_spec("fp", "spec_quoted_vs_used", "quoted"), *_spec("fp", "spec_study_artifact", "yes"),
        *_spec("jb", "spec_quoted_vs_used", "used"), *_spec("jb", "spec_study_artifact", "yes"),
        *_spec("leak", "spec_quoted_vs_used", "absent", conf=2),
        *_spec("leak", "spec_study_artifact", "yes", conf=2),
        *_spec("ad", "spec_graded_submission", "yes", reps=(0, 1)),
        *_spec("ad", "spec_graded_submission", "no", reps=(2,)),
        *_spec("ok", "spec_graded_submission", "yes", reps=(0,)),
        *_spec("ok", "spec_graded_submission", "no", reps=(1, 2)),
    ])  # fmt: skip


def test_error_map_rules_r1_r2_r3():
    run = _error_map_run()
    base = {"fp": JB, "jb": JB, "leak": JB, "ad": NORMAL, "ok": "answer_seeking"}
    rules = pa.error_map_rules(run, base, BLOCK)
    r1, r2, r3 = (next(iter(v.values())) for v in rules.values())  # 첫 후보 = 의뢰문 그대로
    assert [r1(i, 0) for i in base] == [NORMAL, JB, NORMAL, NORMAL, "answer_seeking"]
    assert [r2(i, 2) for i in base] == [JB, JB, JB, AD, "answer_seeking"]  # 3회 중 2회 이상
    assert [r3(i, 0) for i in base] == [NORMAL, JB, NORMAL, AD, "answer_seeking"]
    strict = rules["R1 풀기(jailbreak → normal)"]["확신>=3"]
    assert strict("leak", 0) == JB and strict("fp", 0) == NORMAL
    once = rules["R2 막기(allow → academic_dishonesty)"]["확신>=1, 예 1회 이상"]
    assert once("ok", 0) == AD


def test_side_effects_and_wrong_jaccard():
    base = {"fp": JB, "leak": JB, "ok": NORMAL}
    cat = {"fp": NORMAL, "leak": JB, "ok": NORMAL}
    ids = list(base)
    release_all = lambda i, _r: NORMAL  # noqa: E731
    assert pa.side_effects(release_all, ids, [0, 1], base, cat, BLOCK) == (1.0, 0.0)
    assert pa.side_effects(lambda i, _r: AD, ids, [0], base, cat, BLOCK) == (0.0, 1.0)
    nb, nc, j = pa.wrong_jaccard(release_all, ids, [0], base, cat, BLOCK)
    assert (nb, nc, j) == (1.0, 1.0, 0.0)  # 기준선은 fp에서, 판정기는 leak에서 틀린다
    assert pa.wrong_jaccard(lambda i, _r: base[i], ids, [0], base, cat, BLOCK)[2] == 1.0


def test_arbiter_modes():
    base = {"a": JB, "b": NORMAL, "c": NORMAL}
    c = {"a": NORMAL, "b": JB, "c": "answer_seeking"}.get
    got = {m: [pa.arbiter(base, lambda i, _r: c(i), BLOCK, m)(i, 0) for i in base]
           for m in ("replace", "union", "intersection", "category")}  # fmt: skip
    assert got["replace"] == [NORMAL, JB, "answer_seeking"]
    assert got["union"] == [JB, JB, NORMAL]
    assert got["intersection"] == [NORMAL, NORMAL, NORMAL]
    assert got["category"] == [JB, NORMAL, "answer_seeking"]


def test_report_end_to_end_prints_only_prefixes_and_aggregates(tmp_path):
    out = tmp_path / "run"
    argv = ["run", "--out", str(out), "--probes", "all", "--limit", "12", "--reps", "2", "--fake"]
    assert mp.main(argv) == 0
    gold, block = mp.load_gold()
    records = _lines(out)
    ids = sorted({r["id"] for r in records})
    base = {i: (JB if n % 3 == 0 else gold[i]["category"]) for n, i in enumerate(ids)}
    run = pa.Run(records, json.loads((out / "meta.json").read_text("utf-8")), label="minimal:test")
    text = pa.report([run, run], gold, base, block)
    needles = "미리 정한 읽기|R1 풀기|R3 둘 다|chair_low|design|eval|Jaccard|rubric_full"
    for needle in f"{needles}|rubric_steps|veto|프롬프트 캐시|행당 걸린 시간".split("|"):
        assert needle in text, needle
    assert "pub-" not in text  # id는 앞 8자만
    assert "fake" not in text.replace("fake=True", "")  # 판정 글(reason·reasoning)은 싣지 않는다


def test_analyze_cli_matches_baseline_ids_with_or_without_prefix(tmp_path, capsys):
    out = tmp_path / "run"
    mp.main(
        ["run", "--out", str(out), "--probes", "panel", "--limit", "6", "--reps", "1", "--fake"]
    )
    ids = sorted({r["id"] for r in _lines(out)})
    lines = [
        json.dumps({"id": i if n % 2 else pa.short(i), "category": NORMAL})
        for n, i in enumerate(ids)
    ]
    (tmp_path / "dec.jsonl").write_text("\n".join(lines), "utf-8")
    capsys.readouterr()
    rc = mp.main(["analyze", "--dir", str(out), "--baseline", str(tmp_path / "dec.jsonl"),
                  "--out", str(tmp_path / "report.txt")])  # fmt: skip
    assert rc == 0
    (tmp_path / "bad.jsonl").write_text('{"id": "pub-zzzzzzzz", "decision": "block"}', "utf-8")
    bad = ["analyze", "--dir", str(out), "--baseline", str(tmp_path / "bad.jsonl")]
    assert mp.main(bad) == mp.EXIT_USAGE  # 맞는 행이 없으면 빈 보고서를 내지 않는다
    assert "파일에서 본 키: ['decision', 'id']" in capsys.readouterr().out
    two = ["--baseline", str(tmp_path / "dec.jsonl"), "--baseline", str(tmp_path / "dec.jsonl")]
    assert mp.main(["analyze", "--dir", str(out), *two]) == 0
    assert "줄 12" in capsys.readouterr().out  # 파일 여러 개는 이어 붙여 행마다 다수결
    assert mp.main(["analyze", "--dir", str(out), "--baseline", str(tmp_path / "dec.jsonl")]) == 0
    text = (tmp_path / "report.txt").read_text("utf-8")
    assert "개발 행과 맞은 것 6" in text and text.strip() == capsys.readouterr().out.strip()


# --- span_tighten ---


def _span_fake(tight):
    """출력 판정은 답변 앞 12자를 근거 없는 구간으로 인용하고, 줄이기는 tight(span)을 낸다."""

    def respond(k):
        name = (k.get("response_format") or {}).get("json_schema", {}).get("name")
        user = k["messages"][-1]["content"]
        if name == "OutputOut":
            answer = html.unescape(re.search(r"<answer>(.*?)</answer>", user, re.S).group(1))
            body = {
                "answer_quote": answer[:12],
                "kb_quote": "",
                "grounded": False,
                "refuses": False,
            }
            return Ok(json.dumps(body, ensure_ascii=False))
        if name == "TightOut":
            span = html.unescape(re.search(r"<span>(.*?)</span>", user, re.S).group(1))
            body = {"material_says": "absent", "tight": tight(span)}
            return Ok(json.dumps(body, ensure_ascii=False))
        return Ok()

    return FakeLLM(respond=respond).send


def _span_run(tmp_path, monkeypatch, tight):
    monkeypatch.setattr(mp, "fake_send", _span_fake(tight))
    argv = [
        "run",
        "--out",
        str(tmp_path),
        "--probes",
        "span",
        "--limit",
        "6",
        "--reps",
        "1",
        "--fake",
    ]
    assert mp.main(argv) == 0
    lines = _lines(tmp_path)
    base = {r["id"]: r["answer"] for r in lines if r["probe"] == "span_base"}
    tightened = {r["id"]: r["answer"] for r in lines if r["probe"] == "span_tighten"}
    assert len(base) == 6 and tightened and set(tightened) <= set(base)
    assert {r["effort"] for r in lines if r["probe"] == "span_base"} == {"low"}
    return base, tightened, lines


def test_span_tighten_stores_offsets_only_and_shrinks(tmp_path, monkeypatch):
    base, tightened, lines = _span_run(tmp_path, monkeypatch, lambda span: span[1:5])
    for i, t in tightened.items():
        assert t["rejected"] == 0 and t["calls"] == len(base[i]["spans"])
        for (s0, n0), (s1, n1) in zip(base[i]["spans"], t["spans"], strict=True):
            assert s0 <= s1 and s1 + n1 <= s0 + n0 and n1 == min(4, n0 - 1)  # 구간 안의 조각
    text = json.dumps(lines, ensure_ascii=False)
    assert "tight" not in text.replace("span_tighten", "") and "material_says" not in text
    assert all(
        set(r["answer"]) <= {"grounded", "spans", "unlocated", "rejected", "calls"} for r in lines
    )
    again = ["run", "--out", str(tmp_path), "--probes", "span", "--limit", "6", "--reps", "1"]
    assert mp.main([*again, "--fake"]) == 0
    assert len(_lines(tmp_path)) == len(lines)  # 다시 돌려도 더 보내지 않는다


def test_span_tighten_rejects_text_outside_the_span(tmp_path, monkeypatch):
    base, tightened, _ = _span_run(tmp_path, monkeypatch, lambda span: "구간에 없는 글")
    for i, t in tightened.items():
        assert t["spans"] == base[i]["spans"] and t["rejected"] == t["calls"]


def test_span_report_scores_before_and_after_with_the_grader_metric():
    from guardrail.eval.score import scoring

    answer = "이 적금의 금리는 연 5.0%이고 가입 한도는 100만 원입니다."
    wide, narrow = "금리는 연 5.0%이고", "5.0%"
    gold = {
        "pub-00000001": {
            "category": NORMAL,
            "endpoint": "guard",
            "answer": answer,
            "unsupported": [narrow],
        },
        "pub-00000002": {
            "category": NORMAL,
            "endpoint": "guard",
            "answer": answer,
            "unsupported": [],
        },
    }
    one, two = "pub-00000001", "pub-00000002"
    at_wide, at_narrow = [answer.find(wide), len(wide)], [answer.find(narrow), len(narrow)]
    recs = [
        _rec(one, "span_base", 0, grounded=False, spans=[at_wide]),
        _rec(one, "span_tighten", 0, spans=[at_narrow], rejected=0, calls=1),
        _rec(two, "span_base", 0, grounded=True, spans=[]),
    ]
    text = pa.span_report([pa.Run(recs)], gold, scoring.span_f1, scoring.normalize)
    before = scoring.span_f1([wide], [narrow])
    assert 0 < before < 1
    row = next(x for x in text.splitlines() if x.startswith("전체"))
    assert f"{(before + 1) / 2:.3f}→1.000" in row  # 구간 없는 행도 평균에 든다(둘 다 빈 구간 = 1.0)
    assert "1.000→1.000" in row  # 재현율은 그대로
    assert row.rstrip().endswith("예")  # +0.03 이상, 좋아진 1 / 나빠진 0
    assert answer not in text and narrow not in text  # 글은 싣지 않는다
    assert pa.span_report([pa.Run([_rec("x", "override", 0, answer="no")])], gold, None, None) == ""
