"""手元に置いた埋め込みモデルのフォルダを使う(`model use` / `model forget`、setup.bat の尋ね方、setup の skill)。

モデルの実物は使わない。読み込み(OnnxEmbedder)は、偽の埋め込みに差し替えるか、壊れたファイルで誤りの道筋だけを
通す。偽のモデルのファイルは、大きさだけを控えの値に合わせる(大きさの比べは本物を通す)。
"""
from __future__ import annotations

import builtins
import json
import re
from argparse import Namespace
from pathlib import Path

import numpy as np
import pytest

from context_foundation import embed
from context_foundation.cli import main as cli_main
from context_foundation.config import EmbeddingConfig
from context_foundation.embed import (DEFAULT_MODEL, ModelUnavailable, locate_model_folder, model_files, resolve_model,
                                      user_model_dirs)
from context_foundation.wizard import main as wizard_main

REPO = Path(__file__).resolve().parents[1]
E5 = embed.MODEL_PRESETS[DEFAULT_MODEL]


GRANITE = "ibm-granite/granite-embedding-97m-multilingual-r2"


@pytest.fixture
def home(monkeypatch, tmp_path, outside_any_workspace):
    """利用者ごとの置き場・設定・モデルの取得先を、一時フォルダに向ける。今のフォルダは、設定の無いフォルダ。
    設定のファイル(settings.json)の場所を返す。"""
    monkeypatch.setenv("CF_HOME", str(tmp_path / "cf-home"))
    monkeypatch.delenv("CF_SETTINGS")                     # 置き場の settings.json を使う(conftest は別の場所に向ける)
    monkeypatch.setenv("CF_MODEL_CACHE", str(tmp_path / "no-models"))
    monkeypatch.delenv("CF_ENTRY", raising=False)
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.chdir(work)
    return tmp_path / "cf-home" / "settings.json"


@pytest.fixture
def loads(monkeypatch):
    """check_model が読み込むモデルを、偽の埋め込みに差し替える。読み込んだ (フォルダ, ONNX のファイル) を並べて
    返す。次元は loads.dim(既定 384)。大きさの比べ(size_notes)は本物のまま。"""
    class Loaded(list):
        dim = 384
    loaded = Loaded()

    class FakeOnnx:
        def __init__(self, spec, **kw):
            onnx, _ = model_files(spec)
            loaded.append((Path(spec.model_dir), spec.onnx_file))

        def embed_query(self, text):
            return np.ones(loaded.dim, dtype=np.float32)

    monkeypatch.setattr(embed, "OnnxEmbedder", FakeOnnx)
    return loaded


def put_model(folder: Path, onnx_name: str = "onnx/model.onnx", model: str = DEFAULT_MODEL) -> Path:
    """偽のモデルのファイル。大きさは、控えてある値に合わせる(中身は読まない。sparse なファイルにする)。"""
    files = embed.MODEL_PRESETS[model].files
    for name, rel in ((onnx_name, embed.MODEL_PRESETS[model].onnx_file), ("tokenizer.json", "tokenizer.json")):
        (folder / name).parent.mkdir(parents=True, exist_ok=True)
        with open(folder / name, "wb") as f:
            f.truncate(files.get(name, files.get(rel, (4, "")))[0])
    return folder


def spec():
    return resolve_model(EmbeddingConfig())


def where(found):
    return (found.folder, found.onnx_file)


def test_locate_accepts_the_folder_a_file_in_it_a_hugging_face_cache_and_a_folder_above(tmp_path):
    plain = put_model(tmp_path / "e5").resolve()
    assert where(locate_model_folder(str(plain), spec())) == (plain, "")
    assert where(locate_model_folder(f'"{plain}"', spec())) == (plain, "")      # 貼り付けた引用符
    assert where(locate_model_folder(str(plain / "onnx" / "model.onnx"), spec())) == (plain, "")
    assert where(locate_model_folder(str(plain / "tokenizer.json"), spec())) == (plain, "")
    flat = put_model(tmp_path / "flat", "model.onnx").resolve()                    # フォルダの直下に置いたもの
    assert where(locate_model_folder(str(flat), spec())) == (flat, "")
    # Hugging Face のキャッシュ: 同じコミットの snapshot を、新しい別の snapshot より先に選ぶ
    cache = tmp_path / "hub" / "models--intfloat--multilingual-e5-small"
    same = put_model(cache / "snapshots" / E5.revision).resolve()
    put_model(cache / "snapshots" / "0123456789abcdef")
    assert where(locate_model_folder(str(cache), spec())) == (same, "")
    # 上のフォルダ: 下にモデルが 1 つだけなら、それを選ぶ。2 つ以上なら、選んでもらう
    assert where(locate_model_folder(str(tmp_path / "hub"), spec())) == (same, "")
    with pytest.raises(ModelUnavailable, match="つあります"):
        locate_model_folder(str(tmp_path), spec())


