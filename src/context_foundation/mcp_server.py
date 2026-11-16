"""読み取り専用の MCP サーバー(stdio)。標準ライブラリだけで JSON-RPC を扱う。

ツールは 2 つだけ:
  search(query, top_k, mode, source_id)  順位つきの passage を返す
  status()                               索引の版・件数・作成日時

しないこと: 書き込み、索引に無いファイルの読み出し、回答の生成。読むのは索引のファイルだけ
(索引の後に変わったかの印を付けるために、結果に出たファイルの大きさと更新日時だけは見る)。

サーバーは起動したまま保たれるので、埋め込みモデルとベクトルを読み込んだままにでき、2 回目からの検索が速い。
設定(.cf)は、--config が無ければ、サーバーの作業フォルダ(= エージェントの作業フォルダ)から親へ探す。
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from . import __version__
from .config import MODES, ConfigError, find_config, load_config
from .render import HARD_MAX_BYTES, status_agent, to_agent

PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
INSTRUCTIONS = (
    "Use `search` when you need to find where something is implemented or documented but do not know its name "
    "or location (another engine's term, a plain-language or Japanese description). It returns ranked passages "
    "with source:path:start-end. For a known identifier, use grep or a reference index instead.")

TOOLS = [
    {
        "name": "search",
        "description": (
            "Find code or design-doc passages by meaning when the name or location is unknown. Searches a local "
            "index (keyword + multilingual semantic) and returns ranked passages as source:path:start-end with "
            "scope, signature and text. Not for known identifiers - use grep for those."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string",
                          "description": "Question or keywords. For code written in English, include English terms."},
                "top_k": {"type": "integer", "minimum": 1, "maximum": 20, "description": "Passages to return (8)."},
                "mode": {"type": "string", "enum": list(MODES), "description": "Default hybrid."},
                "source_id": {"type": "string", "description": "Limit to one source (see status)."},
            },
            "required": ["query"],
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": True, "openWorldHint": False},
    },
    {
        "name": "status",
        "description": "Index version, sources, passage counts and build time.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
        "annotations": {"readOnlyHint": True, "openWorldHint": False},
    },
]


class _State:
    def __init__(self, config: str | None):
        self.config = config
        self.searchers: dict[str, tuple] = {}

    def searcher(self):
        from .search import Searcher
        path = find_config(self.config)
        if path is None:
            raise LookupError(
                "No context-foundation index: no .cf folder found from " + os.getcwd() + " upward. "
                "Search with grep as usual. Building an index takes minutes; ask the user first.")
        key = str(path)
        hit = self.searchers.get(key)
        # 設定か、作業フォルダの用語の対応表が変わったら読み直す
        if hit is None or hit[0] != (path.stat().st_mtime_ns, _glossary_mtime(path.parent, hit[1])):
            cfg = load_config(path)
            hit = ((path.stat().st_mtime_ns, _glossary_mtime(path.parent, cfg)), cfg, Searcher.from_config(cfg))
            self.searchers[key] = hit
        return hit[1], hit[2]


def _glossary_mtime(cf_dir: Path, cfg) -> int | None:
    """作業フォルダの用語の対応表の更新時刻(無ければ None)。設定で場所を変えていれば、そのファイル。"""
    setting = cfg.search.glossary
    p = Path(setting) if setting and Path(setting).is_absolute() else cf_dir / (setting or "glossary.tsv")
    try:
        return p.stat().st_mtime_ns
    except OSError:
        return None


def call_tool(state: _State, name: str, args: dict) -> tuple[bool, str]:
    """(誤りか, 文面) を返す。"""
    from .store import IndexError_
    try:
        cfg, searcher = state.searcher()
        if name == "status":
            return False, status_agent(searcher.status())
        if name != "search":
            return True, f"Unknown tool: {name}"
        query = args.get("query")
        if not isinstance(query, str) or not query.strip():
            return True, "search needs a non-empty string `query`."
        top_k = args.get("top_k")
        if top_k is not None and (isinstance(top_k, bool) or not isinstance(top_k, int)):
            return True, "`top_k` must be an integer."
        mode = args.get("mode")
        source = args.get("source_id")
        sources = [source] if isinstance(source, str) and source else None
        resp = searcher.search(query.strip(), mode=mode, top_k=min(top_k or cfg.search.top_k, 20),
                               source_ids=sources)
        return False, to_agent(resp, min(cfg.search.max_output_bytes, HARD_MAX_BYTES))
    except LookupError as e:
        return True, str(e)
    except IndexError_ as e:
        return True, ("No context-foundation index yet (" + str(e).splitlines()[0] + "). "
                      "Search with grep as usual. Building an index takes minutes; ask the user first.")
    except (ValueError, ConfigError) as e:
        return True, str(e)
    except Exception as e:   # 1 行の誤りにして返す(サーバーは落とさない)
        return True, f"{type(e).__name__}: {e}"


def handle(state: _State, msg: dict) -> dict | None:
    """1 つの JSON-RPC の要求に答える。通知(id が無い)には何も返さない。"""
    mid, method, params = msg.get("id"), msg.get("method"), msg.get("params") or {}
    if method is None or mid is None:
        return None

    def ok(result: dict) -> dict:
        return {"jsonrpc": "2.0", "id": mid, "result": result}

    def err(code: int, message: str) -> dict:
        return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": message}}

    if method == "initialize":
        want = params.get("protocolVersion")
        return ok({"protocolVersion": want if want in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0],
                   "capabilities": {"tools": {"listChanged": False}},
                   "serverInfo": {"name": "context-foundation", "version": __version__},
                   "instructions": INSTRUCTIONS})
    if method == "ping":
        return ok({})
    if method == "tools/list":
        return ok({"tools": TOOLS})
    if method == "tools/call":
        name, args = params.get("name"), params.get("arguments") or {}
        if not isinstance(name, str) or not isinstance(args, dict):
            return err(-32602, "tools/call needs `name` (string) and `arguments` (object)")
        if name not in ("search", "status"):
            return err(-32602, f"Unknown tool: {name}")
        is_error, text = call_tool(state, name, args)
        return ok({"content": [{"type": "text", "text": text}], "isError": is_error})
    return err(-32601, f"Method not found: {method}")


def serve(config: str | None = None, stdin=None, stdout=None) -> int:
    stdin = stdin or sys.stdin.buffer
    stdout = stdout or sys.stdout.buffer
    state = _State(config)
    while True:
        line = stdin.readline()
        if not line:
            return 0
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            reply = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}}
        else:
            if isinstance(msg, list):       # まとめて送る形は、順に答える
                replies = [r for r in (handle(state, m) for m in msg if isinstance(m, dict)) if r is not None]
                reply = replies or None
            elif isinstance(msg, dict):
                reply = handle(state, msg)
            else:
                reply = {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Invalid Request"}}
        if reply is not None:
            stdout.write(json.dumps(reply, ensure_ascii=False).encode("utf-8") + b"\n")
            stdout.flush()
