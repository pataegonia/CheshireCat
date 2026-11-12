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
PROXY_CEILING = 2000  # 운영진 프록시: 스트리밍이 아닌 응답의 출력 상한(넘기면 400, 11/12 런박스)


def case(text="질문", category="normal", action="answer", evidence=(), roles=("user",)):
    messages = [cd.DraftMessage(role=r, content=f"{text}-{i}") for i, r in enumerate(roles)]
    return cd.DraftCase(
        messages=messages, category=category, action=action, evidence=list(evidence)
    )


def test_jobs_cycle_forms_and_sections_per_axis():
    assert set(cd.AXIS_FORMS) == set(team_cases.AXES)
    jobs = cd.plan_jobs(15, random.Random(0), KB, UNSEEN)
    assert len(jobs) == 5 * 15 == cd.expected_calls(15)  # 호출 하나에 초안 하나
    by_axis = {axis: [j for j in jobs if j.axis == axis] for axis in team_cases.AXES}
    for axis, forms in cd.AXIS_FORMS.items():  # 꼴은 축 안에서 차례로 돈다
        assert [j.form for j in by_axis[axis]] == [forms[i % len(forms)] for i in range(15)]
    unseen_jobs = by_axis["unseen_section"]
    assert [j.sections for j in unseen_jobs] == [(UNSEEN[i % len(UNSEEN)],) for i in range(15)]
    assert {j.sections[0] for j in unseen_jobs} == set(UNSEEN)  # 7개 모두
    others = [j for j in jobs if j.axis != "unseen_section"]
    assert all(len(set(j.sections)) == 2 for j in others)  # 두 섹션(mixed는 비교)
    assert all(
        len({sid[: sid.rindex("-")] for sid in j.sections}) == 1 for j in others
    )  # 같은 단원
    assert all(sid in KB for j in jobs for sid in j.sections)
    assert cd.plan_jobs(15, random.Random(0), KB, UNSEEN) == jobs  # 시드가 같으면 같다


def test_prompt_carries_the_table_the_form_and_the_sections():
    form = cd.AXIS_FORMS["mixed"][-1]
    system, user = cd.messages_for(cd.Job("mixed", form, ("FIN-03-01", "FIN-03-02")), KB, TABLE)
    assert (system["role"], user["role"]) == ("system", "user")
    assert all(category in system["content"] for category in TABLE)
    assert TABLE["harmful_content"].variants[0].when in system["content"]  # 표에서 읽는다
    assert form in user["content"]
    assert "FIN-03-02" in user["content"] and KB["FIN-03-01"][:40] in user["content"]


def test_no_public_question_reaches_any_prompt():
    jobs = [  # 모든 축 × 모든 꼴 × 모든 섹션
        cd.Job(axis, form, tuple(KB)) for axis, forms in cd.AXIS_FORMS.items() for form in forms
    ]
    prompt = "".join(m["content"] for job in jobs for m in cd.messages_for(job, KB, TABLE))
    questions = {m["content"] for row in PUBLIC for m in row["messages"] if m["role"] == "user"}
    assert not [q for q in questions if len(q) >= 12 and q in prompt]  # 공개 세트 밖 변형(TU-16)


def test_assemble_keeps_valid_drafts_and_counts_what_it_drops():
    job = cd.Job("paraphrase", "꼴", ("PY-01-01", "PY-01-03"))
    blank = cd.DraftCase(
        messages=[cd.DraftMessage(role="user", content="  ")],
        category="low_signal_input",
        action="clarify",
        evidence=[],
    )
    drafts = [
        case("a", evidence=["PY-01-01", "NOPE-99", "PY-01-02", "PY-01-01"]),
        case("b", category="off_topic", action="answer"),  # 표와 다른 action
        case("c", roles=("user", "assistant")),  # 마지막이 user가 아님
        case("a", evidence=["PY-01-01"]),  # 마지막 학습자 발화가 겹침
        case("d", "jailbreak_injection", "refuse", ["PY-01-03"]),  # 인용 never 범주
        case("e", "harmful_content", "redirect", roles=("user", "assistant", "user")),
        case("f", "answer_seeking", "scaffold", ["PY-01-02"]),  # 준 섹션 근거가 없음
        case("g", "low_signal_input", "clarify", roles=("user", "user")),  # 교대하지 않음
        blank,  # 빈 발화
        None,  # 호출 실패
        case("PY-01-01의 내용", evidence=["PY-01-01"]),  # 대화에 섹션 ID(학생은 모른다)
        case("정답만 원해요(answer_seeking)", "answer_seeking", "scaffold", ["PY-01-01"]),  # 라벨
        case("def my_var_name(): 코드", evidence=["PY-01-01"]),  # 코드의 밑줄 이름은 메타 아님
    ]
    rows, dropped = cd.assemble([(job, draft) for draft in drafts], KB, TABLE)
    assert [r["id"] for r in rows] == [f"draft-paraphrase-00{n}" for n in (1, 2, 3, 4)]
    # 근거는 그 호출에 준 섹션만, 겹침 없이 남긴다(준 적 없는 PY-01-02와 모르는 ID는 뺀다)
    assert rows[0]["labels"] == {"action": "answer", "category": "normal", "evidence": ["PY-01-01"]}
    assert rows[1]["labels"]["evidence"] == []
    assert [m["role"] for m in rows[2]["messages"]] == ["user", "assistant", "user"]
    assert rows[3]["messages"][0]["content"].startswith("def my_var_name")
    assert dropped == {
        "표와 다른 action": 1,
        "형식": 3,
        "메타": 2,
        "근거 없음": 1,
        "겹침": 1,
        "호출 실패": 1,
    }


