"""Claude Code(claude -p)を代わりのエージェントにして、質問を流す(Copilot CLI の agent_run.py の代わり)。
費用は API の従量課金ではなく、Claude Code の利用枠から出る(記録の cost_usd は Claude Code が示す目安)。
結果の形は agent_run.py と同じなので、grade_pack.py と compare.py がそのまま使える。

  python bench/claude_run.py --config <作業フォルダ> --cases <cases.jsonl> --method agent_grep|agent_cf_mcp
         --out <フォルダ> --run-id <名前> [--case <id> ...] [--trials 2] [--model haiku] [--yes]

- 利用者の設定・CLAUDE.md・記憶は読ませない(--setting-sources "")。claude.ai の連携の MCP も外す
  (--strict-mcp-config)。--bare は使わない(OAuth を使わず API キーでの認証になり、従量課金になる)
- 道具は Read・Grep・Glob と、agent_cf_mcp ではこの道具の MCP サーバー(search・status)だけ
- agent_cf_mcp では、Copilot の sessionStart の hook と同じ 1 文(ctxf.py session-hint --mcp)を、
  システムプロンプトに足す
- 質問の文は標準入力から渡す(Windows の claude.cmd は、改行を含む引数を正しく渡せない)
- 止まっても、同じ引数で続きから流せる(results.jsonl にある回は流し直さない)

Copilot CLI とはエージェントの作りが違うので、数字は Copilot CLI の結果とは並べず、同じ Claude Code の中の
手法どうしで比べる。
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import subprocess
import sys
import time
from pathlib import Path

BENCH = Path(__file__).resolve().parent
REPO = BENCH.parent
MCP_NAME = "context-foundation"
TOOLS = ["Read", "Grep", "Glob"]


def load_agent_run():
    """質問の文の作り方と、根拠の取り出しを agent_run.py から借りる。"""
    spec = importlib.util.spec_from_file_location("agent_run", BENCH / "agent_run.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("agent_run", mod)
    spec.loader.exec_module(mod)
    return mod


def build_command(exe: str, method: str, model: str, roots: list[Path], mcp_file: Path, empty_mcp: Path,
                  hint: str) -> list[str]:
    """claude -p の引数(質問の文は標準入力から渡すので、ここには入れない)。"""
    cmd = [exe, "-p", "--model", model, "--setting-sources", "", "--tools", ",".join(TOOLS),
           "--strict-mcp-config", "--output-format", "stream-json", "--verbose", "--no-session-persistence",
           "--permission-mode", "default"]
    allowed = list(TOOLS)
    if method == "agent_cf_mcp":
        cmd += ["--mcp-config", str(mcp_file), "--append-system-prompt", " ".join(hint.split())]
        allowed += [f"mcp__{MCP_NAME}__search", f"mcp__{MCP_NAME}__status"]
    else:
        cmd += ["--mcp-config", str(empty_mcp)]
    cmd += ["--allowedTools", ",".join(allowed)]
    for r in roots:
        cmd += ["--add-dir", str(r)]
    return cmd


def parse_stream(raw: str) -> tuple[dict | None, int, dict[str, int]]:
    """stream-json の出力から、最後の result と、道具の呼び出しの数を取り出す。"""
    final, calls, by_name = None, 0, {}
    for line in raw.splitlines():
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if ev.get("type") == "assistant":
            for b in (ev.get("message") or {}).get("content") or []:
                if b.get("type") == "tool_use":
                    calls += 1
                    by_name[b.get("name")] = by_name.get(b.get("name"), 0) + 1
        elif ev.get("type") == "result":
            final = ev
    return final, calls, by_name


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", required=True, help="作業フォルダ(.cf/config.toml のある場所)")
    ap.add_argument("--cases", required=True)
    ap.add_argument("--method", required=True, choices=("agent_grep", "agent_cf_mcp"))
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--run-id", required=True)
    ap.add_argument("--case", action="append", default=[], help="この case_id だけを回す(繰り返せる)")
    ap.add_argument("--split", default="eval")
    ap.add_argument("--trials", type=int, default=2)
    ap.add_argument("--model", default="haiku", help="Claude Code のモデルの名前か別名")
    ap.add_argument("--python", default=sys.executable, help="MCP サーバーと始めの 1 文を動かす python")
    ap.add_argument("--time-limit", type=int, default=600)
    ap.add_argument("--yes", action="store_true", help="Claude Code の利用枠を使うことを了解して、実行する")
    args = ap.parse_args()

    ar = load_agent_run()
    from context_foundation.config import find_config, load_config
    path = find_config(args.config)
    if path is None:
        raise SystemExit("設定(.cf/config.toml)が見つかりません")
    cfg = load_config(path)
    mapper = ar.Mapper(cfg)
    roots = [Path(os.path.normpath(s.root)) for s in cfg.sources]
    cases = [json.loads(x) for x in Path(args.cases).read_text(encoding="utf-8").splitlines() if x.strip()]
    cases = [c for c in cases if (not args.case or c["case_id"] in args.case) and c.get("split", "eval") == args.split]
    if not args.yes:
        print(f"{args.method}: {len(cases)} 件 × {args.trials} 回。Claude Code の利用枠を使います。--yes で実行します")
        return 2
    args.out.mkdir(parents=True, exist_ok=True)
    results = args.out / "results.jsonl"
    done = set()
    if results.is_file():
        done = {(r["case_id"], r["trial"]) for r in map(json.loads, results.read_text(encoding="utf-8").splitlines())}

    hint = ""
    if args.method == "agent_cf_mcp":
        p = subprocess.run([args.python, "-I", str(REPO / "ctxf.py"), "session-hint", "--mcp"], capture_output=True,
                           input=json.dumps({"cwd": str(cfg.workspace)}), text=True, encoding="utf-8",
                           env=dict(os.environ, CF_PYTHON=args.python, CF_INDEX_AGE_WARN_DAYS="0"), timeout=60)
        hint = json.loads(p.stdout)["additionalContext"] if p.stdout.strip() else ""
        if not hint:
            raise SystemExit("始めの 1 文が取れません(索引が見つからない?)")
    mcp_file = args.out / "mcp.json"
    mcp_file.write_text(json.dumps({"mcpServers": {MCP_NAME: {
        "command": args.python, "args": ["-I", "-m", "context_foundation", "serve", "--config", str(cfg.workspace)]}}}),
        encoding="utf-8")
    empty_mcp = args.out / "mcp-empty.json"
    empty_mcp.write_text(json.dumps({"mcpServers": {}}), encoding="utf-8")
    exe = "claude.cmd" if os.name == "nt" else "claude"
    cmd = build_command(exe, args.method, args.model, roots, mcp_file, empty_mcp, hint)
    (args.out / "logs").mkdir(exist_ok=True)

    for case in cases:
        for trial in range(1, args.trials + 1):
            if (case["case_id"], trial) in done:
                continue
            name = f"{args.run_id}-{case['case_id']}-{args.method}-t{trial}"
            t0 = time.time()
            status = "completed"
            try:
                p = subprocess.run(cmd, cwd=str(cfg.workspace), input=ar.build_prompt(case["question"], roots),
                                   capture_output=True, text=True, encoding="utf-8", errors="replace",
                                   timeout=args.time_limit)
                raw = p.stdout
            except subprocess.TimeoutExpired as e:
                status = "timeout"
                raw = e.stdout.decode("utf-8", "replace") if isinstance(e.stdout, bytes) else (e.stdout or "")
            secs = round(time.time() - t0, 1)
            (args.out / "logs" / f"{name}.jsonl").write_text(raw, encoding="utf-8")
            final, calls, by_name = parse_stream(raw)
            answer = (final or {}).get("result") or ""
            if status == "completed" and (not final or final.get("is_error") or not answer):
                status = "gave_up"
            u = (final or {}).get("usage") or {}
            rec = {"run_id": args.run_id, "case_id": case["case_id"], "method": args.method, "trial": trial,
                   "status": status, "retrieved_evidence": ar.parse_evidence(answer, mapper), "search_time_ms": None,
                   "answer": answer, "answer_correctness": None, "task_time_seconds": secs,
                   "input_tokens": (u.get("input_tokens") or 0) + (u.get("cache_read_input_tokens") or 0)
                   + (u.get("cache_creation_input_tokens") or 0),
                   "output_tokens": u.get("output_tokens"), "cache_read_tokens": u.get("cache_read_input_tokens"),
                   "cache_write_tokens": u.get("cache_creation_input_tokens"), "ai_units": None,
                   "cost_usd": (final or {}).get("total_cost_usd"), "tool_calls": calls,
                   "model_calls": (final or {}).get("num_turns"), "returned_bytes": None, "offloaded_results": None,
                   "session_id": (final or {}).get("session_id"), "tools_by_name": by_name}
            with results.open("a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            print(f"{name}: {status} {secs}s tools={by_name}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
