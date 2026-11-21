"""手法どうしを比べる: 採点役の点と、採点役を使わない識別子の指標を、質問を単位にした 95% 区間で見る。

  python bench/compare.py summary <results.jsonl> <cases.jsonl>
      手法ごとの回数・合格の数(answer_correctness >= 0.8)・点の平均・識別子の指標
  python bench/compare.py paired <results.jsonl> <cases.jsonl> <手法 A> <手法 B>
      A − B の、質問ごとの平均の差の平均と、ブートストラップ(質問を引き直して 5,000 回)の 95% 区間

識別子の指標(id recall): 答えの要点に出てくる識別子(CamelCase・::・_ を含む名前)の最後の名前
(DeviceFence::WaitOnCpu なら WaitOnCpu)が、回答に語として出てくる割合。採点役のぶれが無い代わりに、
名前を挙げずに正しく説明した回答は拾えない。答えの無い質問と、最後まで通らなかった回は除く。

手法どうしは、同じ時間帯に質問ごとに交互に流した回答でだけ比べる(別の日の回答とは、伏せて採点し直しても
比べられない。docs/benchmarks.md の「0.4.9 の後に試した、品質を上げる案」)。
"""
from __future__ import annotations

import argparse
import collections
import json
import random
import re
import statistics as st
from pathlib import Path

IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:::[A-Za-z_~][A-Za-z0-9_]*)*")
# 名前としてありふれていて、回答に出ても要点を述べた証拠にならない語
COMMON = frozenset({"GPU", "CPU", "RHI", "RPI", "JSON", "API", "AZ", "AZStd", "Asset", "Load", "Store", "Init",
                    "Get", "Set"})


def code_like(tok: str) -> bool:
    if "::" in tok or "_" in tok.strip("_"):
        return True
    return bool(re.search(r"[a-z][A-Z]|[A-Z]{2}[a-z]", tok))


def ids_of(point: str) -> list[str]:
    """答えの要点 1 つから、回答に出てくるはずの名前を取り出す(重なりは除く)。"""
    out: list[str] = []
    for tok in IDENT.findall(point):
        if not code_like(tok):
            continue
        last = tok.split("::")[-1]
        if last in COMMON or len(last) < 4:
            parts = [p for p in tok.split("::") if p not in COMMON and len(p) >= 4]
            if not parts:
                continue
            last = parts[-1]
        if last not in out:
            out.append(last)
    return out


def mentions(answer: str, name: str) -> bool:
    return re.search(rf"(?<![A-Za-z0-9_]){re.escape(name)}(?![A-Za-z0-9_])", answer or "") is not None


def id_recall(answer: str, case: dict) -> float | None:
    """回答 1 つの識別子の指標(0〜1)。答えの無い質問と、要点に名前が無い質問では None。"""
    if case.get("category") == "unanswerable":
        return None
    ids = [i for p in case.get("expected_answer_points") or [] for i in ids_of(p)]
    if not ids:
        return None
    return sum(mentions(answer, i) for i in ids) / len(ids)


def load(results, cases) -> tuple[list[dict], dict]:
    rows = [json.loads(x) for x in Path(results).read_text(encoding="utf-8").splitlines() if x.strip()]
    cs = {c["case_id"]: c for c in map(json.loads, Path(cases).read_text(encoding="utf-8").splitlines()) if c}
    return [r for r in rows if r.get("status", "completed") == "completed"], cs


def per_question(rows: list[dict], cases: dict, metric: str) -> dict[str, dict[str, list[float]]]:
    """質問 → 手法 → 回ごとの値。metric は grader(answer_correctness)か ids(識別子の指標)。"""
    out: dict = collections.defaultdict(lambda: collections.defaultdict(list))
    for r in rows:
        if metric == "grader":
            v = r.get("answer_correctness")
        else:
            v = id_recall(r.get("answer") or "", cases[r["case_id"]])
        if v is not None:
            out[r["case_id"]][r["method"]].append(float(v))
    return out


def paired(per: dict, a: str, b: str, reps: int = 5000, seed: int = 1) -> dict | None:
    """A − B の、質問ごとの平均の差。両方の値がある質問だけを使う。"""
    diffs = [st.mean(m[a]) - st.mean(m[b]) for m in per.values() if m.get(a) and m.get(b)]
    if len(diffs) < 2:
        return None
    rng = random.Random(seed)
    boot = sorted(st.mean(rng.choices(diffs, k=len(diffs))) for _ in range(reps))
    lo, hi = boot[int(reps * 0.025)], boot[int(reps * 0.975) - 1]
    return {"mean": st.mean(diffs), "low": lo, "high": hi, "questions": len(diffs),
            "better": sum(d > 0 for d in diffs), "worse": sum(d < 0 for d in diffs)}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("summary")
    s.add_argument("results")
    s.add_argument("cases")
    p = sub.add_parser("paired")
    p.add_argument("results")
    p.add_argument("cases")
    p.add_argument("a")
    p.add_argument("b")
    args = ap.parse_args()
    rows, cases = load(args.results, args.cases)
    if args.cmd == "summary":
        by = collections.defaultdict(list)
        for r in rows:
            by[r["method"]].append(r)
        for m, rs in by.items():
            sc = [r["answer_correctness"] for r in rs if r.get("answer_correctness") is not None]
            ids = [v for v in (id_recall(r.get("answer") or "", cases[r["case_id"]]) for r in rs) if v is not None]
            line = f"{m:16} runs={len(rs)}"
            if sc:
                line += f" pass={sum(x >= 0.8 for x in sc)}/{len(sc)} mean={st.mean(sc):.3f}"
            if ids:
                line += f" id_recall={st.mean(ids):.3f}"
            print(line)
        return
    for metric, name in (("grader", "grader score"), ("ids", "id recall")):
        res = paired(per_question(rows, cases, metric), args.a, args.b)
        if res is None:
            print(f"{name:13} {args.a} - {args.b}: not enough data")
            continue
        print(f"{name:13} {args.a} - {args.b}: {res['mean']:+.3f}  95% [{res['low']:+.3f}, {res['high']:+.3f}]  "
              f"questions {res['questions']}: better {res['better']} / worse {res['worse']}")


if __name__ == "__main__":
    main()
