"""読み取り専用の MCP サーバー(stdio)。標準ライブラリだけで JSON-RPC を扱う。

ツールは 2 つだけ:
  search(query, top_k, mode, source_id)  順位つきの passage を返す
  status()                               索引の版・件数・作成日時

しないこと: 書き込み、索引に無いファイルの読み出し、回答の生成。読むのは索引のファイルだけ
(索引の後に変わったかの印を付けるために、結果に出たファイルの大きさと更新日時だけは見る)。

サーバーは起動したまま保たれるので、埋め込みモデルとベクトルを読み込んだままにでき、2 回目からの検索が速い。
最初の 1 回も待たせないように、索引のある作業フォルダでは、起動の挨拶(initialize)のときに裏で読み込みを始める
(設定の [search] preload = false で止められる)。
設定(.cf)は、--config が無ければ、エージェントの作業フォルダから親へ探す。エージェントの作業フォルダは、
ホストが環境変数で知らせていればその場所(session_dir)、無ければサーバーの作業フォルダ。
"""
from __future__ import annotations

import json
import math
import os
import sys
import threading
import traceback
from pathlib import Path

from . import __version__
from .config import MODES, ConfigError, find_config, load_config
from .render import HARD_MAX_BYTES, status_agent, to_agent

PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
INSTRUCTIONS = (
    "Call `search` first, before grepping, to find where something is implemented or documented in this workspace. "
    "Describe what the code does in English and add identifiers you expect. It returns ranked passages with "
    "source:path:start-end and their text, usually enough to answer and cite; grep or open files afterwards only "
    "for what the passages do not show.")
# 索引の無い作業フォルダ(プラグインは利用者ごとに入るので、どの作業フォルダでもサーバーが起動する)での挨拶。
# 検索を grep より先に呼ぶようには促さない(呼んでも、索引が無いという誤りが返るだけ)
INSTRUCTIONS_NO_INDEX = "No context-foundation index in this workspace; search with grep."
# 質問の長さの上限(UTF-8 のバイト数)。自然文の質問は数百バイト。長いコードを貼られても、keyword の側は
# 先頭の 256 語しか使わず、意味検索の側も 512 トークンで切れる
MAX_QUERY_BYTES = 8_192

