"""S3.2 P0: 입력 오판 행의 원인을 저장된 실행으로 가른다(API 0회).

실행(guardrail/): `uv run python -X utf8 scripts/r14/input_v2_causes.py <runs 폴더> [--evidence]`
행마다 변형별 오판 횟수와, 그때 nano가 본 few-shot 예시(교차 검증 폴드로 재현)를 함께 낸다.
`--evidence`는 nano의 근거 문장도 찍는다(문항 내용이 드러나므로 문서에 옮기지 않는다).
"""

import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

from guardrail import knn
from guardrail.config import load_config
from guardrail.data import labeled_rows, load_public
from guardrail.eval import cv
from guardrail.schema import decision_of

root = Path(__file__).resolve().parents[2]
cfg = load_config(root / "configs" / "default.yaml")
B = frozenset(cfg.block_categories)
rows = labeled_rows(load_public(cfg.paths.public))
by = {r.id: r for r in rows}
split = cv.load_splits()
runs_dir = Path(sys.argv[1])
EVID = "--evidence" in sys.argv

# 변형 → [(실행 이름, 회차)]. s29-r10은 앞 3회만 유효(4회차부터 429)
GROUPS = {
    "v1": [("v1-input-1113", 0), ("v1-input-1113-r2", 0), ("v1-input-1113-r2", 1)]
    + [(n, k) for n in ("s29-merged", "s29-r10") for k in range(3)],
    "nofs": [("s-a-nofewshot", 0), ("nofewshot-r2", 0), ("nofewshot-r2", 1)],
    "act": [("s-f-act", 0)],
    "med": [("s-g-medium", 0)],
    "v2a": [("s-b-v2a", 0), ("s-c-v2a-first", 0)],
    "v2a0": [("s-e-v2a-nofewshot", 0)],
    "subl": [("s-d-sublinear", 0)],
}
cache: dict[str, list[dict]] = {}


def load(name: str, rep: int) -> dict[str, dict]:
    if name not in cache:
        text = (runs_dir / name / "responses.jsonl").read_text("utf-8")
        cache[name] = [json.loads(x) for x in text.splitlines()]
    # nano가 답한 행만(실패 기본값·저신호 직접 판정은 뺀다)
    return {r["id"]: r for r in cache[name] if r["repeat"] == rep and r["source"] == "nano"}


runs = {g: [load(n, k) for n, k in spec] for g, spec in GROUPS.items()}

# 그때 nano가 본 예시를 폴드 학습 행으로 다시 만든다
shots: dict[str, list] = {}
sim_top: dict[str, float] = {}
sim_opp: dict[str, float] = {}
for k in range(split.folds):
    idx = knn.KnnIndex(cv.train_rows(split, rows, fold=k), cfg)
    for r in rows:
        if split.fold.get(split.cluster[r.id]) != k:
            continue
        shots[r.id] = idx.fewshot(r.text)
        sims = (idx._matrix @ idx._vec.transform([r.text]).T).toarray().ravel()  # noqa: SLF001
        gold_side = decision_of(r.category, B)
        sim_top[r.id] = float(sims.max())
        sim_opp[r.id] = max(
            (
                float(s)
                for s, t in zip(sims, idx.rows, strict=True)
                if decision_of(t.category, B) != gold_side
            ),
            default=0.0,
        )
mates = defaultdict(list)
for r in rows:
    mates[split.cluster[r.id]].append(r)


def pred(rec: dict) -> str:
    return rec["response"]["input"]["category"]


def wrong(g: str, i: str) -> tuple[int, int]:
    """(block 판정이 틀린 횟수, nano가 답한 횟수)."""
    got = [pred(run[i]) for run in runs[g] if i in run]
    return sum((p in B) != (by[i].category in B) for p in got), len(got)


def out(s: str = "") -> None:
    sys.stdout.write(s + "\n")


