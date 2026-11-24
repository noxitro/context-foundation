"""Claude Code(claude -p)を代わりのエージェントにして、質問を流す(Copilot CLI の agent_run.py の代わり)。
費用は API の従量課金ではなく、Claude Code の利用枠から出る(記録の cost_usd は Claude Code が示す目安)。
結果の形は agent_run.py と同じなので、grade_pack.py と compare.py がそのまま使える。

  python bench/claude_run.py --config <作業フォルダ> --cases <cases.jsonl> --method <手法>
         --out <フォルダ> --run-id <名前> [--case <id> ...] [--trials 2] [--model haiku] [--yes]

手法(bench/methods.py): agent_grep・agent_cf_mcp・agent_lsp・agent_cf_mcp_lsp・agent_serena・agent_cf_mcp_serena

- 利用者の設定・CLAUDE.md・記憶は読ませない(--setting-sources "")。claude.ai の連携の MCP も外す
  (--strict-mcp-config)。--bare は使わない(OAuth を使わず API キーでの認証になり、従量課金になる)
- 道具は Read・Grep・Glob と、手法が足すものだけ: この道具の MCP サーバー(search・status)、言語サーバーの
  道具 LSP(--lsp-command で渡す言語サーバーを、この回だけのプラグインにして渡す)、Serena の MCP サーバー
- この道具を足す手法では、Copilot の sessionStart の hook と同じ 1 文(ctxf.py session-hint --mcp)を、
  Serena を足す手法では、Serena 自身の始めの指示(print-system-prompt)を、システムプロンプトに足す
- Claude Code は、クラウドのセッション(claude.ai の Claude Code)の中では、プラグインの言語サーバーを
  起動しない。LSP の手法は、手元の PC で流す
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
import shlex
import subprocess
import sys
import time
from pathlib import Path

BENCH = Path(__file__).resolve().parent
REPO = BENCH.parent
sys.path.insert(0, str(BENCH))
import methods as M  # noqa: E402

MCP_NAME = "context-foundation"
TOOLS = ["Read", "Grep", "Glob"]


def load_agent_run():
    """質問の文の作り方と、根拠の取り出しを agent_run.py から借りる。"""
    spec = importlib.util.spec_from_file_location("agent_run", BENCH / "agent_run.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("agent_run", mod)
    spec.loader.exec_module(mod)
    return mod


def mcp_servers(method: str, python: str, workspace: Path, serena: dict | None) -> dict:
    """手法が足す MCP サーバー(この道具と Serena)。"""
    f = M.features(method)
    servers: dict = {}
    if "cf_mcp" in f:
        servers[MCP_NAME] = {"command": python, "args": ["-I", "-m", "context_foundation", "serve", "--config",
                                                          str(workspace)]}
    if "serena" in f:
        cmd = M.serena_server(serena["project"], "claude-code", serena["command"])
        servers[M.SERENA_NAME] = {"command": cmd[0], "args": cmd[1:], "env": serena["env"]}
    return servers


def build_command(exe: str, method: str, model: str, roots: list[Path], mcp_file: Path, prompt: str,
                  plugin: Path | None = None) -> list[str]:
    """claude -p の引数(質問の文は標準入力から渡すので、ここには入れない)。prompt は、システムプロンプトに足す文
    (この道具の始めの 1 文と、Serena の始めの指示)。"""
    f = M.features(method)
    tools = list(TOOLS) + (["LSP"] if "lsp" in f else [])
    cmd = [exe, "-p", "--model", model, "--setting-sources", "", "--tools", ",".join(tools),
           "--strict-mcp-config", "--output-format", "stream-json", "--verbose", "--no-session-persistence",
           "--permission-mode", "default", "--mcp-config", str(mcp_file)]
    allowed = list(tools)
    if "cf_mcp" in f:
        allowed += [f"mcp__{MCP_NAME}__search", f"mcp__{MCP_NAME}__status"]
    if "serena" in f:
        allowed.append(f"mcp__{M.SERENA_NAME}")
    if prompt:
        cmd += ["--append-system-prompt", prompt]
    if "lsp" in f:
        if plugin is None:
            raise SystemExit(f"{method} には --lsp-command(言語サーバーのコマンド)が要ります")
        cmd += ["--plugin-dir", str(plugin)]
    cmd += ["--allowedTools", ",".join(allowed)]
    for r in roots:
        cmd += ["--add-dir", str(r)]
    return cmd


def evidence_from_stream(raw: str, mapper, serena_root: Path | None) -> list[dict]:
    """エージェントが読んだ場所(Read)・検索が返した passage・Serena が返した記号の場所を、出てきた順に。"""
    import re
    hits = re.compile(r"^\[\d+\] ([A-Za-z0-9][A-Za-z0-9._-]*):(.+?):(\d+)-(\d+)\s", re.MULTILINE)
    out: list[dict] = []
    names: dict[str, str] = {}
    for line in raw.splitlines():
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        for b in ((ev.get("message") or {}).get("content") or []) if isinstance(ev.get("message"), dict) else []:
            if not isinstance(b, dict):
                continue
            if b.get("type") == "tool_use":
                names[b.get("id")] = b.get("name") or ""
                args = b.get("input") or {}
                if b.get("name") == "Read" and isinstance(args.get("file_path"), str):
                    found = mapper.resolve(args["file_path"])
                    if found and found[2].is_file():
                        start = max(1, int(args.get("offset") or 1))
                        limit = args.get("limit")
                        end = start + int(limit) - 1 if limit else mapper.line_count(found[2])
                        out.append({"source_id": found[0], "path": found[1], "start_line": start,
                                    "end_line": max(start, end), "origin": "opened"})
            elif b.get("type") == "tool_result":
                c = b.get("content")
                text = c if isinstance(c, str) else "\n".join(
                    x.get("text", "") for x in c or [] if isinstance(x, dict))
                name = names.get(b.get("tool_use_id"), "")
                if M.tool_family(name) == "search":
                    for h in hits.finditer(text):
                        if h.group(1) in mapper.ids:
                            out.append({"source_id": h.group(1), "path": h.group(2), "start_line": int(h.group(3)),
                                        "end_line": int(h.group(4)), "origin": "search_result"})
                elif M.tool_family(name) == "serena" and serena_root is not None:
                    for rel, start, end in M.serena_locations(text):
                        found = mapper.resolve(str(serena_root / rel))
                        if found:
                            out.append({"source_id": found[0], "path": found[1], "start_line": start,
                                        "end_line": max(start, end), "origin": "symbol_result"})
    return out


def returned_bytes(raw: str) -> int:
    """道具の結果の大きさの合計(UTF-8 のバイト数)。"""
    total = 0
    for line in raw.splitlines():
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        msg = ev.get("message") if ev.get("type") == "user" else None
        for b in (msg or {}).get("content") or [] if isinstance(msg, dict) else []:
            if isinstance(b, dict) and b.get("type") == "tool_result":
                c = b.get("content")
                text = c if isinstance(c, str) else "".join(x.get("text", "") for x in c or [] if isinstance(x, dict))
                total += len(text.encode("utf-8"))
    return total


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


def claude_version(exe: str) -> str:
    try:
        out = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=60).stdout
        return out.split()[0] if out.split() else "unknown"
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def write_run(args, cfg, hints: list[str], serena: dict | None) -> None:
    """runs.jsonl に、この流しの条件を残す(手法ごとに 1 項目。agent_run.py と同じ形)。"""
    sys.path.insert(0, str(REPO / "src"))
    from context_foundation.sysinfo import host_info
    runs = args.out / "runs.jsonl"
    known = {r["run_id"]: r for r in map(json.loads, runs.read_text(encoding="utf-8").splitlines()) if r} \
        if runs.is_file() else {}
    exe = "claude.cmd" if os.name == "nt" else "claude"
    meta = known.get(args.run_id) or {
        "run_id": args.run_id, "created_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "corpus_revision": "",
        "methods": {}, "host": host_info(),
        "agent": {"name": "claude-code", "version": claude_version(exe), "model": args.model,
                  "provider": "claude-code", "time_limit_seconds": args.time_limit, "trials": args.trials,
                  "price_per_million_tokens_usd": None},
        "data_handling": "Questions and the passages the agent reads are sent to Anthropic through Claude Code. "
                         "The index and embeddings stay local."}
    f = M.features(args.method)
    method = {"kind": "agent", "tools": M.described_tools(args.method, list(TOOLS)),
              "permissions": "read-only tools; no shell, no edits", "session_hint": bool(hints),
              "isolated_config": True, "custom_instructions": "disabled"}
    if "serena" in f:
        method["serena"] = {"command": serena["command"], "context": "claude-code", "modes": list(M.SERENA_MODES)}
    if "lsp" in f:
        method["lsp_command"] = args.lsp_command
    meta["methods"][args.method] = method
    known[args.run_id] = meta
    runs.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in known.values()), encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", required=True, help="作業フォルダ(.cf/config.toml のある場所)")
    ap.add_argument("--cases", required=True)
    ap.add_argument("--method", required=True, choices=M.CLAUDE_METHODS)
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--run-id", required=True)
    ap.add_argument("--case", action="append", default=[], help="この case_id だけを回す(繰り返せる)")
    ap.add_argument("--split", default="eval")
    ap.add_argument("--trials", type=int, default=2)
    ap.add_argument("--model", default="haiku", help="Claude Code のモデルの名前か別名")
    ap.add_argument("--python", default=sys.executable, help="MCP サーバーと始めの 1 文を動かす python")
    ap.add_argument("--time-limit", type=int, default=600)
    ap.add_argument("--lsp-command", help="LSP の手法: 言語サーバーのコマンド(例: \"clangd --background-index\")")
    ap.add_argument("--serena-project", type=Path,
                    help="Serena の手法: .serena/project.yml のあるフォルダ(省くと、source が 1 つならそのルート、"
                         "ほかは作業フォルダ)")
    ap.add_argument("--serena-command", default=M.SERENA_COMMAND, help="Serena を起こすコマンド")
    ap.add_argument("--serena-home", type=Path, help="Serena の設定の置き場(SERENA_HOME。省くと <out>/serena-home)")
    ap.add_argument("--dry-run", action="store_true", help="実行せずに、claude の引数と MCP の設定だけを出す")
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
    if not args.yes and not args.dry_run:
        print(f"{args.method}: {len(cases)} 件 × {args.trials} 回。Claude Code の利用枠を使います。--yes で実行します")
        return 2
    args.out.mkdir(parents=True, exist_ok=True)
    results = args.out / "results.jsonl"
    done = set()
    if results.is_file():
        done = {(r["run_id"], r["case_id"], r["method"], r["trial"])
                for r in map(json.loads, results.read_text(encoding="utf-8").splitlines()) if r}

    f = M.features(args.method)
    hints: list[str] = []
    if "cf_mcp" in f:
        p = subprocess.run([args.python, "-I", str(REPO / "ctxf.py"), "session-hint", "--mcp"], capture_output=True,
                           input=json.dumps({"cwd": str(cfg.workspace)}), text=True, encoding="utf-8",
                           env=dict(os.environ, CF_PYTHON=args.python, CF_INDEX_AGE_WARN_DAYS="0"), timeout=60)
        hint = json.loads(p.stdout)["additionalContext"] if p.stdout.strip() else ""
        if not hint:
            raise SystemExit("始めの 1 文が取れません(索引が見つからない?)")
        hints.append(" ".join(hint.split()))
    serena = None
    if "serena" in f:
        project = (args.serena_project or (roots[0] if len(roots) == 1 else cfg.workspace)).resolve()
        if not M.serena_project_ok(project):
            raise SystemExit(M.serena_setup_hint(project, args.serena_command))
        home = (args.serena_home or args.out / "serena-home").resolve()
        home.mkdir(parents=True, exist_ok=True)
        env = {"SERENA_HOME": str(home)}
        serena = {"project": project, "command": args.serena_command, "env": env}
        hints.append(M.serena_prompt(project, "claude-code", args.serena_command, dict(os.environ, **env),
                                     args.out / "serena-prompt.txt"))
    plugin = None
    if "lsp" in f:
        if not args.lsp_command:
            raise SystemExit(f"{args.method} には --lsp-command(言語サーバーのコマンド)が要ります")
        plugin = M.lsp_plugin(args.out / "lsp-plugin", shlex.split(args.lsp_command))
    mcp_file = args.out / f"mcp-{args.method}.json"
    mcp_file.write_text(json.dumps({"mcpServers": mcp_servers(args.method, args.python, cfg.workspace, serena)}),
                        encoding="utf-8")
    exe = "claude.cmd" if os.name == "nt" else "claude"
    cmd = build_command(exe, args.method, args.model, roots, mcp_file, "\n\n".join(hints), plugin)
    if args.dry_run:
        shown = [c if not (i and cmd[i - 1] == "--append-system-prompt") else f"<{len(c)} 文字>" for i, c in enumerate(cmd)]
        print(json.dumps({"cwd": str(cfg.workspace), "cmd": shown,
                          "mcp": json.loads(mcp_file.read_text(encoding="utf-8"))}, ensure_ascii=False, indent=1))
        return 0
    (args.out / "logs").mkdir(exist_ok=True)
    write_run(args, cfg, hints, serena)

    for case in cases:
        for trial in range(1, args.trials + 1):
            if (args.run_id, case["case_id"], args.method, trial) in done:
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
            seen: set[tuple] = set()
            evidence: list[dict] = []
            for e in evidence_from_stream(raw, mapper, serena["project"] if serena else None) \
                    + ar.parse_evidence(answer, mapper):
                key = (e["source_id"], e["path"], e["start_line"], e["end_line"], e["origin"])
                if key not in seen:
                    seen.add(key)
                    evidence.append(dict(rank=len(evidence) + 1, **e))
            if status == "completed" and (not final or final.get("is_error") or not answer):
                status = "gave_up"
            u = (final or {}).get("usage") or {}
            rec = {"run_id": args.run_id, "case_id": case["case_id"], "method": args.method, "trial": trial,
                   "status": status, "retrieved_evidence": evidence, "search_time_ms": None,
                   "answer": answer, "answer_correctness": None, "task_time_seconds": secs,
                   "input_tokens": (u.get("input_tokens") or 0) + (u.get("cache_read_input_tokens") or 0)
                   + (u.get("cache_creation_input_tokens") or 0),
                   "output_tokens": u.get("output_tokens"), "cache_read_tokens": u.get("cache_read_input_tokens"),
                   "cache_write_tokens": u.get("cache_creation_input_tokens"), "ai_units": None,
                   "cost_usd": (final or {}).get("total_cost_usd"), "tool_calls": calls,
                   "model_calls": (final or {}).get("num_turns"), "returned_bytes": returned_bytes(raw),
                   "offloaded_results": None,
                   "session_id": (final or {}).get("session_id"), "tools_by_name": by_name}
            with results.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
            print(f"{name}: {status} {secs}s tools={by_name}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
