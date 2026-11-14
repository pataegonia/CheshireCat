"""S2.10 부하 도구: 요청 만들기·분류·집계·접근 로그 집계 (C2 §S2.10, GR-10). API 0회."""

import json
import socket
import time

import pytest
from fastapi.testclient import TestClient

from guardrail.eval import load
from guardrail.eval.cv import load_splits
from guardrail.guard import ConstantGuard
from guardrail.server import create_app

TOKEN = "tok-load"  # noqa: S105 — 테스트용 가짜 토큰
OK = json.dumps({"input": {"decision": "allow", "category": "normal"}}).encode()


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("GUARD_CONFIG", raising=False)
    monkeypatch.setenv("TEAM_TOKEN", TOKEN)


async def _constant(cfg):
    return ConstantGuard()


def test_guard_rows_exclude_holdout():
    rows = load.guard_rows()
    split = load_splits()
    assert len(rows) == 160 and all(r.endpoint == "guard" for r in rows)
    assert not {split.cluster[r.id] for r in rows} & set(split.holdout)


def test_payloads_cycle_rows_and_inflate_big():
    rows = load.guard_rows()
    ps = load.build_payloads(rows, 600, big=3)
    assert len(ps) == 600 and len({p["session_id"] for p in ps}) == 600
    assert ps == load.build_payloads(rows, 600, big=3)  # 같은 시드 → 같은 순서
    big = [p for p in ps if len(p["messages"]) > 100]
    assert len(big) == 3
    for p in big:
        assert 900_000 < len(json.dumps(p).encode()) < 4 * 1024 * 1024  # 서버 본문 상한 아래
        assert p["messages"][0]["role"] == "user" and p["messages"][-1]["role"] == "user"


@pytest.mark.parametrize(
    ("status", "body", "want"),
    [
        (200, OK, "ok"),
        (401, b"", "http"),
        (200, b"<html>", "json"),
        (200, b'{"input": {"decision": "deny", "category": "normal"}}', "format"),
        (200, b"[]", "format"),
    ],
)
def test_classify(status, body, want):
    assert load.classify({"messages": []}, status, body) == want


def test_classify_requires_output_when_candidate_sent():
    p = {"messages": [], "candidate_answer": "답"}
    assert load.classify(p, 200, OK) == "format"
    full = json.loads(OK) | {"output": {"grounded": True, "unsupported": [], "safe": True}}
    assert load.classify(p, 200, json.dumps(full).encode()) == "ok"


def test_run_load_against_app_all_ok():
    payloads = load.build_payloads(load.guard_rows(), 600, big=2)
    with TestClient(create_app(_constant)) as c:

        def send(p):
            r = c.post("/guard", json=p, headers={"Authorization": f"Bearer {TOKEN}"})
            return r.status_code, r.content

        rep = load.run_load(send, payloads, concurrency=16)
    assert rep["requests"] == 600 and rep["counts"]["ok"] == 600
    assert rep["latency_s"]["n"] == 598 and rep["latency_big_s"]["n"] == 2
    assert rep["with_candidate"] == sum("candidate_answer" in p for p in payloads) > 0


def test_run_load_counts_slow_and_failed_sends():
    def send(p):
        i = int(p["session_id"][-4:])
        if i == 0:
            time.sleep(0.3)  # 답은 왔지만 제한을 넘겼다
        if i == 1:
            raise TimeoutError
        if i == 2:
            raise ConnectionError("refused")
        return 200, OK

    payloads = [{"session_id": f"load_{i:04d}", "messages": []} for i in range(10)]
    rep = load.run_load(send, payloads, concurrency=4, timeout_s=0.1)
    assert rep["counts"] == {"ok": 7, "timeout": 2, "http": 0, "json": 0, "format": 0, "error": 1}


def test_log_failures_counts_roles_once_per_request():
    lines = [
        "2026-11-13T00:00:00.0Z /guard 10 20 200 1.0 -",
        "2026-11-13T00:00:00.0Z /guard 10 20 200 1.0 output_judge:TimeoutError",
        "2026-11-13T00:00:00.0Z /guard 10 20 200 1.0 input_judge,output_judge",
        "2026-11-13T00:00:00.0Z /tutor 10 20 200 1.0 ValueError",
        "2026-11-13T00:00:00.0Z /guard 0 24 401 1.0 HTTPException",
        "INFO: uvicorn started",
    ]
    rep = load.log_failures(lines, requests=4, with_candidate=2)
    assert rep["guard_lines"] == 3
    assert rep["by_role"] == {"output_judge": 2, "input_judge": 1}
    assert rep["rate"] == {"output_judge": 1.0, "input_judge": 0.25}


