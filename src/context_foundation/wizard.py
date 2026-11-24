""".bat から呼ぶ、対話の形(日本語)。ダブルクリックで使う人のためのもの。

  wizard setup   初回の用意の続き(埋め込みモデルが入っているかを知らせ、手元に置いたモデルのフォルダを尋ねる。取得はしない)
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

from . import command_hint
from .config import CONFIG_DIR_NAME, CONFIG_FILE_NAME, MODES, ConfigError, config_template, load_config


def _state_file() -> Path:
    if os.environ.get("CF_HOME") or (os.name == "nt" and os.environ.get("LOCALAPPDATA")):
        from .embed import data_home
        return data_home() / "state.json"
    return Path.home() / ".cache" / "context-foundation" / "state.json"


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
        if not default:      # 入力が閉じられた(対話でない実行)。尋ね続けないで終わる
            raise SystemExit("\n入力が閉じられたので、終わります") from None
        s = ""
    return s or default


def yes(prompt: str, default: bool = True, when_closed: bool | None = None) -> bool:
    """はい・いいえを尋ねる。入力が閉じられていたら(対話でない実行)、when_closed(無ければ default)を返す。"""
    try:
        s = input(f"{prompt} ({'Y/n' if default else 'y/N'}) ").strip().lower()
    except EOFError:
        return default if when_closed is None else when_closed
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
    from .config import source_id_for
    return source_id_for(path, used)


def _model_ready(cfg) -> bool:
    from .embed import ModelUnavailable, model_files, resolve_model
    try:
        model_files(resolve_model(cfg.embedding))
        return True
    except ModelUnavailable:
        return False


def _had_embeddings(cfg) -> bool:
    """前の索引(置き換えられずに残ったものを含む)に埋め込みがあるか。"""
    from .indexer import previous_had_embeddings
    return previous_had_embeddings(cfg.index_path)


def _offer_model(embedding_cfg) -> bool:
    """埋め込みモデルが使える状態なら True。無ければ「インストールされていません」と、入手先と取得のコマンドを出す。

    モデルは PyPI の外の OSS なので、ここでは取得しない(尋ねもしない)。この PC で使ってよいかは、利用者が
    入手先とライセンスを確かめて決め、自分で `model download` を実行する。"""
    from .embed import MODEL_PRESETS, USE_HINT, ModelUnavailable, model_files, model_folder, resolve_model
    try:
        spec = resolve_model(embedding_cfg)
    except ModelUnavailable as e:      # 設定の誤り(既定の候補に無いモデルなど)
        print()
        print(f"埋め込みモデルを使えません: {e}")
        return False
    try:
        model_files(spec)
        return True
    except ModelUnavailable:
        pass
    preset = MODEL_PRESETS.get(spec.name)
    size = sum(preset.files[n][0] for n in (spec.onnx_file, spec.tokenizer_file) if n in preset.files) if preset else 0
    print()
    print("埋め込みモデルがインストールされていません。意味検索(日本語の質問で英語のコードを探す、など)に要ります。")
    print(f"  モデル: {spec.name}(ライセンス {spec.license or '不明'}" + (f"、約 {size / 1e6:.0f} MB" if size else "") + ")")
    if spec.model_dir_source == "user":
        print(f"  置き場(`model use` で指定したフォルダ): {model_folder(spec)}")
        print("  このフォルダに、モデルのファイルがもうありません。フォルダを確かめるか、"
              f"`{command_hint(USE_HINT)}` で指定し直します。")
    elif spec.model_dir:
        print(f"  置き場([embedding] model_dir): {model_folder(spec)}")
        print("  このフォルダにモデルのファイルを置くと使えます。")
    else:
        print("  入手先: https://huggingface.co/" + spec.repo)
        print("  このモデルは PyPI の外の OSS なので、自動では入れません。入手先とライセンスを確かめ、この PC で")
        print("  使ってよければ、次のコマンドで取得します(取得のときに送るのは、ファイルの名前だけです):")
        print(f"    {command_hint('model download')}")
        print("  すでにこの PC に置いていれば、取得せずに、そのフォルダを指定して使えます:")
        print(f"    {command_hint(USE_HINT)}")
    print("  入れるまでは、keyword(BM25)だけの検索になります。")
    return False


def _ask_model_folder(embedding_cfg) -> bool:
    """手元に置いた埋め込みモデルのフォルダを尋ねて、確かめてから覚える(`model use` と同じ)。覚えたら True。
    空のまま Enter・入力が閉じられたときは、何もしない。"""
    from dataclasses import replace

    from .embed import ModelUnavailable, check_model, locate_model_folder, resolve_model, save_user_model_dir
    try:
        spec = resolve_model(replace(embedding_cfg, model_dir=""))
    except ModelUnavailable:
        return False
    print()
    print(f"{spec.name} をすでにこの PC に置いていれば、そのフォルダを入れてください({spec.onnx_file} と "
          f"{spec.tokenizer_file} が入ったフォルダ。Hugging Face のキャッシュのフォルダや、使う .onnx のファイルの"
          "パスでも構いません)。")
    while True:
        try:
            given = input("モデルのフォルダ(空のまま Enter で飛ばす): ").strip().strip('"')
        except EOFError:
            return False
        if not given:
            return False
        try:
            located = locate_model_folder(given, spec)
            onnx = f"(ONNX: {located.onnx_file})" if located.onnx_file else ""
            print(f"  モデルを読み込んで確かめます: {located.folder}{onnx}")
            dim, notes = check_model(replace(spec, model_dir=str(located.folder), model_dir_source="user",
                                             onnx_file=located.onnx_file or spec.onnx_file))
        except ModelUnavailable as e:
            print(f"  {e}")
            continue
        for n in notes:
            print(f"  注意: {n}")
        if notes and not yes("  このフォルダを使いますか", default=False, when_closed=False):
            continue
        try:
            written = save_user_model_dir(spec.name, str(located.folder), located.onnx_file)
        except ModelUnavailable as e:
            print(f"  {e}")
            return False
        print(f"  覚えました(埋め込みの次元 {dim}。設定: {written})。この PC のすべての作業フォルダで使います。")
        return True


def wizard_setup() -> int:
    from .config import EmbeddingConfig
    print("== context-foundation: 初回の用意")
    if not _offer_model(EmbeddingConfig()) and _ask_model_folder(EmbeddingConfig()):
        print("埋め込みモデルを使えるようになりました。")
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
        if _had_embeddings(cfg):
            print("  前の索引には埋め込みがあります。keyword だけの索引に置き換えると、次に埋め込みつきで作るときに、"
                  "すべての passage を埋め込み直すことになります(時間がかかります)")
            # 入力が閉じられているときは、置き換えない(尋ねずに埋め込みを捨てない)
            if not yes("keyword だけの索引に置き換えますか", default=False, when_closed=False):
                print("やめました。前の索引はそのまま使えます。モデルを入れてから、もう一度実行してください")
                return 2
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
    except Exception as e:
        # 索引づくりの、利用者に見せる失敗(モデルを読めない・ファイルが無い・置き換えられない)
        from .embed import ModelUnavailable
        from .indexer import BuildError, ReplaceError
        if not isinstance(e, (ModelUnavailable, BuildError)):
            raise
        print(e)
        return 1 if isinstance(e, ReplaceError) else 2
