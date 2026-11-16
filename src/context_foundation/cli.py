"""コマンドライン: index / search / status / eval / serve / validate / model / copilot / wizard。

  ctxf index                    設定(.cf/config.toml)に従って索引を作り直す
  ctxf search "<質問>"          検索する(端末なら人が読む形、そうでなければエージェント向けの形)
  ctxf status                   索引の版・件数・作成日時
  ctxf eval --cases cases.jsonl 評価(keyword・semantic・hybrid を回して要約を書く。別に取った結果も並べる)
  ctxf serve                    読み取り専用の MCP サーバー(stdio)
  ctxf validate --cases ...     cases.jsonl / results.jsonl の検査
  ctxf model download           埋め込みモデルを取得する
  ctxf copilot register         Copilot CLI・アプリへの登録(unregister で解除)
  ctxf wizard index|search|eval .bat から呼ぶ、対話の形
"""
from __future__ import annotations

import argparse
import json
import os
import sys

from . import __version__
from .config import MODES, ConfigError, find_config, load_config

EXIT_USAGE, EXIT_NO_INDEX = 2, 3


def _utf8_stdio() -> None:
    """標準出力を UTF-8 にする(shell の既定のコードページで、日本語のパスや本文が化けないように)。"""
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def _for_agent(args) -> bool:
    fmt = getattr(args, "format", None)
    return fmt == "agent" or (fmt is None and not sys.stdout.isatty())


def _load(args):
    """設定を読む。見つからなければ、読み手(人かエージェント)に合わせた文面で終わる。"""
    path = find_config(getattr(args, "config", None))
    if path is None:
        if _for_agent(args):
            sys.stdout.write(
                "No context-foundation index: no .cf folder found from " + os.getcwd() + " upward.\n"
                "Search with grep as usual. Building an index takes minutes; ask the user first "
                "(they run index.bat in the context-foundation folder).\n")
        else:
            sys.stderr.write(
                "設定(.cf/config.toml)が見つかりません。作業フォルダで index.bat を実行するか、"
                "--config で場所を指定してください\n")
        raise SystemExit(EXIT_NO_INDEX)
    return load_config(path)


def cmd_index(args) -> int:
    from .embed import ModelUnavailable, download_model, resolve_model
    from .indexer import build_index

    cfg = _load(args)
    use_embedding = False if args.no_embed else None
    if args.download and not args.no_embed and cfg.embedding.enabled:
        download_model(resolve_model(cfg.embedding), log=lambda s: print(s, file=sys.stderr))
    try:
        result = build_index(cfg, use_embedding=use_embedding, require_embedding=args.require_embedding,
                             progress=not args.quiet, revision=args.revision, reuse=not args.full)
    except ModelUnavailable as e:
        sys.stderr.write(f"{e}\n")
        return EXIT_USAGE
    if args.json:
        sys.stdout.write(json.dumps(result, ensure_ascii=False, indent=1) + "\n")
    else:
        from .render import status_human
        from .search import Searcher
        sys.stdout.write(status_human(Searcher.from_config(cfg).status()))
    return 0


