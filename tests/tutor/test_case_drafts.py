"""점검 세트 초안 생성(TU-16·TU-19, #80): 운영진 nano를 부르는 대신 가짜로 바꿔 시험한다.

여기의 문항 내용은 테스트용 자리표시뿐이다(실제 초안은 런박스에서 nano가 쓴다).
"""

import json
import random
import re
from collections import Counter
from types import SimpleNamespace

import pytest

from integration.actions import load_actions
from tutor.eval import case_drafts as cd
from tutor.eval import case_set, run_local, team_cases
from tutor.eval.grader import KB_DIR, score_local, scoring
from tutor.runtime.config import load_config

KB = score_local.load_kb_dir(KB_DIR)
PUBLIC = case_set.public_rows()
UNSEEN = case_set.uncovered_sections(PUBLIC, KB)
TABLE = load_actions()
PROXY_CEILING = 2000  # 운영진 프록시: 스트리밍이 아닌 응답의 출력 상한(넘기면 400, 11/12 런박스)


def title(sid):
    """nano가 근거로 돌려줄 섹션 제목(원문 첫 줄 `# <ID> <제목>`의 제목)."""
    return cd.section_title(sid, KB[sid])


def case(text="질문 자리", category="normal", action="answer", evidence=(), roles=("user",)):
    messages = [cd.DraftMessage(role=r, content=f"{text}-{i}") for i, r in enumerate(roles)]
    return cd.DraftCase(
        messages=messages, category=category, action=action, evidence=list(evidence)
    )


def all_jobs(per_axis=40):
    return cd.plan_jobs(per_axis, random.Random(0), KB, UNSEEN)


def prompt_text(jobs):
    return "".join(m["content"] for job in jobs for m in cd.messages_for(job, KB, TABLE))


def dialog_only(texts):
    """KB 원문(프롬프트에 섹션으로 들어간다)에 그대로 있는 문구를 뺀 대화 발화(12자 이상)."""
    return {t for t in texts if len(t) >= 12 and not any(t in text for text in KB.values())}


PUBLIC_TEXTS = dialog_only(m["content"] for row in PUBLIC for m in row["messages"])


def public_in(prompt):
    """프롬프트에 든 공개 튜터 행 id·대화 발화(KB 원문에도 있는 문구는 뺀다)."""
    ids = [row["id"] for row in PUBLIC if row["id"] in prompt]
    return ids + [t for t in PUBLIC_TEXTS if t in prompt]


# --- 호출 계획 ---


def test_default_generate_asks_sixteen_drafts_per_axis():
    assert cd.DEFAULT_PER_AXIS == 16 and cd.expected_calls(16) == 80
    assert set(cd.AXIS_FORMS) == set(case_set.AXES)


def test_jobs_cycle_forms_and_sections_per_axis():
    jobs = cd.plan_jobs(15, random.Random(0), KB, UNSEEN)
    assert len(jobs) == 5 * 15 == cd.expected_calls(15)  # 호출 하나에 초안 하나
    by_axis = {axis: [j for j in jobs if j.axis == axis] for axis in case_set.AXES}
    for axis, forms in cd.AXIS_FORMS.items():  # 꼴은 축 안에서 차례로 돈다
        assert [j.form for j in by_axis[axis]] == [forms[i % len(forms)] for i in range(15)]
    unseen_jobs = by_axis["unseen_section"]
    assert [j.sections for j in unseen_jobs] == [(UNSEEN[i % len(UNSEEN)],) for i in range(15)]
    assert {j.sections[0] for j in unseen_jobs} == set(UNSEEN)  # 7개 모두
    for axis in ("paraphrase", "mixed", "structure", "premise"):  # 같은 단원의 두 섹션
        assert all(len(set(j.sections)) == 2 for j in by_axis[axis])
        assert all(len({s.rsplit("-", 1)[0] for s in j.sections}) == 1 for j in by_axis[axis])
    assert all(sid in KB for j in jobs for sid in j.sections)
    assert cd.plan_jobs(15, random.Random(0), KB, UNSEEN) == jobs  # 시드가 같으면 같다


