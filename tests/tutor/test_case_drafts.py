"""점검 세트 초안 생성(TU-16·TU-19, #80): 운영진 nano를 부르는 대신 가짜로 바꿔 시험한다.

여기의 문항 내용은 테스트용 자리표시뿐이다(실제 초안은 런박스에서 nano가 쓴다).
"""

import json
import random
from collections import Counter
from types import SimpleNamespace

import pytest

from integration.actions import load_actions
from tutor.eval import case_drafts as cd
from tutor.eval import team_cases
from tutor.eval.grader import KB_DIR, PUBLIC_DIR, score_local
from tutor.runtime.config import ROOT, load_config

KB = score_local.load_kb_dir(KB_DIR)
PUBLIC = score_local._read_jsonl(PUBLIC_DIR / "tutor.jsonl")
UNSEEN = team_cases.uncovered_sections(PUBLIC, KB)
TABLE = load_actions()


def case(text="질문", category="normal", action="answer", evidence=(), roles=("user",)):
    messages = [cd.DraftMessage(role=r, content=f"{text}-{i}") for i, r in enumerate(roles)]
    return cd.DraftCase(
        messages=messages, category=category, action=action, evidence=list(evidence)
    )


def test_jobs_cover_every_axis_and_use_the_unseen_sections():
    assert set(cd.AXIS_TASKS) == set(team_cases.AXES)
    jobs = cd.plan_jobs(15, random.Random(0), KB, UNSEEN)
    assert len(jobs) == 5 * 3 == cd.expected_calls(15)  # 축 5개 × 15문항 ÷ 5
    assert cd.expected_calls(6) == 5 * 2  # 5개씩 묶으니 올림
    by_axis = {axis: [j for j in jobs if j.axis == axis] for axis in team_cases.AXES}
    assert all(len(v) == 3 for v in by_axis.values())
    unseen_jobs = by_axis["unseen_section"]
    assert [j.sections[0] for j in unseen_jobs] == UNSEEN[:3]
    assert sorted(sid for j in unseen_jobs for sid in j.sections) == UNSEEN  # 7개 모두
    others = [j for j in jobs if j.axis != "unseen_section"]
    assert all(len(set(j.sections)) == 2 for j in others)  # 두 섹션(mixed는 비교)
    assert all(
        len({sid[: sid.rindex("-")] for sid in j.sections}) == 1 for j in others
    )  # 같은 단원
    assert all(sid in KB for j in jobs for sid in j.sections)
    assert cd.plan_jobs(15, random.Random(0), KB, UNSEEN) == jobs  # 시드가 같으면 같다


def test_more_calls_than_unseen_sections_reuse_them():
    jobs = cd.plan_jobs(50, random.Random(0), KB, UNSEEN)
    unseen_jobs = [j for j in jobs if j.axis == "unseen_section"]
    assert len(unseen_jobs) == 10 and all(len(j.sections) == 1 for j in unseen_jobs)
    assert {j.sections[0] for j in unseen_jobs} == set(UNSEEN)


def test_prompt_carries_the_table_and_the_sections():
    job = cd.Job("mixed", ("FIN-03-01", "FIN-03-02"))
    system, user = cd.messages_for(job, KB, TABLE)
    assert (system["role"], user["role"]) == ("system", "user")
    assert all(category in system["content"] for category in TABLE)
    assert TABLE["harmful_content"].variants[0].when in system["content"]  # 표에서 읽는다
    assert "FIN-03-02" in user["content"] and KB["FIN-03-01"][:40] in user["content"]


def test_no_public_question_reaches_any_prompt():
    jobs = [cd.Job(axis, tuple(KB)) for axis in team_cases.AXES]  # 모든 축 × 모든 섹션
    prompt = "".join(m["content"] for job in jobs for m in cd.messages_for(job, KB, TABLE))
    questions = {m["content"] for row in PUBLIC for m in row["messages"] if m["role"] == "user"}
    assert not [q for q in questions if len(q) >= 12 and q in prompt]  # 공개 세트 밖 변형(TU-16)


