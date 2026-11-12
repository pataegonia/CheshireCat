"""공개 검색 함수 `retrieve`(TU-03). 통합 하네스(IN-02)도 이 함수로 recall을 잰다."""

from collections.abc import Sequence
from functools import cache
from pathlib import Path

from guardrail.config import load_config, resolve_path

from tutor.retrieval.embedding import EmbeddingSearch, is_english
from tutor.retrieval.index import KbIndex
from tutor.retrieval.kb import Section, bigrams

# 가드 retrieval.max_query_chars와 같은 상한. 넘으면 뒤쪽(최근 발화)을 남긴다
MAX_QUERY_CHARS = 4000
DEFAULT_K = 5


def default_kb_dir() -> Path:
    """가드와 같은 KB 폴더: 가드 설정 `paths.kb`(GUARD_KB_DIR로 덮어쓸 수 있다)."""
    return resolve_path(load_config().paths.kb)


@cache
def default_index() -> KbIndex:
    """기동 때 한 번 만드는 KB 색인. 배포 자료 파생물만 담고 요청 내용은 담지 않는다(TU-18)."""
    return KbIndex.from_dir(default_kb_dir())


async def retrieve(
    query: str,
    history: Sequence[str] = (),
    *,
    subject: str | None = None,
    k: int = DEFAULT_K,
    embedding: EmbeddingSearch | None = None,
) -> list[Section]:
    """KB 섹션을 점수 높은 순으로 0개 이상 k개 이하 돌려준다.

    임베딩을 쓰지 않으면 겹치는 2-gram이 없을 때 빈 목록이다. 임베딩을 섞은 영어 질의는 BM25가
    아무것도 못 찾아도 임베딩 순위로 k개를 채운다.

    query는 판단 대상 학습자 발화, history는 창 안의 다른 학습자 발화다. 어느 발화를 넣을지는
    부르는 쪽이 고른다(버튼 문구는 빼서 넘긴다). 둘을 이어 한 질의로 쓰고 너무 길면 뒤쪽을 남긴다.
    subject는 과목 폴더 이름(python·prompt·finance)으로 거른다.
    embedding(기동 때 만든 임베딩 검색)을 주면 영어 위주 질의에 한해 질의 임베딩 순위를 BM25
    순위와 RRF로 섞는다(TU-11). 2-gram이 없는 질의는 임베딩도 부르지 않는다.
    """
    if k <= 0:
        return []
    index = default_index()
    text = "\n".join([*history, query])[-MAX_QUERY_CHARS:]
    ranked = [sid for sid, _score in index.search(text, subject=subject)]
    if embedding is not None and is_english(query) and bigrams(query):
        allowed = None
        if subject is not None:
            allowed = {sid for sid, section in index.sections.items() if section.subject == subject}
        ranked = await embedding.rerank(text, ranked, allowed=allowed)
    return [index.section(sid) for sid in ranked[:k]]
