"""T 관문 영향 점검(측정 전용, API 0회).

저장소 루트에서 `python -m harness.veto_check <결과 JSON …>`.

동결 후보 실행의 row_trace T 이름표가 "학습 질문"이면서 대행 신호(DELEGATION)가 걸린 행, 그리고
T 이름표가 "학습 질문"이 아닌데 주 호출이 실패한 행(인용 답 관문에 걸리는 행)을 센다.
행 id·정답 라벨·낸 action만 출력한다(본문 없음).
"""

import json
import sys
from pathlib import Path

from tutor.eval.run_local import public_tutor_rows
from tutor.judge.conversation import tidy
from tutor.judge.flow import _messages
from tutor.judge.signals import Signal, detect
from tutor.request import parse_request


def main() -> int:
    gold = {r["id"]: r for r in public_tutor_rows()}
    deleg = {}
    for rid, row in gold.items():
        conv = tidy(_messages(parse_request({"messages": row["messages"]})))
        deleg[rid] = Signal.DELEGATION in detect(conv)
    veto, gate = [], []
    for path in sys.argv[1:]:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        emitted_runs = data["report"]["cv_emitted_by_run"]
        traces = data["row_trace"]["by_run"]
        for emitted, trace in zip(emitted_runs, traces, strict=True):
            for rid, t in trace.items():
                lab = gold[rid]["labels"]
                got = emitted[rid].get("action")
                tag = f"{Path(path).stem[-8:]} {rid} {lab['category']}/{lab['action']} 낸 {got}"
                if t.get("intent") == "학습 질문" and deleg.get(rid):
                    veto.append(tag)
                if t.get("main_call") == "failed" and t.get("intent") != "학습 질문":
                    gate.append(f"{tag} T {t.get('intent')}")
    out = sys.stdout
    out.write(f"대행 신호 + T 학습 질문(거부권으로 바뀌는 행×실행) {len(veto)}\n")
    out.write("".join(f"  {line}\n" for line in veto))
    out.write(f"주 호출 실패 + T 학습 질문 아님(인용 답 관문에 걸리는 행×실행) {len(gate)}\n")
    out.write("".join(f"  {line}\n" for line in gate))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
