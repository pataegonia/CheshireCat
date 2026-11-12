"""영어 위주 질의의 임베딩 혼합 검색(TU-11). BM25 순위와 질의 임베딩 순위를 RRF로 섞는다.

- KB 조각(BM25와 같은 조각)의 임베딩은 서버를 띄울 때 한 번 만든다(SPEC §7.1). 배포 자료
  파생물이고 요청 처리 중에는 바뀌지 않는다. 파일로 저장하지 않는다.
- 질의 임베딩은 요청 안에서만 쓰고 버린다(TU-18). 실패·시간 초과면 BM25 순위를 그대로 쓴다.
- 부르는 곳은 운영진 임베딩 API뿐이다(EMBEDDING_BASE_URL·EMBEDDING_MODEL·EMBEDDING_API_KEY).
"""

import asyncio
import logging
import math
import os
from collections.abc import Awaitable, Callable, Collection, Sequence
from dataclasses import dataclass, field
from typing import Any

import openai

from tutor.retrieval.index import KbIndex

log = logging.getLogger("tutor.retrieval")
Embed = Callable[[Sequence[str]], Awaitable[list[list[float]]]]
RRF_K = 60  # RRF 순위 상수(관례값)
BATCH = 64  # 기동 때 한 번에 보내는 KB 조각 수
BUILD_TIMEOUT_S = 60.0  # 기동 때 KB 임베딩 전체 상한(넘으면 BM25만 쓴다)
ENGLISH_SHARE = 0.5  # 글자 중 라틴 문자 비율이 이보다 크면 영어 질의


def is_english(text: str) -> bool:
    """글자 대부분이 라틴 문자인 질의. 한국어 KB와 문자 2-gram이 거의 겹치지 않는다(TU-11)."""
    letters = [ch for ch in text if ch.isalpha()]
    return bool(letters) and sum(ch.isascii() for ch in letters) / len(letters) > ENGLISH_SHARE


def rrf(*rankings: Sequence[str], k: int = RRF_K) -> list[str]:
    """순위들을 역순위 합(1 / (k + 순위))으로 섞는다. 점수가 같으면 ID 순."""
    score: dict[str, float] = {}
    for ranking in rankings:
        for rank, sid in enumerate(ranking, 1):
            score[sid] = score.get(sid, 0.0) + 1.0 / (k + rank)
    return sorted(score, key=lambda sid: (-score[sid], sid))


def _unit(vector: Sequence[float]) -> tuple[float, ...]:
    """단위 벡터. 영벡터·NaN·무한대처럼 비교할 수 없는 벡터면 ValueError."""
    norm = math.sqrt(sum(x * x for x in vector))
    if not math.isfinite(norm) or norm == 0.0:
        raise ValueError("비교할 수 없는 임베딩 벡터")
    return tuple(x / norm for x in vector)


@dataclass(frozen=True)
class EmbeddingSearch:
    """KB 조각 임베딩과 질의 임베딩 함수. 기동 때 build로 만들고 바꾸지 않는다."""

    embed: Embed = field(repr=False)
    chunk_ids: tuple[str, ...]  # 조각마다 섹션 ID(KbIndex.chunks 순서)
    vectors: tuple[tuple[float, ...], ...] = field(repr=False)  # 조각 임베딩(단위 벡터)
    timeout_s: float  # 질의 임베딩 호출 상한(설정 llm.timeout_s.embedding)

    @classmethod
    async def build(
        cls, index: KbIndex, embed: Embed, *, timeout_s: float, batch: int = BATCH
    ) -> "EmbeddingSearch":
        """KB 조각을 batch개씩 임베딩한다.

        받은 벡터 수가 조각 수와 다르거나, 차원이 섞였거나, 비교할 수 없는 벡터(영벡터·NaN)가
        있으면 ValueError다. 기동 때 한 번 잡아야 요청마다 조용히 BM25로 떨어지지 않는다.
        """
        texts = [text for _sid, text in index.chunks]
        vectors: list[list[float]] = []
        for start in range(0, len(texts), batch):
            vectors.extend(await embed(texts[start : start + batch]))
        if len(vectors) != len(texts):
            raise ValueError("임베딩 수가 KB 조각 수와 다르다")
        if len({len(v) for v in vectors}) != 1:
            raise ValueError("KB 조각 임베딩의 차원이 섞였다")
        chunk_ids = tuple(sid for sid, _text in index.chunks)
        return cls(embed, chunk_ids, tuple(_unit(v) for v in vectors), timeout_s)

    def ranking(self, query_vector: Sequence[float]) -> list[str]:
        """섹션 순위: 조각 코사인 유사도를 섹션 최고값으로 접는다(BM25와 같은 접기)."""
        q = _unit(query_vector)
        best: dict[str, float] = {}
        for sid, vector in zip(self.chunk_ids, self.vectors, strict=True):
            score = sum(a * b for a, b in zip(q, vector, strict=True))
            if sid not in best or score > best[sid]:
                best[sid] = score
        return sorted(best, key=lambda sid: (-best[sid], sid))

    async def rerank(
        self, text: str, ranked: Sequence[str], *, allowed: Collection[str] | None
    ) -> list[str]:
        """질의 임베딩 순위를 BM25 순위(ranked)와 RRF로 섞는다. allowed가 있으면 그 섹션만.

        질의 임베딩이 실패하거나 timeout_s를 넘으면 ranked를 그대로 돌려주고, 까닭은 예외 클래스
        이름만 경고로 남긴다(본문·메시지 없음, TU-15). 취소는 전파한다. 유사도 계산(조각 244개,
        수십 ms)은 이벤트 루프를 막지 않게 스레드에서 한다.
        """
        try:
            async with asyncio.timeout(self.timeout_s):
                vectors = await self.embed([text])
            found = await asyncio.to_thread(self.ranking, vectors[0])
        except Exception as exc:  # 시간 초과·API 오류·모양이 다른 벡터: BM25만 쓴다
            log.warning("질의 임베딩을 못 써서 BM25만 쓴다: %s", type(exc).__name__)
            return list(ranked)
        return rrf(ranked, [sid for sid in found if allowed is None or sid in allowed])


def openai_embed() -> Embed:
    """운영진 임베딩 API(OpenAI 호환 /embeddings). 클라이언트는 이벤트 루프마다 첫 호출 때 만든다.

    환경변수: EMBEDDING_API_KEY·EMBEDDING_MODEL(없으면 오류), EMBEDDING_BASE_URL(비면 SDK 기본).
    SDK 재시도는 0이다.
    """
    made: dict[str, Any] = {"loop": None, "client": None}

    async def embed(texts: Sequence[str]) -> list[list[float]]:
        key, model = os.environ.get("EMBEDDING_API_KEY"), os.environ.get("EMBEDDING_MODEL")
        if not key or not model:
            raise openai.OpenAIError("EMBEDDING_API_KEY·EMBEDDING_MODEL이 없다")
        loop = asyncio.get_running_loop()
        if made["loop"] is not loop:
            base_url = os.environ.get("EMBEDDING_BASE_URL") or None
            made["client"] = openai.AsyncOpenAI(api_key=key, base_url=base_url, max_retries=0)
            made["loop"] = loop
        resp = await made["client"].embeddings.create(model=model, input=list(texts))
        return [item.embedding for item in sorted(resp.data, key=lambda item: item.index)]

    return embed