def test_assemble_keeps_valid_drafts_and_counts_what_it_drops():
    jobs = [cd.Job("paraphrase", ("PY-01-01", "PY-01-03")), cd.Job("premise", ("FIN-01-02",))]
    blank = cd.DraftCase(
        messages=[cd.DraftMessage(role="user", content="  ")],
        category="low_signal_input",
        action="clarify",
        evidence=[],
    )
    batches = [
        cd.DraftBatch(
            cases=[
                case("a", evidence=["PY-01-01", "NOPE-99", "PY-01-02", "PY-01-01"]),
                case("b", category="off_topic", action="answer"),  # 표와 다른 action
                case("c", roles=("user", "assistant")),  # 마지막이 user가 아님
                case("a", evidence=["PY-01-01"]),  # 마지막 학습자 발화가 겹침
                case("d", "jailbreak_injection", "refuse", ["PY-01-03"]),  # 인용 never 범주
                case("e", "harmful_content", "redirect", roles=("user", "assistant", "user")),
                case("f", "answer_seeking", "scaffold", ["PY-01-02"]),  # 준 섹션 근거가 없음
                case("g", "low_signal_input", "clarify", roles=("user", "user")),  # 교대하지 않음
                blank,  # 빈 발화
            ]
        ),
        None,  # 호출 실패
    ]
    rows, dropped = cd.assemble(list(zip(jobs, batches, strict=True)), KB, TABLE)
    assert [r["id"] for r in rows] == [f"draft-paraphrase-00{n}" for n in (1, 2, 3)]
    # 근거는 그 호출에 준 섹션만, 겹침 없이 남긴다(준 적 없는 PY-01-02와 모르는 ID는 뺀다)
    assert rows[0]["labels"] == {"action": "answer", "category": "normal", "evidence": ["PY-01-01"]}
    assert rows[1]["labels"]["evidence"] == []
    assert [m["role"] for m in rows[2]["messages"]] == ["user", "assistant", "user"]
    assert dropped == {
        "표와 다른 action": 1,
        "형식": 3,
        "근거 없음": 1,
        "겹침": 1,
        "호출 실패": 1,
    }


def _fake_llm(batch, *, fail_first=False, on_call=None):
    calls = Counter()

    async def complete(role, messages, schema, *, deadline, timeout_s, retries):
        assert schema is cd.DraftBatch and [m["role"] for m in messages] == ["system", "user"]
        assert retries == cd.RETRIES and timeout_s == cd.TIMEOUT_S
        calls[role] += 1
        if on_call is not None:
            on_call()
        if batch is None or (fail_first and sum(calls.values()) == 1):
            return SimpleNamespace(parsed=None, error="bad_json", finish_reason="length")
        return SimpleNamespace(parsed=batch, error=None, finish_reason="stop")

    return SimpleNamespace(complete=complete, calls=calls, max_calls=None)


@pytest.fixture
def api_env(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "k")
    monkeypatch.setenv("OPENAI_MODEL", "m")
    monkeypatch.setenv("OPENAI_BASE_URL", "http://127.0.0.1:9")


@pytest.mark.usefixtures("api_env")
def test_cli_writes_loadable_drafts_and_prints_numbers_only(monkeypatch, tmp_path, capsys):
    batch = cd.DraftBatch(cases=[case(f"본문{i}", evidence=["PY-01-01"]) for i in range(5)])
    llm = _fake_llm(batch, fail_first=True)
    configs = []
    monkeypatch.setattr(cd, "make_llm", lambda cfg: configs.append(cfg) or llm)
    monkeypatch.setattr(cd, "plan_jobs", lambda *a: [cd.Job("paraphrase", ("PY-01-01",))] * 5)
    out = tmp_path / "drafts.jsonl"
    assert cd.main(["--out", str(out), "--per-axis", "5"]) == 0
    assert configs[0].llm.max_completion_tokens == cd.MAX_TOKENS
    assert sum(llm.calls.values()) == 5 and llm.max_calls == 10  # 축마다 1회, 다시 보내기 몫까지
    rows = team_cases.load_cases(out, KB)  # 점검 세트 검사를 그대로 통과한다
    assert len(rows) == 5  # 같은 가짜 묶음이라 뒤 호출의 초안은 마지막 발화가 겹쳐 빠진다
    shown = capsys.readouterr().out
    assert "nano 예상 호출 5회" in shown and "nano 호출 5회" in shown
    assert "호출 실패 까닭: bad_json 1" in shown and "잘린 응답 1개" in shown
    assert "본문" not in shown


