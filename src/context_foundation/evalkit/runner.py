"""`ctxf eval`: 検索だけの手法(keyword・semantic・hybrid)を回し、結果と要約を書く。

- cases.jsonl を検査してから、質問ごと・手法ごとに検索して、results.jsonl に 1 行ずつ書く
- 別に取った results.jsonl(エージェントを使う手法など)を取り込んで、同じ要約に並べる
- 検索時間は、索引とモデルを読み込んだ後の 1 回の時間(最初に 1 回、空打ちして読み込ませる)

書くファイル(--out のフォルダ): results.jsonl・runs.jsonl・summary.md・summary.json。
どれもコーパスごとの内容を含むので、この公開リポジトリには入れない。
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

from .. import __version__
from ..config import MODES, find_config, load_config
from ..render import to_agent
from ..sysinfo import host_info
from .data import check_results, load_cases, read_jsonl
from .metrics import aggregate
from .summary import DEFAULT_THRESHOLDS, build_summary, to_markdown

DATA_HANDLING = "Source content stays on the local machine. Embeddings are computed locally."


def run_metadata(run_id: str, searcher, methods: list[str], top_k: int, corpus_revision: str, search_cfg,
                 candidates: list[str]) -> dict:
    st = searcher.status()
    stats, tm, emb = st["stats"], st["timings"], st["embedding"]
    return {
        "run_id": run_id, "created_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "corpus_revision": corpus_revision,
        "candidate_methods": candidates,
        "methods": {m: {"kind": "retrieval_only", "top_k": top_k, "candidates": search_cfg.candidates,
                        "rrf_k": search_cfg.rrf_k, "hybrid_weighting": search_cfg.hybrid_weighting,
                        "bm25_weights": {"body": search_cfg.weight_body, "symbols": search_cfg.weight_symbols,
                                         "path": search_cfg.weight_path},
                        "reranker": search_cfg.reranker or None} for m in methods},
        "corpus": {"files": stats["files"], "lines": stats["lines"], "chunks": stats["chunks"],
                   "sources": [{"source_id": s["id"], "files": s["files"], "lines": s["lines"], "chunks": s["chunks"]}
                               for s in st["sources"]]},
        "index": {"size_bytes": st["index_bytes"], "build_seconds": tm.get("total_seconds", 0.0),
                  "ingest_chunk_seconds": tm.get("ingest_chunk_seconds", 0.0),
                  "embed_seconds": tm.get("embed_seconds", 0.0),
                  # 差分の更新はまだ無い(作り直しの方式)ので、更新の費用は、全体を作り直す時間
                  "update_seconds": tm.get("total_seconds", 0.0), "update_mode": "full_rebuild",
                  "peak_memory_bytes": tm.get("peak_memory_bytes"),
                  "syntax_fallback_ratio": stats.get("syntax_fallback_ratio", 0.0),
                  "created_at": st["created_at"],
                  # 索引を作ったときの版(index --revision で渡したもの)。評価の版と違えば、鮮度の影響が出る
                  "indexed_revision": st.get("revision", "")},
        "embedding": {"model": emb["model"], "revision": emb.get("revision", ""), "dimensions": emb["dim"],
                      "vector_dtype": emb.get("vector_dtype", "")} if emb else None,
        "chunking": st.get("chunk_config"),
        "agent": None,
        "thresholds": dict(DEFAULT_THRESHOLDS),
        "data_handling": DATA_HANDLING,
        "tool_version": __version__,
        "host": host_info(),
    }


def run_retrieval(cases: dict[str, dict], searcher, methods: list[str], run_id: str, top_k: int,
                  log=lambda s: None, max_output_bytes: int = 12_000) -> list[dict]:
    rows: list[dict] = []
    for m in methods:      # 空打ち: モデルとベクトルの読み込みを、時間の計測の外に出す
        searcher.search("warm up", mode=m, top_k=1, check_stale=False)
    for i, (cid, case) in enumerate(cases.items(), 1):
        for m in methods:
            resp = searcher.search(case["question"], mode=m, top_k=top_k)
            row = {
                "run_id": run_id, "case_id": cid, "method": m, "trial": 1, "status": "completed",
                "retrieved_evidence": [
                    dict({"rank": p["rank"], "source_id": p["source_id"], "path": p["path"],
                          "start_line": p["start_line"], "end_line": p["end_line"], "score": p["score"],
                          "origin": "search_result"}, **({"stale": True} if p.get("stale") else {}))
                    for p in resp.passages],
                "search_time_ms": resp.timings["search_ms"],
                "answer": None, "answer_correctness": None, "task_time_seconds": None,
                "input_tokens": None, "output_tokens": None, "ai_units": None, "tool_calls": None,
                # エージェントに返すときの出力の大きさ(既定の件数と上限で)
                "returned_bytes": len(to_agent(resp, max_output_bytes).encode("utf-8")),
            }
            if resp.mode != m:
                row["notes"] = f"ran as {resp.mode}: " + " ".join(resp.notes)
            rows.append(row)
        log(f"  {i} / {len(cases)} 件")
    return rows


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def main(args) -> int:
    all_cases, issues = load_cases(args.cases)
    split = getattr(args, "split", "eval")
    cases = {cid: c for cid, c in all_cases.items() if split == "all" or c.get("split", "eval") == split}
    candidates = [m.strip() for m in getattr(args, "candidate", "hybrid,agent_cf").split(",") if m.strip()]
    errors = [i for i in issues if i.level == "error"]
    if errors:
        for i in errors:
            print(i.format(), file=sys.stderr)
        sys.stderr.write("cases.jsonl に誤りがあります。`ctxf validate --cases ...` で確かめて直してください\n")
        return 1
    if not cases:
        sys.stderr.write(f"cases.jsonl に、split が {split} の質問がありません\n")
        return 1
    if len(cases) != len(all_cases):
        print(f"質問 {len(all_cases)} 件のうち、split が {split} の {len(cases)} 件を使います", file=sys.stderr)
    methods = [] if args.methods.strip().lower() in ("", "none") else [m.strip() for m in args.methods.split(",")]
    bad = [m for m in methods if m not in MODES]
    if bad:
        sys.stderr.write(f"--methods に使えるのは {', '.join(MODES)} です(渡されたもの: {', '.join(bad)})\n")
        return 2
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    run_id = args.run_id or time.strftime("run-%Y%m%d-%H%M%S")

    own_rows: list[dict] = []
    runs: list[dict] = []
    if methods:
        from ..search import Searcher
        from ..store import IndexError_
        path = find_config(args.config)
        if path is None:
            sys.stderr.write("設定(.cf/config.toml)が見つかりません。--config で指定してください\n")
            return 3
        cfg = load_config(path)
        searcher = Searcher.from_config(cfg)
        try:
            available = searcher.status()["modes"]
        except IndexError_ as e:
            sys.stderr.write(f"{e}\n")
            return 3
        skipped = [m for m in methods if m not in available]
        if skipped:
            print(f"注意: 索引に埋め込みが無いので、{', '.join(skipped)} は回しません(keyword だけ)", file=sys.stderr)
            methods = [m for m in methods if m in available]
        print(f"検索だけの手法を回します: {', '.join(methods)}({len(cases)} 件)", file=sys.stderr)
        own_rows = run_retrieval(cases, searcher, methods, run_id, args.top_k,
                                 log=lambda s: print(s, file=sys.stderr) if sys.stderr.isatty() else None,
                                 max_output_bytes=cfg.search.max_output_bytes)
        runs.append(run_metadata(run_id, searcher, methods, args.top_k, args.corpus_revision, cfg.search,
                                 candidates))
        _write_jsonl(out / "results.jsonl", own_rows)
        _write_jsonl(out / "runs.jsonl", runs)

    all_rows = list(own_rows)
    case_ids = set(all_cases)
    for path in args.imports:
        records, found = read_jsonl(path, "results")
        found += check_results(records, Path(path).name, case_ids)
        errs = [i for i in found if i.level == "error"]
        for i in errs:
            print(i.format(), file=sys.stderr)
        if errs:
            sys.stderr.write(f"{path} に誤りがあるので、取り込みません\n")
            return 1
        all_rows += [r for _, r in records]
        print(f"取り込みました: {path}({len(records)} 行)", file=sys.stderr)
    for path in args.import_runs:
        records, found = read_jsonl(path, "runs")
        for i in found:
            if i.level == "error":
                print(i.format(), file=sys.stderr)
        runs += [r for _, r in records]
    if not all_rows:
        sys.stderr.write("結果が 1 行もありません(--methods none のときは --import が要ります)\n")
        return 2

    entries = aggregate(cases, all_rows, DEFAULT_THRESHOLDS["acceptable_min"])
    summary = build_summary(cases, entries, runs, args.baseline, candidates=candidates)
    (out / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    md = to_markdown(summary)
    (out / "summary.md").write_text(md, encoding="utf-8", newline="\n")
    sys.stdout.write(md)
    print(f"\n書きました: {out / 'summary.md'}、{out / 'summary.json'}"
          + (f"、{out / 'results.jsonl'}、{out / 'runs.jsonl'}" if own_rows else ""), file=sys.stderr)
    return 0
