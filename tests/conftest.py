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
def _hash_embedder(monkeypatch):
    """既定では、テスト用の埋め込みを使う(モデルの実物を使うテストは、自分で外す)。"""
    monkeypatch.setenv("CF_TEST_EMBEDDER", "hash")
    monkeypatch.delenv("CF_CONFIG", raising=False)


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
