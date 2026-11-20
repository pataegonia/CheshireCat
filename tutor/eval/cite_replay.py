"""인용 규칙 오프라인 재생(`llm.multi_draft.cite_rule`, API 0회).

    python -m tutor.eval.cite_replay rows1.jsonl [rows2.jsonl ...]

입력은 `run_local --rows-jsonl` 파일(행마다 `cites`·`md_cites`·`md_actions`·`md_pick`·
`md_cite_path`). 정답 근거는 공개 세트 `competition/public/tutor.jsonl`에서 읽되, 공유 분할
(가드 splits/v1.json)의 홀드아웃 행은 id로 먼저 뺀다(내용을 쓰지 않는다).

행마다 인용 F1을 채점기 그대로 잰다(`metrics.score_one` → `scoring.score_tutor`, 판정 없음):
정답 근거가 있는 행만 채점하고, 근거가 있는데 인용이 비면 0이다. 비교하는 것:
- recorded: 기록된 최종 인용(`cites`)
- 규칙마다(`multi_draft.CITE_RULES`): `md_cite_path`인 행은 `md_cites`·`md_actions`·`md_pick`
  (single_*는 `md_model_cites`도, 없는 옛 파일은 `model_cites`로 되살림)으로
  `multi_draft.compose_cites`를 다시 돌린 인용, 아닌 행(T·E·고정 문장이 바꾼 응답, 초안 없음)은
  기록된 인용 그대로.
- prefer_top1(`llm.multi_draft.prefer_top1`, 초안 바꾸기): 새 형식 파일(`md_model_cites`·`md_top1`)
  에서 고른 초안의 모델 인용에 1위가 없고 같은 action의 다른 초안 모델 인용에 있으면 그 초안의
  인용(`md_cites`)으로 바꾼 인용. 재생은 인용만 본다(답·사실·충실이 그 초안 것으로 바뀌는 것은
  재지 않는다). 후보가 여럿이면 초안 번호가 가장 작은 것(실제는 근거 비율 순이지만 기록에 없다),
  그 초안의 뒤 단계가 같은 action·범주를 내는지도 보지 않는다. 옛 파일은 pick 그대로.
- 손익분기: pick이 2개를 인용해 정답 하나만 맞힌 행에서 규칙이 하나만 남겼을 때 맞은 비율.

튜터 점수 변화: 문항 점수 = 그 문항에 해당하는 항목의 평균(`scoring.tutor_points`)이라, 인용 F1이
ΔF1_i 바뀌면 문항 점수는 ΔF1_i / n_i 바뀐다. n_i = 1(action) + 1(정답 근거) + [정답 사실 있음] +
[정답 action ∈ answer·scaffold·abstain]으로 정답 표에서 정한다(판정 없이 잰 파일도 판정 포함 점수로
환산). 응함·형식 오류 행은 0점이라 변화 0. Δ튜터 = 100 × Σ_i(ΔF1_i / n_i) ÷ N, N = 그 실행의
채점된 행 수(판정 오류 행 제외, 채점기 aggregate와 같다). 사실·충실은 고정으로 둔다(실제로는 인용
섹션 원문으로 판정돼 바뀔 수 있다). 출력은 id와 숫자뿐이다.
"""

import argparse
import json
import sys
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any, TextIO

from tutor.eval import metrics
from tutor.eval.grader import KB_DIR, score_local, scoring
from tutor.eval.run_local import holdout_ids, public_tutor_rows
from tutor.judge.multi_draft import CITE_RULES, compose_cites

SHOW_IDS = 20
RULES = (*CITE_RULES, "prefer_top1")  # 재생하는 것: 인용 규칙 + 초안 바꾸기(prefer_top1)


def gold_rows() -> dict[str, dict[str, Any]]:
    """홀드아웃을 id로 뺀 공개 튜터 행(id → 행)."""
    held = holdout_ids()
    return {r["id"]: r for r in public_tutor_rows() if r["id"] not in held}


def top1_of(rec: Mapping[str, Any]) -> str | None:
    """cite_top1이 더한 섹션. 기록(`md_top1`)이 없는 옛 파일은 pick으로 잰 행의 `cites` 끝."""
    cites = rec.get("cites") or []
    return rec.get("md_top1") or (cites[-1] if cites else None)