@pytest.mark.usefixtures("api_env")
def test_cli_writes_nothing_when_every_call_fails(monkeypatch, tmp_path):
    monkeypatch.setattr(cd, "make_llm", lambda cfg: _fake_llm(None))
    out = tmp_path / "drafts.jsonl"
    assert cd.main(["--out", str(out), "--per-axis", "5"]) == 1
    assert not out.exists()


@pytest.mark.usefixtures("api_env")
def test_cli_never_overwrites_a_file_that_appears_while_running(monkeypatch, tmp_path):
    out = tmp_path / "drafts.jsonl"
    batch = cd.DraftBatch(cases=[case(evidence=["PY-01-01"])])
    llm = _fake_llm(batch, on_call=lambda: out.write_text("다른 실행", encoding="utf-8"))
    monkeypatch.setattr(cd, "make_llm", lambda cfg: llm)
    monkeypatch.setattr(cd, "plan_jobs", lambda *a: [cd.Job("paraphrase", ("PY-01-01",))])
    assert cd.main(["--out", str(out), "--per-axis", "5"]) == 1
    assert out.read_text(encoding="utf-8") == "다른 실행"


def test_cli_checks_inputs_before_calling(monkeypatch, tmp_path, capsys):
    out = tmp_path / "x.jsonl"
    inside = ROOT / "tutor" / "eval" / "team_cases" / "drafts.jsonl"
    monkeypatch.setattr(cd, "make_llm", lambda cfg: pytest.fail("검사 전에는 부르지 않는다"))

    def stops(argv, reason):  # 멈추고, 그 까닭으로 멈췄다
        with pytest.raises(SystemExit):
            cd.main(argv)
        assert reason in capsys.readouterr().err

    env = {"OPENAI_API_KEY": "k", "OPENAI_MODEL": "m", "OPENAI_BASE_URL": "http://127.0.0.1:9"}
    for missing in env:  # 운영진 API 설정이 하나라도 없으면(주소가 없으면 SDK 기본 주소로 나간다)
        for key, value in env.items():
            monkeypatch.setenv(key, value)
        monkeypatch.delenv(missing)
        stops(["--out", str(out)], missing)
    monkeypatch.setenv("OPENAI_BASE_URL", env["OPENAI_BASE_URL"])
    stops(["--out", str(out), "--per-axis", "100000"], "상한")
    stops(["--out", str(out), "--per-axis", "0"], "1 이상")
    stops(["--out", str(tmp_path / "없는 폴더" / "x.jsonl")], "있는 폴더 안의 새 파일")
    stops(["--out", str(inside)], "저장소 밖")
    assert not out.exists() and not inside.exists()
    out.write_text("이전 초안", encoding="utf-8")
    stops(["--out", str(out)], "있는 폴더 안의 새 파일")  # 있는 파일은 덮지 않는다
    assert out.read_text(encoding="utf-8") == "이전 초안"


def test_real_client_sends_the_draft_token_cap(monkeypatch):
    monkeypatch.setenv("OPENAI_MODEL", "m")
    llm = cd.make_llm(load_config(overrides={"llm.max_completion_tokens": cd.MAX_TOKENS}))
    assert llm.model == "m" and llm.params["max_completion_tokens"] == cd.MAX_TOKENS


def test_written_rows_are_json_lines(tmp_path):
    rows = [{"id": "draft-x-001", "endpoint": "tutor", "axis": "premise", "text": "한글"}]
    path = tmp_path / "o.jsonl"
    cd.write_rows(path, rows)
    assert path.read_bytes().count(b"\r") == 0  # 줄 끝은 \n
    text = path.read_text(encoding="utf-8")
    assert "한글" in text and [json.loads(line) for line in text.splitlines()] == rows
    with pytest.raises(FileExistsError):
        cd.write_rows(path, rows)
