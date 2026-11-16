"""T 의도 판정 모의(harness/intent_probe). 운영진 API 없이 본다.

입력 문장은 지어낸 것이다(공개 세트 · KB 문구를 옮기지 않는다).
"""

import json
from types import SimpleNamespace

import pytest

from harness import intent_probe as ip


def rec(rid, label, gold_action, *, cat="normal", signals=()):
    return {"id": rid, "gold_category": cat, "gold_action": gold_action,
            "signals": list(signals), "label": label}  # fmt: skip


def test_load_system_strips_source_comment(tmp_path):
    path = tmp_path / "p.md"
    path.write_text("<!-- 출처 -->\n본문 첫 줄\n둘째 줄\n", encoding="utf-8")
    assert ip.load_system(path) == "본문 첫 줄\n둘째 줄"
    path.write_text("주석 없음\n본문\n", encoding="utf-8")
    with pytest.raises(ValueError):
        ip.load_system(path)


def test_shipped_prompt_loads_and_names_every_label():
    system = ip.load_system()
    for label in ip.ACTION_OF:
        assert label in system


def test_end_request_only_for_long_target_keeps_last_two_sentences():
    assert ip.end_request("짧은 질문이에요?") is None
    long = "가나다라 " * 70 + "첫 문장이다. 둘째 문장이다. 마지막 요청이다!"
    assert ip.end_request(long) == "둘째 문장이다. 마지막 요청이다!"


def test_build_user_has_blocks_and_end_request_when_long():
    toc = ip.table_of_contents({"X-01": SimpleNamespace(title="고양이 식당")})
    short = {"id": "r1", "messages": [{"role": "user", "content": "고양이 식당은 언제 열어요?"}]}
    user, signals = ip.build_user(toc, short)
    assert "<과목 목차>\n- X-01 고양이 식당\n</과목 목차>" in user
    assert "<신호>없음</신호>" in user and '판단대상="예"' in user
    assert "<끝 요청>" not in user and signals == []
    long = {"id": "r2", "messages": [{"role": "user", "content": "가나다 " * 120 + "끝 요청이다."}]}
    user, _ = ip.build_user(toc, long)
    assert "<끝 요청>" in user


def test_override_rules():
    jail = rec("a", "탈옥", "refuse")
    assert ip.override(jail, "answer", only_stronger=True, needs_signal=False) == "refuse"
    assert ip.override(jail, "answer", only_stronger=True, needs_signal=True) is None
    jail_sig = rec("a", "탈옥", "refuse", signals=["override"])
    assert ip.override(jail_sig, "answer", only_stronger=True, needs_signal=True) == "refuse"
    low = rec("b", "저신호", "clarify")
    assert ip.override(low, "refuse", only_stronger=True, needs_signal=False) is None
    assert ip.override(low, "refuse", only_stronger=False, needs_signal=False) == "clarify"
    assert ip.override(rec("c", "학습 질문", "answer"), "refuse", only_stronger=False,
                       needs_signal=False) is None  # fmt: skip
    assert ip.override(rec("d", None, "answer"), None, only_stronger=False,
                       needs_signal=False) is None  # fmt: skip
    assert ip.override(rec("e", "유해-실행", "refuse"), None, only_stronger=True,
                       needs_signal=False) == "refuse"  # fmt: skip


def test_simulate_counts_right_wrong_other_per_run():
    records = {
        "r1": rec("r1", "탈옥", "refuse", cat="jailbreak_injection"),  # answer → refuse: 맞게
        "r2": rec("r2", "정답만 요구", "answer"),  # answer → scaffold: 틀리게
        "r3": rec("r3", "무관 결과물", "clarify", cat="off_topic"),  # scaffold → redirect: 그 밖
        "r4": rec("r4", "학습 질문", "answer"),  # 덮지 않음
    }
    run = {r: {"action": a} for r, a in
           (("r1", "answer"), ("r2", "answer"), ("r3", "scaffold"), ("r4", "answer"))}  # fmt: skip
    got = ip.simulate(records, [run, run], only_stronger=True, needs_signal=False)
    assert (got["right"], got["wrong"], got["other"], got["net"]) == (1.0, 1.0, 1.0, 0.0)
    assert got["by_category"]["jailbreak_injection"] == {"right": 2}
    assert got["by_category"]["normal"] == {"wrong": 2}


def test_main_with_records_prints_simulation_without_api(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(ip, "missing_api_env", lambda: pytest.fail("API를 보면 안 된다"))
    records = [rec("r1", "탈옥", "refuse", cat="jailbreak_injection") | {"error": None,
               "latency_ms": 1200.0}]  # fmt: skip
    rpath = tmp_path / "records.json"
    rpath.write_text(json.dumps({"rows": records}, ensure_ascii=False), encoding="utf-8")
    mpath = tmp_path / "main.json"
    runs = [{"r1": {"action": "answer"}}]
    mpath.write_text(json.dumps({"report": {"cv_emitted_by_run": runs}}), encoding="utf-8")
    assert ip.main(["--main", str(mpath), "--records", str(rpath)]) == 0
    out = capsys.readouterr().out
    assert "jailbreak_injection: 탈옥 1" in out
    assert '- 강할 때만: {"right": 1.0, "wrong": 0.0, "other": 0.0, "net": 1.0' in out
    assert "탈옥은 신호 있을 때만" in out
