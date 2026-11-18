"""索引づくりが止められた・置き換えられなかった・モデルを読めなかった・source が無いときに、計算した結果と
前の索引を失わないこと。

確かめること:
- 途中で止められた索引づくりの埋め込みは、次の索引づくりで計算し直さない(<索引>.partial)
- 止められた索引づくりが残した途中のファイル(<索引>.tmp-<pid>)は、そのプロセスが無ければ消す
- 前の索引を置き換えられないときは、できた索引を <索引>.new に残し、次の索引づくりがそこから使い回す
- --no-embed の索引づくりは <索引>.partial を残し、設定で埋め込みを切った索引づくりは消す
- 埋め込みのある索引を、モデルを読めないからといって、黙って keyword だけの索引に置き換えない
- 索引の形式の版が違っても、埋め込みの作り方が同じなら使い回す
- source のルートが無い・入るファイルが無いときは、traceback ではなく 1 つの文面で止まる
"""
from __future__ import annotations

import builtins
import os
import sqlite3
import subprocess
import sys
from argparse import Namespace
from contextlib import closing
from pathlib import Path

import numpy as np
import pytest

from context_foundation import indexer
from context_foundation.cli import main as cli_main
from context_foundation.config import ConfigError, load_config
from context_foundation.embed import HashEmbedder, ModelUnavailable
from context_foundation.indexer import BuildError, ReplaceError, build_index
from context_foundation.store import load_meta, load_vectors, open_readonly

from conftest import write_config


def _cfg(ws: Path, corpus: Path, extra: str = ""):
    return load_config(write_config(ws, corpus, extra))


def _build(ws: Path, corpus: Path, **kw) -> dict:
    return build_index(_cfg(ws, corpus), progress=False, log=lambda s: None, **kw)


def _vectors(index: Path) -> np.ndarray:
    with closing(open_readonly(index)) as db:
        emb = load_meta(index)["embedding"]
        return load_vectors(db, emb["dim"], emb["vector_dtype"])


class _Counting(HashEmbedder):
    """計算した passage を数え、stop_after 回目の呼び出しで止められたふりをする(KeyboardInterrupt)。"""

    def __init__(self, stop_after: int | None = None, on_call=None):
        self.calls = self.passages = 0
        self.stop_after, self.on_call = stop_after, on_call

    def embed_passages(self, texts, progress=None):
        self.calls += 1
        if self.on_call:
            self.on_call(self.calls)
        if self.stop_after is not None and self.calls > self.stop_after:
            raise KeyboardInterrupt
        self.passages += len(texts)
        return super().embed_passages(texts, progress)


def _partial_rows(index: Path) -> int:
    path = index.with_name(index.name + ".partial")
    if not path.is_file():
        return 0
    with closing(sqlite3.connect(str(path))) as db:
        return db.execute("SELECT count(*) FROM vectors").fetchone()[0]


def test_interrupted_build_resumes_from_the_computed_embeddings(tmp_path, corpus_copy, monkeypatch):
    monkeypatch.setattr(indexer, "EMBED_TASK", 4)
    monkeypatch.setattr(indexer, "RESUME_EVERY", 8)
    index = tmp_path / "ws" / ".cf" / "index.db"
    seen: list[int] = []
    # 止められる前から、RESUME_EVERY 件ごとにファイルへ書いている(強制終了では finally が走らない)
    first = _Counting(stop_after=5, on_call=lambda n: seen.append(_partial_rows(index)) if n == 5 else None)
    with pytest.raises(KeyboardInterrupt):
        _build(tmp_path / "ws", corpus_copy, embedder=first)
    assert seen and seen[0] >= 8
    assert not index.exists() and not list(index.parent.glob("index.db.tmp-*"))
    assert _partial_rows(index) == first.passages == 20        # 止められたときに、残りも書く

    second = _Counting()
    result = _build(tmp_path / "ws", corpus_copy, embedder=second)
    total = result["stats"]["chunks"] - result["timings"]["unembedded_passages"]
    assert second.passages == total - 20
    assert result["timings"]["reused_passages"] == 20
    assert not index.with_name("index.db.partial").exists()     # できあがったら消す

    # 続きから作った索引は、最初から作ったものと同じ
    _build(tmp_path / "clean", corpus_copy, embedder=_Counting())
    assert np.array_equal(_vectors(index), _vectors(tmp_path / "clean" / ".cf" / "index.db"))


