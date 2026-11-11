"""S1.4 5폴드 + 홀드아웃 분할 (GR-13, ADR-0013, master-plan §4.5).

묶음 단위로 나눠 짝 문항이 폴드·홀드아웃에 걸치지 않게 한다. 파일 splits/v1.json은 만든 뒤
바꾸지 않는다(바꾸면 v2). CLI: python -m guardrail.eval.cv [--write]
"""

import argparse
import json
import random
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from guardrail.clusters import build_clusters
from guardrail.config import load_config
from guardrail.data import LabeledRow, labeled_rows, load_public

SPLIT_PATH = Path(__file__).parent / "splits" / "v1.json"
VERSION = "v1"
DEFAULT_SEED = 13
JACCARD = 0.5


@dataclass(frozen=True)
class Split:
    version: str
    jaccard: float
    folds: int
    seed: int
    cluster: dict[str, str]  # 행 id → 묶음 id
    fold: dict[str, int]  # 묶음 id → 폴드
    holdout: tuple[str, ...]  # 묶음 id (정렬)


def make_splits(
    rows: Iterable[LabeledRow],
    folds: int = 5,
    holdout: float = 0.2,
    seed: int = DEFAULT_SEED,
    jaccard: float = JACCARD,
) -> Split:
    rows = list(rows)
    cluster = build_clusters(rows, jaccard)
    guard_n: Counter[str] = Counter()
    total_n: Counter[str] = Counter()
    for r in rows:
        total_n[cluster[r.id]] += 1
        guard_n[cluster[r.id]] += r.endpoint == "guard"

    rng = random.Random(seed)  # noqa: S311  재현용 시드
    cids = sorted(total_n)
    rng.shuffle(cids)

    # 홀드아웃: 가드 행 기준 holdout 비율. 튜터 행만 있는 묶음은 넣지 않는다.
    target = round(holdout * sum(guard_n.values()))
    held: list[str] = []
    count = 0
    for cid in cids:
        if guard_n[cid] and count + guard_n[cid] <= target:
            held.append(cid)
            count += guard_n[cid]
    held_set = set(held)

    # 나머지: 큰 묶음부터 가드 행(다음 총 행) 수가 가장 적은 폴드에 넣는다.
    rest = sorted((c for c in cids if c not in held_set), key=lambda c: (-guard_n[c], -total_n[c]))
    load = [[0, 0] for _ in range(folds)]
    fold: dict[str, int] = {}
    for cid in rest:
        k = min(range(folds), key=lambda i: (load[i][0], load[i][1], i))
        fold[cid] = k
        load[k][0] += guard_n[cid]
        load[k][1] += total_n[cid]
    return Split(VERSION, jaccard, folds, seed, cluster, fold, tuple(sorted(held)))


def dumps(split: Split) -> bytes:
    body = {
        "version": split.version,
        "jaccard": split.jaccard,
        "folds": split.folds,
        "seed": split.seed,
        "cluster": split.cluster,
        "fold": split.fold,
        "holdout": list(split.holdout),
    }
    text = json.dumps(body, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    return text.encode("utf-8")


def load_splits(path: Path = SPLIT_PATH) -> Split:
    d = json.loads(path.read_text(encoding="utf-8"))
    return Split(
        d["version"],
        d["jaccard"],
        d["folds"],
        d["seed"],
        d["cluster"],
        d["fold"],
        tuple(d["holdout"]),
    )


def train_rows(
    split: Split, rows: Iterable[LabeledRow], *, fold: int | None = None, holdout: bool = False
) -> list[LabeledRow]:
    """fold=k: 폴드 k와 홀드아웃 묶음을 뺀다. holdout=True: 홀드아웃 묶음만 뺀다(ADR-0013)."""
    if (fold is None) == (not holdout):
        raise ValueError("fold와 holdout 중 하나만 지정한다")
    if fold is not None and not 0 <= fold < split.folds:
        raise ValueError(f"fold는 0..{split.folds - 1}")
    excluded = set(split.holdout)
    if fold is not None:
        excluded |= {c for c, k in split.fold.items() if k == fold}
    out = []
    for r in rows:
        if r.id not in split.cluster:
            raise ValueError(f"분할 파일에 없는 행: {r.id}")
        if split.cluster[r.id] not in excluded:
            out.append(r)
    return out


def write_splits(path: Path = SPLIT_PATH, seed: int = DEFAULT_SEED) -> Split:
    rows = labeled_rows(load_public(load_config().paths.public))
    split = make_splits(rows, seed=seed)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(dumps(split))
    return split


def summary(split: Split, rows: list[LabeledRow]) -> str:
    sizes = Counter(split.cluster.values())
    dist = Counter(sizes.values())
    held = set(split.holdout)
    held_cats = Counter(
        r.category for r in rows if r.endpoint == "guard" and split.cluster[r.id] in held
    )
    guard_held = sum(r.endpoint == "guard" and split.cluster[r.id] in held for r in rows)
    held_rows = sum(split.cluster[r.id] in held for r in rows)
    lines = [
        f"clusters {len(sizes)} (rows {len(rows)})",
        "size distribution " + ", ".join(f"{s}x{n}" for s, n in sorted(dist.items())),
        f"largest {max(sizes.values())}",
        f"holdout clusters {len(split.holdout)}, rows {held_rows}, guard rows {guard_held}",
        "holdout guard rows by category "
        + ", ".join(f"{c}={n}" for c, n in sorted(held_cats.items())),
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="짝 묶음 분할 파일 생성·요약")
    ap.add_argument("--write", action="store_true", help=f"{SPLIT_PATH.name}을 쓴다")
    ap.add_argument("--seed", type=int, default=DEFAULT_SEED)
    args = ap.parse_args(argv)
    rows = labeled_rows(load_public(load_config().paths.public))
    split = write_splits(seed=args.seed) if args.write else make_splits(rows, seed=args.seed)
    print(summary(split, rows))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
