"""점검 세트 초안 생성(TU-16·TU-19, #80): 운영진 nano를 부르는 대신 가짜로 바꿔 시험한다.

여기의 문항 내용은 테스트용 자리표시뿐이다(실제 초안은 런박스에서 nano가 쓴다).
"""

import json
import random
from collections import Counter
from types import SimpleNamespace

import pytest
from guardrail.eval.cv import Split, load_splits

from integration.actions import load_actions
from tutor.eval import case_drafts as cd
from tutor.eval import case_set, run_local, team_cases
from tutor.eval.grader import KB_DIR, score_local
from tutor.runtime.config import load_config

KB = score_local.load_kb_dir(KB_DIR)
PUBLIC = case_set.public_rows()
UNSEEN = case_set.uncovered_sections(PUBLIC, KB)
TABLE = load_actions()
CV = case_set.cv_rows(PUBLIC, load_splits())
BY_ID = {row["id"]: row for row in CV}
PROXY_CEILING = 2000  # 운영진 프록시: 스트리밍이 아닌 응답의 출력 상한(넘기면 400, 11/12 런박스)


def case(text="질문 자리", category="normal", action="answer", evidence=(), roles=("user",)):
    messages = [cd.DraftMessage(role=r, content=f"{text}-{i}") for i, r in enumerate(roles)]
    return cd.DraftCase(
        messages=messages, category=category, action=action, evidence=list(evidence)
    )


def all_jobs(seeds=CV, per_axis=40):
    return cd.plan_jobs(per_axis, random.Random(0), KB, UNSEEN, seeds)


def prompt_text(jobs, seeds):
    return "".join(m["content"] for job in jobs for m in cd.messages_for(job, KB, TABLE, seeds))


def dialog_only(texts):
    """KB 원문(프롬프트에 섹션으로 들어간다)에 그대로 있는 문구를 뺀 대화 발화(12자 이상)."""
    return {t for t in texts if len(t) >= 12 and not any(t in text for text in KB.values())}


# --- 호출 계획 ---


def test_default_generate_asks_sixteen_drafts_per_axis():
    assert cd.DEFAULT_PER_AXIS == 16 and cd.expected_calls(16) == 80
    assert set(cd.AXIS_FORMS) == set(case_set.AXES)


def test_jobs_cycle_forms_and_sections_per_axis():
    jobs = cd.plan_jobs(15, random.Random(0), KB, UNSEEN, CV)
    assert len(jobs) == 5 * 15 == cd.expected_calls(15)  # 호출 하나에 초안 하나
    by_axis = {axis: [j for j in jobs if j.axis == axis] for axis in case_set.AXES}
    for axis, forms in cd.AXIS_FORMS.items():  # 꼴은 축 안에서 차례로 돈다
        assert [j.form for j in by_axis[axis]] == [forms[i % len(forms)] for i in range(15)]
    unseen_jobs = by_axis["unseen_section"]
    assert [j.sections for j in unseen_jobs] == [(UNSEEN[i % len(UNSEEN)],) for i in range(15)]
    assert {j.sections[0] for j in unseen_jobs} == set(UNSEEN)  # 7개 모두
    for axis in ("structure", "premise"):  # 같은 단원의 두 섹션, 씨앗 없음
        assert all(j.seed is None and len(set(j.sections)) == 2 for j in by_axis[axis])
        assert all(len({s.rsplit("-", 1)[0] for s in j.sections}) == 1 for j in by_axis[axis])
    assert all(sid in KB for j in jobs for sid in j.sections)
    assert cd.plan_jobs(15, random.Random(0), KB, UNSEEN, CV) == jobs  # 시드가 같으면 같다