def test_locate_takes_another_onnx_by_its_path_or_when_it_is_the_only_one(tmp_path):
    q = tmp_path / "quantized"
    put_model(q, "onnx/model_qint8_avx512_vnni.onnx")
    q = q.resolve()
    # 既定の名前の ONNX が無く、隣に 1 つだけ: それを使う。ファイルを名指ししても同じ
    assert where(locate_model_folder(str(q), spec())) == (q, "onnx/model_qint8_avx512_vnni.onnx")
    assert where(locate_model_folder(str(q / "onnx" / "model_qint8_avx512_vnni.onnx"), spec())) == \
        (q, "onnx/model_qint8_avx512_vnni.onnx")
    # 2 つあれば、どちらかを名指ししてもらう
    (q / "onnx" / "model_fp16.onnx").write_bytes(b"x")
    with pytest.raises(ModelUnavailable) as e:
        locate_model_folder(str(q), spec())
    assert "model_fp16.onnx" in str(e.value) and "パスを渡してください" in str(e.value)
    assert where(locate_model_folder(str(q / "onnx" / "model_fp16.onnx"), spec())) == (q, "onnx/model_fp16.onnx")


def test_locate_says_what_is_missing(tmp_path, monkeypatch):
    with pytest.raises(ModelUnavailable, match="そのパスはありません"):
        locate_model_folder(str(tmp_path / "nope"), spec())
    lone = tmp_path / "lone" / "onnx"
    lone.mkdir(parents=True)
    (lone / "model.onnx").write_bytes(b"x")                                      # tokenizer.json が無い
    with pytest.raises(ModelUnavailable, match="tokenizer.json がありません"):
        locate_model_folder(str(lone / "model.onnx"), spec())
    with pytest.raises(ModelUnavailable) as e:
        locate_model_folder(str(tmp_path), spec())
    assert "onnx/model.onnx" in str(e.value) and "lone/onnx/model.onnx" in str(e.value)
    assert "途中まで" not in str(e.value)
    monkeypatch.setattr(embed, "_find_onnx", lambda base, **kw: ([], True))       # 探すフォルダの上限に届いた
    with pytest.raises(ModelUnavailable, match="途中までしか探していません"):
        locate_model_folder(str(tmp_path), spec())


def test_model_use_remembers_the_folder_for_every_workspace_and_forget_drops_it(home, loads, tmp_path, capsys):
    folder = put_model(tmp_path / "models" / "e5").resolve()
    assert cli_main(["model", "use", str(folder / "onnx" / "model.onnx")]) == 0
    out = capsys.readouterr().out
    assert "フォルダを覚えました" in out and "埋め込みの次元 384" in out and "model forget" in out
    assert out.rstrip().endswith("result: saved") and "注意:" not in out
    assert loads == [(folder, "onnx/model.onnx")]                                # 読み込んで確かめた
    assert json.loads(home.read_text(encoding="utf-8")) == {
        "model": DEFAULT_MODEL, "model_dirs": {DEFAULT_MODEL: str(folder)}, "onnx_files": {}}
    # 設定の無い作業フォルダでも、そのフォルダのモデルを使う(取得しない)
    s = spec()
    assert (s.model_dir, s.model_dir_source) == (str(folder), "user")
    assert model_files(s)[1] == folder / "tokenizer.json"
    assert cli_main(["model", "info"]) == 0
    out = capsys.readouterr().out
    assert "`model use` で指定したフォルダ" in out and "手元にあります" in out
    # 覚えている間は、取得しない
    assert cli_main(["model", "download"]) == 2
    assert "model forget" in capsys.readouterr().err
    assert not (tmp_path / "no-models").exists()
    # 外すと、取得先のキャッシュに戻る。ファイルは消さない
    assert cli_main(["model", "forget"]) == 0
    out = capsys.readouterr().out
    assert "外しました" in out and "result: forgotten" in out
    assert user_model_dirs() == {} and spec().model_dir == "" and (folder / "onnx" / "model.onnx").is_file()
    assert cli_main(["model", "forget"]) == 0
    assert "result: nothing to forget" in capsys.readouterr().out