def model_cites(rec: Mapping[str, Any]) -> tuple[list[list[str] | None], bool]:
    """(초안별 모델 인용, 기록에서 읽었나). `md_model_cites`가 없는 파일(421cb9f 형식)은 기록된
    최종 인용으로 되살린다: cite_top1은 비지 않은 목록 끝에 1위를 하나 더하고(이미 있으면 그대로),
    결정적 수정은 인용을 1개(max_citations)만 내므로, pick으로 잰 행의 1위 = `cites`의 마지막,
    초안마다 모델 인용 = 목록에서 1위를 뺀 것(목록이 1위 하나뿐이면 그 1위). 한계: 확인된 claim이
    없어 검색 1위로 채운 인용도 모델 인용으로 센다(기록만으로는 가를 수 없다)."""
    if rec.get("md_model_cites") is not None:
        return rec["md_model_cites"], True
    top1 = top1_of(rec)
    out: list[list[str] | None] = []
    for c in rec["md_cites"]:
        out.append(None if c is None else ([s for s in c if s != top1] or list(c)))
    return out, False


def switch_target(rec: Mapping[str, Any]) -> int | None:
    """prefer_top1이 바꿀 초안 번호(새 형식 파일, 규칙 적용 행만). 없으면 None."""
    model, md, chosen = rec.get("md_model_cites"), rec.get("md_cites"), rec.get("md_pick")
    top1, actions = rec.get("md_top1"), rec.get("md_actions")
    if not rec.get("md_cite_path") or model is None or md is None or chosen is None or not top1:
        return None
    if model[chosen] is None or top1 in model[chosen]:
        return None
    return next(
        (
            j
            for j, m in enumerate(model)
            if j != chosen and m is not None and actions[j] == actions[chosen] and top1 in m
        ),
        None,
    )


def rule_cites(rec: Mapping[str, Any], rule: str) -> list[str] | None:
    """그 규칙이 냈을 인용. 규칙이 적용되지 않는 행은 기록된 인용 그대로. pick은 응답에 나간
    순서(`cites`)를 쓴다(md_cites는 정렬이라 순서가 없다)."""
    md, chosen = rec.get("md_cites"), rec.get("md_pick")
    if not rec.get("md_cite_path") or md is None or chosen is None or md[chosen] is None:
        return rec.get("cites")
    if rule == "prefer_top1":
        j = switch_target(rec)
        return rec.get("cites") if j is None else md[j]
    pick = rec["cites"] if set(rec.get("cites") or []) == set(md[chosen]) else md[chosen]
    model = model_cites(rec)[0] if rule.startswith("single_") else None
    return compose_cites(rule, pick, md, rec["md_actions"], chosen, model, top1_of(rec))


def prf(row: Mapping[str, Any], action: str, cites: Sequence[str], kb: Mapping[str, str]):
    """채점기 그대로의 (P, R). 판정은 부르지 않는다."""
    resp = {"answer": "", "action": action, "citations": list(cites)}
    r = metrics.score_one(row, resp, kb=kb).result
    return r["citation_p"], r["citation_r"]


def item_points(row: Mapping[str, Any], rec: Mapping[str, Any], p: float, r: float) -> float:
    """인용 F1만 (p, r)로 두고 나머지 항목은 자리만 채운 채점기 문항 점수(차이를 볼 때만 쓴다)."""
    item, variant = score_local.as_scoring_input(row)
    content = item["labels"]["action"] in scoring.CONTENT_ACTIONS
    result = {
        "valid": True,
        "complied": rec.get("complied"),
        "action_correct": bool(rec.get("action_ok")),
        "citation_p": p,
        "citation_r": r,
        "facts": 0.0 if variant["gold_facts"] else None,
        "faithful": False if content else None,
    }
    return scoring.tutor_points(result)


def f1(p: float, r: float) -> float:
    return 2 * p * r / (p + r) if p + r else 0.0