def test_seeded_axes_use_cross_validation_rows_and_their_evidence():
    jobs = all_jobs()
    paraphrase = [j for j in jobs if j.axis == "paraphrase"]
    categories = Counter(BY_ID[j.seed]["labels"]["category"] for j in paraphrase)
    assert set(categories) == {r["labels"]["category"] for r in CV}  # 범주를 돌려 가며 고른다
    for j in paraphrase:
        assert j.sections == tuple(BY_ID[j.seed]["labels"]["evidence"])
    for j in (j for j in jobs if j.axis == "mixed"):
        seed = BY_ID[j.seed]
        assert seed["labels"]["category"] == "normal" and seed["labels"]["evidence"]
        evidence = tuple(seed["labels"]["evidence"])
        if j.form == cd.COMPARE_FORM:  # 비교 꼴은 같은 단원의 다른 섹션 하나를 더 준다
            assert j.sections[:-1] == evidence and j.sections[-1] not in evidence
            assert j.sections[-1].rsplit("-", 1)[0] == evidence[0].rsplit("-", 1)[0]
        else:
            assert j.sections == evidence
    assert all(j.seed is None for j in jobs if j.axis not in cd.SEEDED_AXES)


def test_top_up_runs_continue_the_form_rotation_and_pick_only_some_axes():
    jobs = cd.plan_jobs(2, random.Random(0), KB, UNSEEN, CV, axes=["premise"], start={"premise": 3})
    forms = cd.AXIS_FORMS["premise"]
    assert [(j.axis, j.form) for j in jobs] == [("premise", forms[1]), ("premise", forms[0])]


# --- 프롬프트 ---


def test_prompt_carries_the_table_the_form_the_seed_dialog_and_the_sections():
    seed = next(r for r in CV if len(r["messages"]) > 1 and r["labels"]["evidence"])
    job = cd.Job("paraphrase", cd.AXIS_FORMS["paraphrase"][0], ("FIN-03-01",), seed["id"])
    system, user = cd.messages_for(job, KB, TABLE, BY_ID)
    assert (system["role"], user["role"]) == ("system", "user")
    assert all(category in system["content"] for category in TABLE)
    assert TABLE["harmful_content"].variants[0].when in system["content"]  # 표에서 읽는다
    assert job.form in user["content"] and KB["FIN-03-01"][:40] in user["content"]
    assert all(m["content"] in user["content"] for m in seed["messages"])  # 씨앗 대화
    assert seed["labels"]["category"] in user["content"]
    assert seed["id"] not in user["content"] + system["content"]  # 행 id는 넣지 않는다
    assert seed["labels"]["reference_answer"] not in user["content"]  # 정답 답변도 넣지 않는다


def fake_split(every=3) -> Split:
    """공개 튜터 행마다 묶음 하나, every번째 행마다 홀드아웃인 가짜 분할."""
    cluster = {row["id"]: f"c{i}" for i, row in enumerate(PUBLIC)}
    holdout = tuple(f"c{i}" for i in range(0, len(PUBLIC), every))
    return Split("test", 0.5, 5, 0, cluster, {}, holdout)


@pytest.mark.parametrize("split", [fake_split(), fake_split(2), load_splits()])
def test_no_holdout_row_reaches_any_prompt(split):
    cv = case_set.cv_rows(PUBLIC, split)
    held = [r for r in PUBLIC if split.cluster[r["id"]] in set(split.holdout)]
    assert held and cv and len(held) + len(cv) == len(PUBLIC)
    jobs = all_jobs(cv)
    used = {j.seed for j in jobs if j.seed is not None}
    prompt = prompt_text(jobs, {r["id"]: r for r in cv})
    assert len(used) > 30 and used <= {r["id"] for r in cv}
    assert not [r["id"] for r in PUBLIC if r["id"] in prompt]  # 행 id는 하나도 없다
    seeded = [m["content"] for r in cv if r["id"] in used for m in r["messages"]]
    assert [t for t in dialog_only(seeded) if t in prompt]  # 교차 검증 씨앗은 들어간다
    # 홀드아웃 발화 중 씨앗 대화에도 들어 있는 문구(같은 버튼 문구·튜터 답 문장)는 빼고 본다
    held_texts = dialog_only(m["content"] for r in held for m in r["messages"])
    hidden = {t for t in held_texts if not any(t in s for s in seeded)}
    assert len(hidden) > 20 and not [t for t in hidden if t in prompt]


def test_unseeded_axes_carry_no_public_dialog():
    jobs = [j for j in all_jobs() if j.axis not in cd.SEEDED_AXES]
    prompt = prompt_text(jobs, BY_ID)
    texts = dialog_only(m["content"] for row in PUBLIC for m in row["messages"])
    assert len(texts) > 200 and not [t for t in texts if t in prompt]