def test_partial_embeddings_of_another_model_are_not_used(tmp_path, corpus_copy, monkeypatch):
    monkeypatch.setattr(indexer, "EMBED_TASK", 4)
    monkeypatch.setattr(indexer, "RESUME_EVERY", 4)
    with pytest.raises(KeyboardInterrupt):
        _build(tmp_path / "ws", corpus_copy, embedder=_Counting(stop_after=2))

    class Other(_Counting):
        name = "another-test-model"

    other = Other()
    result = _build(tmp_path / "ws", corpus_copy, embedder=other)
    assert result["timings"]["reused_passages"] == 0
    assert other.passages == result["stats"]["chunks"] - result["timings"]["unembedded_passages"]


def test_tmp_files_of_dead_processes_are_removed(tmp_path, corpus_copy):
    ws = tmp_path / "ws"
    cf = ws / ".cf"
    cf.mkdir(parents=True)
    done = subprocess.run([sys.executable, "-c", "import os; print(os.getpid())"], capture_output=True, text=True)
    dead = cf / f"index.db.tmp-{int(done.stdout)}"
    # 生きている側は、このテストが起こして待たせている子(親プロセスの pid は、コンテナの PID 1 で動かすと 0 になる)
    waiting = subprocess.Popen([sys.executable, "-c", "input()"], stdin=subprocess.PIPE)
    try:
        alive = cf / f"index.db.tmp-{waiting.pid}"
        other = cf / "index.db.tmp-notapid"
        for p in (dead, alive, other):
            p.write_bytes(b"x" * 100)
        _build(ws, corpus_copy)
        assert not dead.exists() and alive.exists() and other.exists()
    finally:
        waiting.communicate(b"\n", timeout=30)


def test_tmp_names_that_are_not_ascii_pids_do_not_stop_the_build(tmp_path, corpus_copy):
    cf = tmp_path / "ws" / ".cf"
    cf.mkdir(parents=True)
    odd = [cf / "index.db.tmp-²", cf / ("index.db.tmp-" + "9" * 30)]    # ² は isdigit だが int にできない
    for p in odd:
        p.write_bytes(b"x")
    _build(tmp_path / "ws", corpus_copy)
    assert all(p.exists() for p in odd)                 # この道具の名前ではないので、触らない


def test_pid_alive_answers_for_a_number_too_large_for_a_pid():
    from context_foundation.sysinfo import pid_alive
    assert pid_alive(10 ** 30) is False                 # POSIX の os.kill は OverflowError を出す


def _interrupted_partial(tmp_path: Path, corpus: Path, monkeypatch) -> Path:
    """止められた索引づくりが残した、本物の形の <索引>.partial。"""
    monkeypatch.setattr(indexer, "EMBED_TASK", 4)
    monkeypatch.setattr(indexer, "RESUME_EVERY", 4)
    with pytest.raises(KeyboardInterrupt):
        _build(tmp_path / "ws", corpus, embedder=_Counting(stop_after=3))
    partial = tmp_path / "ws" / ".cf" / "index.db.partial"
    assert _partial_rows(tmp_path / "ws" / ".cf" / "index.db") == 12
    return partial


def test_partial_rows_of_the_wrong_type_or_length_are_not_used(tmp_path, corpus_copy, monkeypatch):
    partial = _interrupted_partial(tmp_path, corpus_copy, monkeypatch)
    with closing(sqlite3.connect(str(partial))) as db, db:
        keys = [r[0] for r in db.execute("SELECT key FROM vectors ORDER BY key")]
        db.execute("UPDATE vectors SET data = 'this is text, not a blob' WHERE key = ?", (keys[0],))
        db.execute("UPDATE vectors SET data = x'0011' WHERE key = ?", (keys[1],))
        db.execute("UPDATE vectors SET key = 'text key' WHERE key = ?", (keys[2],))
    second = _Counting()
    result = _build(tmp_path / "ws", corpus_copy, embedder=second)
    assert result["timings"]["reused_passages"] == 12 - 3
    assert not partial.exists()


