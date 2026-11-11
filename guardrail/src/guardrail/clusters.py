"""S1.4 짝 묶음 (GR-13, ADR-0013, master-plan §4.5).

마지막 학습자 발화(정규화본)의 문자 3-gram Jaccard ≥ 기준이거나 튜터 정답 사실이 같은 행끼리
union-find로 잇는다.
서버(S2.5 KnnIndex)도 기동 때 이 함수를 쓰므로 eval 패키지를 import하지 않는다.
"""

from collections.abc import Iterable

from guardrail.data import LabeledRow

# 빠른 질문 버튼 문구(R13-A §1.4). 여러 행이 같은 문장을 써 무관한 행을 잇기 때문에 비교에서 뺀다.
BUTTON_PHRASES = (
    "학습자료의 내용을 요약해 주세요.",
    "실습 도중 어려운 부분이 있어요. 도움을 받을 수 있을까요?",
    "영상의 내용을 요약해주세요.",
    "이 코드를 설명해주세요.",
    "문제 의도를 자세히 알고 싶어요.",
    "힌트를 알려주세요.",
    "문제를 푸는 방법을 차근차근 설명해주세요.",
    "이 퀴즈를 푸는 데 힌트를 받을 수 있을까요?",
)
N = 3


def _grams(text: str) -> frozenset[str]:
    for phrase in BUTTON_PHRASES:
        if text.startswith(phrase):  # 정확한 접두 일치만. 뒤에 덧붙은 말은 남긴다
            text = text[len(phrase) :]
            break
    text = text.strip()
    if not text:
        return frozenset()
    if len(text) < N:
        return frozenset({text})
    return frozenset(text[i : i + N] for i in range(len(text) - N + 1))


def build_clusters(rows: Iterable[LabeledRow], jaccard: float = 0.5) -> dict[str, str]:
    """{행 id: 묶음 id}. 묶음 id는 멤버 중 사전순 첫 id라 행 순서와 무관하다."""
    rows = list(rows)
    grams = [_grams(r.text) for r in rows]
    parent = list(range(len(rows)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i, gi in enumerate(grams):
        if not gi:
            continue
        for j in range(i + 1, len(rows)):
            gj = grams[j]
            if gj and len(gi & gj) / len(gi | gj) >= jaccard:
                parent[find(j)] = find(i)

    # 튜터 정답 사실(gold_facts)이 같으면 잇는다. 근거 섹션은 안 본다(섹션 공유는 누수 아님)
    first_with_fact: dict[str, int] = {}
    for i, r in enumerate(rows):
        for fact in r.facts:
            parent[find(i)] = find(first_with_fact.setdefault(fact, i))

    members: dict[int, list[str]] = {}
    for i, r in enumerate(rows):
        members.setdefault(find(i), []).append(r.id)
    return {rid: min(ids) for ids in members.values() for rid in ids}