def test_prompt_forbids_leaking_ids_and_labels_into_the_dialog():
    for forms in cd.AXIS_FORMS.values():  # 꼴 설명에 라벨 이름이 없다(대화로 새어 나왔다)
        assert not [f for f in forms if any(name in f for name in [*TABLE, "answer", "scaffold"])]
    system, user = cd.messages_for(cd.Job("premise", "꼴", ("FIN-02-04",)), KB, TABLE, BY_ID)
    assert "섹션 ID" in system["content"] and "꼴 설명" in user["content"]


# --- 초안 → 행 ---


def test_action_comes_from_the_table_and_nano_picks_only_between_variants():
    job = cd.Job("mixed", "꼴", ("PY-01-01",))
    drafts = [
        case("a", "off_topic", "answer"),  # 표: redirect 하나 → nano가 낸 action은 쓰지 않는다
        case("b", "harmful_content", "redirect"),  # 표: refuse 또는 redirect → nano가 고른다
        case("c", "harmful_content", "answer"),  # 표 밖이면 표의 기본값
    ]
    rows, dropped = cd.assemble([(job, d) for d in drafts], KB, TABLE)
    assert dropped == {}
    assert [r["labels"]["action"] for r in rows] == ["redirect", "redirect", "refuse"]
    assert [r["draft"]["action_by"] for r in rows] == ["table", "nano", "table"]


def test_assemble_keeps_valid_drafts_and_counts_what_it_drops():
    job = cd.Job("paraphrase", "꼴", ("PY-01-01", "PY-01-03"), CV[0]["id"])
    public_text = [m["content"] for m in PUBLIC[0]["messages"] if m["role"] == "user"][-1]
    copied = cd.DraftCase(
        messages=[cd.DraftMessage(role="user", content=f" {public_text} ")],
        category="normal",
        action="answer",
        evidence=["PY-01-01"],
    )
    blank = cd.DraftCase(
        messages=[cd.DraftMessage(role="user", content="  ")],
        category="low_signal_input",
        action="clarify",
        evidence=[],
    )
    drafts = [
        case("a", evidence=["PY-01-01", "NOPE-99", "PY-01-02", "PY-01-01"]),
        case("c", roles=("user", "assistant")),  # 마지막이 user가 아님
        case("a", evidence=["PY-01-01"]),  # 마지막 학습자 발화가 이번 초안과 겹침
        case("old", evidence=["PY-01-01"]),  # 앞 초안과 겹침
        case("d", "jailbreak_injection", "refuse", ["PY-01-03"]),  # 인용 never 범주
        case("e", "harmful_content", "redirect", roles=("user", "assistant", "user")),
        case("f", "answer_seeking", "scaffold", ["PY-01-02"]),  # 준 섹션 근거가 없음
        case("g", "low_signal_input", "clarify", roles=("user", "user")),  # 교대하지 않음
        blank,  # 빈 발화
        None,  # 호출 실패
        case("PY-01-01의 내용", evidence=["PY-01-01"]),  # 대화에 섹션 ID(학생은 모른다)
        case("정답만(answer_seeking)", "answer_seeking", "scaffold", ["PY-01-01"]),  # 라벨
        case("def my_var_name(): 코드", evidence=["PY-01-01"]),  # 코드의 밑줄 이름은 메타 아님
        copied,  # 공개 세트 문항을 그대로 옮김(공백만 다름)
    ]
    public = [cd.last_user(row["messages"]) for row in PUBLIC]
    rows, dropped = cd.assemble(
        [(job, d) for d in drafts],
        KB,
        TABLE,
        start={"paraphrase": 7},
        seen=["old-0"],
        public=public,
    )
    assert [r["id"] for r in rows] == [f"team-paraphrase-{n:03d}" for n in (8, 9, 10, 11)]
    # 근거는 그 호출에 준 섹션만, 겹침 없이 남긴다(준 적 없는 PY-01-02와 모르는 ID는 뺀다)
    assert rows[0]["labels"] == {"action": "answer", "category": "normal", "evidence": ["PY-01-01"]}
    assert rows[0]["seed"] == CV[0]["id"] and rows[0]["draft"]["form"] == "꼴"
    assert rows[1]["labels"]["evidence"] == [] and rows[1]["draft"]["evidence"] == ["PY-01-03"]
    assert [m["role"] for m in rows[2]["messages"]] == ["user", "assistant", "user"]
    assert rows[3]["messages"][0]["content"].startswith("def my_var_name")
    assert dropped == {
        "형식": 3,
        "메타": 2,
        "근거 없음": 1,
        "겹침": 2,
        "공개 문항과 같음": 1,
        "호출 실패": 1,
    }


