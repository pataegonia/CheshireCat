"""영어 위주 질의의 임베딩 혼합 검색(TU-11). BM25 순위와 질의 임베딩 순위를 RRF로 섞는다.

- KB 조각(BM25와 같은 조각)의 임베딩은 서버를 띄울 때 한 번 만든다(SPEC §7.1). 배포 자료
  파생물이고 요청 처리 중에는 바뀌지 않는다. 파일로 저장하지 않는다.
- 질의 임베딩은 요청 안에서만 쓰고 버린다(TU-18). 실패·시간 초과면 BM25 순위를 그대로 쓴다.
  `toggles.share_query_embedding`이면 한 요청 안에서 같은 질의 문자열은 한 번만 부르고 나눠 쓴다
  (`for_request`, `RequestEmbeddings`). 요청 사이에는 나누지 않는다.
- 질의 임베딩 호출은 튜터 LLM과 같은 게이트(서버는 가드 전역 slot)를 쓴다(같은 팀 키, TU-13).
- 유사도는 기동 때 만든 단위 벡터 행렬과의 행렬곱 한 번이다(조각 244개에 1ms 아래, 스레드 없음).
- 부르는 곳은 운영진 임베딩 API뿐이다(EMBEDDING_BASE_URL·EMBEDDING_MODEL·EMBEDDING_API_KEY).
"""

import asyncio
import contextlib
import dataclasses
import logging
import os
from collections.abc import Awaitable, Callable, Collection, Sequence
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import openai

from tutor.retrieval.index import KbIndex

log = logging.getLogger("tutor.retrieval")
Embed = Callable[[Sequence[str]], Awaitable[list[list[float]]]]
Gate = Callable[[], AbstractAsyncContextManager[Any]]
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


def _unit_rows(matrix: np.ndarray) -> np.ndarray:
    """마지막 축마다 단위 벡터. 영벡터·NaN·무한대처럼 비교할 수 없는 벡터가 있으면 ValueError."""
    norms = np.linalg.norm(matrix, axis=-1, keepdims=True)
    if not np.all(np.isfinite(norms)) or np.any(norms == 0.0):
        raise ValueError("비교할 수 없는 임베딩 벡터")
    return matrix / norms


def _cancelling() -> bool:
    """지금 작업이 취소되고 있는가(바깥 취소·asyncio.timeout)."""
    task = asyncio.current_task()
    return task is not None and task.cancelling() > 0


@dataclass
class _Shared:
    task: "asyncio.Task[list[float]]"
    waiters: int = 0


class RequestEmbeddings:
    """한 요청 안의 질의 임베딩 나눠 쓰기(`toggles.share_query_embedding`).

    같은 질의 문자열이면 진행 중이거나 성공한 호출 하나를 함께 기다린다. 실패(예외)한 호출은
    지우고 나누지 않아 뒤에 오는 쪽은 새로 부른다. 기다리는 쪽 하나가 시간 초과·취소로 빠져도
    다른 쪽의 호출은 이어 가고(asyncio.shield), 아무도 기다리지 않게 되면 그 호출을 취소한다(나눠
    쓰지 않을 때 시간 초과가 호출을 접던 것과 같다). 요청이 끝나면 `close`로 남은 호출을 모두
    취소하고 결과를 버린다. 벡터만 담고 질의 글은 키로만 쓴다(TU-18, 요청 밖으로 나가지 않는다).
    """

    def __init__(self, call: Callable[[str], Awaitable[list[float]]]) -> None:
        self._call = call
        self._jobs: dict[str, _Shared] = {}
        self.calls = 0  # 실제로 부른 수(테스트·측정용)

    async def vector(self, text: str) -> list[float]:
        job = self._jobs.get(text)
        if job is None or (job.task.done() and (job.task.cancelled() or job.task.exception())):
            self.calls += 1
            job = _Shared(asyncio.ensure_future(self._call(text)))
            self._jobs[text] = job
        job.waiters += 1
        try:
            return await asyncio.shield(job.task)
        except asyncio.CancelledError:
            if job.task.cancelled() and not _cancelling():
                # 나눠 쓰던 호출이 다른 쪽 사정으로 취소됐다: 이쪽에는 실패로 돌려준다
                raise RuntimeError("나눠 쓰던 질의 임베딩이 취소됐다") from None
            raise
        finally:
            job.waiters -= 1
            if job.task.done() and not job.task.cancelled() and job.task.exception() is not None:
                if self._jobs.get(text) is job:
                    del self._jobs[text]  # 실패는 나누지 않는다
            elif job.waiters == 0 and not job.task.done():
                job.task.cancel()
                if self._jobs.get(text) is job:
                    del self._jobs[text]

    def close(self) -> None:
        """요청이 끝날 때: 남은 호출을 취소하고 모두 버린다."""
        for job in self._jobs.values():
            if not job.task.done():
                job.task.cancel()
        self._jobs.clear()