TOOLS = [
    {
        "name": "search",
        "description": (
            "Find where something is implemented or documented in this workspace. Searches a local index of the "
            "code and design docs by meaning and by keywords, and returns ranked passages as source:path:start-end "
            "with scope, signature and text - usually enough to answer and cite without opening files. Call it "
            "before grep."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string",
                          "description": ("What to find: what the code does, in English, plus identifiers you "
                                          "expect (function, class or file names).")},
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


# ホストが、エージェントの作業フォルダを知らせる環境変数。Copilot CLI は、プラグインに入っている MCP サーバーを、
# プラグインのフォルダを作業フォルダにして起動し、利用者の作業フォルダはこの変数で渡す(1.0.112 で実測。
# MCP の roots は空で返る)
SESSION_DIR_ENV = ("COPILOT_SESSION_CWD",)


def session_dir() -> str:
    """エージェントの作業フォルダ(設定を探し始める場所)。"""
    for name in SESSION_DIR_ENV:
        value = os.environ.get(name)
        if value and os.path.isdir(value):
            return value
    return os.getcwd()


class _State:
    def __init__(self, config: str | None):
        self.config = config
        self.searchers: dict[str, tuple] = {}
        # 検索と、起動時の先読み(warm_up)が、同じ索引とモデルを同時に読み込まないようにする
        self.lock = threading.RLock()
        self.warm_up_started = False

    def searcher(self):
        from .search import Searcher
        start = session_dir()
        path = find_config(self.config, start=start)
        if path is None:
            raise LookupError(
                "No context-foundation index: no .cf folder found from " + start + " upward. "
                "Search with grep as usual. Building an index takes minutes; ask the user first.")
        key = str(path)
        hit = self.searchers.get(key)
        # 設定か、作業フォルダの用語の対応表が変わったら読み直す
        if hit is None or hit[0] != (path.stat().st_mtime_ns, _glossary_mtime(path.parent, hit[1])):
            cfg = load_config(path)
            hit = ((path.stat().st_mtime_ns, _glossary_mtime(path.parent, cfg)), cfg, Searcher.from_config(cfg))
            self.searchers[key] = hit
        return hit[1], hit[2]


def has_index(state: _State) -> bool:
    """エージェントの作業フォルダ(か、その親)に、索引があるか。設定が読めないときは、あるとみなす
    (誤りは、道具を呼んだときに文面で返す)。"""
    try:
        path = find_config(state.config, start=session_dir())
        return path is not None and load_config(path).index_path.is_file()
    except Exception:
        return True


def _glossary_mtime(cf_dir: Path, cfg) -> int | None:
    """作業フォルダの用語の対応表の更新時刻(無ければ None)。設定で場所を変えていれば、そのファイル。"""
    setting = cfg.search.glossary
    p = Path(setting) if setting and Path(setting).is_absolute() else cf_dir / (setting or "glossary.tsv")
    try:
        return p.stat().st_mtime_ns
    except OSError:
        return None


def warm_up(state: _State) -> None:
    """索引のベクトルと埋め込みモデルを、先に読み込む(裏のスレッドで動かす)。

    最初の検索は、モデルの読み込みで 1.5 秒ほどかかる。起動の挨拶のときに始めておけば、エージェントのモデルが
    最初の応答を返すまでの間に済む。索引が無い作業フォルダでは何もしない。ここでの失敗は黙って終わる
    (検索のときに、同じ誤りをあらためて文面で返す)。
    """
    try:
        with state.lock:
            cfg, searcher = state.searcher()
            if cfg.search.preload:
                searcher.warm_up()
    except Exception:
        pass


def start_warm_up(state: _State) -> threading.Thread | None:
    """先読みを、裏のスレッドで 1 回だけ始める。"""
    if state.warm_up_started:
        return None
    state.warm_up_started = True
    th = threading.Thread(target=warm_up, args=(state,), name="cf-warm-up", daemon=True)
    th.start()
    return th


def call_tool(state: _State, name: str, args: dict) -> tuple[bool, str]:
    """(誤りか, 文面) を返す。先読みが動いていれば、終わるのを待ってから検索する。"""
    with state.lock:
        return _call_tool(state, name, args)


def _call_tool(state: _State, name: str, args: dict) -> tuple[bool, str]:
    from .store import IndexError_
    try:
        cfg, searcher = state.searcher()
        if name == "status":
            from .autoindex import read_state

            st = searcher.status()
            st["auto_update"] = read_state(cfg)
            return False, status_agent(st)
        if name != "search":
            return True, f"Unknown tool: {name}"
        query = args.get("query")
        if not isinstance(query, str) or not query.strip():
            return True, "search needs a non-empty string `query`."
        size = len(query.encode("utf-8", "surrogatepass"))
        if size > MAX_QUERY_BYTES:
            return True, (f"query too long ({size:,} bytes; the limit is {MAX_QUERY_BYTES:,}): describe what you are "
                          "looking for in a sentence or two, plus the identifiers you expect.")
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
    mid, method, params = msg.get("id"), msg.get("method"), msg.get("params")
    if method is None or mid is None:
        return None
    if isinstance(mid, float) and not math.isfinite(mid):     # 1e999 は inf になる。JSON に書き戻せない
        return {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Invalid Request"}}

    def ok(result: dict) -> dict:
        return {"jsonrpc": "2.0", "id": mid, "result": result}

    def err(code: int, message: str) -> dict:
        return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": message}}

    if not params:
        params = {}
    elif not isinstance(params, dict):          # 位置で渡す形(配列)は、この道具には無い
        return err(-32602, "`params` must be an object")
    if method == "initialize":
        start_warm_up(state)
        want = params.get("protocolVersion")
        return ok({"protocolVersion": want if want in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0],
                   "capabilities": {"tools": {"listChanged": False}},
                   "serverInfo": {"name": "context-foundation", "version": __version__},
                   "instructions": INSTRUCTIONS if has_index(state) else INSTRUCTIONS_NO_INDEX})
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


def _handle_safely(state: _State, msg: dict) -> dict | None:
    """handle で思わぬ例外が出ても、サーバーを落とさずに -32603 で答える(traceback は標準エラーへ)。"""
    try:
        return handle(state, msg)
    except Exception:
        traceback.print_exc(file=sys.stderr)
        mid = msg.get("id")
        if mid is None:
            return None
        return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32603, "message": "Internal error"}}


# 返事を JSON にできなかったときに書く、決まった 1 行(標準出力には JSON-RPC の行だけを書く)
ENCODE_FAILED = b'{"jsonrpc": "2.0", "id": null, "error": {"code": -32603, "message": "Internal error"}}\n'


def _reject_constant(name: str):
    raise ValueError(f"not JSON: {name}")


def _encode(reply) -> bytes:
    """返事を 1 行のバイト列にする。対になっていないサロゲート(JSON の \\ud83d など)が混ざっていても落ちないように、
    そのときは ASCII に逃がした形で書く(JSON としては同じ文字列)。NaN・Infinity は書かない(JSON ではない)。"""
    try:
        return json.dumps(reply, ensure_ascii=False, allow_nan=False).encode("utf-8") + b"\n"
    except UnicodeEncodeError:
        return json.dumps(reply, allow_nan=False).encode("ascii") + b"\n"


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
            msg = json.loads(line.decode("utf-8"), parse_constant=_reject_constant)   # NaN・Infinity は受けない
        except (ValueError, UnicodeDecodeError, RecursionError):
            reply = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}}
        else:
            if isinstance(msg, list):       # まとめて送る形は、順に答える
                replies = [r for r in (_handle_safely(state, m) for m in msg if isinstance(m, dict)) if r is not None]
                reply = replies or None
            elif isinstance(msg, dict):
                reply = _handle_safely(state, msg)
            else:
                reply = {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Invalid Request"}}
        if reply is not None:
            try:
                data = _encode(reply)
            except Exception:
                traceback.print_exc(file=sys.stderr)
                data = ENCODE_FAILED
            stdout.write(data)
            stdout.flush()