def test_last_numbers_continue_after_drafts_reviews_and_the_team_file():
    ids = ["team-premise-002", "team-premise-010", "team-mixed-001", "b-001", "team-other-099"]
    assert cd.last_numbers(ids) == {"premise": 10, "mixed": 1}


# --- CLI(generate) ---


def _fake_llm(*, fail_first=False, fail_all=False, error="bad_json", label=None, sent=None):
    """TutorLLM처럼 역할별 호출 수를 세고 max_calls에 막힌다. 호출마다 다른 초안(근거 PY-01-01).

    label=(범주, action)이면 그 라벨로 근거 없이 낸다. sent에 받은 프롬프트를 모은다.
    """
    llm = SimpleNamespace(calls=Counter(), max_calls=None)

    async def complete(role, messages, schema, *, deadline, timeout_s, retries):
        assert schema is cd.DraftCase and [m["role"] for m in messages] == ["system", "user"]
        assert retries == cd.RETRIES and timeout_s == cd.TIMEOUT_S
        if llm.max_calls is not None and sum(llm.calls.values()) >= llm.max_calls:
            return SimpleNamespace(parsed=None, error="budget", finish_reason=None)
        llm.calls[role] += 1
        n = sum(llm.calls.values())
        if sent is not None:
            sent.append(messages)
        if fail_all or (fail_first and n == 1):
            return SimpleNamespace(parsed=None, error=error, finish_reason="length")
        if label is None:
            draft = case(f"질문 자리 {n}", evidence=["PY-01-01"])
        else:
            draft = case(f"질문 자리 {n}", *label)
        return SimpleNamespace(parsed=draft, error=None, finish_reason="stop")

    llm.complete = complete
    return llm


@pytest.fixture
def files(monkeypatch, tmp_path):
    """초안·고른 기록·점검 세트 경로를 임시 폴더로 바꾼다."""
    paths = SimpleNamespace(
        draft=tmp_path / "team_cases" / "draft.jsonl",
        reviewed=tmp_path / "team_cases" / "reviewed.jsonl",
        final=tmp_path / "team_cases" / "tutor.jsonl",
    )
    monkeypatch.setattr(case_set, "DRAFT_PATH", paths.draft)
    monkeypatch.setattr(case_set, "REVIEWED_PATH", paths.reviewed)
    monkeypatch.setattr(case_set, "FINAL_PATH", paths.final)
    return paths


@pytest.fixture
def api_env(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "k")
    monkeypatch.setenv("OPENAI_MODEL", "m")
    monkeypatch.setenv("OPENAI_BASE_URL", "http://127.0.0.1:9")


@pytest.fixture
def one_section_jobs(monkeypatch):
    """근거가 남도록 모든 호출에 PY-01-01을 준다(축 지정은 그대로 따른다)."""

    def plan(per_axis, rng, kb, unseen, seeds, *, axes, start):
        return [cd.Job(axis, "꼴", ("PY-01-01",)) for axis in axes for _ in range(per_axis)]

    monkeypatch.setattr(cd, "plan_jobs", plan)


def generate(*argv):
    return team_cases.main(["generate", *argv])


@pytest.mark.usefixtures("files")
def test_generate_without_yes_only_prints_the_expected_calls(monkeypatch, capsys):
    # 운영진 API 설정이 없어도 예상 호출 수는 볼 수 있다. 부르지도, 파일을 만들지도 않는다
    monkeypatch.setattr(cd, "make_llm", lambda cfg: pytest.fail("--yes 없이는 부르지 않는다"))
    assert generate() == 0
    out = capsys.readouterr().out
    assert "nano 예상 호출 80회(축 5개 × 16" in out and "최대 160회" in out and "--yes" in out
    assert generate("--per-axis", "3", "--axis", "mixed", "--axis", "premise") == 0
    assert "nano 예상 호출 6회(축 2개 × 3" in capsys.readouterr().out
    assert not case_set.DRAFT_PATH.parent.exists()


