"""エージェントを使う手法の定義(agent_run.py・claude_run.py・compare.py で共通)。

手法は、既存の手順(grep と読む道具)に足すものの組み合わせ:

  agent_grep           足さない(既存の手順)
  agent_grep_cxxref    記号参照の索引 cxxref の skill と hook(Copilot CLI だけ)
  agent_cf             この道具の skill と、始めの 1 文(Copilot CLI だけ。0.2 までの形)
  agent_cf_mcp         この道具の MCP サーバーと、始めの 1 文(0.3 からの既定の形)
  agent_lsp            言語サーバー(clangd など)の道具。Copilot CLI は lsp、Claude Code は LSP
  agent_cf_mcp_lsp     agent_cf_mcp と agent_lsp の両方
  agent_serena         Serena(言語サーバーを使う記号の道具の MCP サーバー)と、Serena 自身の始めの指示
  agent_cf_mcp_serena  agent_cf_mcp と agent_serena の両方

Serena は、手を加えない既定の形で比べる(道具の出し方は Serena の設定のまま、始めの指示は Serena の
print-system-prompt が出すもの)。書き込みの道具を外すモード planning と、記憶の道具を外すモード benchmark を
付ける。どちらも Serena に入っているモード。
"""
from __future__ import annotations

import json
import re
import shlex
import subprocess
from pathlib import Path

METHODS = ("agent_grep", "agent_grep_cxxref", "agent_cf", "agent_cf_mcp", "agent_lsp", "agent_cf_mcp_lsp",
           "agent_serena", "agent_cf_mcp_serena")
# Claude Code の流し手で流せる手法(skill と cxxref は Copilot CLI の仕組みに頼る)
CLAUDE_METHODS = ("agent_grep", "agent_cf_mcp", "agent_lsp", "agent_cf_mcp_lsp", "agent_serena",
                  "agent_cf_mcp_serena")

SERENA_NAME = "serena"
SERENA_VERSION = "2.1.0"
SERENA_COMMAND = f"uvx --from serena-agent=={SERENA_VERSION} serena"
SERENA_MODES = ("planning", "benchmark")
# 言語サーバーに渡す、C・C++ の拡張子(Claude Code の .lsp.json の extensionToLanguage)
CPP_EXTENSIONS = {".c": "c", ".h": "cpp", ".hh": "cpp", ".hpp": "cpp", ".hxx": "cpp", ".inl": "cpp", ".cc": "cpp",
                  ".cpp": "cpp", ".cxx": "cpp", ".ipp": "cpp", ".m": "objective-c", ".mm": "objective-cpp"}


def features(method: str) -> frozenset[str]:
    """手法が足すもの: cf_mcp・cf_skill・cxxref・lsp・serena。"""
    f = set()
    if method.startswith("agent_cf_mcp"):
        f.add("cf_mcp")
    if method == "agent_cf":
        f.add("cf_skill")
    if method == "agent_grep_cxxref":
        f.add("cxxref")
    if method.endswith("_lsp"):
        f.add("lsp")
    if method.endswith("_serena"):
        f.add("serena")
    return frozenset(f)


def described_tools(method: str, base: list[str], cf_name: str = "context-foundation") -> list[str]:
    """runs.jsonl に残す、手法の道具の一式。"""
    f = features(method)
    return base + (["skill:context-search"] if "cf_skill" in f else []) + (["skill:cxxref"] if "cxxref" in f else []) \
        + ([f"mcp:{cf_name}"] if "cf_mcp" in f else []) + (["lsp"] if "lsp" in f else []) \
        + ([f"mcp:{SERENA_NAME}"] if "serena" in f else [])


# --- Serena ---

def serena_project_ok(project: Path) -> bool:
    return (project / ".serena" / "project.yml").is_file()


def serena_setup_hint(project: Path, command: str = SERENA_COMMAND) -> str:
    return (f"Serena のプロジェクトがありません: {project / '.serena' / 'project.yml'}。先に、言語サーバー(clangd)を PATH に置き、"
            f"次で作って記号の索引を作ります(.serena フォルダを、そのフォルダに書きます):\n"
            f"  {command} project create \"{project}\" --language cpp --index\n"
            "書き込みの道具を出さないよう、できた .serena/project.yml の read_only を true にしてください")