def test_an_unreadable_partial_file_is_discarded(tmp_path, corpus_copy, monkeypatch):
    partial = _interrupted_partial(tmp_path, corpus_copy, monkeypatch)
    monkeypatch.setattr(np, "frombuffer", lambda *a, **k: (_ for _ in ()).throw(TypeError("planted")))
    logs: list[str] = []
    result = build_index(_cfg(tmp_path / "ws", corpus_copy), progress=False, log=logs.append,
                         embedder=_Counting())
    assert result["timings"]["reused_passages"] == 0
    assert any("前回の途中までの埋め込みは使えないので消します" in s for s in logs)
    assert not partial.exists()


def test_full_rebuild_does_not_use_the_partial_file(tmp_path, corpus_copy, monkeypatch):
    partial = _interrupted_partial(tmp_path, corpus_copy, monkeypatch)
    again = _Counting()
    result = _build(tmp_path / "ws", corpus_copy, embedder=again, reuse=False)
    assert result["timings"]["reused_passages"] == 0
    assert again.passages == result["stats"]["chunks"] - result["timings"]["unembedded_passages"]
    assert not partial.exists()


def test_an_interrupted_full_rebuild_leaves_only_what_it_computed(tmp_path, corpus_copy, monkeypatch):
    """--full は、始めに前の .partial を捨てる(読まないだけではない): --full も止められたとき、次のふつうの
    索引づくりが使うのは、--full が計算し直した分だけ。"""
    _interrupted_partial(tmp_path, corpus_copy, monkeypatch)
    index = tmp_path / "ws" / ".cf" / "index.db"
    seen: list[int] = []
    stopped = _Counting(stop_after=1, on_call=lambda n: seen.append(_partial_rows(index)))
    with pytest.raises(KeyboardInterrupt):
        _build(tmp_path / "ws", corpus_copy, embedder=stopped, reuse=False)
    assert seen[0] == 0                                  # 最初の埋め込みの前に、前の分は消えている
    assert _partial_rows(index) == stopped.passages > 0


def test_no_embed_build_keeps_the_partial_file_for_the_next_embedding_build(tmp_path, corpus_copy, monkeypatch):
    ws = tmp_path / "ws"

    class Other(_Counting):
        name = "another-test-model"

    _build(ws, corpus_copy, embedder=Other())      # 埋め込みのある前の索引(作り方が違うので、使い回されない)
    partial = _interrupted_partial(tmp_path, corpus_copy, monkeypatch)
    logs: list[str] = []
    build_index(_cfg(ws, corpus_copy), use_embedding=False, progress=False, log=logs.append)     # --no-embed
    assert _partial_rows(ws / ".cf" / "index.db") == 12
    assert any("index.db.partial" in s and "残してあり" in s for s in logs)
    assert not any("すべての passage を埋め込み直します" in s for s in logs)
    result = _build(ws, corpus_copy, embedder=_Counting())
    assert result["timings"]["reused_passages"] == 12
    assert not partial.exists()


def test_build_with_embedding_disabled_in_the_config_removes_the_partial_file(tmp_path, corpus_copy, monkeypatch):
    partial = _interrupted_partial(tmp_path, corpus_copy, monkeypatch)
    result = build_index(_cfg(tmp_path / "ws", corpus_copy, "[embedding]\nenabled = false\n"), progress=False,
                         log=lambda s: None)
    assert result["embedding"] is None
    assert not partial.exists()                     # 設定で埋め込みを切ったので、残しても使われない


def _link(link: Path, target: Path) -> None:
    try:
        os.symlink(target, link)
    except (OSError, NotImplementedError) as e:
        pytest.skip(f"シンボリックリンクを作れない: {e}")