def test_only_paraphrase_jobs_name_a_category_and_they_cycle_through_all():
    jobs = all_jobs(per_axis=20)
    paraphrase = [j.category for j in jobs if j.axis == "paraphrase"]
    assert paraphrase == [scoring.CATEGORIES[i % 10] for i in range(20)]  # SPEC 10범주를 차례로
    assert set(paraphrase) == set(TABLE)
    assert all(j.category is None for j in jobs if j.axis != "paraphrase")


def test_top_up_runs_continue_the_form_rotation_and_pick_only_some_axes():
    jobs = cd.plan_jobs(2, random.Random(0), KB, UNSEEN, axes=["premise"], start={"premise": 3})
    forms = cd.AXIS_FORMS["premise"]
    assert [(j.axis, j.form) for j in jobs] == [("premise", forms[1]), ("premise", forms[0])]
    jobs = cd.plan_jobs(
        2, random.Random(0), KB, UNSEEN, axes=["paraphrase"], start={"paraphrase": 9}
    )
    assert [j.category for j in jobs] == [scoring.CATEGORIES[9], scoring.CATEGORIES[0]]


# --- 프롬프트 ---


def test_prompt_carries_the_table_the_form_the_category_and_the_sections():
    job = cd.Job("paraphrase", cd.AXIS_FORMS["paraphrase"][0], ("FIN-03-01",), "off_topic")
    system, user = cd.messages_for(job, KB, TABLE)
    assert (system["role"], user["role"]) == ("system", "user")
    assert all(category in system["content"] for category in TABLE)
    assert TABLE["harmful_content"].variants[0].when in system["content"]  # 표에서 읽는다
    shown = cd.shown_section("FIN-03-01", KB["FIN-03-01"])
    assert job.form in user["content"] and shown in user["content"]
    assert shown.splitlines()[0] == f"# {title('FIN-03-01')}"  # 근거로 돌려받을 제목이 원문 머리에
    assert "범주: off_topic" in user["content"]
    _system, plain = cd.messages_for(job._replace(category=None), KB, TABLE)
    assert "범주:" not in plain["content"]  # 범주는 정해 준 축(표현 변형)에서만


def test_no_public_row_reaches_any_prompt():
    # 공개 행(교차 검증·홀드아웃 모두)의 행 id·대화는 어느 축 프롬프트에도 없다(TD-10)
    jobs = all_jobs()
    assert {j.axis for j in jobs} == set(case_set.AXES)
    assert len(PUBLIC_TEXTS) > 200 and public_in(prompt_text(jobs)) == []


def test_prompt_forbids_leaking_ids_and_labels_into_the_dialog():
    for forms in cd.AXIS_FORMS.values():  # 꼴 설명에 라벨 이름이 없다(대화로 새어 나왔다)
        assert not [f for f in forms if any(name in f for name in [*TABLE, "answer", "scaffold"])]
        assert not [f for f in forms if "섹션" in f]  # 11/15: "아래 섹션"이 "섹션 A"로 새었다
    system, user = cd.messages_for(cd.Job("premise", "꼴", ("FIN-02-04",)), KB, TABLE)
    assert "범주·action 이름" in system["content"] and "꼴 설명" in user["content"]
    assert "제목" in system["content"]  # 근거는 교재 내용의 제목으로 받는다


def test_no_section_id_or_name_tag_reaches_any_prompt():
    # 11/14 런박스 진단: 프롬프트 세 곳에 ID가 보였고 premise·mixed 초안이 모두 메타로 빠졌다.
    # 11/15 런박스 진단: ID 대신 붙인 이름표 "섹션 A·B"가 12개 중 11개에서 대화로 새었다.
    # 그래서 nano는 섹션을 원문 머리의 제목으로만 보고, 근거도 제목으로 돌려준다
    prompts = prompt_text(all_jobs())
    assert cd.LEAKED_SID.findall(prompts) == []
    assert cd.LEAKED_NAME.findall(prompts) == []
    assert all(f"# {title(sid)}\n" in prompts for j in all_jobs() for sid in j.sections)


