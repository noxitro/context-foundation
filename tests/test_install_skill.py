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


def test_install_skill_is_a_skill_with_the_same_relay():
    front = re.match(r"---\n(.*?)\n---\n", _text(), re.S).group(1)
    assert re.search(r"^name: context-search-install$", front, re.M)
    assert re.search(r"^description: .{40,1024}$", front, re.M)
    assert "/context-search-install" in front
    # 取り次ぎは、検索の skill と同じもの(skill のフォルダに名前を足すだけで呼べる)
    assert (SKILL / "ctxf.py").read_bytes() == (REPO / "skills" / "context-search" / "ctxf.py").read_bytes()


def test_install_skill_runs_only_where_and_setup_without_the_model():
    text = _text()
    commands = set(re.findall(r"`ctxf\.py ([a-z-]+)", text))
    # 動かすのは where と setup だけ。model と init と index は、案内(利用者がすること)としてだけ書く
    assert {"where", "setup"} <= commands
    assert "`ctxf.py setup --no-model`" in text
    run_part = text.split("## 4. Check and finish")[0]
    assert not re.search(r"`ctxf\.py (model|init|index)", run_part)
    assert re.search(r"Do not\s+run `model download` or `index --download`", text)
    # 引数: gpu・cpu・help。両方は断る
    for word in ("`gpu`", "`cpu`", "`help`", "`--gpu`", "`--cpu`", "together"):
        assert word in text, word
    assert re.findall(r"^## (\d)\. ", text, re.M) == ["0", "1", "2", "3", "4"]


def test_install_skill_relies_on_words_the_launcher_prints():
    text = _text()
    launcher = (REPO / "ctxf.py").read_text(encoding="utf-8")
    for word in ("PATH python: ", "does not start", "PATH python: none. ", "Setup is done", "Python packages: ",
                 "Another setup is running", "setup:     ", "WindowsApps", "App execution aliases"):
        assert word in launcher, word
        assert word.strip() in text, word
    for kind in ("runtime", "local .venv", "CF_PYTHON"):
        assert f'"{kind}"' in launcher and f"`({kind})`" in text, kind
    assert re.search(r'SETUP_OPTIONS = \("--no-model", "--gpu", "--cpu"\)', launcher)
    assert "setup.bat" in text and "exits with code 4" in text and "[Console]::OutputEncoding" in text


def test_sdist_carries_the_install_skill():
    manifest = (REPO / "MANIFEST.in").read_text(encoding="utf-8")
    assert "skills/context-search-install/SKILL.md" in manifest and "skills/context-search-install/ctxf.py" in manifest
