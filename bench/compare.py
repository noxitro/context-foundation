"""手法どうしを比べる: 採点役の点と、採点役を使わない識別子の指標を、質問を単位にした 95% 区間で見る。

  python bench/compare.py summary <results.jsonl> <cases.jsonl> [--by category|language|area] [--format text|markdown|json]
      手法ごとの回数・合格の数(answer_correctness >= 0.8)・点の平均・識別子の指標・根拠の当たり・
      最後まで通らなかった回の内訳。--by で、質問の種類・言語・区域(case_id の、最後の - より前)ごとにも出す
  python bench/compare.py cost <results.jsonl> <cases.jsonl> [--format ...]
      手法ごとの時間・費用・トークン(中央値と合計)・道具の呼び出しと結果の大きさ
  python bench/compare.py tools <results.jsonl> <cases.jsonl> [--format ...]
      手法ごとに、道具の種類(検索・LSP・Serena・grep・読む・…)を、1 回あたり何回呼んだかと、使った回の割合
  python bench/compare.py paired <results.jsonl> <cases.jsonl> <手法 A> <手法 B>
      A − B の、質問ごとの平均の差の平均と、ブートストラップ(質問を引き直して 5,000 回)の 95% 区間。
      採点役の点・識別子の指標・根拠の当たり・時間・費用

識別子の指標(id recall): 答えの要点に出てくる識別子(CamelCase・::・_ を含む名前)の最後の名前
(DeviceFence::WaitOnCpu なら WaitOnCpu)が、回答に語として出てくる割合。採点役のぶれが無い代わりに、
名前を挙げずに正しく説明した回答は拾えない。答えの無い質問と、最後まで通らなかった回は除く。

根拠の当たり(evidence hit): 正解の場所(expected_evidence)のどれかに重なる場所を、回答が根拠に挙げた
(cited)回の割合。seen は、根拠に挙げたものに加えて、開いた・検索や記号の道具が返した場所も数える。
正解の場所の無い質問は除く。

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
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import methods as M  # noqa: E402

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


def load_all(results, cases) -> tuple[list[dict], dict]:
    """すべての回(最後まで通らなかった回を含む)と、質問。"""
    rows = [json.loads(x) for x in Path(results).read_text(encoding="utf-8").splitlines() if x.strip()]
    cs = {c["case_id"]: c for c in (json.loads(x) for x in Path(cases).read_text(encoding="utf-8").splitlines()
                                    if x.strip())}
    return rows, cs


def load(results, cases) -> tuple[list[dict], dict]:
    """最後まで通った回と、質問。"""
    rows, cs = load_all(results, cases)
    return [r for r in rows if r.get("status", "completed") == "completed"], cs


def _overlaps(a: dict, b: dict) -> bool:
    return (a.get("source_id") == b.get("source_id") and a.get("path") == b.get("path")
            and a["start_line"] <= b["end_line"] and b["start_line"] <= a["end_line"])


def evidence_hit(row: dict, case: dict, cited_only: bool = True) -> float | None:
    """回答が根拠に挙げた場所(cited_only が偽なら、見た場所も)が、正解の場所のどれかに重なれば 1、無ければ 0。
    正解の場所の無い質問では None。"""
    expected = case.get("expected_evidence") or []
    if not expected:
        return None
    got = [e for e in row.get("retrieved_evidence") or [] if not cited_only or e.get("origin") == "cited"]
    return float(any(_overlaps(e, x) for e in got for x in expected))


def group_of(case: dict, by: str) -> str:
    if by == "category":
        return case.get("category") or "?"
    if by == "language":
        return case.get("question_language") or "?"
    cid = case.get("case_id", "")
    return cid.rsplit("-", 1)[0] if "-" in cid else cid       # area: O3E-B01 なら O3E


def median(xs: list) -> float | None:
    xs = [x for x in xs if x is not None]
    return st.median(xs) if xs else None


def families(row: dict) -> dict[str, int]:
    """1 回の、道具の種類ごとの呼び出しの数。"""
    out: dict[str, int] = collections.Counter()
    for name, n in (row.get("tools_by_name") or {}).items():
        out[M.tool_family(name)] += int(n or 0)
    return dict(out)


def per_question(rows: list[dict], cases: dict, metric: str) -> dict[str, dict[str, list[float]]]:
    """質問 → 手法 → 回ごとの値。metric は grader(answer_correctness)か ids(識別子の指標)。"""
    out: dict = collections.defaultdict(lambda: collections.defaultdict(list))
    for r in rows:
        if metric == "grader":
            v = r.get("answer_correctness")
            v = None if isinstance(v, str) else v
        elif metric == "ids":
            v = id_recall(r.get("answer") or "", cases[r["case_id"]])
        elif metric == "evidence":
            v = evidence_hit(r, cases[r["case_id"]])
        else:                                        # time / cost: 少ないほうがよい値
            v = r.get({"time": "task_time_seconds", "cost": "cost_usd"}[metric])
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


def quality_row(rows: list[dict], all_rows: list[dict], cases: dict) -> dict:
    """手法(か、手法と組)ごとの品質の値。rows は最後まで通った回、all_rows はすべての回。"""
    sc = [r["answer_correctness"] for r in rows if isinstance(r.get("answer_correctness"), (int, float))]
    ids = [v for v in (id_recall(r.get("answer") or "", cases[r["case_id"]]) for r in rows) if v is not None]
    cited = [v for v in (evidence_hit(r, cases[r["case_id"]]) for r in rows) if v is not None]
    seen = [v for v in (evidence_hit(r, cases[r["case_id"]], cited_only=False) for r in rows) if v is not None]
    status = collections.Counter(r.get("status", "completed") for r in all_rows)
    return {"runs": len(all_rows), "completed": status.get("completed", 0), "timeout": status.get("timeout", 0),
            "gave_up": status.get("gave_up", 0), "scored": len(sc), "pass": sum(x >= 0.8 for x in sc),
            "score": st.mean(sc) if sc else None, "id_recall": st.mean(ids) if ids else None,
            "evidence_cited": st.mean(cited) if cited else None, "evidence_seen": st.mean(seen) if seen else None}


def cost_row(rows: list[dict]) -> dict:
    def total(k):
        xs = [r.get(k) for r in rows if r.get(k) is not None]
        return sum(xs) if xs else None
    return {"runs": len(rows), "time_median": median([r.get("task_time_seconds") for r in rows]),
            "cost_median": median([r.get("cost_usd") for r in rows]), "cost_total": total("cost_usd"),
            "input_median": median([r.get("input_tokens") for r in rows]),
            "output_median": median([r.get("output_tokens") for r in rows]),
            "cache_read_median": median([r.get("cache_read_tokens") for r in rows]),
            "tool_calls_median": median([r.get("tool_calls") for r in rows]),
            "model_calls_median": median([r.get("model_calls") for r in rows]),
            "returned_kb_median": median([(r.get("returned_bytes") or 0) / 1024 if r.get("returned_bytes") is not None
                                          else None for r in rows])}


def tools_row(rows: list[dict]) -> dict:
    """道具の種類ごとの、1 回あたりの呼び出しの平均と、使った回の割合。tools_by_name の無い回は除く。"""
    known = [families(r) for r in rows if r.get("tools_by_name") is not None]
    out: dict = {"runs": len(known)}
    for fam in M.FAMILIES:
        counts = [f.get(fam, 0) for f in known]
        out[f"{fam}_per_run"] = st.mean(counts) if counts else None
        out[f"{fam}_used"] = (sum(c > 0 for c in counts) / len(counts)) if counts else None
    return out


def _fmt(v, kind: str = "") -> str:
    if v is None:
        return "-"
    if kind == "pct":
        return f"{v * 100:.1f}%"
    if kind == "usd":
        return f"{v:.4f}"
    if isinstance(v, float):
        return f"{v:.3f}" if abs(v) < 10 else f"{v:.1f}"
    return str(v)


def render(table: list[dict], columns: list[tuple[str, str, str]], fmt: str, title: str = "") -> str:
    """table の行を、text(空白でそろえる)・markdown(表)・json で出す。columns は (鍵, 見出し, 形)。"""
    if fmt == "json":
        return json.dumps(table, ensure_ascii=False, indent=1)
    head = [h for _, h, _ in columns]
    body = [[_fmt(r.get(k), kind) if k not in ("method", "group") else str(r.get(k, "")) for k, _, kind in columns]
            for r in table]
    if fmt == "markdown":
        lines = ([f"**{title}**", ""] if title else []) + ["| " + " | ".join(head) + " |",
                                                          "| " + " | ".join(["---"] + ["---:"] * (len(head) - 1)) + " |"]
        return "\n".join(lines + ["| " + " | ".join(b) + " |" for b in body])
    widths = [max(len(x) for x in col) for col in zip(head, *body)]
    lines = ([title] if title else []) + ["  ".join(h.ljust(w) for h, w in zip(head, widths))]
    return "\n".join(lines + ["  ".join(c.ljust(w) for c, w in zip(b, widths)) for b in body])


QUALITY_COLUMNS = [("method", "method", ""), ("runs", "runs", ""), ("timeout", "timeout", ""),
                   ("gave_up", "gave_up", ""), ("pass", "pass", ""), ("scored", "scored", ""),
                   ("score", "score", ""), ("id_recall", "id_recall", ""), ("evidence_cited", "evidence(cited)", "pct"),
                   ("evidence_seen", "evidence(seen)", "pct")]
COST_COLUMNS = [("method", "method", ""), ("runs", "runs", ""), ("time_median", "time_s", ""),
                ("cost_median", "cost_usd", "usd"), ("cost_total", "cost_total", "usd"),
                ("input_median", "input_tok", ""), ("cache_read_median", "cache_read", ""),
                ("output_median", "output_tok", ""), ("model_calls_median", "model_calls", ""),
                ("tool_calls_median", "tool_calls", ""), ("returned_kb_median", "returned_kb", "")]


def summary_tables(rows_all: list[dict], cases: dict, by: str | None) -> list[tuple[str, list[dict]]]:
    by_method: dict = collections.defaultdict(list)
    for r in rows_all:
        by_method[r["method"]].append(r)
    done = lambda rs: [r for r in rs if r.get("status", "completed") == "completed"]  # noqa: E731
    out = [("", [dict(method=m, **quality_row(done(rs), rs, cases)) for m, rs in by_method.items()])]
    if by:
        groups: dict = collections.defaultdict(lambda: collections.defaultdict(list))
        for r in rows_all:
            groups[group_of(cases[r["case_id"]], by)][r["method"]].append(r)
        for g in sorted(groups):
            out.append((f"{by}: {g}", [dict(method=m, **quality_row(done(rs), rs, cases))
                                       for m, rs in groups[g].items()]))
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("summary", "cost", "tools"):
        s = sub.add_parser(name)
        s.add_argument("results")
        s.add_argument("cases")
        s.add_argument("--format", choices=("text", "markdown", "json"), default="text")
        if name == "summary":
            s.add_argument("--by", choices=("category", "language", "area"))
    p = sub.add_parser("paired")
    p.add_argument("results")
    p.add_argument("cases")
    p.add_argument("a")
    p.add_argument("b")
    args = ap.parse_args()
    rows_all, cases = load_all(args.results, args.cases)
    rows = [r for r in rows_all if r.get("status", "completed") == "completed"]
    if args.cmd == "summary":
        tables = summary_tables(rows_all, cases, args.by)
        if args.format == "json":
            print(json.dumps({t or "all": rs for t, rs in tables}, ensure_ascii=False, indent=1))
            return
        print("\n\n".join(render(rs, QUALITY_COLUMNS, args.format, t) for t, rs in tables))
        return
    by_method: dict = collections.defaultdict(list)
    for r in rows:
        by_method[r["method"]].append(r)
    if args.cmd == "cost":
        print(render([dict(method=m, **cost_row(rs)) for m, rs in by_method.items()], COST_COLUMNS, args.format))
        return
    if args.cmd == "tools":
        table = [dict(method=m, **tools_row(rs)) for m, rs in by_method.items()]
        cols = [("method", "method", ""), ("runs", "runs", "")]
        for fam in M.FAMILIES:
            if any((t.get(f"{fam}_per_run") or 0) > 0 for t in table):
                cols += [(f"{fam}_per_run", f"{fam}/run", ""), (f"{fam}_used", f"{fam} used", "pct")]
        print(render(table, cols, args.format))
        return
    for metric, name in (("grader", "grader score"), ("ids", "id recall"), ("evidence", "evidence hit"),
                         ("time", "time (s)"), ("cost", "cost (usd)")):
        res = paired(per_question(rows, cases, metric), args.a, args.b)
        if res is None:
            print(f"{name:13} {args.a} - {args.b}: not enough data")
            continue
        better, worse = ("lower", "higher") if metric in ("time", "cost") else ("better", "worse")
        b, w = (res["worse"], res["better"]) if metric in ("time", "cost") else (res["better"], res["worse"])
        print(f"{name:13} {args.a} - {args.b}: {res['mean']:+.3f}  95% [{res['low']:+.3f}, {res['high']:+.3f}]  "
              f"questions {res['questions']}: {better} {b} / {worse} {w}")


if __name__ == "__main__":
    main()
