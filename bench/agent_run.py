"""エージェントを使う手法を GitHub Copilot CLI で回して、results.jsonl に書く(計測の道具)。

  python bench/agent_run.py --config <作業フォルダ> --cases cases.jsonl --method agent_grep --out <フォルダ> --yes

手法(同じエージェント・同じモデル・同じ質問文・同じ道具の一式で、検索の道具だけを変える。bench/methods.py)
  agent_grep           既存の手順。view・grep・glob で探す
  agent_grep_cxxref    agent_grep に、記号参照の索引 cxxref の skill と hook を足す(--cxxref に、その置き場所)
  agent_cf             agent_grep に、この道具の skill(context-search)と、セッションの始めの 1 文(hook)を足す
  agent_cf_mcp         agent_grep に、この道具の MCP サーバー(search・status)を足す
  agent_lsp            agent_grep に、言語サーバーの道具 lsp を足す(--lsp-config に、Copilot CLI の lsp-config.json)
  agent_cf_mcp_lsp     agent_cf_mcp と agent_lsp の両方
  agent_serena         agent_grep に、Serena の MCP サーバーと、Serena 自身の始めの指示(hook)を足す
  agent_cf_mcp_serena  agent_cf_mcp と agent_serena の両方

1 回の実行(1 質問 × 1 回)ごとに、空の設定フォルダ(COPILOT_HOME)で新しいセッションを始める。ふだんの設定
(登録済みの MCP サーバーやプラグイン)を混ぜないため。記録するもの: 回答、開いたファイルと行・検索が返した
passage・回答で根拠に挙げた場所(出現順)、時間、道具の呼び出しの数、トークン、AI credits、道具の出力の大きさ。

指示のファイル(AGENTS.md・CLAUDE.md・GEMINI.md・.github/copilot-instructions.md など)
  Copilot CLI は、作業フォルダと、その版管理のルートまでにあるこれらを、指示として毎回読む。中身は入力に混ざる。
  この道具は、見つけたものを実行の前に知らせ、runs.jsonl に残す。--no-custom-instructions を付けると、
  どの手法でも読ませない。

注意
- 実行のたびに、Copilot の AI credits か、BYOK の提供元の料金がかかる。--yes を付けないと実行しない
- 質問と、エージェントが読んだ本文は、選んだモデルの提供元に送られる。非公開のコーパスでは、
  その提供元に送ってよいかを先に決めること(docs/comparison-runbook.md)
- 作業フォルダは書き換えない(書き込みは断る。shell は python の起動だけを通す)
- git には頼らない
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

BENCH = Path(__file__).resolve().parent
REPO = BENCH.parent
sys.path.insert(0, str(REPO / "src"))

from context_foundation.config import find_config, load_config  # noqa: E402
from context_foundation.evalkit.data import load_cases, read_jsonl  # noqa: E402
from context_foundation.sysinfo import host_info  # noqa: E402

sys.path.insert(0, str(BENCH))
import methods as M  # noqa: E402

METHODS = M.METHODS
# どの手法にも渡す道具の一式(読むための道具と、skill と shell)
BASE_TOOLS = ["view", "grep", "glob", "skill", "powershell" if os.name == "nt" else "bash"]
MCP_NAME = "context-foundation"
OFFLOAD_MARK = "Output too large to read at once"
# Copilot CLI が指示として読むファイル(公式の「Adding custom instructions for GitHub Copilot CLI」の表)
INSTRUCTION_NAMES = ("AGENTS.md", "CLAUDE.md", "GEMINI.md", ".claude/CLAUDE.md", ".github/copilot-instructions.md")
INSTRUCTION_GLOBS = ((".github/instructions", "*.instructions.md"), (".claude/rules", "*.md"))
EVIDENCE_LINE = re.compile(r"^\s*[-*]\s*`?(.+?):(\d+)(?:\s*[-–]\s*(\d+))?`?\s*$")
SEARCH_HIT = re.compile(r"^\[\d+\] ([A-Za-z0-9][A-Za-z0-9._-]*):(.+?):(\d+)-(\d+)\s", re.MULTILINE)


def find_copilot() -> str:
    exe = os.environ.get("CF_COPILOT") or shutil.which("copilot.exe") or shutil.which("copilot")
    if not exe:
        raise SystemExit("Copilot CLI(copilot)が見つかりません")
    return exe


def copilot_version(exe: str) -> str:
    try:
        out = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=60).stdout
        m = re.search(r"\d+\.\d+\.\d+", out)
        return m.group(0) if m else out.strip()[:40]
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def build_prompt(question: str, roots: list[Path]) -> str:
    fmt = (BENCH / "answer-format.txt").read_text(encoding="utf-8").strip()
    folders = "\n".join(f"- {r}" for r in roots)
    return f"{question}\n\n{fmt}\n\nFolders:\n{folders}\n"


class Mapper:
    """エージェントが書いたパスを、(source_id, source のルートからの相対パス) に直す。"""

    def __init__(self, cfg):
        self.workspace = cfg.workspace
        self.sources = sorted(((s.id, Path(os.path.normpath(s.root))) for s in cfg.sources),
                              key=lambda x: -len(str(x[1])))
        self.ids = {s.id for s in cfg.sources}
        self._lines: dict[Path, int] = {}

    def resolve(self, text: str) -> tuple[str, str, Path] | None:
        text = text.strip().strip("`'\"").replace("\\", "/")
        head, _, rest = text.partition(":")
        if head in self.ids and rest and not rest.startswith("/"):       # source:path の形
            root = dict(self.sources)[head]
            return head, rest.lstrip("./"), root / rest
        p = Path(text)
        if not p.is_absolute():
            p = self.workspace / p
        p = Path(os.path.normpath(p))
        for sid, root in self.sources:
            try:
                rel = p.relative_to(root)
            except ValueError:
                try:                                                   # 大文字と小文字の違いだけなら通す
                    rel = Path(os.path.normcase(p)).relative_to(os.path.normcase(root))
                except ValueError:
                    continue
            return sid, rel.as_posix(), p
        return None

    def line_count(self, path: Path) -> int:
        if path not in self._lines:
            try:
                data = path.read_bytes()
                self._lines[path] = max(1, data.count(b"\n") + (0 if data.endswith(b"\n") else 1))
            except OSError:
                self._lines[path] = 1
        return self._lines[path]


def parse_evidence(answer: str, mapper: Mapper) -> list[dict]:
    """回答の EVIDENCE: の節から、根拠に挙げた場所を取り出す。"""
    out: list[dict] = []
    # 見出しの行だけに一致させる(\s は改行もまたぐので、行の中の空白は [ \t] で書く)
    m = re.search(r"^[ \t]*[*#]*[ \t]*EVIDENCE[ \t]*:?[ \t]*\**[ \t]*(.*)$", answer or "", re.MULTILINE | re.IGNORECASE)
    if not m:
        return out
    for line in (answer[m.end():]).splitlines():
        if not line.strip():
            continue
        hit = EVIDENCE_LINE.match(line)
        if not hit:
            if out:
                break
            continue
        found = mapper.resolve(hit.group(1))
        if found:
            start = int(hit.group(2))
            end = int(hit.group(3) or start)
            out.append({"source_id": found[0], "path": found[1], "start_line": min(start, end),
                        "end_line": max(start, end), "origin": "cited"})
    return out


def _result_text(result) -> str:
    if result is None:
        return ""
    if isinstance(result, str):
        return result
    if isinstance(result, dict):
        for key in ("textResultForLlm", "content", "text", "output"):
            v = result.get(key)
            if isinstance(v, str):
                return v
    return json.dumps(result, ensure_ascii=False)


def collect(home: Path, usage_file: Path, mapper: Mapper, serena_root: Path | None = None) -> dict:
    """COPILOT_HOME の記録から、回答・見た場所・呼び出し・トークンを集める。serena_root は、Serena が返す相対パスの
    もとのフォルダ(Serena の手法のとき)。"""
    rec: dict = {"answer": None, "retrieved": [], "tool_calls": 0, "tools": {}, "returned_bytes": 0,
                 "offloaded_results": 0, "session_id": "", "serena_root": serena_root}
    state = home / "session-state"
    sessions = [d for d in state.iterdir() if d.is_dir() and not d.name.startswith(".")] if state.is_dir() else []
    if len(sessions) == 1:
        rec["session_id"] = sessions[0].name
        events = sessions[0] / "events.jsonl"
        names: dict[str, str] = {}
        if events.is_file():
            for line in events.read_text(encoding="utf-8", errors="replace").splitlines():
                try:
                    e = json.loads(line)
                except ValueError:
                    continue
                kind, data = e.get("type"), e.get("data") or {}
                if kind == "tool.execution_start":
                    name = str(data.get("toolName", "?"))
                    names[data.get("toolCallId")] = name
                    rec["tool_calls"] += 1
                    rec["tools"][name] = rec["tools"].get(name, 0) + 1
                    args = data.get("arguments") if isinstance(data.get("arguments"), dict) else {}
                    if name == "view" and isinstance(args.get("path"), str):
                        found = mapper.resolve(args["path"])
                        if found and found[2].is_file():
                            rng = args.get("view_range")
                            if isinstance(rng, list) and len(rng) == 2 and all(isinstance(x, int) for x in rng):
                                start = max(1, rng[0])
                                end = mapper.line_count(found[2]) if rng[1] < 0 else max(start, rng[1])
                            else:
                                start, end = 1, mapper.line_count(found[2])
                            rec["retrieved"].append({"source_id": found[0], "path": found[1], "start_line": start,
                                                     "end_line": end, "origin": "opened"})
                elif kind == "tool.execution_complete":
                    text = _result_text(data.get("result"))
                    if M.tool_family(names.get(data.get("toolCallId"), "")) == "serena" and rec.get("serena_root"):
                        for rel, start, end in M.serena_locations(text):
                            found = mapper.resolve(str(rec["serena_root"] / rel))
                            if found:
                                rec["retrieved"].append({"source_id": found[0], "path": found[1], "start_line": start,
                                                         "end_line": max(start, end), "origin": "symbol_result"})
                    rec["returned_bytes"] += len(text.encode("utf-8"))
                    if OFFLOAD_MARK in text:
                        rec["offloaded_results"] += 1
                    if text.startswith("context-foundation search:") or "\ncontext-foundation search:" in text[:200]:
                        for hit in SEARCH_HIT.finditer(text):
                            if hit.group(1) in mapper.ids:
                                rec["retrieved"].append({
                                    "source_id": hit.group(1), "path": hit.group(2), "start_line": int(hit.group(3)),
                                    "end_line": int(hit.group(4)), "origin": "search_result"})
                elif kind == "assistant.message" and isinstance(data.get("content"), str) and data["content"].strip():
                    rec["answer"] = data["content"]
    db = home / "session-store.db"
    if db.is_file() and rec["session_id"]:
        try:
            con = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
            rows = con.execute(
                "select input_tokens, cache_read_tokens, cache_write_tokens, output_tokens, total_nano_aiu "
                "from assistant_usage_events where session_id=? order by id", (rec["session_id"],)).fetchall()
            con.close()
            rec["model_calls"] = len(rows)
            rec["input_tokens"] = sum(r[0] or 0 for r in rows)
            rec["cache_read_tokens"] = sum(r[1] or 0 for r in rows)
            rec["cache_write_tokens"] = sum(r[2] or 0 for r in rows)
            rec["output_tokens"] = sum(r[3] or 0 for r in rows)
        except sqlite3.Error:
            pass
    if usage_file.is_file():
        try:
            usage = json.loads(usage_file.read_text(encoding="utf-8"))
            rec["usage"] = usage
            nano = usage.get("totalNanoAiu")
            rec["ai_units"] = round(nano / 1e9, 4) if nano else None
        except ValueError:
            pass
    return rec


def cost_usd(rec: dict, prices: dict | None) -> float | None:
    """BYOK のときの費用の見積もり(100 万トークンあたりの単価から)。input_tokens は、キャッシュの読み書きを含む数。"""
    if not prices or rec.get("input_tokens") is None:
        return None
    read, write = rec.get("cache_read_tokens") or 0, rec.get("cache_write_tokens") or 0
    fresh = max(0, rec["input_tokens"] - read - write)
    total = (fresh * prices["input"] + read * prices.get("cache_read", prices["input"])
             + write * prices.get("cache_write", prices["input"]) + (rec.get("output_tokens") or 0) * prices["output"])
    return round(total / 1e6, 6)


def added_dirs(args, roots: list[Path]) -> list[tuple[str, Path]]:
    """作業フォルダのほかに Copilot に読ませるフォルダ(--add-dir で渡すもの)と、その種類。

    source: 作業フォルダの外にあるコーパス(どの手法でも渡す)。tool: この道具を置いたフォルダ(agent_cf だけ。
    skill が CLI を呼ぶため)。cxxref: cxxref を置いたフォルダ(agent_grep_cxxref だけ)。
    """
    dirs: dict[Path, str] = {}
    for r in roots:
        if args.workspace not in r.parents and r != args.workspace:
            dirs.setdefault(r, "source")
    if args.method == "agent_cf":
        dirs.setdefault(REPO, "tool")
    elif args.method == "agent_grep_cxxref" and args.cxxref:
        dirs.setdefault(Path(args.cxxref), "cxxref")
    return [(label, d) for d, label in dirs.items()]


def _up_to_repository_root(start: Path, limit: int = 16) -> list[Path]:
    """start から、版管理のルート(.git のあるフォルダ)まで。ルートが見つからなければ、start だけ。"""
    chain: list[Path] = []
    for d in [start, *start.parents][:limit]:
        chain.append(d)
        if (d / ".git").exists():
            return chain
    return [start]


def instruction_files(workspace: Path, added: list[tuple[str, Path]]) -> list[str]:
    """Copilot CLI が指示として読むファイルを探す。「置き場の種類:名前」で返す(絶対パスは記録に残さない)。

    workspace(作業フォルダ)と repository(そこから版管理のルートまで)にあるものは、どの手法でも毎回読まれる。
    source・tool・cxxref(--add-dir で渡すフォルダから、その版管理のルートまで)にあるものは、その中のファイルを
    扱うときに読まれることがある。tool と cxxref は、手法によって渡すかどうかが違う。
    """
    found: list[str] = []
    seen: set[Path] = set()

    def scan(label: str, d: Path) -> None:
        if d in seen:
            return
        seen.add(d)
        for name in INSTRUCTION_NAMES:
            if (d / name).is_file():
                found.append(f"{label}:{name}")
        for folder, pattern in INSTRUCTION_GLOBS:
            if (d / folder).is_dir() and next((d / folder).rglob(pattern), None) is not None:
                found.append(f"{label}:{folder}/**/{pattern}")

    for i, d in enumerate(_up_to_repository_root(Path(workspace))):
        scan("workspace" if i == 0 else "repository", d)
    for label, start in added:
        for d in _up_to_repository_root(Path(start)):
            scan(label, d)
    return found


def command_for(args, exe: str, prompt: str, usage_file: Path, share: Path, roots: list[Path]) -> tuple[list, dict]:
    """手法ごとの、copilot の引数と環境変数。

    渡す道具の一式(BASE_TOOLS)と許可は、どの手法でも同じにする。道具の定義は、使わない回でもモデルを呼ぶたびに
    送られるので、手法によって一式が違うと、それだけで入力トークンに差が出るため。違うのは、検索の道具だけ:
    skill のフォルダと hook(agent_cf・agent_grep_cxxref)か、MCP サーバー(agent_cf_mcp)。
    許可: 読み取りは通す。書き込みは断る。shell は python の起動だけを通す(skill が CLI を 1 回呼ぶため。
    確認できない場面なので、ほかの shell の命令は断られる)。
    """
    tools = list(BASE_TOOLS)
    f = M.features(args.method)
    cmd = [exe, "-p", prompt, "--model", args.model, "-s", "--log-level", "error",
           "--usage-output-file", str(usage_file), "--share", str(share),
           "--allow-tool", "read", "--allow-tool", "shell(python:*)", "--deny-tool", "write"]
    # 索引の古さの 1 文(session-hint)は足さない。足すと、前の回と文面が変わって比べられなくなる
    env: dict[str, str] = {"CF_INDEX_AGE_WARN_DAYS": "0"}
    if args.method == "agent_cf":
        env["COPILOT_SKILLS_DIRS"] = str(REPO / "skills")
    elif args.method == "agent_grep_cxxref":
        if not args.cxxref or not (Path(args.cxxref) / "skills").is_dir():
            raise SystemExit("agent_grep_cxxref には --cxxref <cxxref を置いたフォルダ> が要ります")
        env["COPILOT_SKILLS_DIRS"] = str(Path(args.cxxref) / "skills")
    servers: dict = {}
    if "cf_mcp" in f:
        servers[MCP_NAME] = {"type": "local", "command": sys.executable,
                             "args": ["-I", "-m", "context_foundation", "serve"], "tools": ["*"]}
    if "serena" in f:
        server = M.serena_server(args.serena["project"], "copilot-cli", args.serena["command"])
        servers[M.SERENA_NAME] = {"type": "local", "command": server[0], "args": server[1:], "tools": ["*"],
                                  "env": args.serena["env"]}
    if servers:
        cmd += ["--additional-mcp-config", json.dumps({"mcpServers": servers})]
        for name in servers:
            cmd += ["--allow-tool", name]
            tools.append(name)
    # 言語サーバー(clangd など)の道具 lsp。LSP の手法と、--lsp-config を渡したほかの手法(0.4.10 までの使い方)。
    # 設定は、この回の設定フォルダに置く
    if "lsp" in f or getattr(args, "lsp_config", None):
        tools.append("lsp")
    cmd += ["--available-tools", ",".join(tools)]
    for _, d in added_dirs(args, roots):
        cmd += ["--add-dir", str(d)]
    if args.no_custom_instructions:
        cmd.append("--no-custom-instructions")
    if args.max_ai_credits:
        cmd += ["--max-ai-credits", str(args.max_ai_credits)]
    if args.trust_folder:
        cmd.append("--trust-folder")
    if args.provider != "copilot":
        env["COPILOT_PROVIDERS_CONFIG"] = str(Path(args.providers_config).expanduser())
    return cmd, env


def hooks_for(args) -> dict | None:
    """手法ごとの、この回の設定フォルダに置く hook。置かない手法では None。

    agent_cf: プラグインが足すのと同じ、セッションの始めの 1 文(入口 ctxf.py の session-hint。索引のある作業
    フォルダで、skill の使いどころを知らせる)。プラグインの hooks.json は ${PLUGIN_ROOT} からの場所で書かれて
    いて、設定フォルダに置く hook では使えないので、shell を通さずに、入口の場所を直接渡す。
    """
    f = M.features(args.method)
    if "serena" in f:                     # Serena 自身の始めの指示(と、この道具の 1 文)。流す前に作ったもの
        return {"version": 1, "hooks": {"sessionStart": [
            {"type": "command", "exec": "python", "args": ["-I", str(BENCH / "hint_hook.py"), str(args.hint_file)],
             "timeoutSec": 10}]}}
    if args.method in ("agent_cf", "agent_cf_mcp", "agent_cf_mcp_lsp") and not args.no_hint:
        hint = ["session-hint"] + (["--mcp"] if "cf_mcp" in f else [])
        return {"version": 1, "hooks": {"sessionStart": [
            {"type": "command", "exec": "python", "args": ["-I", str(REPO / "ctxf.py"), *hint],
             "timeoutSec": 10}]}}
    if args.method == "agent_grep_cxxref":
        script = str(Path(args.cxxref) / "scripts" / "cxxref_hook.py")
        return {"version": 1, "hooks": {
            "sessionStart": [{"type": "command", "exec": "python", "args": ["-I", script, "session"],
                              "timeoutSec": 10}],
            "preToolUse": [{"type": "command", "exec": "python", "args": ["-I", script], "timeoutSec": 10}]}}
    return None


def run_one(args, exe: str, case: dict, trial: int, mapper: Mapper, roots: list[Path], prices: dict | None) -> dict:
    name = f"{args.run_id}-{case['case_id']}-{args.method}-t{trial}"
    home = args.out / "homes" / name
    if home.exists():
        shutil.rmtree(home)
    home.mkdir(parents=True)
    logs = args.out / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    usage_file, share = logs / f"{name}.usage.json", logs / f"{name}.md"
    cmd, extra_env = command_for(args, exe, build_prompt(case["question"], roots), usage_file, share, roots)
    env = dict(os.environ, COPILOT_HOME=str(home), **extra_env)
    for v in ("COPILOT_PROVIDER_TYPE", "COPILOT_PROVIDER_BASE_URL", "COPILOT_PROVIDER_API_KEY",
              "COPILOT_PROVIDER_API_KEY_COMMAND", "COPILOT_MODEL", "COPILOT_ALLOW_ALL", "CF_CONFIG",
              "COPILOT_CUSTOM_INSTRUCTIONS_DIRS"):
        env.pop(v, None)
    if args.provider == "copilot":
        env.pop("COPILOT_PROVIDERS_CONFIG", None)
    if getattr(args, "lsp_config", None):
        shutil.copyfile(args.lsp_config, home / "lsp-config.json")
    hooks = hooks_for(args)
    if hooks:
        (home / "hooks").mkdir()
        (home / "hooks" / "bench.json").write_text(json.dumps(hooks), encoding="utf-8")
    if args.dry_run:
        shown = [c if i != 2 else "<質問と回答の書式>" for i, c in enumerate(cmd)]
        print(json.dumps({"name": name, "cwd": str(args.workspace), "env": extra_env, "cmd": shown},
                         ensure_ascii=False, indent=1))
        shutil.rmtree(home)
        return {}
    t0 = time.time()
    status = "completed"
    try:
        p = subprocess.run(cmd, cwd=args.workspace, env=env, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=args.time_limit)
        code, out = p.returncode, (p.stdout or "") + (p.stderr or "")
    except subprocess.TimeoutExpired as e:
        code, out, status = "timeout", str(e.stdout or "") + str(e.stderr or ""), "timeout"
    seconds = round(time.time() - t0, 1)
    (logs / f"{name}.out.txt").write_text(out, encoding="utf-8")
    rec = collect(home, usage_file, mapper, args.serena["project"] if getattr(args, "serena", None) else None)
    answer = rec["answer"] or (out.strip() if status == "completed" and code == 0 else None)
    if status == "completed" and (code != 0 or not answer):
        status = "gave_up"
    seen: set[tuple] = set()
    retrieved: list[dict] = []
    for e in rec["retrieved"] + parse_evidence(answer or "", mapper):
        key = (e["source_id"], e["path"], e["start_line"], e["end_line"], e["origin"])
        if key not in seen:
            seen.add(key)
            retrieved.append(dict(rank=len(retrieved) + 1, **e))
    row = {
        "run_id": args.run_id, "case_id": case["case_id"], "method": args.method, "trial": trial, "status": status,
        "retrieved_evidence": retrieved, "search_time_ms": None, "answer": answer, "answer_correctness": None,
        "task_time_seconds": seconds, "input_tokens": rec.get("input_tokens"),
        "output_tokens": rec.get("output_tokens"), "cache_read_tokens": rec.get("cache_read_tokens"),
        "cache_write_tokens": rec.get("cache_write_tokens"), "ai_units": rec.get("ai_units"),
        "cost_usd": cost_usd(rec, prices), "tool_calls": rec["tool_calls"], "model_calls": rec.get("model_calls"),
        "returned_bytes": rec["returned_bytes"], "offloaded_results": rec["offloaded_results"],
        "session_id": rec["session_id"], "tools_by_name": rec["tools"],
    }
    if code not in (0, "timeout"):
        row["notes"] = f"copilot exit {code}"
    print(f"{name}: {status} {seconds}s tools={rec['tools']} calls={rec.get('model_calls')} "
          f"in={rec.get('input_tokens')} out={rec.get('output_tokens')} aiu={rec.get('ai_units')} "
          f"usd={row['cost_usd']} evidence={len(retrieved)}", flush=True)
    return row


def main() -> int:
    ap = argparse.ArgumentParser(description="エージェントを使う手法を Copilot CLI で回して、results.jsonl に書く")
    ap.add_argument("--config", help="作業フォルダ(.cf/config.toml のある場所)。省くと、今のフォルダから探す")
    ap.add_argument("--cases", required=True)
    ap.add_argument("--method", required=True, choices=METHODS)
    ap.add_argument("--out", required=True, type=Path, help="結果を書くフォルダ(非公開の場所)")
    ap.add_argument("--case", action="append", default=[], help="この case_id だけを回す(繰り返せる)")
    ap.add_argument("--split", choices=("eval", "tune", "all"), default="eval")
    ap.add_argument("--trials", type=int, default=3, help="1 質問あたりの回数(既定 3)")
    ap.add_argument("--model", default="claude-haiku-5-5", help="モデルの名前(BYOK なら providers.json の id)")
    ap.add_argument("--provider", default="anthropic",
                    help="BYOK の提供元の名前(providers.json の name)。copilot なら、Copilot の契約のモデル")
    ap.add_argument("--providers-config", default="~/.copilot/providers.json")
    ap.add_argument("--time-limit", type=int, default=600, help="1 回の上限(秒)。超えたら timeout")
    ap.add_argument("--max-ai-credits", type=float, default=0, help="1 回の AI credits の上限(Copilot の契約のとき)")
    ap.add_argument("--price", action="append", default=[], metavar="KIND=USD",
                    help="100 万トークンあたりの単価(input / output / cache_read / cache_write)。BYOK の費用の見積もり用")
    ap.add_argument("--cxxref", help="agent_grep_cxxref で使う、cxxref を置いたフォルダ")
    ap.add_argument("--lsp-config", help="Copilot CLI の lsp-config.json。LSP の手法に要る。ほかの手法に渡すと、"
                                         "その手法にも道具 lsp を足す(0.4.10 までの使い方)")
    ap.add_argument("--serena-project", type=Path,
                    help="Serena の手法: .serena/project.yml のあるフォルダ(省くと、source が 1 つならそのルート、"
                         "ほかは作業フォルダ)")
    ap.add_argument("--serena-command", default=M.SERENA_COMMAND, help="Serena を起こすコマンド")
    ap.add_argument("--serena-home", type=Path, help="Serena の設定の置き場(SERENA_HOME。省くと <out>/serena-home)")
    ap.add_argument("--trust-folder", action="store_true", help="作業フォルダの設定(hooks・MCP)を読ませる")
    ap.add_argument("--no-hint", action="store_true",
                    help="agent_cf で、セッションの始めの 1 文(hook)を置かない(skill の説明文だけにする)")
    ap.add_argument("--no-custom-instructions", action="store_true",
                    help="指示のファイル(AGENTS.md・CLAUDE.md・.github/copilot-instructions.md など)を、どの手法でも読ませない")
    ap.add_argument("--run-id")
    ap.add_argument("--dry-run", action="store_true", help="実行せずに、コマンドだけを出す")
    ap.add_argument("--yes", action="store_true", help="費用がかかることを了解して、実行する")
    args = ap.parse_args()

    path = find_config(args.config)
    if path is None:
        raise SystemExit("設定(.cf/config.toml)が見つかりません。--config で作業フォルダを指定してください")
    cfg = load_config(path)
    args.workspace = cfg.workspace
    mapper = Mapper(cfg)
    roots = [Path(os.path.normpath(s.root)) for s in cfg.sources]
    all_cases, issues = load_cases(args.cases)
    if any(i.level == "error" for i in issues):
        raise SystemExit("cases.jsonl に誤りがあります。`ctxf validate --cases ...` で確かめてください")
    cases = [c for cid, c in all_cases.items()
             if (not args.case or cid in args.case) and (args.split == "all" or c.get("split", "eval") == args.split)]
    if not cases:
        raise SystemExit("回す質問がありません")
    prices = None
    if args.price:
        prices = {k: float(v) for k, v in (x.split("=", 1) for x in args.price)}
        if not {"input", "output"} <= set(prices):
            raise SystemExit("--price には、少なくとも input=… と output=… が要ります")
    args.out = args.out.resolve()
    args.out.mkdir(parents=True, exist_ok=True)
    args.run_id = args.run_id or time.strftime("agent-%Y%m%d-%H%M%S")
    model = args.model if args.provider == "copilot" else f"{args.provider}/{args.model}"
    args.model = model
    f = M.features(args.method)
    if "lsp" in f and not args.lsp_config:
        raise SystemExit(f"{args.method} には --lsp-config(Copilot CLI の lsp-config.json)が要ります")
    args.serena = None
    args.hint_file = None
    if "serena" in f:
        project = (args.serena_project or (roots[0] if len(roots) == 1 else args.workspace)).resolve()
        if not M.serena_project_ok(project):
            raise SystemExit(M.serena_setup_hint(project, args.serena_command))
        serena_home = (args.serena_home or args.out / "serena-home").resolve()
        serena_home.mkdir(parents=True, exist_ok=True)
        env = {"SERENA_HOME": str(serena_home)}
        args.serena = {"project": project, "command": args.serena_command, "env": env}
        parts = []
        if "cf_mcp" in f and not args.no_hint:
            # agent_cf_mcp の hook と同じ 1 文(索引の古さの 1 文は足さない。1 文が出なければ止める)
            parts.append(M.session_hint(sys.executable, REPO / "ctxf.py", args.workspace))
        parts.append(M.serena_prompt(project, "copilot-cli", args.serena_command, dict(os.environ, **env),
                                     args.out / "serena-prompt-copilot.txt"))
        args.hint_file = args.out / f"hint-{args.method}.txt"
        args.hint_file.write_text("\n\n".join(parts), encoding="utf-8")
    exe = find_copilot()

    results = args.out / "results.jsonl"
    done: set[tuple] = set()
    if results.is_file():
        done = {(r["run_id"], r["case_id"], r["method"], r["trial"]) for _, r in read_jsonl(results, "results")[0]}
    todo = [(c, t) for c in cases for t in range(1, args.trials + 1)
            if (args.run_id, c["case_id"], args.method, t) not in done]
    print(f"{args.method}: {len(cases)} 件 × {args.trials} 回 = {len(cases) * args.trials} 回(残り {len(todo)} 回)、"
          f"モデル {model}、1 回の上限 {args.time_limit} 秒")
    instructions = instruction_files(args.workspace, added_dirs(args, roots))
    if instructions and args.no_custom_instructions:
        print(f"指示のファイルが {len(instructions)} 個ありますが、読ませません(--no-custom-instructions): "
              + "、".join(instructions))
    elif instructions:
        print("注意: Copilot CLI が指示として読むファイルがあります。中身が、質問と一緒にモデルへの入力に入ります。\n"
              + "".join(f"  - {x}\n" for x in instructions)
              + "  workspace と repository のものは、どの手法でも毎回読まれます。source・tool・cxxref のものは、その中の\n"
                "  ファイルを扱うときに読まれることがあり、tool と cxxref は手法によって条件が変わります。\n"
                "  どの手法でも読ませないなら、--no-custom-instructions を付けてください。どちらで回したかは runs.jsonl に残ります。")
    if not args.dry_run and not args.yes:
        print("実行のたびに、AI credits か BYOK の料金がかかります。質問と、エージェントが読んだ本文は、モデルの提供元に"
              "送られます。了解したら --yes を付けて実行してください(--dry-run で、コマンドだけを確かめられます)")
        return 2

    if not args.dry_run:
        runs = args.out / "runs.jsonl"
        known = {r["run_id"]: r for _, r in read_jsonl(runs, "runs")[0]} if runs.is_file() else {}
        meta = known.get(args.run_id) or {
            "run_id": args.run_id, "created_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "corpus_revision": "",
            "methods": {}, "host": host_info(),
            "agent": {"name": "copilot-cli", "version": copilot_version(exe), "model": model,
                      "provider": "copilot" if args.provider == "copilot" else f"byok:{args.provider}",
                      "time_limit_seconds": args.time_limit, "trials": args.trials,
                      "price_per_million_tokens_usd": prices},
            "data_handling": "Questions and the passages the agent reads are sent to the model provider named in "
                             "agent.provider. The index and embeddings stay local.",
        }
        meta["methods"][args.method] = {
            "kind": "agent", "tools": M.described_tools(args.method, list(BASE_TOOLS), MCP_NAME)
            + (["lsp"] if args.lsp_config and "lsp" not in M.features(args.method) else []),
            "permissions": "read allowed, write denied, shell limited to python",
            "session_hint": hooks_for(args) is not None,
            "isolated_config": True, "trust_folder": args.trust_folder,
            "custom_instructions": "disabled" if args.no_custom_instructions else "enabled",
            "instruction_files": instructions}
        if args.serena:
            meta["methods"][args.method]["serena"] = {"command": args.serena_command, "context": "copilot-cli",
                                                      "modes": list(M.SERENA_MODES)}
        if args.lsp_config:
            meta["methods"][args.method]["lsp_config"] = Path(args.lsp_config).name
        known[args.run_id] = meta
        runs.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in known.values()), encoding="utf-8")

    for case, trial in todo:
        row = run_one(args, exe, case, trial, mapper, roots, prices)
        if row:
            with open(results, "a", encoding="utf-8", newline="\n") as f:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
    if not args.dry_run:
        print(f"書きました: {results}\n要約に並べるには: ctxf eval --cases … --out … --import {results} "
              f"--import-runs {args.out / 'runs.jsonl'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