def test_kb_bench_runs_without_api():
    rep = load.kb_bench()
    assert rep["queries"] > 200 and 0 < rep["p50_ms"] <= rep["p95_ms"]


FULL = json.dumps(
    json.loads(OK) | {"output": {"grounded": True, "unsupported": [], "safe": True}}
).encode()


def _main_out(monkeypatch, capsys, argv):
    monkeypatch.setattr(load, "http_sender", lambda url, token: lambda p: (200, FULL))
    load.main(["--n", "3", "--concurrency", "2", *argv])
    return capsys.readouterr()


def test_main_warns_without_access_log(monkeypatch, capsys):
    assert "--access-log" in _main_out(monkeypatch, capsys, []).err


def test_main_no_warning_with_access_log(monkeypatch, capsys, tmp_path):
    log = tmp_path / "access.log"
    log.write_text("2026-11-13T00:00:00.0Z /guard 10 20 200 1.0 -\n" * 3, "utf-8")
    assert _main_out(monkeypatch, capsys, ["--access-log", str(log)]).err == ""


def _closed_url() -> str:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    return f"http://127.0.0.1:{port}/guard"  # 닫힌 로컬 포트


def test_main_exits_1_when_all_requests_fail(capsys, tmp_path):
    out = tmp_path / "r.json"
    args = ["--url", _closed_url(), "--n", "3", "--concurrency", "2", "--out", str(out)]
    with pytest.raises(SystemExit) as e:
        load.main(args)
    assert e.value.code == 1
    rep = json.loads(out.read_text("utf-8"))
    assert json.loads(capsys.readouterr().out) == rep
    assert rep["all_ok"] is False and rep["access_log"] is False
    assert rep["counts"]["ok"] == 0 and rep["latency_ok_s"] is None
    assert rep["latency_s"]["n"] == 3  # 기존 키는 실패 요청도 포함


def test_main_all_ok_flags(monkeypatch, capsys, tmp_path):
    rep = json.loads(_main_out(monkeypatch, capsys, []).out)
    assert rep["all_ok"] is True and rep["access_log"] is False
    assert rep["latency_ok_s"]["n"] == 3
    log = tmp_path / "a.log"
    log.write_text("", "utf-8")
    assert (
        json.loads(_main_out(monkeypatch, capsys, ["--access-log", str(log)]).out)["access_log"]
        is True
    )


def test_latency_ok_excludes_failed_requests():
    def send(p):
        if p["session_id"].endswith("0"):
            raise ConnectionError("refused")
        return 200, OK

    payloads = [{"session_id": f"load_{i:04d}", "messages": []} for i in range(10)]
    rep = load.run_load(send, payloads, concurrency=4)
    assert rep["latency_s"]["n"] == 10 and rep["latency_ok_s"]["n"] == 9


def test_main_missing_access_log_sends_nothing(monkeypatch, capsys, tmp_path):
    sent = []
    monkeypatch.setattr(load, "http_sender", lambda url, token: lambda p: sent.append(p))
    out = tmp_path / "r.json"
    with pytest.raises(SystemExit) as e:
        load.main(["--n", "3", "--access-log", str(tmp_path / "nope.log"), "--out", str(out)])
    assert e.value.code == 2 and sent == [] and not out.exists()
    err = capsys.readouterr().err
    assert "nope.log" in err and len(err.strip().splitlines()) == 1


def test_main_flags_log_mismatch(monkeypatch, capsys, tmp_path):
    log = tmp_path / "a.log"
    line = "2026-11-13T00:00:00.0Z /guard 10 20 200 1.0 -\n"
    log.write_text(line, "utf-8")
    res = _main_out(monkeypatch, capsys, ["--access-log", str(log)])
    assert json.loads(res.out)["log_mismatch"] is True and "1" in res.err
    log.write_text(line * 3, "utf-8")
    res = _main_out(monkeypatch, capsys, ["--access-log", str(log)])
    assert json.loads(res.out)["log_mismatch"] is False
