"""근거 문구로 섹션 ID 바로잡기(flow.repair_quote_sids). API 0회, 지어낸 문장."""

from integration.contracts import Evidence, Section, Sentence
from tutor.judge.flow import repair_quote_sids

A = Section(id="PY-01-01", subject="python", title="가", text="점수판은 3회까지 기록해요.")
B = Section(id="PY-01-02", subject="python", title="나", text="사과는 4개, 배는 2개 들어 있어요.")


def claim(sid, quote, text="문장"):
    return Sentence(text=text, kind="claim", evidence=[Evidence(sid=sid, quote=quote)])


def test_wrong_sid_moves_to_the_section_that_has_the_quote():
    got = repair_quote_sids([claim("PY-01-01", "사과는 4개")], [A, B], 2)
    assert got[0].evidence[0].sid == "PY-01-02"


def test_right_sid_and_missing_quote_and_non_claim_stay():
    right = claim("PY-01-01", "3회까지 기록")
    nowhere = claim("PY-01-01", "없는 문구입니다")
    plain = Sentence(text="좋아요", kind="non_claim", evidence=[])
    got = repair_quote_sids([right, nowhere, plain], [A, B], 2)
    assert got == [right, nowhere, plain]


def test_first_section_in_bundle_order_wins():
    c = Section(id="PY-01-03", subject="python", title="다", text="사과는 4개, 배는 2개 들어 있어요.")
    got = repair_quote_sids([claim("PY-09-09", "사과는 4개")], [A, c, B], 2)
    assert got[0].evidence[0].sid == "PY-01-03"
