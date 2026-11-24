"""コマンドの形の用意の skill(context-search-install)の文面を確かめる。

パッケージを入れることと、PATH の python を確かめることだけをする。モデルと索引には触れない。
"""
from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SKILL = REPO / "skills" / "context-search-install"


def _text():
    return (SKILL / "SKILL.md").read_text(encoding="utf-8")


def _steps(text):
    """手順の部分(## 0. から ## 4. の前まで)。エージェントが動かすコマンドは、ここにだけ書く。"""
    return text[text.index("## 0. "):text.index("## 4. ")]


def test_install_skill_is_a_skill_with_the_same_relay():
    front = re.match(r"---\n(.*?)\n---\n", _text(), re.S).group(1)
    assert re.search(r"^name: context-search-install$", front, re.M)
    assert re.search(r"^description: .{40,1024}$", front, re.M)
    assert "/context-search-install" in front
    # 取り次ぎは、検索の skill と同じもの(skill のフォルダに名前を足すだけで呼べる)
    assert (SKILL / "ctxf.py").read_bytes() == (REPO / "skills" / "context-search" / "ctxf.py").read_bytes()


def test_install_skill_runs_only_where_and_setup():
    text = _text()
    steps = _steps(text)
    # 手順で動かすのは where と setup だけ
    assert set(re.findall(r"`ctxf\.py ([a-z-]+)", steps)) == {"where", "setup"}
    assert "`ctxf.py setup --no-model`" in steps
    # モデル・設定・索引のコマンドは、手順の中では名前も出さない(終わりに、利用者がすることとしてだけ書く)
    assert not re.search(r"`(model [a-z]|init\b|index(?!\.bat)\b)", steps)
    # 動かさないものを、手順の前に名指しする(setup の出力の「Next, build an index」に従わない)
    never = text[text.index("## What this command never does"):text.index("## 0. ")]
    for word in ("`init`", "`index`", "`model use`", "`model download`", "`model forget`", "`index --download`",
                 "Next, build an index"):
        assert word in never, word
    launcher = (REPO / "ctxf.py").read_text(encoding="utf-8")
    assert "Next, build an index" in launcher
    # 引数: gpu・cpu・help。両方は断る。指定が無ければ前の選び方を引き継ぐ(ctxf.py の wants_gpu)
    for word in ("`gpu`", "`cpu`", "`help`", "`--gpu`", "`--cpu`", "together", "keeps the build chosen at the last"):
        assert word in text, word
    assert "return bool(stamp.get(\"gpu\"))" in launcher
    assert re.findall(r"^## (\d)\. ", text, re.M) == ["0", "1", "2", "3", "4"]


def test_install_skill_does_not_race_an_index_build_or_retry_forever():
    steps = _steps(_text())
    assert "An index build" in steps and "/context-index" in steps
    assert "at most\n  three times" in steps and "do not\n  delete it yourself" in steps


def test_install_skill_relies_on_words_the_launcher_prints():
    text = _text()
    launcher = (REPO / "ctxf.py").read_text(encoding="utf-8")
    for word in ("PATH python: ", "does not start", "PATH python: none.", "Setup is done", "Python packages: ",
                 "Another setup is running", "WindowsApps", "App execution aliases", "onnxruntime-directml"):
        assert word in launcher, word
        assert word.strip() in text, word
    # where の行の名前と、使える python が無いときの形、直すコマンドの終わり
    assert '_say("python:    " + (py + "  (" + kind + ")" if py else "none: " + reason))' in launcher
    assert '_say("model:     "' in launcher and '_say("setup:     "' in launcher
    assert 'return entry_command() + " setup"' in launcher and "ends with `ctxf.py\" setup`" in text
    for kind in ("runtime", "local .venv", "CF_PYTHON"):
        assert f'"{kind}"' in launcher and f"`({kind})`" in text, kind
    # 読み込めないときの model: の行(where がパッケージを読んで確かめる)
    assert 'return "unknown ("' in launcher and "starts with `unknown (`" in text
    # 終了コード: where は用意が無ければ 4、setup は排他で 3
    assert "return 0 if py else 4" in launcher and "exits with code 4" in text
    lock = launcher[launcher.index("Another setup is running"):]
    assert lock.index("return 3") < 400 and "Exit code 3" in text
    assert re.search(r'SETUP_OPTIONS = \("--no-model", "--gpu", "--cpu"\)', launcher)
    assert "[Console]::OutputEncoding" in text and "python-is-python3" in text


def test_sdist_carries_the_install_skill():
    manifest = (REPO / "MANIFEST.in").read_text(encoding="utf-8")
    assert "skills/context-search-install/SKILL.md" in manifest and "skills/context-search-install/ctxf.py" in manifest
