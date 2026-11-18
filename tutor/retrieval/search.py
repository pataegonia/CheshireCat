"""공개 검색 함수 `retrieve`(TU-03). 통합 하네스(IN-02)도 이 함수로 recall을 잰다."""

from collections.abc import Sequence
from functools import cache

from guardrail.config import GuardConfig, load_config, resolve_path

from tutor.retrieval.embedding import EmbeddingSearch, is_english
from tutor.retrieval.index import KbIndex
from tutor.retrieval.kb import Section, bigrams

DEFAULT_K = 5


def build_index(cfg: GuardConfig) -> KbIndex:
    """가드 설정 cfg의 KB 폴더(`paths.kb`)·BM25 값(`kb`)·질의 상한(`retrieval`)으로 색인을 만든다.

    서버는 `tutor.startup`이 가드가 기동 때 읽은 설정(`app.state.cfg`)으로 한 번 부른다. 설정
    값이 `default_index()`를 만든 값과 같으면 그 색인을 같이 쓴다(테스트처럼 앱을 여러 번 띄울
    때 다시 만들지 않는다). 그래서 cfg가 기본 설정과 다르면 기본 색인도 한 번 만든다(배포에서는
    두 설정이 같아 한 번만 만든다). 배포 자료 파생물만 담고 요청 내용은 담지 않는다(TU-18).
    """
    settings, index = _default()
    if _settings(cfg) == settings:
        return index
    return KbIndex.from_dir(resolve_path(cfg.paths.kb), cfg)


def _settings(cfg: GuardConfig) -> tuple[str, int, float, float, int]:
    """색인이 cfg에서 읽는 값 전부(KB 폴더·n-gram·k1·b·질의 상한). 같으면 색인도 같다."""
    kb = cfg.kb
    return (str(resolve_path(cfg.paths.kb)), kb.ngram, kb.k1, kb.b, cfg.retrieval.max_query_chars)


@cache
def _default() -> tuple[tuple[str, int, float, float, int], KbIndex]:
    """처음 부를 때의 `load_config()`로 만든 색인과 그 설정 값."""
    cfg = load_config()
    return _settings(cfg), KbIndex.from_dir(resolve_path(cfg.paths.kb), cfg)


def default_index() -> KbIndex:
    """앱 state가 없는 쪽(로컬 측정·프로세스 안 handle·테스트)의 색인. 처음 부를 때 한 번 만든다.

    가드 설정은 `load_config()`로 읽는다(GUARD_CONFIG·GUARD_KB_DIR를 따른다).
    """
    return _default()[1]


async def retrieve(
    query: str,
    history: Sequence[str] = (),
    *,
    subject: str | None = None,
    k: int = DEFAULT_K,
    embedding: EmbeddingSearch | None = None,
    english_only_embedding: bool = False,
    index: KbIndex | None = None,
) -> list[Section]:
    """KB 섹션을 점수 높은 순으로 0개 이상 k개 이하 돌려준다.

    임베딩을 쓰지 않으면 겹치는 2-gram이 없을 때 빈 목록이다. 임베딩을 섞은 영어 질의는 BM25가
    아무것도 못 찾아도 임베딩 순위로 k개를 채운다.

    query는 판단 대상 학습자 발화, history는 창 안의 다른 학습자 발화다. 어느 발화를 넣을지는
    부르는 쪽이 고른다(버튼 문구는 빼서 넘긴다). 둘을 이어 한 질의로 쓰고 가드 설정
    retrieval.max_query_chars보다 길면 뒤쪽을 남긴다.
    subject는 과목 폴더 이름(python·prompt·finance)으로 거른다.
    embedding(기동 때 만든 임베딩 검색)을 주면 영어 위주 질의에 한해 질의 임베딩 순위를 BM25
    순위와 RRF로 섞는다(TU-11). 2-gram이 없는 질의는 임베딩도 부르지 않는다.
    english_only_embedding(`toggles.english_embedding`)이면 그 영어 질의는 BM25와 섞지 않고
    임베딩 순위만 쓴다(임베딩을 못 쓰면 BM25 순위). 한국어 질의에는 영향이 없다.
    index는 기동 때 만든 색인이다(서버는 `app.state.tutor_kb`). 없으면 `default_index()`를 쓴다.
    """
    if k <= 0:
        return []
    if index is None:
        index = default_index()
    text = "\n".join([*history, query])[-index.max_query_chars :]
    ranked = [sid for sid, _score in index.search(text, subject=subject)]
    if embedding is not None and is_english(query) and bigrams(query):
        allowed = None
        if subject is not None:
            allowed = {sid for sid, section in index.sections.items() if section.subject == subject}
        ranked = await embedding.rerank(
            text, ranked, allowed=allowed, embedding_only=english_only_embedding
        )
    return [index.section(sid) for sid in ranked[:k]]