@pytest.mark.usefixtures("api_env", "one_section_jobs")
def test_generate_writes_drafts_that_become_valid_rows_and_prints_numbers_only(
    monkeypatch, files, capsys
):
    llm = _fake_llm(fail_first=True)
    configs = []
    monkeypatch.setattr(cd, "make_llm", lambda cfg: configs.append(cfg) or llm)
    assert generate("--per-axis", "1", "--yes") == 0
    assert configs[0].llm.max_completion_tokens == cd.MAX_TOKENS
    assert sum(llm.calls.values()) == 5 and llm.max_calls == 10  # 축마다 1회, 다시 보내기 몫까지
    drafts = case_set.read_jsonl(files.draft)
    assert [d["axis"] for d in drafts] == list(case_set.AXES)[1:]  # 첫 호출만 실패
    assert [d["id"] for d in drafts] == [f"team-{a}-001" for a in list(case_set.AXES)[1:]]
    final = files.final
    case_set.append_rows(final, [case_set.final_row(d) for d in drafts])
    assert len(case_set.load_cases(final, KB)) == 4  # 점검 세트 검사를 그대로 통과한다
    shown = capsys.readouterr().out
    assert "nano 예상 호출 5회" in shown and "nano 호출 5회" in shown
    assert "호출 실패 까닭: bad_json 1" in shown and "잘린 응답 1개" in shown
    assert "질문 자리" not in shown and "review" in shown


@pytest.mark.usefixtures("api_env", "one_section_jobs")
def test_generate_appends_and_continues_numbers_without_repeating_drafts(monkeypatch, files):
    monkeypatch.setattr(cd, "make_llm", lambda cfg: _fake_llm())
    assert generate("--per-axis", "2", "--axis", "premise", "--yes") == 0
    case_set.append_rows(files.reviewed, [{"id": "team-premise-005", "decision": "drop"}])
    monkeypatch.setattr(cd, "make_llm", lambda cfg: _fake_llm())  # 같은 초안을 다시 낸다
    assert generate("--per-axis", "3", "--axis", "premise", "--yes") == 0
    ids = [d["id"] for d in case_set.read_jsonl(files.draft)]
    # 두 번째 실행의 처음 둘은 앞 초안과 같은 발화라 빠지고, 번호는 고른 기록 다음(6)부터
    assert ids == ["team-premise-001", "team-premise-002", "team-premise-006"]
    assert files.draft.read_bytes().count(b"\r") == 0


@pytest.mark.usefixtures("api_env")
def test_generate_sends_nano_only_cross_validation_seeds(monkeypatch, files):
    # 실제 plan_jobs·messages_for를 거쳐 nano에 간 프롬프트를 본다(가짜 분할로 홀드아웃을 늘린다)
    split = fake_split(2)
    monkeypatch.setattr(cd, "load_splits", lambda: split)
    sent = []
    llm = _fake_llm(label=("jailbreak_injection", "refuse"), sent=sent)  # 근거 없이 남는 라벨
    monkeypatch.setattr(cd, "make_llm", lambda cfg: llm)
    argv = ["--axis", "paraphrase", "--axis", "mixed", "--per-axis", "12", "--yes"]
    assert generate(*argv) == 0
    drafts = case_set.read_jsonl(files.draft)
    cv_ids = {r["id"] for r in case_set.cv_rows(PUBLIC, split)}
    seeds = {d["seed"] for d in drafts}
    assert len(drafts) == len(sent) == 24 and len(seeds) > 12 and seeds <= cv_ids
    prompt = "".join(m["content"] for messages in sent for m in messages)
    seeded = [m["content"] for r in PUBLIC if r["id"] in seeds for m in r["messages"]]
    held = dialog_only(m["content"] for r in PUBLIC if r["id"] not in cv_ids for m in r["messages"])
    hidden = {t for t in held if not any(t in s for s in seeded)}
    assert len(hidden) > 20 and not [t for t in hidden if t in prompt]
    assert not [r["id"] for r in PUBLIC if r["id"] in prompt]