def cmd_init(args) -> int:
    """作業フォルダに .cf/config.toml を作る(対話なし。エージェントや .bat から呼ぶ)。"""
    from pathlib import Path

    from .config import CONFIG_DIR_NAME, CONFIG_FILE_NAME, config_template, source_id_for

    ws = Path(args.workspace or os.getcwd()).resolve()
    cfg_path = ws / CONFIG_DIR_NAME / CONFIG_FILE_NAME
    if cfg_path.is_file() and not args.force:
        sys.stderr.write(f"設定がすでにあります: {cfg_path}\n(作り直すなら --force。索引を作るなら `ctxf index`)\n")
        return EXIT_USAGE
    sources: list[tuple[str, str]] = []
    used: set[str] = set()
    for raw in args.folders:
        p = Path(raw)
        p = (ws / p if not p.is_absolute() else p).resolve()
        if not p.is_dir():
            sys.stderr.write(f"フォルダがありません: {p}\n")
            return EXIT_USAGE
        sid = source_id_for(p, used)
        used.add(sid)
        try:
            rel = os.path.relpath(p, ws)
            outside = rel == ".." or rel.startswith(".." + os.sep)
            root = str(p) if outside else rel
        except ValueError:                  # ドライブが違う
            root = str(p)
        sources.append((sid, root))
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    ignore = cfg_path.parent / ".gitignore"
    if not ignore.exists():
        ignore.write_text("*\n", encoding="utf-8")       # 設定と索引を git に入れない
    if cfg_path.is_file():                  # --force: 手で書いた設定を失わないよう、前のものを残す
        backup = cfg_path.with_name(CONFIG_FILE_NAME + ".bak")
        backup.write_bytes(cfg_path.read_bytes())
        print(f"前の設定を残しました: {backup}")
    cfg_path.write_text(config_template(sources), encoding="utf-8")
    load_config(cfg_path)                   # 書いたものが読めることを確かめる
    print(f"設定を書きました: {cfg_path}")
    for sid, root in sources:
        print(f"  source {sid}: {root}")
    print("次は `ctxf index` で索引を作ります(大きなコードでは時間がかかります)。")
    return 0


def cmd_search(args) -> int:
    from .render import to_agent, to_human, to_json
    from .search import Searcher
    from .store import IndexError_

    cfg = _load(args)
    query = " ".join(args.query).strip()
    if not query:
        sys.stderr.write("質問を渡してください: ctxf search \"<質問>\"\n")
        return EXIT_USAGE
    fmt = args.format or ("human" if sys.stdout.isatty() else "agent")
    try:
        resp = Searcher.from_config(cfg).search(query, mode=args.mode, top_k=args.top_k, source_ids=args.source)
    except IndexError_ as e:
        if fmt == "agent":
            sys.stdout.write("No context-foundation index yet (" + str(e).splitlines()[0] + ").\n"
                             "Search with grep as usual. Building an index takes minutes; ask the user first.\n")
        else:
            sys.stderr.write(f"{e}\n")
        return EXIT_NO_INDEX
    except ValueError as e:
        sys.stderr.write(f"{e}\n")
        return EXIT_USAGE
    if fmt == "json":
        sys.stdout.write(to_json(resp))
    elif fmt == "human":
        sys.stdout.write(to_human(resp))
    else:
        sys.stdout.write(to_agent(resp, args.max_bytes or cfg.search.max_output_bytes, args.line_numbers))
    return 0


def cmd_status(args) -> int:
    from .render import status_agent, status_human
    from .search import Searcher
    from .store import IndexError_

    cfg = _load(args)
    fmt = args.format or ("human" if sys.stdout.isatty() else "agent")
    try:
        st = Searcher.from_config(cfg).status()
    except IndexError_ as e:
        sys.stderr.write(f"{e}\n")
        return EXIT_NO_INDEX
    if fmt == "json":
        sys.stdout.write(json.dumps(st, ensure_ascii=False, indent=1) + "\n")
    else:
        sys.stdout.write(status_human(st) if fmt == "human" else status_agent(st))
    return 0


def cmd_model(args) -> int:
    from .config import EmbeddingConfig
    from .embed import MODEL_PRESETS, ModelUnavailable, download_model, model_files, model_folder, resolve_model

    path = find_config(getattr(args, "config", None))
    ecfg = load_config(path).embedding if path else EmbeddingConfig()
    if args.model:
        ecfg.model = args.model
    try:
        spec = resolve_model(ecfg)
    except ModelUnavailable as e:
        sys.stderr.write(f"{e}\n")
        return EXIT_USAGE
    if args.action == "download":
        try:
            folder = download_model(spec, log=print)
        except ModelUnavailable as e:
            sys.stderr.write(f"{e}\n")
            return EXIT_USAGE
        print(f"モデルを置きました: {folder}")
        return 0
    preset = MODEL_PRESETS.get(spec.name)
    print(f"モデル: {spec.name}(版 {spec.revision[:12]}、ライセンス {spec.license or '不明'})")
    if preset:
        size = sum(preset.files[n][0] for n in (spec.onnx_file, spec.tokenizer_file) if n in preset.files)
        print(f"  大きさ: 約 {size / 1e6:.0f} MB  次元: {preset.dim}  {preset.note}")
    print(f"  置き場: {model_folder(spec)}")
    try:
        model_files(spec)
        print("  状態: 手元にあります")
    except ModelUnavailable:
        print("  状態: まだありません(`ctxf model download` で取得)")
    return 0