def test_prompt_forbids_leaking_ids_and_labels_into_the_dialog():
    for forms in cd.AXIS_FORMS.values():  # 꼴 설명에 라벨 이름이 없다(대화로 새어 나왔다)
        assert not [f for f in forms if any(name in f for name in [*TABLE, "answer", "scaffold"])]
    system, user = cd.messages_for(cd.Job("premise", "꼴", ("FIN-02-04",)), KB, TABLE)
    assert "섹션 ID" in system["content"] and "꼴 설명" in user["content"]


def _fake_llm(*, fail_first=False, fail_all=False, on_call=None):
    """TutorLLM처럼 역할별 호출 수를 센다. 호출마다 다른 초안(근거 PY-01-01)을 돌려준다."""
    calls = Counter()

    async def complete(role, messages, schema, *, deadline, timeout_s, retries):
        assert schema is cd.DraftCase and [m["role"] for m in messages] == ["system", "user"]
        assert retries == cd.RETRIES and timeout_s == cd.TIMEOUT_S
        calls[role] += 1
        n = sum(calls.values())
        if on_call is not None:
            on_call()
        if fail_all or (fail_first and n == 1):
            return SimpleNamespace(parsed=None, error="bad_json", finish_reason="length")
        draft = case(f"본문{n}", evidence=["PY-01-01"])
        return SimpleNamespace(parsed=draft, error=None, finish_reason="stop")

    return SimpleNamespace(complete=complete, calls=calls, max_calls=None)


@pytest.fixture
def api_env(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "k")
    monkeypatch.setenv("OPENAI_MODEL", "m")
    monkeypatch.setenv("OPENAI_BASE_URL", "http://127.0.0.1:9")


@pytest.fixture
def one_section_jobs(monkeypatch):
    """근거가 남도록 모든 호출에 PY-01-01을 준다."""
    jobs = [cd.Job(axis, "꼴", ("PY-01-01",)) for axis in team_cases.AXES]
    monkeypatch.setattr(cd, "plan_jobs", lambda *a: jobs)


@pytest.mark.usefixtures("api_env", "one_section_jobs")
def test_cli_writes_loadable_drafts_and_prints_numbers_only(monkeypatch, tmp_path, capsys):
    llm = _fake_llm(fail_first=True)
    configs = []
    monkeypatch.setattr(cd, "make_llm", lambda cfg: configs.append(cfg) or llm)
    out = tmp_path / "drafts.jsonl"
    assert cd.main(["--out", str(out), "--per-axis", "1"]) == 0
    assert configs[0].llm.max_completion_tokens == cd.MAX_TOKENS
    assert sum(llm.calls.values()) == 5 and llm.max_calls == 10  # 축마다 1회, 다시 보내기 몫까지
    rows = team_cases.load_cases(out, KB)  # 점검 세트 검사를 그대로 통과한다
    assert [r["axis"] for r in rows] == list(team_cases.AXES)[1:]  # 첫 호출만 실패
    shown = capsys.readouterr().out
    assert "nano 예상 호출 5회" in shown and "nano 호출 5회" in shown
    assert "호출 실패 까닭: bad_json 1" in shown and "잘린 응답 1개" in shown
    assert "본문" not in shown


@pytest.mark.usefixtures("api_env")
def test_cli_writes_nothing_when_every_call_fails(monkeypatch, tmp_path):
    monkeypatch.setattr(cd, "make_llm", lambda cfg: _fake_llm(fail_all=True))
    out = tmp_path / "drafts.jsonl"
    assert cd.main(["--out", str(out), "--per-axis", "1"]) == 1
    assert not out.exists()


@pytest.mark.usefixtures("api_env", "one_section_jobs")
def test_cli_never_overwrites_a_file_that_appears_while_running(monkeypatch, tmp_path):
    out = tmp_path / "drafts.jsonl"
    llm = _fake_llm(on_call=lambda: out.write_text("다른 실행", encoding="utf-8"))
    monkeypatch.setattr(cd, "make_llm", lambda cfg: llm)
    assert cd.main(["--out", str(out), "--per-axis", "1"]) == 1
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


def test_real_client_stays_under_the_proxy_ceiling(monkeypatch):
    assert cd.MAX_TOKENS <= PROXY_CEILING
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
