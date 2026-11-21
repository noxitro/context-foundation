"""採点の束を作る / 採点を結果に戻す。採点する人(AI のサブエージェントなど)には、手法と回を伏せて、順番を混ぜて渡す。

  python bench/grade_pack.py pack <results.jsonl> <cases.jsonl> <outdir> [--batches 6] [--seed 20261119]
      outdir/batch-<n>.json(採点する人に渡す。手法・回は入れない)と outdir/key.json(伏せた番号 → 回)を書く。
      同じ回答を 2 回採点するときは、--seed を変えて別の outdir に作る(混ぜ方が変わる)
  python bench/grade_pack.py merge <results.jsonl> <outdir> <scored.jsonl>
      outdir/score-<n>.json を読み、answer_correctness を埋めた results を scored.jsonl に書く

score-<n>.json の形: [{"item": "<伏せた番号>", "score": 0..1, "met": [満たした要点の番号], "reason": "..."}, ...]
key.json は、採点が終わるまで採点する人に見せない。
"""
from __future__ import annotations

import argparse
import json
import random
from pathlib import Path


def load_jsonl(p) -> list[dict]:
    return [json.loads(x) for x in Path(p).read_text(encoding="utf-8").splitlines() if x.strip()]


def ident(d: dict) -> tuple:
    return d["run_id"], d["case_id"], d["method"], d["trial"]


def pack(results, cases, outdir, batches: int = 6, seed: int = 20261119) -> int:
    res = load_jsonl(results)
    cs = {c["case_id"]: c for c in load_jsonl(cases)}
    out = Path(outdir)
    out.mkdir(parents=True, exist_ok=True)
    rng = random.Random(seed)
    items, key = [], {}
    for r in res:
        iid = f"{rng.getrandbits(40):010x}"
        while iid in key:
            iid = f"{rng.getrandbits(40):010x}"
        key[iid] = {"run_id": r["run_id"], "case_id": r["case_id"], "method": r["method"], "trial": r["trial"]}
        c = cs[r["case_id"]]
        items.append({
            "item": iid,
            "case_id": r["case_id"],
            "question": c["question"],
            "unanswerable": c["category"] == "unanswerable",
            "expected_answer_points": c.get("expected_answer_points") or [],
            "expected_evidence": [{k: e.get(k) for k in ("source_id", "path", "start_line", "end_line")}
                                  for e in (c.get("expected_evidence") or [])],
            "answer": r.get("answer") or "",
        })
    rng.shuffle(items)
    n = max(1, int(batches))
    for b in range(n):
        (out / f"batch-{b + 1}.json").write_text(json.dumps(items[b::n], ensure_ascii=False, indent=1),
                                                 encoding="utf-8")
    (out / "key.json").write_text(json.dumps(key, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"{len(items)} items in {n} batches -> {out}")
    return len(items)


def merge(results, outdir, scored) -> int:
    out = Path(outdir)
    key = json.loads((out / "key.json").read_text(encoding="utf-8"))
    scores: dict[str, float] = {}
    for f in sorted(out.glob("score-*.json")):
        for s in json.loads(f.read_text(encoding="utf-8")):
            scores[s["item"]] = float(s["score"])
    by_run = {(v["run_id"], v["case_id"], v["method"], v["trial"]): k for k, v in key.items()}
    res = load_jsonl(results)
    missing = [ident(r) for r in res if by_run.get(ident(r)) not in scores]
    if missing:
        raise SystemExit(f"{len(missing)} results have no score, e.g. {missing[:3]}")
    with open(scored, "w", encoding="utf-8", newline="\n") as f:
        for r in res:
            r["answer_correctness"] = scores[by_run[ident(r)]]
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    print(f"wrote {len(res)} scored results -> {scored}")
    return len(res)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("pack")
    p.add_argument("results")
    p.add_argument("cases")
    p.add_argument("outdir")
    p.add_argument("--batches", type=int, default=6)
    p.add_argument("--seed", type=int, default=20261119)
    m = sub.add_parser("merge")
    m.add_argument("results")
    m.add_argument("outdir")
    m.add_argument("scored")
    a = ap.parse_args()
    if a.cmd == "pack":
        pack(a.results, a.cases, a.outdir, a.batches, a.seed)
    else:
        merge(a.results, a.outdir, a.scored)


if __name__ == "__main__":
    main()