def test_links_planted_in_cf_are_not_followed_when_writing(tmp_path, corpus_copy, monkeypatch):
    """.cf に置かれた宙ぶらりんのリンク(途中の埋め込み・途中の索引・.gitignore)をたどって、外にファイルを作らない。"""
    monkeypatch.setattr(indexer, "EMBED_TASK", 4)
    monkeypatch.setattr(indexer, "RESUME_EVERY", 4)
    cf = tmp_path / "ws" / ".cf"
    cf.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    targets = {"index.db.partial": outside / "partial.bin", f"index.db.tmp-{os.getpid()}": outside / "tmp.bin",
               ".gitignore": outside / "gitignore.txt"}
    for name, target in targets.items():
        _link(cf / name, target)
    result = _build(tmp_path / "ws", corpus_copy, embedder=_Counting())
    assert result["stats"]["chunks"] > 0 and (cf / "index.db").is_file()
    assert sorted(p.name for p in outside.iterdir()) == []
    assert not os.path.lexists(cf / "index.db.partial") and not os.path.lexists(cf / f"index.db.tmp-{os.getpid()}")


def _refuse_replacing(monkeypatch, dest: Path):
    real = indexer.os.replace

    def fake(src, dst):
        if Path(dst) == dest:
            raise PermissionError(13, "in use", str(dst))
        return real(src, dst)

    monkeypatch.setattr(indexer.os, "replace", fake)
    monkeypatch.setattr(indexer.time, "sleep", lambda s: None)


def test_replace_failure_keeps_the_new_index_and_the_next_build_reuses_it(tmp_path, corpus_copy, monkeypatch,
                                                                         capsys):
    ws = tmp_path / "ws"
    index = ws / ".cf" / "index.db"
    _build(ws, corpus_copy)
    before = index.read_bytes()
    (corpus_copy / "docs" / "added-note.md").write_text("# Added\n\nTexture streaming loads mips.\n",
                                                        encoding="utf-8")

    with monkeypatch.context() as m:
        _refuse_replacing(m, index)
        with pytest.raises(ReplaceError, match="index.db.new"):
            _build(ws, corpus_copy)
        assert index.read_bytes() == before                     # 前の索引はそのまま
        pending = index.with_name("index.db.new")
        assert load_meta(pending)["stats"]["files"] == load_meta(index)["stats"]["files"] + 1
        # コマンドでは、traceback を出さずに 1 で終わる
        assert cli_main(["index", "--config", str(ws), "--quiet"]) == 1
        err = capsys.readouterr().err
        assert "Traceback" not in err and "置き換えられません" in err and "index.db.new" in err

    # 置き換えに失敗した索引づくりは .partial も残す。それを消して、.new だけが頼りの形にする
    partial = index.with_name("index.db.partial")
    assert partial.exists()
    partial.unlink()
    counting = _Counting()
    result = _build(ws, corpus_copy, embedder=counting)
    assert counting.passages == 0                                # 置き換えられなかった索引から、すべて使い回す
    assert result["timings"]["reused_passages"] > 0
    assert not index.with_name("index.db.new").exists()


def _model_missing(monkeypatch):
    def unavailable(*a, **kw):
        raise ModelUnavailable("埋め込みモデルが手元にありません: (test)")
    monkeypatch.setattr(indexer, "make_embedder", unavailable)


def test_model_unavailable_does_not_replace_an_embedded_index(tmp_path, corpus_copy, monkeypatch, capsys):
    ws = tmp_path / "ws"
    index = ws / ".cf" / "index.db"
    _build(ws, corpus_copy)
    before = index.read_bytes()
    _model_missing(monkeypatch)
    with pytest.raises(ModelUnavailable, match="--no-embed"):
        _build(ws, corpus_copy)
    assert cli_main(["index", "--config", str(ws), "--quiet"]) == 2
    err = capsys.readouterr().err
    assert "keyword だけの索引に置き換えずに止めました" in err and "Traceback" not in err
    assert index.read_bytes() == before
    assert not list(index.parent.glob("index.db.tmp-*"))

    # はっきり --no-embed と言われたら、keyword だけの索引にする
    assert cli_main(["index", "--config", str(ws), "--quiet", "--no-embed"]) == 0
    assert load_meta(index)["embedding"] is None


def test_model_unavailable_without_a_previous_index_still_builds_keyword_only(tmp_path, corpus_copy, monkeypatch):
    _model_missing(monkeypatch)
    result = _build(tmp_path / "ws", corpus_copy)
    assert result["embedding"] is None