@dataclass(frozen=True)
class EmbeddingSearch:
    """KB 조각 임베딩과 질의 임베딩 함수. 기동 때 build로 만들고 바꾸지 않는다."""

    embed: Embed = field(repr=False)
    chunk_ids: tuple[str, ...]  # 조각마다 섹션 ID(KbIndex.chunks 순서)
    matrix: np.ndarray = field(repr=False, compare=False)  # 조각 임베딩 단위 벡터(조각 수 × 차원)
    timeout_s: float  # 질의 임베딩 호출 상한(설정 llm.timeout_s.embedding). 게이트 대기도 포함
    gate: Gate = field(default=contextlib.nullcontext, repr=False, compare=False)
    # 요청 하나에서 질의 임베딩을 나눠 쓸 때만(`for_request`). 기동 때 만든 것은 None
    shared: RequestEmbeddings | None = field(default=None, repr=False, compare=False)

    def for_request(self) -> "EmbeddingSearch":
        """이 요청 동안 같은 질의 임베딩을 나눠 쓰는 사본. 요청이 끝나면 `shared.close()`."""
        return dataclasses.replace(self, shared=RequestEmbeddings(self._query_vector))

    async def _query_vector(self, text: str) -> list[float]:
        async with self.gate():
            vectors = await self.embed([text])
        return vectors[0]

    @classmethod
    async def build(
        cls,
        index: KbIndex,
        embed: Embed,
        *,
        timeout_s: float,
        batch: int = BATCH,
        gate: Gate = contextlib.nullcontext,
    ) -> "EmbeddingSearch":
        """KB 조각을 batch개씩 임베딩한다.

        묶음마다 받은 벡터 수가 보낸 조각 수와 다르거나(총수만 맞아도 조각과 어긋난다), 차원이
        섞였거나, 비교할 수 없는 벡터(영벡터·NaN·무한대)가 있으면 ValueError다. 기동 때 한 번
        잡아야 요청마다 조용히 BM25로 떨어지거나 틀린 조각과 비교하지 않는다.
        """
        texts = [text for _sid, text in index.chunks]
        vectors: list[list[float]] = []
        for start in range(0, len(texts), batch):
            sent = texts[start : start + batch]
            got = await embed(sent)
            if len(got) != len(sent):
                raise ValueError("임베딩 수가 보낸 KB 조각 수와 다르다")
            vectors.extend(got)
        if len({len(v) for v in vectors}) != 1:
            raise ValueError("KB 조각 임베딩의 차원이 섞였다")
        chunk_ids = tuple(sid for sid, _text in index.chunks)
        matrix = _unit_rows(np.asarray(vectors, dtype=np.float64))
        return cls(embed, chunk_ids, matrix, timeout_s, gate)

    def ranking(self, query_vector: Sequence[float]) -> list[str]:
        """섹션 순위: 조각 코사인 유사도(행렬곱 한 번)를 섹션 최고값으로 접는다(BM25와 같은 접기).

        차원이 다르거나 비교할 수 없는 질의 벡터(영벡터·NaN)면 ValueError.
        """
        q = np.asarray(query_vector, dtype=np.float64)
        if q.shape != self.matrix.shape[1:]:
            raise ValueError("질의 임베딩의 차원이 KB 조각과 다르다")
        scores = self.matrix @ _unit_rows(q)
        best: dict[str, float] = {}
        for sid, score in zip(self.chunk_ids, scores.tolist(), strict=True):
            if sid not in best or score > best[sid]:
                best[sid] = score
        return sorted(best, key=lambda sid: (-best[sid], sid))

    async def rerank(
        self,
        text: str,
        ranked: Sequence[str],
        *,
        allowed: Collection[str] | None,
        embedding_only: bool = False,
    ) -> list[str]:
        """질의 임베딩 순위를 BM25 순위(ranked)와 RRF로 섞는다. allowed가 있으면 그 섹션만.
        embedding_only면 섞지 않고 임베딩 순위만 돌려준다(`toggles.english_embedding`).

        `shared`가 있으면 같은 요청의 같은 질의 임베딩을 나눠 쓴다(`RequestEmbeddings`).
        질의 임베딩이 실패하거나 timeout_s(게이트 대기 포함)를 넘으면 ranked를 그대로 돌려주고,
        까닭은 예외 클래스 이름만 경고로 남긴다(본문·메시지 없음, TU-15). 취소는 전파한다.
        """
        try:
            async with asyncio.timeout(self.timeout_s):
                if self.shared is not None:
                    vector = await self.shared.vector(text)
                else:
                    vector = await self._query_vector(text)
            found = self.ranking(vector)
        except Exception as exc:  # 시간 초과·API 오류·모양이 다른 벡터: BM25만 쓴다
            log.warning("질의 임베딩을 못 써서 BM25만 쓴다: %s", type(exc).__name__)
            return list(ranked)
        by_embedding = [sid for sid in found if allowed is None or sid in allowed]
        return by_embedding if embedding_only else rrf(ranked, by_embedding)


def openai_embed() -> Embed:
    """운영진 임베딩 API(OpenAI 호환 /embeddings). 클라이언트는 이벤트 루프마다 첫 호출 때 만든다.

    환경변수: EMBEDDING_API_KEY·EMBEDDING_MODEL·EMBEDDING_BASE_URL. 하나라도 비면 클라이언트를
    만들지 않고 오류다. 주소가 비면 SDK가 기본 주소(공개 OpenAI)로 보내기 때문이다(운영진 API만
    부른다, SPEC §7). SDK 재시도는 0이다.
    """
    made: dict[str, Any] = {"loop": None, "client": None}

    async def embed(texts: Sequence[str]) -> list[list[float]]:
        key, model, base_url = (
            (os.environ.get(name) or "").strip()
            for name in ("EMBEDDING_API_KEY", "EMBEDDING_MODEL", "EMBEDDING_BASE_URL")
        )
        if not (key and model and base_url):
            raise openai.OpenAIError("EMBEDDING_API_KEY·EMBEDDING_MODEL·EMBEDDING_BASE_URL이 없다")
        loop = asyncio.get_running_loop()
        if made["loop"] is not loop:
            made["client"] = openai.AsyncOpenAI(api_key=key, base_url=base_url, max_retries=0)
            made["loop"] = loop
        resp = await made["client"].embeddings.create(model=model, input=list(texts))
        return [item.embedding for item in sorted(resp.data, key=lambda item: item.index)]

    return embed
