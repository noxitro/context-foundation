"""手元に置いた埋め込みモデルのフォルダを使う(`model use` / `model forget`、setup.bat の尋ね方、setup の skill)。

モデルの実物は使わない。読み込みの確かめ(check_model)は、偽の埋め込みに差し替えるか、壊れたファイルで
誤りの道筋だけを通す。
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


@pytest.fixture
def home(monkeypatch, tmp_path, outside_any_workspace):
    """利用者ごとの置き場とモデルの取得先を、一時フォルダに向ける。今のフォルダは、設定の無いフォルダ。"""
    monkeypatch.setenv("CF_HOME", str(tmp_path / "cf-home"))
    monkeypatch.setenv("CF_MODEL_CACHE", str(tmp_path / "no-models"))
    monkeypatch.delenv("CF_ENTRY", raising=False)
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.chdir(work)
    return tmp_path / "cf-home"


@pytest.fixture
def loads(monkeypatch):
    """check_model が読み込むモデルを、偽の埋め込み(384 次元)に差し替え、大きさの比べを省く。読み込んだフォルダを
    並べて返す。"""
    loaded: list[Path] = []

    class FakeOnnx:
        def __init__(self, spec, **kw):
            model_files(spec)
            loaded.append(Path(spec.model_dir))

        def embed_query(self, text):
            return np.ones(384, dtype=np.float32)

    monkeypatch.setattr(embed, "OnnxEmbedder", FakeOnnx)
    monkeypatch.setattr(embed, "size_notes", lambda spec: [])     # 偽のファイルは、控えの大きさと違う
    return loaded


def put_model(folder: Path, onnx_name: str = "onnx/model.onnx") -> Path:
    (folder / onnx_name).parent.mkdir(parents=True, exist_ok=True)
    (folder / onnx_name).write_bytes(b"onnx")
    (folder / "tokenizer.json").write_text("{}", encoding="utf-8")
    return folder


def spec():
    return resolve_model(EmbeddingConfig())


def test_locate_accepts_the_folder_a_file_in_it_a_hugging_face_cache_and_a_folder_above(tmp_path):
    plain = put_model(tmp_path / "e5")
    assert locate_model_folder(str(plain), spec()) == plain.resolve()
    assert locate_model_folder(f'"{plain}"', spec()) == plain.resolve()          # 貼り付けた引用符
    assert locate_model_folder(str(plain / "onnx" / "model.onnx"), spec()) == plain.resolve()
    assert locate_model_folder(str(plain / "tokenizer.json"), spec()) == plain.resolve()
    flat = put_model(tmp_path / "flat", "model.onnx")                             # フォルダの直下に置いたもの
    assert locate_model_folder(str(flat), spec()) == flat.resolve()
    # Hugging Face のキャッシュ: 同じコミットの snapshot を、新しい別の snapshot より先に選ぶ
    cache = tmp_path / "hub" / "models--intfloat--multilingual-e5-small"
    same = put_model(cache / "snapshots" / E5.revision)
    put_model(cache / "snapshots" / "0123456789abcdef")
    assert locate_model_folder(str(cache), spec()) == same.resolve()
    # 上のフォルダ: 下にモデルが 1 つだけなら、それを選ぶ。2 つ以上なら、選んでもらう
    assert locate_model_folder(str(tmp_path / "hub"), spec()) == same.resolve()
    with pytest.raises(ModelUnavailable, match="つあります"):
        locate_model_folder(str(tmp_path), spec())


def test_locate_says_what_is_missing_and_which_onnx_files_it_saw(tmp_path):
    with pytest.raises(ModelUnavailable, match="そのパスはありません"):
        locate_model_folder(str(tmp_path / "nope"), spec())
    other = tmp_path / "quantized"
    (other / "onnx").mkdir(parents=True)
    (other / "onnx" / "model_qint8_avx512_vnni.onnx").write_bytes(b"x")
    (other / "tokenizer.json").write_text("{}", encoding="utf-8")
    with pytest.raises(ModelUnavailable) as e:
        locate_model_folder(str(other), spec())
    assert "onnx/model.onnx" in str(e.value) and "tokenizer.json" in str(e.value)
    assert "model_qint8_avx512_vnni.onnx" in str(e.value) and "onnx_file" in str(e.value)


def test_model_use_remembers_the_folder_for_every_workspace_and_forget_drops_it(home, loads, tmp_path, capsys):
    folder = put_model(tmp_path / "models" / "e5")
    assert cli_main(["model", "use", str(folder / "onnx" / "model.onnx")]) == 0
    out = capsys.readouterr().out
    assert "フォルダを覚えました" in out and "埋め込みの次元 384" in out and "model forget" in out
    assert loads == [folder.resolve()]                                            # 読み込んで確かめた
    settings = json.loads((home / "settings.json").read_text(encoding="utf-8"))
    assert settings == {"model_dirs": {DEFAULT_MODEL: str(folder.resolve())}}
    # 設定の無い作業フォルダでも、そのフォルダのモデルを使う(取得しない)
    s = spec()
    assert (s.model_dir, s.model_dir_source) == (str(folder.resolve()), "user")
    assert model_files(s)[1] == folder.resolve() / "tokenizer.json"
    assert cli_main(["model", "info"]) == 0
    out = capsys.readouterr().out
    assert "`model use` で指定したフォルダ" in out and "手元にあります" in out
    # 覚えている間は、取得しない
    assert cli_main(["model", "download"]) == 2
    assert "model forget" in capsys.readouterr().err
    assert not (tmp_path / "no-models").exists()
    # 外すと、取得先のキャッシュに戻る。ファイルは消さない
    assert cli_main(["model", "forget"]) == 0
    assert "外しました" in capsys.readouterr().out
    assert user_model_dirs() == {} and spec().model_dir == "" and (folder / "onnx" / "model.onnx").is_file()
    assert cli_main(["model", "forget"]) == 0
    assert "ありません" in capsys.readouterr().out


def test_model_use_refuses_a_folder_it_cannot_load_and_remembers_nothing(home, tmp_path, capsys):
    folder = put_model(tmp_path / "broken")          # ONNX でも tokenizer でもない中身(本物の OnnxEmbedder で読む)
    assert cli_main(["model", "use", str(folder)]) == 2
    assert "読み込めませんでした" in capsys.readouterr().err
    assert not (home / "settings.json").exists()
    assert cli_main(["model", "use"]) == 2                                        # フォルダが無い
    assert "model use" in capsys.readouterr().err
    assert cli_main(["model", "info", str(folder)]) == 2                          # info はフォルダを受け取らない
    assert cli_main(["model", "use", str(folder), "--no-check"]) == 0             # 読み込みを省けば、覚える
    out = capsys.readouterr().out
    assert "フォルダを覚えました" in out and out.count("注意:") == 2 and "大きさ" in out   # 大きさは控えの値と違う


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
    other = resolve_model(EmbeddingConfig(model="ibm-granite/granite-embedding-97m-multilingual-r2"))
    assert other.model_dir == "" and other.model_dir_source == ""


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


def test_setup_wizard_skips_the_model_on_an_empty_answer_or_closed_input(home, monkeypatch, capsys):
    monkeypatch.setattr(builtins, "input", lambda prompt="": "")
    assert wizard_main(Namespace(action="setup")) == 0
    monkeypatch.setattr(builtins, "input", lambda prompt="": (_ for _ in ()).throw(EOFError))
    assert wizard_main(Namespace(action="setup")) == 0
    assert "用意ができました" in capsys.readouterr().out and user_model_dirs() == {}


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
    for word in ("フォルダを覚えました", "注意:", "設定:"):
        assert word in (REPO / "src" / "context_foundation" / "cli.py").read_text(encoding="utf-8"), word
    for word in ("present:", "set with `model use`", "setup:", "runtime:", "index:"):
        assert word in (REPO / "ctxf.py").read_text(encoding="utf-8"), word


def test_sdist_carries_the_setup_skill():
    manifest = (REPO / "MANIFEST.in").read_text(encoding="utf-8")
    assert "skills/context-search-setup/SKILL.md" in manifest and "skills/context-search-setup/ctxf.py" in manifest