def test_model_use_with_another_model_makes_it_the_model_for_every_workspace(home, loads, tmp_path, capsys):
    e5 = put_model(tmp_path / "e5").resolve()
    granite = put_model(tmp_path / "granite", model=GRANITE).resolve()
    assert cli_main(["model", "use", str(e5)]) == 0
    loads.dim = 384
    assert cli_main(["model", "use", "--model", GRANITE, str(granite)]) == 0
    assert f"result: saved (model {GRANITE})" in capsys.readouterr().out
    s = spec()                                                                    # 設定に model の無い作業フォルダ
    assert (s.name, s.model_dir, s.model_dir_source) == (GRANITE, str(granite), "user")
    # 作業フォルダの設定の model が先
    assert resolve_model(EmbeddingConfig(model=DEFAULT_MODEL)).model_dir == str(e5)
    # --model を付けずに use すると、既定のモデルとして覚える(前に選んだモデルの名前を引き継がない)
    assert cli_main(["model", "use", str(e5)]) == 0
    assert spec().name == DEFAULT_MODEL and user_model_dirs() == {DEFAULT_MODEL: str(e5), GRANITE: str(granite)}
    # 選んでいるモデルを forget すると、既定のモデルに戻る
    assert cli_main(["model", "use", "--model", GRANITE, str(granite)]) == 0
    assert cli_main(["model", "forget"]) == 0
    assert spec().name == DEFAULT_MODEL and spec().model_dir == str(e5)


def test_model_use_remembers_another_onnx_file(home, loads, tmp_path, capsys):
    q = put_model(tmp_path / "q", "onnx/model_qint8_avx512_vnni.onnx").resolve()
    assert cli_main(["model", "use", str(q)]) == 0
    out = capsys.readouterr().out
    assert "ONNX: onnx/model_qint8_avx512_vnni.onnx" in out and "注意:" not in out    # 控えの大きさと合う
    s = spec()
    assert s.onnx_file == "onnx/model_qint8_avx512_vnni.onnx" and model_files(s)[0].name == "model_qint8_avx512_vnni.onnx"
    # 作業フォルダの設定の onnx_file が先。フォルダを設定したときは、覚えた ONNX を使わない
    assert resolve_model(EmbeddingConfig(onnx_file="onnx/x.onnx")).onnx_file == "onnx/x.onnx"
    assert resolve_model(EmbeddingConfig(model_dir="/m")).onnx_file == E5.onnx_file
    # --onnx-file で名指しもできる
    (q / "onnx" / "model.onnx").write_bytes(b"x")
    assert cli_main(["model", "use", str(q), "--onnx-file", "onnx/model_qint8_avx512_vnni.onnx"]) == 0
    assert spec().onnx_file == "onnx/model_qint8_avx512_vnni.onnx"


def test_model_use_does_not_save_when_the_check_finds_something(home, loads, tmp_path, capsys):
    folder = put_model(tmp_path / "e5").resolve()
    with open(folder / "tokenizer.json", "wb") as f:                              # 控えの大きさと違う
        f.truncate(10)
    loads.dim = 768                                                               # 別のモデルの次元
    assert cli_main(["model", "use", str(folder)]) == 4
    out = capsys.readouterr().out
    assert "大きさ" in out and "次元が 768" in out and out.count("注意:") == 2
    assert "result: not saved (warnings" in out and "--yes" in out and not home.exists()
    assert cli_main(["model", "use", str(folder), "--yes"]) == 0
    assert "result: saved" in capsys.readouterr().out and user_model_dirs() == {DEFAULT_MODEL: str(folder)}
    # --no-check でも、大きさは比べる
    assert cli_main(["model", "use", str(folder), "--no-check"]) == 4


def test_model_use_refuses_a_folder_it_cannot_load_and_remembers_nothing(home, tmp_path, capsys):
    folder = put_model(tmp_path / "broken")          # ONNX でも tokenizer でもない中身(本物の OnnxEmbedder で読む)
    assert cli_main(["model", "use", str(folder)]) == 2
    captured = capsys.readouterr()
    assert "読み込めませんでした" in captured.err and "result: not saved (error)" in captured.out
    assert not home.exists()
    assert cli_main(["model", "use"]) == 2                                        # フォルダが無い
    assert "model use" in capsys.readouterr().err
    assert cli_main(["model", "info", str(folder)]) == 2                          # info はフォルダを受け取らない
    assert cli_main(["model", "use", str(folder), "--no-check"]) == 0             # 読み込みを省けば、覚える


