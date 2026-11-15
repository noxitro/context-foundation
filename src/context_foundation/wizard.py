""".bat から呼ぶ、対話の形(日本語)。ダブルクリックで使う人のためのもの。

  wizard setup   初回の用意の続き(埋め込みモデルを取得するかを尋ねる)
  wizard index   作業フォルダと検索したいフォルダを尋ねて、設定を作り、索引を作る
  wizard search  質問を尋ねて、結果を人が読む形で出す(繰り返す)
  wizard eval    cases.jsonl を尋ねて、検索だけの手法の評価を回す
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path

from .config import CONFIG_DIR_NAME, CONFIG_FILE_NAME, MODES, ConfigError, config_template, load_config


def _state_file() -> Path:
    base = os.environ.get("LOCALAPPDATA") if os.name == "nt" else None
    root = Path(base) / "context-foundation" if base else Path.home() / ".cache" / "context-foundation"
    return root / "state.json"


def _load_state() -> dict:
    try:
        return json.loads(_state_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save_state(**kv) -> None:
    st = _load_state()
    st.update(kv)
    try:
        _state_file().parent.mkdir(parents=True, exist_ok=True)
        _state_file().write_text(json.dumps(st, ensure_ascii=False, indent=1), encoding="utf-8")
    except OSError:
        pass


def ask(prompt: str, default: str = "") -> str:
    hint = f"(空のまま Enter で {default})" if default else ""
    try:
        s = input(f"{prompt}{hint}: ").strip().strip('"')
    except EOFError:
        s = ""
    return s or default


def yes(prompt: str, default: bool = True) -> bool:
    try:
        s = input(f"{prompt} ({'Y/n' if default else 'y/N'}) ").strip().lower()
    except EOFError:
        s = ""
    return default if not s else s.startswith("y")


def ask_folder(prompt: str, default: str = "", allow_empty: bool = False) -> Path | None:
    while True:
        s = ask(prompt, default)
        if not s:
            if allow_empty:
                return None
            print("  フォルダを入れてください")
            continue
        p = Path(s).expanduser()
        if p.is_dir():
            return p.resolve()
        print(f"  フォルダがありません: {p}")
        default = ""


def _source_id(path: Path, used: set[str]) -> str:
    base = re.sub(r"[^A-Za-z0-9._-]+", "-", path.name).strip("-._") or "source"
    if not base[0].isalnum():
        base = "s" + base
    sid, n = base[:40], 2
    while sid in used:
        sid = f"{base[:36]}-{n}"
        n += 1
    return sid


def _model_ready(cfg) -> bool:
    from .embed import ModelUnavailable, model_files, resolve_model
    try:
        model_files(resolve_model(cfg.embedding))
        return True
    except ModelUnavailable:
        return False


def _offer_model(embedding_cfg) -> bool:
    """埋め込みモデルが無ければ、取得するかを尋ねる。使える状態なら True。"""
    from .embed import MODEL_PRESETS, ModelUnavailable, download_model, model_files, resolve_model
    try:
        spec = resolve_model(embedding_cfg)
        model_files(spec)
        return True
    except ModelUnavailable:
        pass
    preset = MODEL_PRESETS.get(spec.name)
    size = sum(preset.files[n][0] for n in (spec.onnx_file, spec.tokenizer_file) if n in preset.files) if preset else 0
    print()
    print("意味検索(日本語の質問で英語のコードを探す、など)には、埋め込みモデルが要ります。")
    print(f"  モデル: {spec.name}(ライセンス {spec.license or '不明'}、約 {size / 1e6:.0f} MB)")
    print("  入手先: https://huggingface.co/" + spec.repo)
    print("  取得のときに送るのは、ファイルの名前だけです(ソースや質問は送りません)。計算は、この PC の中で行います。")
    if not yes("今、取得しますか"):
        print("  取得しませんでした。keyword(BM25)だけの検索になります。後で setup.bat から取得できます")
        return False
    try:
        download_model(spec, log=lambda s: print("  " + s))
    except Exception as e:   # ネットワークの失敗など
        print(f"  取得に失敗しました({type(e).__name__}: {e})。keyword だけで続けます")
        return False
    return True


def wizard_setup() -> int:
    from .config import EmbeddingConfig
    print("== context-foundation: 初回の用意")
    _offer_model(EmbeddingConfig())
    print()
    print("用意ができました。次は index.bat で索引を作ります。")
    return 0


def _pick_workspace(purpose: str) -> Path:
    last = _load_state().get("workspace", "")
    if last and not Path(last).is_dir():
        last = ""
    ws = ask_folder(f"{purpose}作業フォルダ(Copilot を起動するフォルダ。ここに .cf フォルダを置きます)", last)
    _save_state(workspace=str(ws))
    return ws


def wizard_index() -> int:
    from .indexer import build_index
    from .render import status_human
    from .search import Searcher

    print("== context-foundation: 索引を作る")
    ws = _pick_workspace("")
    cf_dir = ws / CONFIG_DIR_NAME
    cfg_path = cf_dir / CONFIG_FILE_NAME
    if cfg_path.is_file():
        cfg = load_config(cfg_path)
        print(f"設定があります: {cfg_path}")
        for s in cfg.sources:
            print(f"  source {s.id}: {s.root}")
        if not yes("この設定で索引を作り直しますか"):
            print("やめました。設定を変えるときは、上のファイルを編集してから、もう一度実行してください")
            return 0
    else:
        print("検索したいフォルダを、1 つずつ入れてください(エンジンのソース、参考にするアプリ、設計文書 など)。")
        sources: list[tuple[str, str]] = []
        used: set[str] = set()
        first = True
        while True:
            p = ask_folder("検索したいフォルダ" + ("" if first else "(空のまま Enter で終わり)"),
                           str(ws) if first else "", allow_empty=not first)
            if p is None:
                break
            sid = ask("  このフォルダの呼び名(英数字と . _ -。検索結果に出ます)", _source_id(p, used))
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", sid) or sid in used:
                sid = _source_id(p, used)
                print(f"  呼び名は {sid} にしました")
            used.add(sid)
            try:
                root = os.path.relpath(p, ws)
                root = "." if root == "." else (str(p) if root.startswith("..") else root)
            except ValueError:      # ドライブが違う
                root = str(p)
            sources.append((sid, root))
            first = False
        cf_dir.mkdir(exist_ok=True)
        (cf_dir / ".gitignore").write_text("*\n", encoding="utf-8")   # 索引と設定を git に入れない
        cfg_path.write_text(config_template(sources), encoding="utf-8")
        print(f"設定を書きました: {cfg_path}")
        print("  (除外するフォルダを足すときなどは、このファイルを編集します。Perforce を使うときは、"
              ".cf を .p4ignore に足してください)")
        cfg = load_config(cfg_path)

    use_embedding = None
    if cfg.embedding.enabled and not _offer_model(cfg.embedding):
        use_embedding = False
    print()
    print("索引を作ります。大きなコードでは時間がかかります(埋め込みは 1 秒に 50 件ほど)。")
    build_index(cfg, use_embedding=use_embedding)
    print()
    sys.stdout.write(status_human(Searcher.from_config(cfg).status()))
    print()
    print("次は search.bat で検索を試すか、register-copilot.bat で Copilot に登録します。")
    return 0


def wizard_search() -> int:
    from .render import to_human
    from .search import Searcher
    from .store import IndexError_

    print("== context-foundation: 検索を試す")
    ws = _pick_workspace("")
    cfg_path = ws / CONFIG_DIR_NAME / CONFIG_FILE_NAME
    if not cfg_path.is_file():
        print(f"設定がありません: {cfg_path}\n先に index.bat で索引を作ってください")
        return 3
    cfg = load_config(cfg_path)
    searcher = Searcher.from_config(cfg)
    try:
        modes = searcher.status()["modes"]
    except IndexError_ as e:
        print(e)
        return 3
    mode = cfg.search.mode if cfg.search.mode in modes else "keyword"
    print(f"質問を入れてください(空のまま Enter で終わり)。方法は {mode}。"
          f"変えるときは「:keyword」「:semantic」「:hybrid」と入れます。")
    while True:
        try:
            q = input("\n質問> ").strip()
        except EOFError:
            break
        if not q:
            break
        if q.startswith(":") and q[1:] in MODES:
            mode = q[1:]
            print(f"方法を {mode} にしました")
            continue
        try:
            sys.stdout.write(to_human(searcher.search(q, mode=mode)))
        except (IndexError_, ValueError) as e:
            print(e)
    return 0


def wizard_eval() -> int:
    from argparse import Namespace

    from .evalkit.runner import main as eval_main

    print("== context-foundation: 評価を回す(検索だけの手法: keyword・semantic・hybrid)")
    print("エージェントを使う手法は、ここでは回しません(docs/comparison-runbook.md)。")
    ws = _pick_workspace("")
    last = _load_state().get("cases", "")
    while True:
        cases = ask("cases.jsonl の場所", last if last and Path(last).is_file() else "")
        if cases and Path(cases).is_file():
            break
        print("  ファイルがありません")
        last = ""
    _save_state(cases=str(Path(cases).resolve()))
    run_id = time.strftime("run-%Y%m%d-%H%M%S")
    out = ask("結果を書くフォルダ", str(ws / CONFIG_DIR_NAME / "eval" / run_id))
    revision = ask("コーパスの版(コミットやチェンジリストの番号。分からなければ空)", "")
    return eval_main(Namespace(config=str(ws), cases=cases, out=out, methods="keyword,semantic,hybrid", top_k=10,
                               imports=[], import_runs=[], baseline="agent_grep", run_id=run_id,
                               corpus_revision=revision, candidate="hybrid,agent_cf", split="eval"))


def main(args) -> int:
    try:
        return {"setup": wizard_setup, "index": wizard_index, "search": wizard_search,
                "eval": wizard_eval}[args.action]()
    except ConfigError as e:
        print(e)
        return 2
    except FileNotFoundError as e:
        print(e)
        return 2
