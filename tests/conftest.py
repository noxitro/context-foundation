"""テストの共通の用意。

合成のフィクスチャ(tests/fixtures/corpus: C++・C#・Markdown と、それに対する cases.jsonl)から、
一時フォルダに作業フォルダ(.cf/config.toml)を作り、索引を作る。埋め込みは、モデルの実物の代わりに
語のハッシュで作るテスト用のもの(HashEmbedder)を使うので、モデルを取得していなくても通しで動く。
"""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"
CORPUS = FIXTURES / "corpus"
CASES = FIXTURES / "cases.jsonl"


def write_config(workspace: Path, corpus: Path = CORPUS, extra: str = "") -> Path:
    cf = workspace / ".cf"
    cf.mkdir(parents=True, exist_ok=True)
    text = "".join(
        f"[[source]]\nid = '{sid}'\nroot = '{corpus / sid}'\n\n" for sid in ("engine", "app", "docs")) + extra
    path = cf / "config.toml"
    path.write_text(text, encoding="utf-8")
    return path


@pytest.fixture(autouse=True)
def _hash_embedder(monkeypatch, tmp_path):
    """既定では、テスト用の埋め込みを使う(モデルの実物を使うテストは、自分で外す)。"""
    monkeypatch.setenv("CF_TEST_EMBEDDER", "hash")
    # 利用者ごとの設定(`model use` で覚えたモデル)は、開発者の PC のものを読まない
    monkeypatch.setenv("CF_SETTINGS", str(tmp_path / "cf-settings.json"))
    monkeypatch.delenv("CF_CONFIG", raising=False)
    monkeypatch.delenv("COPILOT_SESSION_CWD", raising=False)   # MCP サーバーが作業フォルダを探す変数(Copilot のシェルで入る)


def workspace_above(path: Path) -> Path | None:
    """path の上のフォルダのうち、作業フォルダ(.cf に設定か索引があるもの)。無ければ None。"""
    for d in Path(path).resolve().parents:
        if (d / ".cf" / "config.toml").is_file() or (d / ".cf" / "index.db").is_file():
            return d
    return None


@pytest.fixture
def outside_any_workspace(tmp_path):
    """「索引も設定も無い」場所を前提にするテスト用。設定と索引は上のフォルダへたどって探すので、一時フォルダの
    上に作業フォルダがあると(ホームを作業フォルダにしているときなど)、そこが見つかってしまう。そのときは、理由を
    付けて飛ばす。"""
    found = workspace_above(tmp_path)
    if found is not None:
        pytest.skip(f"一時フォルダの上に作業フォルダ(.cf)がある: {found}。pytest の --basetemp で、"
                    "作業フォルダの外を指すと確かめられます")


@pytest.fixture(scope="session")
def indexed_workspace(tmp_path_factory) -> Path:
    """フィクスチャのコーパスの索引を作った作業フォルダ(テスト全体で 1 回だけ作る)。"""
    import os

    from context_foundation.config import load_config
    from context_foundation.indexer import build_index

    ws = tmp_path_factory.mktemp("workspace")
    old = os.environ.get("CF_TEST_EMBEDDER")
    os.environ["CF_TEST_EMBEDDER"] = "hash"
    try:
        build_index(load_config(write_config(ws)), progress=False, log=lambda s: None)
    finally:
        if old is None:
            os.environ.pop("CF_TEST_EMBEDDER", None)
        else:
            os.environ["CF_TEST_EMBEDDER"] = old
    return ws


@pytest.fixture
def corpus_copy(tmp_path) -> Path:
    """書き換えてよい、コーパスの写し。"""
    dest = tmp_path / "corpus"
    shutil.copytree(CORPUS, dest)
    return dest