def test_wizard_asks_before_dropping_the_embeddings(tmp_path, corpus_copy, monkeypatch, capsys):
    from context_foundation import wizard

    ws = tmp_path / "ws"
    index = ws / ".cf" / "index.db"
    _build(ws, corpus_copy)
    before = index.read_bytes()
    monkeypatch.setenv("CF_HOME", str(tmp_path / "cf-home"))
    monkeypatch.setattr(wizard, "_offer_model", lambda ecfg: False)     # モデルを取得しなかった
    queue = [str(ws), "y"]                                               # 作業フォルダ・作り直す。次は閉じる

    def fake_input(prompt=""):
        if not queue:
            raise EOFError
        return queue.pop(0)

    monkeypatch.setattr(builtins, "input", fake_input)
    assert wizard.main(Namespace(action="index")) == 2
    assert "前の索引には埋め込みがあります" in capsys.readouterr().out
    assert index.read_bytes() == before

    queue += [str(ws), "y", "y"]                                         # 置き換えてよいと答えた
    assert wizard.main(Namespace(action="index")) == 0
    assert load_meta(index)["embedding"] is None


def test_embeddings_are_reused_from_an_index_of_another_schema_version(tmp_path, corpus_copy):
    ws = tmp_path / "ws"
    index = ws / ".cf" / "index.db"
    first = _build(ws, corpus_copy)
    with closing(sqlite3.connect(str(index))) as db:
        db.execute("UPDATE meta SET value = '99' WHERE key = 'schema_version'")
        db.commit()
    counting = _Counting()
    result = _build(ws, corpus_copy, embedder=counting)
    assert counting.passages == 0
    assert result["timings"]["reused_passages"] == first["timings"]["embedded_passages"]


def test_missing_source_roots_stop_with_one_message(tmp_path, corpus_copy, capsys):
    ws = tmp_path / "ws"
    _build(ws, corpus_copy)
    index = ws / ".cf" / "index.db"
    before = index.read_bytes()
    gone1, gone2 = tmp_path / "unmounted" / "depot", tmp_path / "renamed"
    extra = f"[[source]]\nid = 'p4'\nroot = '{gone1}'\n\n[[source]]\nid = 'old'\nroot = '{gone2}'\n"
    write_config(ws, corpus_copy, extra)
    with pytest.raises(ConfigError, match="p4"):
        build_index(load_config(ws / ".cf" / "config.toml"), progress=False, log=lambda s: None)
    assert cli_main(["index", "--config", str(ws), "--quiet"]) == 2
    err = capsys.readouterr().err
    assert "Traceback" not in err and "p4:" in err and "old:" in err and str(gone1) in err
    assert index.read_bytes() == before                          # 前の索引はそのまま
    assert not list(index.parent.glob("index.db.tmp-*"))


def test_index_download_failure_stops_with_one_message(tmp_path, corpus_copy, monkeypatch, capsys):
    from context_foundation import embed

    def fail(*a, **kw):
        raise ModelUnavailable("onnx/model.onnx を取得できませんでした(test)")

    monkeypatch.setattr(embed, "download_model", fail)
    ws = tmp_path / "ws"
    write_config(ws, corpus_copy)
    assert cli_main(["index", "--download", "--config", str(ws), "--quiet"]) == 2
    err = capsys.readouterr().err
    assert "Traceback" not in err and "取得できませんでした(test)" in err
    assert not (ws / ".cf" / "index.db").exists()


def test_no_files_stops_without_a_traceback(tmp_path, capsys):
    ws = tmp_path / "ws"
    empty = tmp_path / "empty"
    empty.mkdir()
    (ws / ".cf").mkdir(parents=True)
    (ws / ".cf" / "config.toml").write_text(f"[[source]]\nid = 'e'\nroot = '{empty}'\n", encoding="utf-8")
    with pytest.raises(BuildError):
        build_index(load_config(ws / ".cf" / "config.toml"), progress=False, log=lambda s: None)
    assert cli_main(["index", "--config", str(ws), "--quiet"]) == 2
    err = capsys.readouterr().err
    assert "Traceback" not in err and "1 つもありません" in err
    assert not list((ws / ".cf").glob("index.db.tmp-*"))
