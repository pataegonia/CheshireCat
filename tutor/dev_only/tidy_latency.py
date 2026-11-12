"""개발용: TU-02 대화 정리 지연(p50·p95·최대) 측정. 서버는 이 파일을 읽지 않는다.

    uv run --project guardrail --no-sync python -m tutor.dev_only.tidy_latency

수용 기준은 공개 세트 튜터 200행 p95 ≤10ms다. 벽시계 측정이라 CI 테스트에서 빼고 여기서 잰다.
출력은 시간 수치뿐이다(본문 출력 없음).
"""

import json
import statistics
import sys
import time
from pathlib import Path

from tutor.judge.conversation import tidy

ROOT = Path(__file__).resolve().parents[2]


def main(repeat: int = 5) -> None:
    path = ROOT / "competition" / "public" / "tutor.jsonl"
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    samples = []
    for _ in range(repeat):
        for row in rows:
            t0 = time.perf_counter()
            tidy(row["messages"])
            samples.append((time.perf_counter() - t0) * 1000)
    samples.sort()
    p95 = samples[int(len(samples) * 0.95) - 1]
    sys.stdout.write(
        f"rows {len(rows)} x {repeat}: p50 {statistics.median(samples):.3f}ms "
        f"p95 {p95:.3f}ms max {samples[-1]:.3f}ms\n"
    )


if __name__ == "__main__":
    main()