def cmd_eval(args) -> int:
    from .evalkit.runner import main as eval_main
    return eval_main(args)


def cmd_validate(args) -> int:
    from .evalkit.validate import main as validate_main
    return validate_main(args)


def cmd_serve(args) -> int:
    from .mcp_server import serve
    return serve(config=args.config)


def cmd_copilot(args) -> int:
    from .copilot_reg import main as copilot_main
    return copilot_main(args)


def cmd_wizard(args) -> int:
    from .wizard import main as wizard_main
    return wizard_main(args)


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="ctxf", description="コードと設計文書から、自然文の質問で根拠の箇所を探す検索の PoC",
                                 allow_abbrev=False)
    ap.add_argument("--version", action="version", version=f"context-foundation {__version__}")
    sub = ap.add_subparsers(dest="cmd", required=True, metavar="<コマンド>")

    def common(p):
        p.add_argument("--config", help="設定ファイル、.cf フォルダ、またはその親フォルダ(省くと作業フォルダから親へ探す)")

    p = sub.add_parser("init", help="作業フォルダに設定(.cf/config.toml)を作る。検索したいフォルダを並べる",
                       allow_abbrev=False)
    p.add_argument("folders", nargs="+", help="検索したいフォルダ(エンジンのソース、参考にするアプリ、設計文書 など)")
    p.add_argument("--workspace", help="作業フォルダ(省くと今のフォルダ。Copilot を起動するフォルダ)")
    p.add_argument("--force", action="store_true", help="設定がすでにあっても書き直す")
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("index", help="索引を作る(前の索引の埋め込みは、変わっていない passage の分を使い回す)",
                       allow_abbrev=False)
    common(p)
    p.add_argument("--full", action="store_true", help="前の索引の埋め込みを使い回さず、すべて埋め込み直す")
    p.add_argument("--no-embed", action="store_true", help="埋め込みを作らない(keyword だけの索引)")
    p.add_argument("--download", action="store_true", help="埋め込みモデルが無ければ、先に取得する")
    p.add_argument("--require-embedding", action="store_true", help="モデルが無いとき、keyword だけで作らずに止まる")
    p.add_argument("--revision", default="", help="コーパスの版(コミットやチェンジリストの番号)。索引に記録する")
    p.add_argument("--quiet", action="store_true", help="途中の進み具合を出さない")
    p.add_argument("--json", action="store_true", help="結果(統計と所要時間)を JSON で出す")
    p.set_defaults(func=cmd_index)

    p = sub.add_parser("search", help="検索する", allow_abbrev=False)
    common(p)
    p.add_argument("query", nargs="+", help="質問(自然文でよい。日本語も可)")
    p.add_argument("--mode", choices=MODES, help="検索の方法(省くと設定の値。既定は hybrid)")
    p.add_argument("--top-k", type=int, help="返す passage の数(既定 8)")
    p.add_argument("--source", action="append", default=[], metavar="ID", help="この source だけを探す(繰り返せる)")
    p.add_argument("--format", choices=("agent", "human", "json"),
                   help="出力の形(省くと、端末なら human、そうでなければ agent)")
    p.add_argument("--max-bytes", type=int, help="agent の形の出力の上限(既定 12000、最大 18000)")
    p.add_argument("--line-numbers", action="store_true", help="agent の形で、本文に行番号を付ける")
    p.set_defaults(func=cmd_search)

    p = sub.add_parser("status", help="索引の版・件数・作成日時", allow_abbrev=False)
    common(p)
    p.add_argument("--format", choices=("agent", "human", "json"))
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("eval", help="評価を回して要約を書く(検索だけの手法)。別に取った結果も並べる",
                       allow_abbrev=False)
    common(p)
    p.add_argument("--cases", required=True, help="cases.jsonl")
    p.add_argument("--out", required=True, help="結果を書くフォルダ(results.jsonl・runs.jsonl・summary.md・summary.json)")
    p.add_argument("--methods", default="keyword,semantic,hybrid", help="回す手法(, 区切り。none で回さない)")
    p.add_argument("--top-k", type=int, default=10, help="記録する passage の数(既定 10)")
    p.add_argument("--import", dest="imports", action="append", default=[], metavar="results.jsonl",
                   help="別に取った results.jsonl を取り込んで、同じ要約に並べる(繰り返せる)")
    p.add_argument("--import-runs", action="append", default=[], metavar="runs.jsonl",
                   help="取り込む結果の、実行ごとの情報(繰り返せる)")
    p.add_argument("--baseline", default="agent_grep", help="相対の条件で比べる、既存の手順の手法の名前")
    p.add_argument("--candidate", default="hybrid,agent_cf",
                   help="しきい値で判定する手法(, 区切り。実行の前に決めておく。既定は hybrid,agent_cf)")
    p.add_argument("--split", choices=("eval", "tune", "all"), default="eval",
                   help="使う質問(既定は eval。調整のときは tune)")
    p.add_argument("--run-id", help="この実行の名前(省くと日時から作る)")
    p.add_argument("--corpus-revision", default="", help="コーパスの版(コミットやチェンジリストの番号など)")
    p.set_defaults(func=cmd_eval)

    p = sub.add_parser("validate", help="cases.jsonl / results.jsonl / runs.jsonl を検査する", allow_abbrev=False)
    common(p)
    p.add_argument("--cases", help="cases.jsonl")
    p.add_argument("--results", action="append", default=[], help="results.jsonl(繰り返せる)")
    p.add_argument("--runs", action="append", default=[], help="runs.jsonl(繰り返せる)")
    p.add_argument("--check-corpus", action="store_true",
                   help="expected_evidence の source_id・パス・行が、設定のコーパスにあるかも確かめる")
    p.set_defaults(func=cmd_validate)

    p = sub.add_parser("serve", help="読み取り専用の MCP サーバー(stdio)", allow_abbrev=False)
    common(p)
    p.set_defaults(func=cmd_serve)

    p = sub.add_parser("model", help="埋め込みモデルの取得と確認", allow_abbrev=False)
    common(p)
    p.add_argument("action", choices=("download", "info"))
    p.add_argument("--model", help="モデルの名前(省くと設定か既定のもの)")
    p.set_defaults(func=cmd_model)

    p = sub.add_parser("copilot", help="Copilot CLI・アプリへの登録と解除", allow_abbrev=False)
    p.add_argument("action", choices=("register", "unregister", "status"))
    p.add_argument("--mcp", action="store_true", help="何もしない(0.3 から、MCP サーバーはプラグインに入っている。前の指定を受け取るだけ)")
    p.add_argument("--yes", action="store_true", help="確認を尋ねない")
    p.set_defaults(func=cmd_copilot)

    p = sub.add_parser("wizard", help=".bat から呼ぶ、対話の形", allow_abbrev=False)
    p.add_argument("action", choices=("setup", "index", "search", "eval"))
    p.set_defaults(func=cmd_wizard)
    return ap


def main(argv: list[str] | None = None) -> int:
    _utf8_stdio()
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args) or 0)
    except ConfigError as e:
        sys.stderr.write(f"{e}\n")
        return EXIT_USAGE
    except KeyboardInterrupt:
        sys.stderr.write("中断しました\n")
        return 130


if __name__ == "__main__":
    sys.exit(main())