def serena_server(project: Path, context: str, command: str = SERENA_COMMAND) -> list[str]:
    """Serena の MCP サーバー(stdio)を起こすコマンド。"""
    cmd = shlex.split(command) + ["start-mcp-server", "--project", str(project), "--context", context]
    for m in SERENA_MODES:
        cmd += ["--mode", m]
    return cmd + ["--enable-web-dashboard", "False", "--open-web-dashboard", "False", "--log-level", "WARNING"]


def serena_prompt(project: Path, context: str, command: str, env: dict, cache: Path) -> str:
    """Serena 自身の始めの指示(print-system-prompt)。流しの間は同じものを使うので、cache に置く。"""
    if cache.is_file():
        return cache.read_text(encoding="utf-8")
    cmd = shlex.split(command) + ["print-system-prompt", str(project), "--context", context]
    for m in SERENA_MODES:
        cmd += ["--mode", m]
    p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", env=env, timeout=600)
    text = (p.stdout or "").strip()
    if p.returncode != 0 or not text:
        raise SystemExit(f"Serena の始めの指示を作れませんでした(終了コード {p.returncode}): "
                         + (p.stderr or "").strip()[-500:])
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(text, encoding="utf-8")
    return text


# --- 言語サーバー(Claude Code のプラグイン) ---

def lsp_plugin(folder: Path, command: list[str]) -> Path:
    """Claude Code に言語サーバーを渡すプラグイン(.claude-plugin/plugin.json と .lsp.json)を folder に書く。"""
    (folder / ".claude-plugin").mkdir(parents=True, exist_ok=True)
    (folder / ".claude-plugin" / "plugin.json").write_text(json.dumps(
        {"name": "bench-lsp", "version": "0.0.1", "description": "Language server for the benchmark"}), encoding="utf-8")
    (folder / ".lsp.json").write_text(json.dumps({"bench": {
        "command": command[0], "args": command[1:], "extensionToLanguage": CPP_EXTENSIONS}}, indent=1),
        encoding="utf-8")
    return folder


# --- 道具の種類(まとめで、手法がどの道具を使ったかを見る) ---

FAMILIES = ("search", "lsp", "serena", "grep", "glob", "read", "shell", "skill", "other")


def tool_family(name: str) -> str:
    """道具の名前(Copilot CLI と Claude Code の両方)を、種類に分ける。"""
    n = (name or "").lower()
    if "context-foundation" in n or "context_foundation" in n:
        return "search"
    if "serena" in n:
        return "serena"
    if n in ("lsp",) or n.startswith("lsp"):
        return "lsp"
    if n in ("grep", "rg", "ripgrep"):
        return "grep"
    if n in ("glob", "ls", "list_dir"):
        return "glob"
    if n in ("view", "read", "notebookread"):
        return "read"
    if n in ("bash", "powershell", "shell"):
        return "shell"
    if n == "skill":
        return "skill"
    return "other"


def _looks_like_path(key: str) -> bool:
    return "/" in key or bool(re.search(r"\.[A-Za-z0-9]{1,5}$", key))


def serena_locations(text: str) -> list[tuple[str, int, int]]:
    """Serena の道具の結果(JSON)から、記号の (相対パス, 始めの行, 終わりの行) を取り出す。Serena の行は 0 始まり
    なので 1 を足す。パスは、記号の項目の relative_path か、まとめの鍵(ファイルごとにまとめた結果)から取る。"""
    text = text or ""
    found: list[tuple[str, int, int]] = []

    def walk(o, hint: str) -> None:
        if isinstance(o, dict):
            rp = o.get("relative_path") if isinstance(o.get("relative_path"), str) else hint
            loc = o.get("body_location")
            if isinstance(loc, dict) and rp and isinstance(loc.get("start_line"), int) \
                    and isinstance(loc.get("end_line"), int):
                found.append((rp, loc["start_line"] + 1, loc["end_line"] + 1))
            for k, v in o.items():
                walk(v, k if isinstance(k, str) and _looks_like_path(k) else rp)
        elif isinstance(o, list):
            for v in o:
                walk(v, hint)

    dec = json.JSONDecoder()
    i = 0
    while True:
        starts = [j for j in (text.find("{", i), text.find("[", i)) if j >= 0]
        if not starts:
            break
        j = min(starts)
        try:
            obj, end = dec.raw_decode(text, j)
        except ValueError:
            i = j + 1
            continue
        walk(obj, "")
        i = end
    return found