def test_no_word_section_reaches_the_prompt_outside_the_kb_text():
    # 11/15 진단의 원인은 "아래 섹션"이라는 말이었다. 교재 원문(PR-04-01에 "섹션"이 있다)을 뺀
    # 나머지(시스템 프롬프트, 꼴 설명, 머리 문구)에는 "섹션"이 없다
    for job in all_jobs():
        system, user = cd.messages_for(job, KB, TABLE)
        rest = user["content"]
        for sid in job.sections:
            rest = rest.replace(cd.shown_section(sid, KB[sid]), "")
        assert "섹션" not in system["content"] and "섹션" not in rest


def test_section_titles_are_unique_so_evidence_maps_back():
    assert len({title(sid) for sid in KB}) == len(KB) == 48
    assert len({cd._title_key(title(sid)) for sid in KB}) == 48  # 맞추는 데 쓰는 키도 서로 다르다
    assert all(title(sid) for sid in KB)


def test_a_call_with_colliding_titles_stops_before_calling():
    kb = {"PY-01-01": "# PY-01-01 같은 제목\n본문", "PY-01-02": "# PY-01-02 같은  「제목」\n본문"}
    with pytest.raises(ValueError, match="겹친다"):  # 정규화하면 같은 키라 근거를 되돌릴 수 없다
        cd.messages_for(cd.Job("mixed", "꼴", ("PY-01-01", "PY-01-02")), kb, TABLE)


