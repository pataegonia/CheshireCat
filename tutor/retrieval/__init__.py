"""KB 색인·검색(TU-03, PRD 담당 표의 B).

가드 GR-03 `KbIndex`가 생기기 전까지 같은 방식을 튜터에서 기동한다.
"""

from tutor.retrieval.index import KbIndex
from tutor.retrieval.kb import Section
from tutor.retrieval.search import default_index, retrieve

__all__ = ["KbIndex", "Section", "default_index", "retrieve"]