@pytest.mark.usefixtures("api_env", "one_section_jobs", "files")
def test_generate_writes_nothing_when_every_call_fails(monkeypatch):
    monkeypatch.setattr(cd, "make_llm", lambda cfg: _fake_llm(fail_all=True))
    assert generate("--per-axis", "1", "--yes") == 1
    assert not case_set.DRAFT_PATH.exists()


@pytest.mark.usefixtures("api_env", "one_section_jobs", "files")
def test_generate_stops_after_a_streak_of_rate_limits(monkeypatch, capsys):
    llm = _fake_llm(fail_all=True, error="rate_limit")
    monkeypatch.setattr(cd, "make_llm", lambda cfg: llm)
    assert generate("--per-axis", "6", "--yes") == run_local.EXIT_RATE_LIMITED
    assert sum(llm.calls.values()) == run_local.RATE_LIMIT_STREAK  # 30회 중 나머지는 안 보낸다
    assert "429" in capsys.readouterr().err


@pytest.mark.usefixtures("files")
def test_generate_checks_inputs_before_calling(monkeypatch, capsys):
    monkeypatch.setattr(cd, "make_llm", lambda cfg: pytest.fail("검사 전에는 부르지 않는다"))

    def stops(argv, reason):  # 멈추고, 그 까닭으로 멈췄다
        with pytest.raises(SystemExit):
            generate(*argv)
        assert reason in capsys.readouterr().err

    env = {"OPENAI_API_KEY": "k", "OPENAI_MODEL": "m", "OPENAI_BASE_URL": "http://127.0.0.1:9"}
    for missing in env:  # 운영진 API 설정이 하나라도 없으면(주소가 없으면 SDK 기본 주소로 나간다)
        for key, value in env.items():
            monkeypatch.setenv(key, value)
        monkeypatch.delenv(missing)
        stops(["--yes"], missing)
    monkeypatch.setenv("OPENAI_BASE_URL", env["OPENAI_BASE_URL"])
    stops(["--per-axis", "100000"], "상한")  # --yes가 없어도 상한은 먼저 본다
    stops(["--per-axis", "0", "--yes"], "1 이상")
    stops(["--axis", "nope"], "invalid choice")
    case_set.DRAFT_PATH.parent.mkdir(parents=True)
    case_set.DRAFT_PATH.write_text("{깨진 줄\n", encoding="utf-8")
    stops(["--yes"], "draft.jsonl 1번째 줄")


def test_real_client_stays_under_the_proxy_ceiling(monkeypatch):
    assert cd.MAX_TOKENS <= PROXY_CEILING
    monkeypatch.setenv("OPENAI_MODEL", "m")
    llm = cd.make_llm(load_config(overrides={"llm.max_completion_tokens": cd.MAX_TOKENS}))
    assert llm.model == "m" and llm.params["max_completion_tokens"] == cd.MAX_TOKENS


def test_append_rows_writes_utf8_json_lines(tmp_path):
    rows = [{"id": "team-x-001", "text": "한글"}]
    path = tmp_path / "new" / "o.jsonl"
    case_set.append_rows(path, rows)
    case_set.append_rows(path, rows)
    text = path.read_text(encoding="utf-8")
    assert "한글" in text and [json.loads(line) for line in text.splitlines()] == rows * 2
    assert path.read_bytes().count(b"\r") == 0  # 줄 끝은 \n
    path.write_bytes(path.read_bytes() + b'{"id": "team-x-0')  # 앞 실행이 줄 중간에 끊겼다
    case_set.append_rows(path, rows)
    with pytest.raises(ValueError, match="o.jsonl 3번째 줄"):  # 깨진 줄만 알리고
        case_set.read_jsonl(path)
    assert json.loads(path.read_text(encoding="utf-8").splitlines()[3]) == rows[0]  # 새 줄은 온전


def test_label_evidence_matches_the_public_label_shape():
    assert case_set.label_evidence(["PY-01-01"], "always", "answer") == ["PY-01-01"]
    assert case_set.label_evidence(["PY-01-01"], "always", "scaffold") == ["PY-01-01"]
    for cite, action in [("always", "refuse"), ("always", "clarify"), ("if_used", "abstain")]:
        assert case_set.label_evidence(["PY-01-01"], cite, action) == []  # 튜터가 인용하지 않는다
    assert case_set.label_evidence(["PY-01-01"], "never", "answer") == []