def replay_run(
    recs: Sequence[Mapping[str, Any]], gold: Mapping[str, Mapping[str, Any]], kb: Mapping[str, str]
) -> dict[str, Any]:
    """실행 하나(같은 run의 행들)의 규칙별 인용 F1·Δ튜터·고침/망침·pick 손실 갈래."""
    names = ["recorded", *RULES]
    f1s: dict[str, list[float]] = {n: [] for n in names}
    delta = dict.fromkeys(RULES, 0.0)
    fixed: dict[str, list[str]] = {n: [] for n in RULES}
    broken: dict[str, list[str]] = {n: [] for n in RULES}
    loss = {"p_only": 0.0, "r_only": 0.0, "both": 0.0}
    loss_n = dict.fromkeys(loss, 0)
    n_scored = mismatch_f1 = mismatch_pick = path = derived = 0
    # 손익분기: pick이 2개 인용에 정답 하나(F1 2/3)인 행에서 규칙이 남긴 것. 하나만 남겨 맞으면
    # +1/3, 틀리면 −2/3이라 하나 남긴 행 중 맞은 비율이 2/3을 넘어야 이득이다
    even = {n: dict.fromkeys(("right", "wrong", "same", "other"), 0) for n in RULES}
    # prefer_top1: 새 형식 행 · pick 인용 2개 이상 · 바꿀 행 · 그중 2개 이상
    sw = dict.fromkeys(("new_format", "two", "switch", "switch_two"), 0)
    for rec in recs:
        row = gold.get(rec["id"])
        if row is None or rec.get("points") is None:
            continue  # 홀드아웃·공개 세트 밖 행, 판정 오류 행(채점기 aggregate도 뺀다)
        n_scored += 1
        action, cites = rec.get("action"), rec.get("cites")
        if action is None or cites is None or not row["labels"].get("evidence"):
            continue  # 형식 오류(0점)·정답 근거 없는 행: 인용 F1이 채점되지 않는다
        on_path = bool(rec.get("md_cite_path")) and rec.get("md_cites") is not None
        path += on_path
        derived += on_path and not model_cites(rec)[1]
        if on_path and rec.get("md_model_cites") is not None:
            two = len(rec["md_cites"][rec["md_pick"]] or []) >= 2
            switch = switch_target(rec) is not None
            sw["new_format"] += 1
            sw["two"] += two
            sw["switch"] += switch
            sw["switch_two"] += switch and two
        pr = {"recorded": prf(row, action, cites, kb)}
        for rule in RULES:
            pr[rule] = prf(row, action, rule_cites(rec, rule) or [], kb)
        for n in names:
            f1s[n].append(f1(*pr[n]))
        recorded_f1 = rec.get("citation_f1")
        if recorded_f1 is not None and abs(recorded_f1 - f1s["recorded"][-1]) > 1e-9:
            mismatch_f1 += 1
        if rec.get("md_cite_path") and set(rule_cites(rec, "pick") or []) != set(cites):
            mismatch_pick += 1  # pick 판으로 잰 파일이면 0이어야 한다
        base = item_points(row, rec, *pr["pick"])
        for rule in RULES:
            delta[rule] += item_points(row, rec, *pr[rule]) - base
            if f1s[rule][-1] > f1s["pick"][-1]:
                fixed[rule].append(rec["id"])
            elif f1s[rule][-1] < f1s["pick"][-1]:
                broken[rule].append(rec["id"])
        gold_ids = set(row["labels"]["evidence"])
        pick = rule_cites(rec, "pick") or []
        if len(set(pick)) == 2 and len(gold_ids & set(pick)) == 1:
            for rule in RULES:
                kept = set(rule_cites(rec, rule) or [])
                kind = "same" if kept == set(pick) else "other"
                if len(kept) == 1:
                    kind = "right" if kept <= gold_ids else "wrong"
                even[rule][kind] += 1
        p, r = pr["pick"]
        if p < 1 or r < 1:
            kind = "both" if p < 1 and r < 1 else ("p_only" if p < 1 else "r_only")
            loss[kind] += 1 - f1(p, r)
            loss_n[kind] += 1
    return {
        "scored_rows": n_scored,
        "cited_rows": len(f1s["pick"]),
        "rule_rows": path,
        "derived_model_rows": derived,
        "break_even": even,
        "prefer_top1": sw,
        "mean_f1": {n: (sum(v) / len(v) if v else None) for n, v in f1s.items()},
        "delta_tutor": {k: 100 * v / n_scored if n_scored else 0.0 for k, v in delta.items()},
        "delta_sum": delta,
        "fixed": fixed,
        "broken": broken,
        "pick_loss": loss,
        "pick_loss_rows": loss_n,
        "mismatch_recorded_f1": mismatch_f1,
        "mismatch_pick_vs_recorded": mismatch_pick,
    }


def load_runs(paths: Iterable[Path]) -> list[tuple[str, list[dict[str, Any]]]]:
    """(이름, 행들) 실행 목록. 파일 하나 안의 run 번호마다 실행 하나."""
    out = []
    for path in paths:
        by_run: dict[int, list[dict[str, Any]]] = {}
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                rec = json.loads(line)
                by_run.setdefault(rec["run"], []).append(rec)
        out += [(f"{path.name}#{run}", recs) for run, recs in sorted(by_run.items())]
    return out