def test_a_broken_settings_file_is_not_overwritten(home, loads, tmp_path, capsys):
    folder = put_model(tmp_path / "e5")
    home.parent.mkdir(parents=True)
    home.write_text("{ not json", encoding="utf-8")
    assert spec().model_dir == ""                                                 # 検索は、設定が無いものとして動く
    assert cli_main(["model", "use", str(folder)]) == 2
    assert "上書きしません" in capsys.readouterr().err and home.read_text(encoding="utf-8") == "{ not json"


def test_a_moved_folder_says_how_to_fix_it(home, loads, tmp_path, capsys):
    folder = put_model(tmp_path / "e5")
    assert cli_main(["model", "use", str(folder)]) == 0
    for f in (folder / "onnx" / "model.onnx", folder / "tokenizer.json"):
        f.unlink()
    with pytest.raises(ModelUnavailable) as e:
        model_files(spec())
    assert "`model use` で覚えたフォルダ" in str(e.value) and "model forget" in str(e.value)


def test_config_model_dir_comes_first(home, loads, tmp_path, monkeypatch, capsys):
    mine = put_model(tmp_path / "e5")
    assert cli_main(["model", "use", str(mine)]) == 0
    ws = tmp_path / "work"
    (ws / ".cf").mkdir()
    (ws / ".cf" / "config.toml").write_text('[[source]]\nid = "a"\nroot = "."\n\n[embedding]\nmodel_dir = "/elsewhere"\n',
                                            encoding="utf-8")
    capsys.readouterr()
    assert cli_main(["model", "use", str(mine)]) == 0
    assert "[embedding] model_dir(/elsewhere)が先に使われます" in capsys.readouterr().out
    s = resolve_model(EmbeddingConfig(model_dir="/elsewhere"))
    assert (s.model_dir, s.model_dir_source) == ("/elsewhere", "config")
    # 別のモデルには効かない(フォルダはモデルの名前ごとに覚える)
    other = resolve_model(EmbeddingConfig(model=GRANITE))
    assert other.model_dir == "" and other.model_dir_source == ""


def test_an_old_index_without_onnx_bytes_is_not_reused_with_a_registered_folder(home, loads, tmp_path):
    from context_foundation.indexer import _missing_ok
    assert _missing_ok("onnx_bytes", EmbeddingConfig())
    assert cli_main(["model", "use", str(put_model(tmp_path / "e5"))]) == 0
    assert not _missing_ok("onnx_bytes", EmbeddingConfig())


def test_setup_wizard_asks_for_the_model_folder_until_it_is_valid(home, loads, tmp_path, monkeypatch, capsys):
    folder = put_model(tmp_path / "models" / "e5")
    answers = [str(tmp_path / "nope"), str(tmp_path / "models")]
    monkeypatch.setattr(builtins, "input", lambda prompt="": answers.pop(0) if answers else (_ for _ in ()).throw(EOFError))
    assert wizard_main(Namespace(action="setup")) == 0
    out = capsys.readouterr().out
    assert "インストールされていません" in out and 'model use "<フォルダ>"' in out
    assert "そのパスはありません" in out and "覚えました" in out and "使えるようになりました" in out
    assert user_model_dirs() == {DEFAULT_MODEL: str(folder.resolve())}
    # 2 回目は、モデルがあるので尋ねない
    answers[:] = ["must not be read"]
    assert wizard_main(Namespace(action="setup")) == 0
    assert answers == ["must not be read"]


def test_setup_wizard_asks_before_keeping_a_folder_with_warnings(home, loads, tmp_path, monkeypatch, capsys):
    odd = put_model(tmp_path / "odd")
    with open(odd / "tokenizer.json", "wb") as f:
        f.truncate(10)
    good = put_model(tmp_path / "good")
    answers = [str(odd), "n", str(good)]                    # 気になる点のあるフォルダは断り、次のフォルダを入れる
    monkeypatch.setattr(builtins, "input", lambda prompt="": answers.pop(0) if answers else (_ for _ in ()).throw(EOFError))
    assert wizard_main(Namespace(action="setup")) == 0
    assert "注意:" in capsys.readouterr().out and answers == []
    assert user_model_dirs() == {DEFAULT_MODEL: str(good.resolve())}