SHORT = {
    "normal": "N", "answer_seeking": "AS", "academic_dishonesty": "AD", "off_topic": "OT",
    "out_of_material": "OOM", "jailbreak_injection": "JB", "pii_disclosure": "PII",
    "harmful_content": "HC", "emotional_distress": "ED", "low_signal_input": "LS",
}  # fmt: skip
ids = [i for i in shots if by[i].endpoint == "guard" and wrong("v1", i)[1]]
bad = sorted((i for i in ids if wrong("v1", i)[0]), key=lambda i: (-wrong("v1", i)[0], i))
out(f"가드 {len(ids)}행 중 v1 9회에서 block 판정이 한 번이라도 틀린 행 {len(bad)}")
out(
    "id | 정답 | v1 오답(최빈) | "
    + " | ".join(GROUPS)
    + " | 예시(먼→가까운) | 끝 | 최고·반대쪽 유사도 | 짝"
)
cause = {}
for i in bad:
    row = by[i]
    modal = Counter(
        pred(run[i]) for run in runs["v1"] if (pred(run[i]) in B) != (row.category in B)
    ).most_common(1)[0][0]
    w = {g: wrong(g, i) for g in GROUPS}
    ex = [SHORT[s.category] for s in shots[i]]
    mate = [SHORT[m.category] for m in mates[split.cluster[i]] if m.id != i]
    v1w, nfw = w["v1"][0], w["nofs"][0]
    if v1w <= 2:
        c = "잡음(1–2/9)"
    elif nfw == 0:
        c = "예시"
    elif nfw >= 2:
        c = "문구·대응" + ("(act가 고침)" if w["act"] == (0, 1) else "")
    else:
        c = "섞임"
    cause[i] = c
    out(
        f"{i} | {SHORT[row.category]} | {SHORT[modal]} | "
        + " | ".join(f"{a}/{n}" if n else "-" for a, n in w.values())
        + f" | {' '.join(ex)} | {ex[-1] if ex else '-'} | {sim_top[i]:.2f}·{sim_opp[i]:.2f}"
        + f" | {','.join(mate) or '-'} | {c}"
    )
    if EVID:
        for g in ("v1", "nofs", "act", "med"):
            for run in runs[g]:
                # 조립본 기록은 출력 판정 호출이 앞에 올 수 있다. 입력 판정 출력만 고른다
                for e in run.get(i, {}).get("llm") or []:
                    parsed = e.get("parsed") or {}
                    if "category" in parsed or "act" in parsed:
                        out(f"      [{g}] {json.dumps(parsed, ensure_ascii=False)}")
out()
out("원인 분류: " + str(dict(Counter(cause.values()))))
by_type = defaultdict(Counter)
for i in bad:
    row = by[i]
    kind = "오탐(allow→block)" if row.category not in B else f"미탐({SHORT[row.category]})"
    by_type[kind][cause[i]] += 1
for kind, c in sorted(by_type.items()):
    out(f"  {kind}: {dict(c)}")

out()
out("== 예시 구성별 오판율(가드). 같은 행 묶음을 예시 없는 실행(nofs)으로도 본다: 차이가 예시의 몫")
feat = defaultdict(lambda: [0, 0, 0, 0, 0])
for i in ids:
    row = by[i]
    g = row.category in B
    ex = shots[i]
    side = "block" if g else "allow"
    keys = [
        (side, "전체"),
        (side, "끝=같은쪽" if (ex[-1].category in B) == g else "끝=반대쪽"),
        (side, f"block 예시 {min(max(sum(s.category in B for s in ex), 2), 4)}개(2↓·4↑ 묶음)"),
    ]
    if row.category == "normal":
        keys.append(("normal", f"JB 예시 {sum(s.category == 'jailbreak_injection' for s in ex)}개"))
    for key in keys:
        f = feat[key]
        for j, grp in enumerate(("v1", "nofs")):
            a, n = wrong(grp, i)
            f[2 * j] += a
            f[2 * j + 1] += n
        f[4] += 1
for key, (a, n, a0, n0, cnt) in sorted(feat.items()):
    out(
        f"  {key[0]} 행, {key[1]} ({cnt}행): v1 {a}/{n} = {a / n:.3f}"
        f" | nofs {a0}/{n0} = {a0 / n0:.3f}"
    )

out()
out("== 유형별 회당 건수(가드)")
types = {
    "N→JB": lambda g, p: g == "normal" and p == "jailbreak_injection",
    "allow→block 합": lambda g, p: g not in B and p in B,
    "JB→allow": lambda g, p: g == "jailbreak_injection" and p not in B,
    "HC→allow": lambda g, p: g == "harmful_content" and p not in B,
    "AD→allow": lambda g, p: g == "academic_dishonesty" and p not in B,
    "OT→allow": lambda g, p: g == "off_topic" and p not in B,
    "PII→N": lambda g, p: g == "pii_disclosure" and p == "normal",
}
for g, rs in runs.items():
    line = []
    for name, cond in types.items():
        vals = [sum(cond(by[i].category, pred(run[i])) for i in ids if i in run) for run in rs]
        line.append(f"{name} {sum(vals) / len(vals):.1f}")
    answered = [sum(i in run for i in ids) for run in rs]
    out(f"  {g}({len(rs)}회, 답한 행 {min(answered)}–{max(answered)}): " + ", ".join(line))
