"""코드 블록 때문에 영어로 보이는 한국어 질문(`toggles.code_block_lang`).

임베딩·LLM은 가짜다(API 0회). 입력 문장은 지어낸 것이다(공개 세트·KB 문구를 옮기지 않는다).
"""

import dataclasses

from tests.tutor.test_embedding import FakeEmbed, run
from tests.tutor.test_english_embedding import fake_search, with_embedding
from tests.tutor.test_flow import FakeLLM, ask, count_retrieves, out, state_with
from tutor.retrieval import default_index, retrieve
from tutor.retrieval import embedding as emb
from tutor.retrieval.search import strip_code_blocks

CODE = "```python\nfor item in items:\n    total = total + item\nprint(total)\n```"
KO_CODE = f"이 반복문에서 합계 변수는 어떻게 바뀌나요?\n{CODE}"
EN_CODE = f"how does the total change in this loop\n{CODE}"
ON = {"toggles.english_embedding": True, "toggles.oom_gate": "named_term"}


def built_search():
    fake = FakeEmbed()
    search = run(emb.EmbeddingSearch.build(default_index(), FakeEmbed(), timeout_s=1.0))
    return fake, dataclasses.replace(search, embed=fake)


def test_strip_code_blocks_removes_fenced_and_unclosed_blocks():
    closed = strip_code_blocks("앞 문장\n```python\nx = 1\n```\n뒤 문장")
    assert "x = 1" not in closed and "앞 문장" in closed and "뒤 문장" in closed
    unclosed = strip_code_blocks("질문입니다\n```python\nprint(1)")
    assert "print" not in unclosed and "질문입니다" in unclosed
    assert strip_code_blocks("코드 없는 질문") == "코드 없는 질문"


def test_korean_question_with_code_is_english_by_letter_share():
    # 전제: 코드의 라틴 글자 때문에 지금 판별은 영어다. 코드를 빼면 한국어다
    assert emb.is_english(KO_CODE)
    assert not emb.is_english(strip_code_blocks(KO_CODE))


def test_toggle_off_keeps_the_embedding_path_for_korean_with_code():
    fake, search = built_search()
    run(retrieve(KO_CODE, k=5, embedding=search, english_only_embedding=True))
    assert fake.calls  # 지금 동작: 임베딩 순위만


def test_toggle_on_searches_korean_with_code_by_bm25_without_the_code():
    fake, search = built_search()
    found = run(
        retrieve(KO_CODE, k=5, embedding=search, english_only_embedding=True, code_block_lang=True)
    )
    plain = run(retrieve(strip_code_blocks(KO_CODE), k=5))
    assert fake.calls == []  # 임베딩을 부르지 않는다
    assert [s.id for s in found] == [s.id for s in plain]


def test_toggle_on_keeps_the_embedding_for_english_with_code():
    fake, search = built_search()
    run(retrieve(EN_CODE, k=5, embedding=search, english_only_embedding=True, code_block_lang=True))
    assert fake.calls  # 코드를 빼도 영어라 그대로


def test_toggle_on_keeps_the_embedding_for_a_code_only_question():
    fake, search = built_search()
    run(retrieve(CODE, k=5, embedding=search, english_only_embedding=True, code_block_lang=True))
    assert fake.calls  # 코드를 빼면 남는 글이 없어 지금 동작 그대로


def test_toggle_on_leaves_plain_korean_unchanged():
    query = "체크카드 사용 규칙이 뭐예요"
    found = {}
    for on in (False, True):
        fake, search = built_search()
        got = run(
            retrieve(query, k=5, embedding=search, english_only_embedding=True, code_block_lang=on)
        )
        found[on] = ([s.id for s in got], fake.calls)
    assert found[True] == found[False] and found[True][1] == []


def test_toggle_on_also_drops_the_embedding_in_rrf_mode():
    # toggles.embedding(RRF)만 켠 조합에서도 코드 때문에 영어로 보인 한국어 질문은 한국어로 다룬다
    fake, search = built_search()
    found = run(retrieve(KO_CODE, k=5, embedding=search, code_block_lang=True))
    plain = run(retrieve(strip_code_blocks(KO_CODE), k=5))
    assert fake.calls == [] and [s.id for s in found] == [s.id for s in plain]


def test_unclosed_fence_before_the_korean_question_keeps_the_english_path():
    # 닫히지 않은 울타리는 끝까지 코드로 보므로 뒤의 한국어도 빠진다
    # → 남는 글이 없어 지금 동작 그대로
    query = "```\nfor item in items:\n    print(item, total)\n이 코드가 왜 안 되나요"
    assert emb.is_english(query) and strip_code_blocks(query).strip() == ""
    fake, search = built_search()
    run(retrieve(query, k=5, embedding=search, english_only_embedding=True, code_block_lang=True))
    assert fake.calls


DIALOG = [
    "how do loops work in this course",
    "a zeta loop repeats the same steps again and again",
    "what is that exactly",
]
ROLES = ["user", "assistant", "user"]
RULE_A = {"toggles.cite_top1": True, "toggles.top1_dialog_query": True}


def test_flow_passes_the_toggle_to_every_search(monkeypatch):
    calls = count_retrieves(monkeypatch)
    state = with_embedding(
        state_with(FakeLLM(out()), **ON, **RULE_A, **{"toggles.code_block_lang": True}),
        fake_search(lambda text: "PY-01-01"),
    )
    ask(state, *DIALOG, roles=ROLES)
    assert len(calls) == 3  # E 근거 묶음, 주 호출 묶음, 규칙 A 재검색
    assert all(kw.get("code_block_lang") is True for _q, _h, kw in calls)


def test_korean_with_code_utterance_never_embeds_or_takes_the_english_gate_path():
    calls = []
    state = with_embedding(
        state_with(FakeLLM(out()), **ON, **RULE_A, **{"toggles.code_block_lang": True}),
        fake_search(lambda text: "PY-01-01", calls),
    )
    ask(state, KO_CODE)
    assert calls == []  # 근거 묶음·규칙 A 모두 코드 뺀 질의로 BM25
    assert "english_called" not in (getattr(state, "oom_gate_counts", None) or {})  # 한글이 있다


def test_flow_passes_no_flag_when_the_toggle_is_off(monkeypatch):
    calls = count_retrieves(monkeypatch)
    state = with_embedding(
        state_with(FakeLLM(out()), **ON, **RULE_A), fake_search(lambda text: "PY-01-01")
    )
    ask(state, *DIALOG, roles=ROLES)
    assert calls and all("code_block_lang" not in kw for _q, _h, kw in calls)