def test_setup_wizard_skips_the_model_on_an_empty_answer_or_closed_input(home, monkeypatch, capsys):
    monkeypatch.setattr(builtins, "input", lambda prompt="": "")
    assert wizard_main(Namespace(action="setup")) == 0
    monkeypatch.setattr(builtins, "input", lambda prompt="": (_ for _ in ()).throw(EOFError))
    assert wizard_main(Namespace(action="setup")) == 0
    assert "用意ができました" in capsys.readouterr().out and user_model_dirs() == {}


def test_tests_do_not_read_the_users_settings(tmp_path):
    # conftest が、利用者ごとの設定を一時フォルダに向けている(開発者が `model use` していても、テストが変わらない)
    assert embed.user_settings_path() == tmp_path / "cf-settings.json"


def test_setup_report_names_model_use(monkeypatch, tmp_path):
    from test_launcher import load_launcher
    ctxf = load_launcher(monkeypatch, tmp_path / "home")
    missing = ctxf.model_report({"state": "missing", "name": "m", "license": "MIT", "url": "https://huggingface.co/m",
                                 "folder": "/c", "own_folder": False, "dir_source": ""})
    assert "model download" in missing and 'model use "<folder>"' in missing
    moved = ctxf.model_report({"state": "missing", "name": "m", "license": "MIT", "folder": "/mine",
                               "own_folder": True, "dir_source": "user"})
    assert "set with `model use`: /mine" in moved and "model forget" in moved and "model download" not in moved


# --- setup の skill ---

SETUP_SKILL = REPO / "skills" / "context-search-setup"


def test_setup_skill_is_a_skill_with_the_same_relay():
    text = (SETUP_SKILL / "SKILL.md").read_text(encoding="utf-8")
    front = re.match(r"---\n(.*?)\n---\n", text, re.S).group(1)
    assert re.search(r"^name: context-search-setup$", front, re.M)
    assert re.search(r"^description: .{40,1024}$", front, re.M)
    # 取り次ぎは、検索の skill と同じもの(skill のフォルダに名前を足すだけで呼べる)
    assert (SETUP_SKILL / "ctxf.py").read_bytes() == (REPO / "skills" / "context-search" / "ctxf.py").read_bytes()


def test_setup_skill_asks_before_each_step_and_never_downloads_the_model():
    text = (SETUP_SKILL / "SKILL.md").read_text(encoding="utf-8")
    commands = re.findall(r"`ctxf\.py ([a-z-]+)", text)
    assert {"where", "setup", "model", "init", "index"} >= set(commands) and "setup" in commands
    assert "`ctxf.py setup --no-model`" in text and 'model use "<folder>"' in text
    # モデルは取得しない(取得のコマンドは、実行しないこととしてだけ書く)
    assert re.search(r"Never download it\*\*: do not run\s+`model download` or `index --download`", text)
    assert "ask" in text.lower() and "setup.bat" in text
    # 文面が頼るコマンドの出力の語が、実際の出力にあること
    cli = (REPO / "src" / "context_foundation" / "cli.py").read_text(encoding="utf-8")
    for word in ("注意:", "モデル:", "大きさ:", "result: saved", "result: not saved (warnings", "result: not saved (error)"):
        assert word in cli and word in text, word
    assert "EXIT_MODEL_NOT_SAVED = 4" in cli and "exit code 4" in text
    launcher = (REPO / "ctxf.py").read_text(encoding="utf-8")
    for word in ("present:", "missing:", "set with `model use`", "setup:", "runtime:", "index:", "Setup is done",
                 "Another setup is running", "python3 -m venv --clear .venv"):
        assert word in launcher, word
    # 1 つで通しで進められること: 用意・モデル・確かめ・索引・終わり
    assert re.findall(r"^## (\d)\. ", text, re.M) == ["1", "2", "3", "4", "5", "6"]
    assert "/context-index" in text and "`ctxf.py init" in text and "new session" in text
    assert "[Console]::OutputEncoding" in text and "exits with code 4" in text


def test_sdist_carries_the_setup_skill():
    manifest = (REPO / "MANIFEST.in").read_text(encoding="utf-8")
    assert "skills/context-search-setup/SKILL.md" in manifest and "skills/context-search-setup/ctxf.py" in manifest
