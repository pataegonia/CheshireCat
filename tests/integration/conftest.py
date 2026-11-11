# I1.1 에서 workspace 가 생기면 삭제
"""루트 workspace(I1.1)가 생기기 전의 임시 경로 설정.

`python -m pytest tests/integration`은 저장소 루트만 sys.path에 넣는다. 가드 패키지가 설치돼 있지
않으면 `guardrail/src`를 더한다. I1.1에서 `uv sync`로 설치되면 이 파일은 지운다.
"""

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
# 루트의 guardrail/ 폴더는 네임스페이스 패키지로 잡힌다(origin이 None). 그건 설치된 게 아니다.
_spec = importlib.util.find_spec("guardrail")
if _spec is None or _spec.origin is None:
    sys.path.insert(0, str(ROOT / "guardrail" / "src"))
