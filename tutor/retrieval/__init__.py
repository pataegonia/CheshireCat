"""KB 색인·검색(TU-03, PRD 담당 표의 B).

BM25는 가드 GR-03 `KbIndex`를 그대로 쓰고, 섹션 접기·`retrieve`·임베딩은 튜터가 얹는다(TD-03).
"""

from tutor.retrieval.index import KbIndex
from tutor.retrieval.kb import Section
from tutor.retrieval.search import build_index, default_index, retrieve

__all__ = ["KbIndex", "Section", "build_index", "default_index", "retrieve"]
