"""S2.10 부하 도구: 요청 만들기·분류·집계·접근 로그 집계 (C2 §S2.10, GR-10). API 0회."""

import json
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

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
    assert rep["counts"] == {
        "ok": 7,
        "timeout": 2,
        "redirect": 0,
        "http": 0,
        "json": 0,
        "format": 0,
        "error": 1,
    }


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


def _body(decision="allow", category="normal", **output):
    d = {"input": {"decision": decision, "category": category}}
    if output:
        d["output"] = output
    return json.dumps(d).encode()


@pytest.mark.parametrize("unsupported", ["abc", None, {"a": 1}, [""], ["a", 1], [None]])
def test_classify_rejects_bad_unsupported(unsupported):
    p = {"messages": [], "candidate_answer": "답"}
    body = _body(grounded=True, unsupported=unsupported, safe=True)
    assert load.classify(p, 200, body) == "format"


@pytest.mark.parametrize(
    ("decision", "category", "want"),
    [
        ("allow", "normal", "ok"),
        ("block", "jailbreak_injection", "ok"),
        ("block", "normal", "format"),
        ("allow", "jailbreak_injection", "format"),
        ("allow", "not_a_category", "format"),
        ("block", "not_a_category", "format"),
        ("allow", ["normal"], "format"),
    ],
)
def test_classify_checks_category_and_decision_mapping(decision, category, want):
    assert load.classify({"messages": []}, 200, _body(decision, category)) == want


def test_classify_allows_any_string_output_category():
    p = {"messages": [], "candidate_answer": "답"}  # SPEC: output.category는 아무 문자열
    d = json.loads(_body(grounded=True, unsupported=["구간"], safe=True))
    d["output"]["category"] = "whatever"
    assert load.classify(p, 200, json.dumps(d).encode()) == "ok"


# ---- S2.10 확장: 튜터 혼합·redirect·probe·서버 지연 ----

MARK = "MARK_7f3"


def _tutor_body(**over):
    d = {"answer": "답", "action": "answer", "citations": ["kb_1"]} | over
    return json.dumps(d).encode()


def _gp(i, tag="g"):
    return {"session_id": f"{tag}_{i:04d}", "messages": [{"role": "user", "content": MARK}]}


def test_tutor_rows_exclude_holdout():
    rows = load.tutor_rows()
    split = load_splits()
    assert rows and all(r.endpoint == "tutor" for r in rows)
    assert not {split.cluster[r.id] for r in rows} & set(split.holdout)


def test_mix_keeps_counts_and_order_without_endpoint_key():
    g = load.build_payloads(load.guard_rows(), 20)
    t = load.build_payloads(load.tutor_rows(), 15)
    jobs = load.mix(g, t)
    assert len(jobs) == 35
    assert [ep for ep, _ in jobs].count("guard") == 20
    assert [ep for ep, _ in jobs].count("tutor") == 15
    assert all("candidate_answer" not in p for ep, p in jobs if ep == "tutor")
    assert all("endpoint" not in p for _, p in jobs)
    assert jobs == load.mix(g, t)  # 같은 시드 → 같은 순서


@pytest.mark.parametrize(
    ("status", "body", "want"),
    [
        (200, _tutor_body(), "ok"),
        (200, _tutor_body(answer="가" * 4000), "ok"),
        (200, _tutor_body(answer="가" * 4001), "format"),
        (200, _tutor_body(action="dance"), "format"),
        (200, _tutor_body(citations=["a", 1]), "format"),
        (200, b"{broken", "json"),
        (500, b"", "http"),
        (307, b"", "redirect"),
    ],
)
def test_classify_tutor(status, body, want):
    assert load.classify_tutor(status, body) == want


def test_classify_guard_redirect():
    assert load.classify({"messages": []}, 301, b"") == "redirect"


@pytest.mark.parametrize("code", [301, 302, 303, 307, 308])
def test_http_sender_does_not_follow_redirect(code):
    """urllib은 POST의 301·302·303을 GET으로 따라간다. 채점기는 3xx를 실패로 본다."""
    hits = []

    class H(BaseHTTPRequestHandler):
        def do_POST(self):
            hits.append(self.path)
            # 본문을 읽지 않고 닫으면 Windows에서 연결 재설정이 3xx보다 먼저 온다
            self.rfile.read(int(self.headers.get("Content-Length") or 0))
            self.send_response(code)
            self.send_header("Location", "/followed")
            self.send_header("Content-Length", "0")
            self.end_headers()

        do_GET = do_POST  # 따라가면 GET으로 온다

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), H)
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    try:
        send = load.http_sender(f"http://127.0.0.1:{srv.server_port}/guard", TOKEN, 5.0)
        assert send({"messages": []}) == (code, b"")
    finally:
        srv.shutdown()
        srv.server_close()
    assert hits == ["/guard"]