def test_shown_section_drops_only_the_ids_and_keeps_title_text_and_tables():
    with_table = [sid for sid, text in KB.items() if "(표: " in text]
    assert len(with_table) >= 20  # 표가 붙은 섹션이 있어야 표 자리를 시험한다
    for sid, text in KB.items():
        shown = cd.shown_section(sid, text)
        assert cd.LEAKED_SID.search(shown) is None
        title = text.splitlines()[0].removeprefix(f"# {sid}").strip()
        assert shown.splitlines()[0] == f"# {title}" and title  # 제목은 남는다
        # 지운 것은 머리말 ID와 표 자리 파일 이름뿐이다(표 내용은 원문 끝에 그대로)
        before, after = text.splitlines()[1:], shown.splitlines()[1:]
        assert len(before) == len(after)
        for old, new in zip(before, after, strict=True):
            if f"(표: {sid}-table" in old:
                start = old.index("(표: ")
                ref = old[start : old.index(")", start) + 1]  # 예: (표: FIN-03-02-table1.csv)
                assert new == old.replace(ref, "(표)")
            else:
                assert new == old


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
    job = cd.Job("paraphrase", "꼴", ("PY-01-01", "PY-01-03"), "normal")
    public_text = [m["content"] for m in PUBLIC[0]["messages"] if m["role"] == "user"][-1]
    a, b = title("PY-01-01"), title("PY-01-03")
    copied = cd.DraftCase(
        messages=[cd.DraftMessage(role="user", content=f" {public_text} ")],
        category="normal",
        action="answer",
        evidence=[a],
    )
    blank = cd.DraftCase(
        messages=[cd.DraftMessage(role="user", content="  ")],
        category="low_signal_input",
        action="clarify",
        evidence=[],
    )
    drafts = [
        case("a", evidence=[a, a]),  # 섹션 제목 → 섹션 ID, 겹침 없이
        case("c", roles=("user", "assistant")),  # 마지막이 user가 아님
        case("a", evidence=[a]),  # 마지막 학습자 발화가 이번 초안과 겹침
        case("old", evidence=[a]),  # 앞 초안과 겹침
        case("d", "jailbreak_injection", "refuse", [b]),  # 인용 never 범주
        case("e", "harmful_content", "redirect", roles=("user", "assistant", "user")),
        case("f", "answer_seeking", "scaffold", []),  # 근거가 있어야 하는 범주인데 없음
        case("g", "low_signal_input", "clarify", roles=("user", "user")),  # 교대하지 않음
        blank,  # 빈 발화
        None,  # 호출 실패
        case("PY-01-01의 내용", evidence=[a]),  # 대화에 섹션 ID(학생은 모른다)
        case("섹션 B에 나온 값", evidence=[b]),  # 대화에 옛 이름표(지금 프롬프트에는 없다)
        case("정답만(answer_seeking)", "answer_seeking", "scaffold", [a]),  # 라벨
        case("def my_var_name(): 코드", evidence=[a]),  # 코드의 밑줄 이름은 메타 아님
        copied,  # 공개 세트 문항을 그대로 옮김(공백만 다름)
    ]
    public = [case_set.last_user(row["messages"]) for row in PUBLIC]
    rows, dropped = cd.assemble(
        [(job, d) for d in drafts],
        KB,
        TABLE,
        start={"paraphrase": 7},
        seen=["old-0"],
        public=public,
    )
    assert [r["id"] for r in rows] == [f"team-paraphrase-{n:03d}" for n in (8, 9, 10, 11)]
    # 근거는 그 호출에 준 섹션 제목을 섹션 ID로 바꿔, 겹침 없이 남긴다
    assert rows[0]["labels"] == {"action": "answer", "category": "normal", "evidence": ["PY-01-01"]}
    assert rows[0]["draft"]["form"] == "꼴" and rows[0]["draft"]["category"] == "normal"
    assert set(rows[0]) == {"id", "endpoint", "axis", "messages", "labels", "draft"}  # seed 없음
    assert rows[1]["labels"]["evidence"] == [] and rows[1]["draft"]["evidence"] == ["PY-01-03"]
    assert [m["role"] for m in rows[2]["messages"]] == ["user", "assistant", "user"]
    assert rows[3]["messages"][0]["content"].startswith("def my_var_name")
    assert dropped == {  # 메타는 무엇이 샜는지 나눠 센다(런박스에서 원인을 바로 본다)
        "형식": 3,
        "메타(섹션 ID)": 1,
        "메타(섹션 이름)": 1,
        "메타(범주 이름)": 1,
        "근거 없음": 1,
        "겹침": 2,
        "공개 문항과 같음": 1,
        "호출 실패": 1,
    }


def test_section_titles_map_back_only_to_the_sections_given_in_that_call():
    one = cd.Job("unseen_section", "꼴", ("FIN-03-02",))  # 섹션 하나: PY-01-01은 준 적 없다
    given, other = title("FIN-03-02"), title("PY-01-01")
    rows, dropped = cd.assemble([(one, case("u", evidence=[other, given]))], KB, TABLE)
    assert rows[0]["labels"]["evidence"] == ["FIN-03-02"] and dropped == {}
    rows, dropped = cd.assemble([(one, case("v", evidence=[other, "A"]))], KB, TABLE)
    assert rows == [] and dropped == {"근거 없음": 1}  # 준 적 없는 제목·옛 이름표는 근거가 아니다
    two = cd.Job("mixed", "꼴", ("PY-01-01", "PY-01-03"))
    b = title("PY-01-03")
    rows, _ = cd.assemble([(two, case("w", evidence=[b, other, b]))], KB, TABLE)
    assert rows[0]["labels"]["evidence"] == ["PY-01-03", "PY-01-01"]  # nano가 낸 순서


@pytest.mark.parametrize("wrap", ["「{}」", "'{}'", '"{}"', " {} ", "# {}", "<{}>"])
def test_evidence_title_matches_despite_quotes_spaces_and_heading_mark(wrap):
    one = cd.Job("unseen_section", "꼴", ("FIN-03-02",))
    loose = wrap.format(title("FIN-03-02"))
    rows, dropped = cd.assemble([(one, case("x", evidence=[loose]))], KB, TABLE)
    assert rows[0]["labels"]["evidence"] == ["FIN-03-02"] and dropped == {}