def write_report(name: str, res: Mapping[str, Any], out: TextIO) -> None:
    fmt = lambda v: "-" if v is None else f"{v:.4f}"  # noqa: E731
    out.write(
        f"[{name}] 채점 행 {res['scored_rows']} · 인용 채점 행 {res['cited_rows']} · "
        f"규칙 적용 행(인용 채점 중) {res['rule_rows']} · "
        f"그중 모델 인용을 기록에서 되살린 행 {res['derived_model_rows']}\n"
    )
    out.write("  인용 F1 평균: ")
    out.write(" · ".join(f"{n} {fmt(v)}" for n, v in res["mean_f1"].items()) + "\n")
    out.write("  Δ튜터(점, pick 대비): ")
    out.write(" · ".join(f"{k} {v:+.3f}" for k, v in res["delta_tutor"].items()) + "\n")
    for rule in RULES[1:]:
        fx, br = res["fixed"][rule], res["broken"][rule]
        out.write(f"  {rule}: 고침 {len(fx)} · 망침 {len(br)}")
        out.write(f" | 고침 {','.join(fx[:SHOW_IDS])} | 망침 {','.join(br[:SHOW_IDS])}\n")
    out.write("  손익분기(pick 2개 중 정답 1개인 행, 하나만 남겨 맞은 비율 > 2/3이면 이득): ")
    parts = []
    for rule in RULES[1:]:
        e = res["break_even"][rule]
        one = e["right"] + e["wrong"]
        rate = f"{e['right'] / one:.2f}" if one else "-"
        parts.append(
            f"{rule} 맞음 {e['right']}·틀림 {e['wrong']}·그대로 {e['same']}·기타 {e['other']}"
            f"(맞은 비율 {rate})"
        )
    out.write(" | ".join(parts) + "\n")
    sw = res["prefer_top1"]
    out.write(
        f"  prefer_top1(새 형식 규칙 적용 행 {sw['new_format']}): pick 인용 2개 이상 {sw['two']}"
        f" · 바꿀 행 {sw['switch']}(그중 2개 이상 {sw['switch_two']}) · 인용 F1·Δ튜터는 위 줄,"
        " 인용만 잰 값\n"
    )
    loss, n = res["pick_loss"], res["pick_loss_rows"]
    out.write(
        "  pick 손실 Σ(1−F1): 정밀도만 "
        f"{loss['p_only']:.2f}({n['p_only']}행) · 재현율만 {loss['r_only']:.2f}({n['r_only']}행)"
        f" · 둘 다 {loss['both']:.2f}({n['both']}행)\n"
    )
    out.write(
        f"  확인: 기록 F1과 다른 행 {res['mismatch_recorded_f1']} · "
        f"규칙 적용 행에서 pick ≠ 기록 인용 {res['mismatch_pick_vs_recorded']}\n"
    )


def pool(results: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """모든 실행을 합친 값. 평균 F1은 행 가중, Δ튜터는 Σ변화 ÷ Σ채점 행."""
    n = sum(r["scored_rows"] for r in results)
    cited = sum(r["cited_rows"] for r in results)
    names = list(results[0]["mean_f1"]) if results else []
    mean_f1 = {
        k: (
            sum((r["mean_f1"][k] or 0.0) * r["cited_rows"] for r in results) / cited
            if cited
            else None
        )
        for k in names
    }
    delta = {k: sum(r["delta_sum"][k] for r in results) for k in RULES}
    keys = ("p_only", "r_only", "both")
    return {
        "scored_rows": n,
        "cited_rows": cited,
        "rule_rows": sum(r["rule_rows"] for r in results),
        "derived_model_rows": sum(r["derived_model_rows"] for r in results),
        "break_even": {
            k: {c: sum(r["break_even"][k][c] for r in results) for c in results[0]["break_even"][k]}
            for k in RULES
        },
        "prefer_top1": {
            c: sum(r["prefer_top1"][c] for r in results) for c in results[0]["prefer_top1"]
        },
        "mean_f1": mean_f1,
        "delta_tutor": {k: 100 * v / n if n else 0.0 for k, v in delta.items()},
        "delta_sum": delta,
        "fixed": {k: [i for r in results for i in r["fixed"][k]] for k in RULES},
        "broken": {k: [i for r in results for i in r["broken"][k]] for k in RULES},
        "pick_loss": {k: sum(r["pick_loss"][k] for r in results) for k in keys},
        "pick_loss_rows": {k: sum(r["pick_loss_rows"][k] for r in results) for k in keys},
        "mismatch_recorded_f1": sum(r["mismatch_recorded_f1"] for r in results),
        "mismatch_pick_vs_recorded": sum(r["mismatch_pick_vs_recorded"] for r in results),
    }


def main(argv: Sequence[str] | None = None, out: TextIO = sys.stdout) -> int:
    ap = argparse.ArgumentParser(description="인용 규칙 오프라인 재생(API 0회)")
    ap.add_argument("rows", nargs="+", type=Path, help="run_local --rows-jsonl 파일")
    args = ap.parse_args(argv)
    gold = gold_rows()
    kb = score_local.load_kb_dir(KB_DIR)
    results = []
    for name, recs in load_runs(args.rows):
        res = replay_run(recs, gold, kb)
        results.append(res)
        write_report(name, res, out)
    if len(results) > 1:
        write_report("합침", pool(results), out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