def test_run_mixed_splits_by_endpoint_and_counts_redirect():
    def g_send(p):
        return (307, b"") if p["session_id"].endswith("0") else (200, OK)

    sends = {"guard": g_send, "tutor": lambda p: (200, _tutor_body())}
    jobs = [("guard", _gp(i)) for i in range(10)] + [("tutor", _gp(i, "t")) for i in range(6)]
    rep = load.run_mixed(sends, jobs, concurrency=4)
    keys = ["ok", "timeout", "redirect", "http", "json", "format", "error"]
    assert list(rep["counts"]) == keys
    assert rep["requests"] == 16 and rep["probe"] is None and rep["total_sent"] == 16
    g, t = rep["by_endpoint"]["guard"], rep["by_endpoint"]["tutor"]
    assert g["requests"] == 10 and g["counts"]["redirect"] == 1 and g["counts"]["ok"] == 9
    assert t["requests"] == 6 and t["counts"]["ok"] == 6 and t["all_ok"] is True
    assert g["latency_s"]["n"] == 10 and t["latency_s"]["n"] == 6
    for k in keys:
        assert g["counts"][k] + t["counts"][k] == rep["counts"][k]


def test_run_mixed_against_app_all_ok():
    g = load.build_payloads(load.guard_rows(), 30)
    t = load.build_payloads(load.tutor_rows(), 30)
    h = {"Authorization": f"Bearer {TOKEN}"}
    with TestClient(create_app(_constant)) as c:

        def mk(path):
            def send(p):
                r = c.post(path, json=p, headers=h)
                return r.status_code, r.content

            return send

        sends = {"guard": mk("/guard"), "tutor": mk("/tutor")}
        rep = load.run_mixed(sends, load.mix(g, t), concurrency=8)
    assert rep["requests"] == 60 and rep["counts"]["ok"] == 60 and rep["all_ok"] is True


def test_run_mixed_probe_counts_separately():
    def probe():
        time.sleep(0.01)
        return 401

    def slow(p):
        time.sleep(0.2)
        return 200, OK

    jobs = [("guard", _gp(i)) for i in range(4)]
    rep = load.run_mixed({"guard": slow}, jobs, concurrency=4, probe=probe, probe_interval_s=0.02)
    n = rep["probe"]["n"]
    assert n >= 1 and rep["probe"]["statuses"] == {"401": n}
    assert rep["requests"] == 4 and rep["total_sent"] == 4 + n
    assert rep["probe"]["p50_ms"] <= rep["probe"]["p95_ms"] <= rep["probe"]["max_ms"]


def test_log_latency_uses_only_200_lines():
    def ln(path, status, ms, extra=""):
        return f"2026-11-13T00:00:00.0Z {path} 10 20 {status} {ms} -{extra}"

    lines = [ln("/guard", 200, m) for m in (1.0, 2.0, 3.0, 4.0)]
    lines += [ln("/guard", 401, 999.0), "INFO: uvicorn started", "a b c"]
    lines += [ln("/tutor", 200, 50.0)]
    rep = load.log_latency(lines)
    assert rep["/guard"] == {"n": 4, "p50_ms": 2.5, "p95_ms": 4.0, "max_ms": 4.0}
    assert rep["/tutor"] == {"n": 1, "p50_ms": 50.0, "p95_ms": 50.0, "max_ms": 50.0}
    assert load.log_latency([])["/tutor"] == {"n": 0, "p50_ms": 0.0, "p95_ms": 0.0, "max_ms": 0.0}
    # 로거 머리말이 붙어도 끝에서 센다(log_failures와 같다)
    assert load.log_latency(["INFO guardrail.access " + ln("/guard", 200, 7.0)])["/guard"]["n"] == 1


def test_main_tutor_n_without_tutor_url_sends_nothing(monkeypatch, capsys):
    sent = []
    monkeypatch.setattr(load, "http_sender", lambda *a, **k: lambda p: sent.append(p))
    with pytest.raises(SystemExit) as e:
        load.main(["--n", "3", "--tutor-n", "5"])
    assert e.value.code == 2 and sent == []
    assert "--tutor-url" in capsys.readouterr().err


def test_mixed_report_has_no_payload_text():
    jobs = [("guard", _gp(i)) for i in range(3)] + [("tutor", _gp(i, "t")) for i in range(3)]
    sends = {"guard": lambda p: (200, OK), "tutor": lambda p: (200, _tutor_body())}
    rep = load.run_mixed(sends, jobs, concurrency=2)
    assert MARK not in json.dumps(rep, ensure_ascii=False)