FULL_WIDTH = str.maketrans({c: chr(ord(c) + 0xFEE0) for c in "abcdefghijklmnopqrstuvwxyz"})


@pytest.mark.parametrize(
    "change",
    [
        str.upper,  # 대소문자
        lambda t: t.replace("·", "∙"),  # 가운뎃점 모양(U+2219)
        lambda t: t.replace("·", "ㆍ"),  # 한글 아래아(U+318D)
        lambda t: t.translate(FULL_WIDTH),  # 전각 영문(NFKC)
    ],
    ids=["case", "bullet_operator", "arae_a", "full_width"],
)
def test_evidence_title_matches_despite_case_middle_dot_and_width(change):
    one = cd.Job("unseen_section", "꼴", ("PY-02-04",))  # while 반복문과 break·continue·pass
    loose = change(title("PY-02-04"))
    assert loose != title("PY-02-04")
    rows, dropped = cd.assemble([(one, case("y", evidence=[loose]))], KB, TABLE)
    assert rows[0]["labels"]["evidence"] == ["PY-02-04"] and dropped == {}


def test_a_call_never_names_more_sections_than_allowed():
    assert all(len(j.sections) <= cd.MAX_SECTIONS == 2 for j in all_jobs())  # 계획은 둘까지
    three = cd.Job("mixed", "꼴", ("PY-01-01", "PY-01-02", "PY-01-03"))
    with pytest.raises(ValueError, match="2개까지"):  # 셋째가 프롬프트에서 조용히 빠지지 않게
        cd.messages_for(three, KB, TABLE)


@pytest.mark.parametrize("text", ["자료 3개를 봤어요", "섹션별로 정리해 줘", "섹션 Apple 예제"])
def test_ordinary_words_are_not_section_name_leaks(text):
    rows, dropped = cd.assemble([(cd.Job("mixed", "꼴", ("PY-01-01",)), case(text))], KB, TABLE)
    assert dropped == {"근거 없음": 1}  # 메타가 아니라 근거 검사까지 간다(normal인데 근거 없음)


def test_last_numbers_continue_after_drafts_reviews_and_the_team_file():
    ids = ["team-premise-002", "team-premise-010", "team-mixed-001", "b-001", "team-other-099"]
    assert cd.last_numbers(ids) == {"premise": 10, "mixed": 1}


# --- CLI(generate) ---


def _fake_llm(*, fail_first=False, fail_all=False, error="bad_json", label=None, sent=None):
    """TutorLLM처럼 역할별 호출 수를 세고 max_calls에 막힌다. 호출마다 다른 초안(근거는 프롬프트의
    첫 섹션 제목).

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
            first = re.search(r"^# (.+)$", messages[-1]["content"], re.MULTILINE)[1]
            draft = case(f"질문 자리 {n}", evidence=[first])
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

    def plan(per_axis, rng, kb, unseen, *, axes, start):
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
def test_generate_sends_nano_no_public_row(monkeypatch, files):
    # 실제 plan_jobs·messages_for를 거쳐 nano에 간 프롬프트를 본다(다섯 축 모두)
    sent = []
    llm = _fake_llm(label=("jailbreak_injection", "refuse"), sent=sent)  # 근거 없이 남는 라벨
    monkeypatch.setattr(cd, "make_llm", lambda cfg: llm)
    assert generate("--per-axis", "12", "--yes") == 0
    drafts = case_set.read_jsonl(files.draft)
    assert len(drafts) == len(sent) == 60 and {d["axis"] for d in drafts} == set(case_set.AXES)
    assert not [d for d in drafts if "seed" in d]  # 초안에도 공개 행 id가 없다
    assert public_in("".join(m["content"] for messages in sent for m in messages)) == []


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
