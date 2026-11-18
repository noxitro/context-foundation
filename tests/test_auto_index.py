"""セッションの始まりの差分更新(0.4.6 から): hook が切り離したプロセスで始め、変わっていなければ書き直さない。

確かめること:
- hook: 索引のある作業フォルダでだけ、標準入出力をつながない切り離したプロセスで始める。すぐに返る
- 始めないとき: 環境変数・設定で切ってある、ロックが使われている、手動の索引づくりが動いている、索引が無い、用意ができていない
- 二重に動かさない(ロックは O_EXCL。古いロックは pid で見分けて取り直す)
- 安い確かめ: 変わっていなければ書き直さない(build_id が同じ)。変わっていれば更新し、--full で作り直した索引と同じになる
- 止まっても前の索引が残る。結果は .cf/auto-index.json に残り、status に出る
"""
from __future__ import annotations

import importlib.util
import io
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
import types
from contextlib import closing
from pathlib import Path

import pytest

from context_foundation import autoindex
from context_foundation.config import load_config
from context_foundation.indexer import build_index
from context_foundation.render import HARD_MAX_BYTES

from conftest import CORPUS, write_config

REPO = Path(__file__).resolve().parents[1]
VOLATILE_META = ("build_id", "created_at", "timings")


# ---- 用意 ----

def _build(ws: Path, corpus: Path, extra: str = "", **kw) -> dict:
    return build_index(load_config(write_config(ws, corpus, extra)), progress=False, log=lambda s: None, **kw)


def _dump(ws: Path) -> dict:
    """索引の中身のうち、作るたびに変わるもの(作った日時・印・時間)を除いた全部(test_chunk_reuse と同じ)。"""
    with closing(sqlite3.connect(str(ws / ".cf" / "index.db"))) as db:
        out = {
            "files": db.execute("SELECT id, source_id, path, size, lines, language, encoding, chunks, fallback_lines "
                                "FROM files ORDER BY id").fetchall(),
            "chunks": db.execute("SELECT * FROM chunks ORDER BY id").fetchall(),
            "file_chunking": db.execute("SELECT * FROM file_chunking ORDER BY file_id").fetchall(),
            "vectors": db.execute("SELECT * FROM vectors ORDER BY block").fetchall(),
        }
        db.execute("CREATE VIRTUAL TABLE temp.vrow USING fts5vocab(main, 'chunks_fts', 'row')")
        db.execute("CREATE VIRTUAL TABLE temp.vinst USING fts5vocab(main, 'chunks_fts', 'instance')")
        out["fts_row"] = db.execute("SELECT * FROM temp.vrow ORDER BY term").fetchall()
        out["fts_instance"] = db.execute("SELECT * FROM temp.vinst ORDER BY term, doc, col, offset").fetchall()
        meta = dict(db.execute("SELECT key, value FROM meta"))
    for k in VOLATILE_META:
        meta.pop(k, None)
    out["meta"] = meta
    return out


def _build_id(ws: Path) -> str:
    with closing(sqlite3.connect(str(ws / ".cf" / "index.db"))) as db:
        return db.execute("SELECT value FROM meta WHERE key = 'build_id'").fetchone()[0]


def _edit(corpus: Path) -> None:
    p = corpus / "engine" / "Core" / "JobScheduler.cpp"
    p.write_text(p.read_text(encoding="utf-8").replace("Pick a random victim", "Pick a victim at random"),
                 encoding="utf-8")
    (corpus / "docs" / "added-note.md").write_text(
        "# Added note\n\nThe streaming system loads texture mips progressively.\n", encoding="utf-8")
    (corpus / "app" / "Input" / sorted(x.name for x in (corpus / "app" / "Input").iterdir())[0]).unlink()
    touched = corpus / "engine" / "Render" / "BufferPool.h"
    st = touched.stat()
    os.utime(touched, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))


@pytest.fixture
def auto_on(monkeypatch):
    """自動の更新を、テストの既定(切)から入に戻す。"""
    monkeypatch.delenv("CF_AUTO_INDEX", raising=False)
    monkeypatch.delenv("CF_AUTO_INDEX_LOCKED", raising=False)


@pytest.fixture
def built(tmp_path, corpus_copy):
    """索引を作った作業フォルダと、その設定。"""
    ws = tmp_path / "ws"
    _build(ws, corpus_copy)
    return ws, load_config(ws / ".cf" / "config.toml"), corpus_copy


@pytest.fixture
def sleeper():
    """生きているプロセス(pid を使う)。終わりに止める。"""
    procs: list[subprocess.Popen] = []

    def start() -> subprocess.Popen:
        p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
        procs.append(p)
        return p

    yield start
    for p in procs:
        p.kill()
        p.wait()


def dead_pid() -> int:
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    return p.pid


# ---- 走らせる(autoindex.run) ----

def test_unchanged_corpus_is_not_rewritten(built, auto_on):
    ws, cfg, corpus = built
    before = _build_id(ws)
    reason, checked = autoindex.check_current(cfg)
    assert reason is None and checked > 0
    assert autoindex.run(cfg) == "skipped"
    assert _build_id(ws) == before
    st = autoindex.read_state(cfg)
    assert st["state"] == "done" and st["result"] == "skipped" and st["seconds"] >= 0
    assert not (ws / ".cf" / autoindex.LOCK_NAME).exists()       # ロックは返した
    assert not list((ws / ".cf").glob("index.db.tmp-*"))


def test_changed_corpus_is_updated_and_equals_a_full_build(built, auto_on, tmp_path):
    ws, cfg, corpus = built
    before = _build_id(ws)
    _edit(corpus)
    reason, _ = autoindex.check_current(cfg)
    assert reason is not None
    assert autoindex.run(cfg) == "updated"
    assert _build_id(ws) != before
    assert autoindex.check_current(cfg)[0] is None               # 更新のあとは、合っている
    full = tmp_path / "full"
    _build(full, corpus, reuse=False)
    mine, ref = _dump(ws), _dump(full)
    for key in ref:
        assert mine[key] == ref[key], key
    assert autoindex.run(cfg) == "skipped"                       # 続けて流しても、書き直さない


@pytest.mark.parametrize("change", ["added", "deleted", "mtime", "grown"])
def test_each_kind_of_change_is_noticed(built, auto_on, change):
    ws, cfg, corpus = built
    target = corpus / "engine" / "Render" / "BufferPool.h"
    if change == "added":
        (corpus / "docs" / "new.md").write_text("# new\n", encoding="utf-8")
    elif change == "deleted":
        target.unlink()
    elif change == "mtime":
        st = target.stat()
        os.utime(target, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))
    else:
        with open(target, "ab") as f:
            f.write(b"\n// more\n")
    reason, _ = autoindex.check_current(cfg)
    assert reason is not None


def test_a_changed_config_is_updated(built, auto_on):
    ws, cfg, corpus = built
    before = _build_id(ws)
    new = load_config(write_config(ws, corpus, "[index]\nmax_line_chars = 4000\n"))
    reason, _ = autoindex.check_current(new)
    assert reason and "設定" in reason
    assert autoindex.run(new) == "updated"
    assert _build_id(ws) != before


def test_changing_auto_update_alone_does_not_rewrite_the_index(built, auto_on):
    ws, cfg, corpus = built
    off = load_config(write_config(ws, corpus, "[index]\nauto_update = false\n"))
    assert autoindex.check_current(off)[0] is None


def test_a_live_lock_means_busy_and_is_left_alone(built, auto_on, sleeper):
    ws, cfg, corpus = built
    other = sleeper()
    lock = ws / ".cf" / autoindex.LOCK_NAME
    lock.write_text(str(other.pid), encoding="ascii")
    _edit(corpus)
    before = _build_id(ws)
    assert autoindex.run(cfg) == "busy"
    assert lock.read_text(encoding="ascii") == str(other.pid)
    assert _build_id(ws) == before


def test_a_stale_lock_is_taken_over_and_released(built, auto_on):
    ws, cfg, corpus = built
    lock = ws / ".cf" / autoindex.LOCK_NAME
    lock.write_text(str(dead_pid()), encoding="ascii")
    assert autoindex.run(cfg) == "skipped"
    assert not lock.exists()


def test_a_running_manual_build_means_busy(built, auto_on, sleeper):
    ws, cfg, corpus = built
    other = sleeper()
    tmp = ws / ".cf" / f"index.db.tmp-{other.pid}"
    tmp.write_bytes(b"")
    _edit(corpus)
    before = _build_id(ws)
    assert autoindex.run(cfg) == "busy"
    assert _build_id(ws) == before
    st = autoindex.read_state(cfg)
    assert st["result"] == "busy" and str(other.pid) in st["message"]
    assert not (ws / ".cf" / autoindex.LOCK_NAME).exists()
    # 動いていない pid の残りは、手動の索引づくりとはみなさない
    tmp.unlink()
    (ws / ".cf" / f"index.db.tmp-{dead_pid()}").write_bytes(b"")
    assert autoindex.running_index_build(cfg.index_path) is None


def test_off_by_config_does_nothing(built, auto_on):
    ws, cfg, corpus = built
    off = load_config(write_config(ws, corpus, "[index]\nauto_update = false\n"))
    _edit(corpus)
    before = _build_id(ws)
    assert autoindex.run(off) == "off"
    assert _build_id(ws) == before
    assert autoindex.read_state(off) == {"enabled": False}


def test_a_failure_keeps_the_previous_index(built, auto_on, tmp_path):
    ws, cfg, corpus = built
    before, dump = _build_id(ws), _dump(ws)
    broken = load_config(write_config(ws, tmp_path / "nowhere"))
    assert autoindex.run(broken) == "failed"
    assert _build_id(ws) == before and _dump(ws) == dump
    st = autoindex.read_state(broken)
    assert st["result"] == "failed" and st["message"].endswith("前の索引はそのまま使えます)")
    assert not (ws / ".cf" / autoindex.LOCK_NAME).exists()


def test_a_missing_model_keeps_the_previous_index(built, auto_on, monkeypatch):
    """前の索引に埋め込みがあるとき、モデルが無ければ keyword だけの索引に置き換えずに止まる。"""
    ws, cfg, corpus = built
    monkeypatch.delenv("CF_TEST_EMBEDDER")
    monkeypatch.setenv("CF_HOME", str(ws / "no-home"))
    cfg = load_config(write_config(ws, corpus, "[embedding]\ncache_dir = '" + str(ws / "no-models").replace("\\", "/")
                                   + "'\n"))
    _edit(corpus)
    before = _build_id(ws)
    result = autoindex.run(cfg)
    assert result == "failed" and _build_id(ws) == before
    assert autoindex.read_state(cfg)["result"] == "failed"


def test_a_running_state_of_a_dead_process_reads_as_stopped(built):
    ws, cfg, corpus = built
    (ws / ".cf" / autoindex.STATE_NAME).write_text(
        json.dumps({"state": "running", "pid": dead_pid(), "started": "2026-01-01T00:00:00+0900"}), encoding="utf-8")
    assert autoindex.read_state(cfg)["state"] == "stopped"
    (ws / ".cf" / autoindex.STATE_NAME).write_text("not json", encoding="utf-8")
    assert autoindex.read_state(cfg) is None                     # 壊れていても、例外にしない


def test_the_hook_lock_is_given_back_even_when_off(built, auto_on, monkeypatch):
    ws, cfg, corpus = built
    off = load_config(write_config(ws, corpus, "[index]\nauto_update = false\n"))
    lock = ws / ".cf" / autoindex.LOCK_NAME
    lock.write_text("", encoding="ascii")                        # hook が作って、pid をまだ書いていない
    monkeypatch.setenv(autoindex.LOCKED_ENV, "1")
    assert autoindex.run(off) == "off"
    assert not lock.exists()


# ---- hook(ctxf.py) ----

def load_launcher(monkeypatch, cf_home: Path, plugin: Path):
    monkeypatch.setenv("CF_HOME", str(cf_home))
    monkeypatch.delenv("CF_PYTHON", raising=False)
    spec = importlib.util.spec_from_file_location("ctxf_auto_index_test", plugin / "ctxf.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class FakePopen:
    """subprocess.Popen の代わり。始め方(引数・フラグ・環境)を覚える。"""
    started: list = []
    fail = False

    def __init__(self, argv, **kw):
        if FakePopen.fail:
            raise OSError("cannot start")
        self.argv, self.kw, self.pid = argv, kw, os.getpid()
        FakePopen.started.append(self)


@pytest.fixture
def hook(monkeypatch, tmp_path, auto_on):
    """索引のある作業フォルダと、用意ができている入口。始めたプロセスは FakePopen に溜まる。"""
    plugin = tmp_path / "plugin"
    plugin.mkdir()
    shutil.copy2(REPO / "ctxf.py", plugin / "ctxf.py")
    mod = load_launcher(monkeypatch, tmp_path / "home", plugin)
    monkeypatch.setenv("CF_PYTHON", sys.executable)
    FakePopen.started, FakePopen.fail = [], False
    monkeypatch.setattr(mod, "subprocess", types.SimpleNamespace(Popen=FakePopen, DEVNULL=subprocess.DEVNULL))
    ws = tmp_path / "ws"
    (ws / ".cf").mkdir(parents=True)
    (ws / ".cf" / "config.toml").write_text("[[source]]\nid = 'a'\nroot = '.'\n", encoding="utf-8")
    (ws / ".cf" / "index.db").write_bytes(b"")
    return mod, ws


def _session_start(monkeypatch, mod, cwd, capsys) -> str:
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(json.dumps({"cwd": str(cwd)}).encode("utf-8"))))
    assert mod.session_hint(mcp=True) == 0
    return capsys.readouterr().out


def test_hook_starts_a_detached_process_and_answers(hook, monkeypatch, capsys):
    mod, ws = hook
    out = _session_start(monkeypatch, mod, ws / "sub", capsys)
    assert "additionalContext" in json.loads(out)
    assert len(FakePopen.started) == 1
    child = FakePopen.started[0]
    assert child.argv[:2] == [sys.executable, "-I"] and Path(child.argv[2]).name == "ctxf.py"
    assert child.argv[3:] == ["--run", "auto-index", "--config", str(ws / ".cf" / "config.toml")]
    kw = child.kw
    assert kw["stdin"] == subprocess.DEVNULL and kw["env"]["CF_AUTO_INDEX_LOCKED"] == "1"
    assert kw["stdout"] is kw["stderr"] and kw["stdout"] not in (None, subprocess.PIPE)      # hook につながない
    assert Path(kw["cwd"]) == ws
    if os.name == "nt":
        flags = kw["creationflags"]
        assert flags & mod._CREATE_NO_WINDOW and flags & mod._CREATE_NEW_PROCESS_GROUP
        assert not flags & 0x8                                   # DETACHED_PROCESS は使わない
    else:
        assert kw["start_new_session"] is True
    assert (ws / ".cf" / "auto-index.log").is_file()
    assert (ws / ".cf" / "auto-index.lock").read_text(encoding="ascii") == str(os.getpid())


def test_hook_does_not_start_twice(hook, monkeypatch, capsys):
    mod, ws = hook
    _session_start(monkeypatch, mod, ws, capsys)
    _session_start(monkeypatch, mod, ws, capsys)
    assert len(FakePopen.started) == 1


def test_hook_does_not_start_while_a_lock_is_held(hook, monkeypatch, capsys, sleeper):
    mod, ws = hook
    (ws / ".cf" / "auto-index.lock").write_text(str(sleeper().pid), encoding="ascii")
    out = _session_start(monkeypatch, mod, ws, capsys)
    assert "additionalContext" in json.loads(out) and not FakePopen.started


def test_hook_takes_over_a_stale_lock(hook, monkeypatch, capsys):
    mod, ws = hook
    lock = ws / ".cf" / "auto-index.lock"
    lock.write_text(str(dead_pid()), encoding="ascii")
    _session_start(monkeypatch, mod, ws, capsys)
    assert len(FakePopen.started) == 1 and lock.read_text(encoding="ascii") == str(os.getpid())
    assert not (ws / ".cf" / "auto-index.lock.break").exists()


def test_hook_waits_for_a_lock_that_has_no_pid_yet(hook, monkeypatch, capsys):
    mod, ws = hook
    (ws / ".cf" / "auto-index.lock").write_text("", encoding="ascii")      # 別のセッションが作ったばかり
    _session_start(monkeypatch, mod, ws, capsys)
    assert not FakePopen.started


def test_hook_does_not_start_while_a_manual_build_runs(hook, monkeypatch, capsys, sleeper):
    mod, ws = hook
    (ws / ".cf" / f"index.db.tmp-{sleeper().pid}").write_bytes(b"")
    _session_start(monkeypatch, mod, ws, capsys)
    assert not FakePopen.started and not (ws / ".cf" / "auto-index.lock").exists()
    # 動いていない pid の残りは、気にしない
    for f in (ws / ".cf").glob("index.db.tmp-*"):
        f.unlink()
    (ws / ".cf" / f"index.db.tmp-{dead_pid()}").write_bytes(b"")
    _session_start(monkeypatch, mod, ws, capsys)
    assert len(FakePopen.started) == 1


def test_hook_does_not_start_when_turned_off(hook, monkeypatch, capsys):
    mod, ws = hook
    monkeypatch.setenv("CF_AUTO_INDEX", "0")
    _session_start(monkeypatch, mod, ws, capsys)
    assert not FakePopen.started and not (ws / ".cf" / "auto-index.lock").exists()
    monkeypatch.delenv("CF_AUTO_INDEX")
    (ws / ".cf" / "config.toml").write_text("[index]\nauto_update = false\n", encoding="utf-8")
    _session_start(monkeypatch, mod, ws, capsys)
    assert not FakePopen.started and not (ws / ".cf" / "auto-index.lock").exists()


def test_auto_update_setting_is_read_safely(hook, tmp_path):
    mod, ws = hook
    cfg = tmp_path / "c.toml"

    def enabled(text: str | None) -> bool:
        if text is not None:
            cfg.write_text(text, encoding="utf-8")
        return mod._auto_update_enabled(str(cfg))

    assert mod._auto_update_enabled(str(tmp_path / "missing.toml")) is False     # 読めなければ始めない
    assert enabled("[[source]]\nid='a'\nroot='.'\n") is True                       # 書いていなければ既定の入
    assert enabled("[index]\n# auto_update = false\n") is True                    # 注釈は無視
    assert enabled("[index]\nauto_update = true\n") is True
    assert enabled("[index]\nauto_update = false\n") is False
    assert enabled("[index]\nauto_update = false  # 切る\n") is False
    assert enabled("[index]\nauto_update = 'no'\n") is False                      # 真偽でなければ始めない
    assert enabled("[index\nauto_update = ") is False                              # 壊れていれば始めない


def test_hook_gives_back_the_lock_when_the_process_cannot_start(hook, monkeypatch, capsys):
    mod, ws = hook
    FakePopen.fail = True
    out = _session_start(monkeypatch, mod, ws, capsys)
    assert "additionalContext" in json.loads(out)                  # セッションは止めない
    assert not (ws / ".cf" / "auto-index.lock").exists()
    FakePopen.fail = False
    _session_start(monkeypatch, mod, ws, capsys)
    assert len(FakePopen.started) == 1


def test_hook_does_nothing_without_an_index(hook, monkeypatch, capsys, tmp_path, outside_any_workspace):
    mod, ws = hook
    other = tmp_path / "other"
    other.mkdir()
    assert _session_start(monkeypatch, mod, other, capsys) == "" and not FakePopen.started


def test_hook_does_nothing_without_setup(hook, monkeypatch, capsys):
    mod, ws = hook
    monkeypatch.delenv("CF_PYTHON")
    out = _session_start(monkeypatch, mod, ws, capsys)
    assert "additionalContext" in json.loads(out)                  # 用意が要ることは知らせる
    assert not FakePopen.started and not (ws / ".cf" / "auto-index.lock").exists()


def test_hook_survives_a_broken_input(hook, monkeypatch, capsys):
    mod, ws = hook
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(b"not json")))
    assert mod.session_hint(mcp=True) == 0


# ---- 実際に切り離して流す(別のプロセスで) ----

def _wait_done(ws: Path, started_after: str = "", timeout: float = 180.0) -> dict:
    state_file, lock = ws / ".cf" / "auto-index.json", ws / ".cf" / "auto-index.lock"
    end = time.time() + timeout
    while time.time() < end:
        try:
            st = json.loads(state_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            st = {}
        if st.get("state") == "done" and st.get("started", "") > started_after and not lock.exists():
            return st
        time.sleep(0.2)
    raise AssertionError(f"自動の更新が終わりません: {state_file.read_text(encoding='utf-8') if state_file.exists() else ''}")


def _session_hint_process(ws: Path) -> tuple[subprocess.CompletedProcess, float]:
    env = dict(os.environ, CF_PYTHON=sys.executable, CF_TEST_EMBEDDER="hash",
               PYTHONPATH=os.pathsep.join(filter(None, [str(REPO / "src"), os.environ.get("PYTHONPATH")])))
    env.pop("CF_AUTO_INDEX", None)
    env.pop("CF_AUTO_INDEX_LOCKED", None)
    t0 = time.perf_counter()
    r = subprocess.run([sys.executable, "-I", str(REPO / "ctxf.py"), "session-hint", "--mcp"], capture_output=True,
                       input=json.dumps({"cwd": str(ws)}).encode("utf-8"), env=env, cwd=ws.parent, timeout=60)
    return r, time.perf_counter() - t0


def test_a_real_session_start_updates_the_index_in_the_background(built, auto_on):
    ws, cfg, corpus = built
    first = _build_id(ws)
    r, seconds = _session_hint_process(ws)
    assert r.returncode == 0 and "additionalContext" in json.loads(r.stdout)
    assert seconds < 10                                              # hook の timeoutSec
    st = _wait_done(ws)
    assert st["result"] == "skipped" and _build_id(ws) == first       # 変わっていなければ、書き直さない
    assert (ws / ".cf" / "auto-index.log").is_file()

    _edit(corpus)
    r, seconds = _session_hint_process(ws)
    assert r.returncode == 0 and seconds < 10
    st = _wait_done(ws, started_after=st["started"])
    assert st["result"] == "updated", st
    assert _build_id(ws) != first and autoindex.check_current(cfg)[0] is None


def test_state_file_is_written_even_if_a_reader_blocks_the_replace(built, monkeypatch):
    ws, cfg, corpus = built
    real, calls = os.replace, []

    def flaky(src, dst):
        calls.append(1)
        if len(calls) < 3:
            raise PermissionError("読まれている間は置き換えられない")
        real(src, dst)

    monkeypatch.setattr(os, "replace", flaky)
    autoindex._write_state(cfg, {"state": "done", "result": "skipped"})
    assert json.loads(autoindex.state_path(cfg).read_text(encoding="utf-8"))["state"] == "done"


# ---- status ----

def test_status_shows_the_last_auto_update(built, auto_on):
    from context_foundation import mcp_server

    ws, cfg, corpus = built
    assert autoindex.run(cfg) == "skipped"
    env = dict(os.environ, CF_TEST_EMBEDDER="hash", PYTHONIOENCODING="utf-8",
               PYTHONPATH=os.pathsep.join(filter(None, [str(REPO / "src"), os.environ.get("PYTHONPATH")])))

    def cli(*fmt):
        r = subprocess.run([sys.executable, "-m", "context_foundation", "status", *fmt], cwd=ws, env=env,
                           capture_output=True, text=True, encoding="utf-8", timeout=120)
        assert r.returncode == 0, r.stderr
        return r.stdout

    agent = cli("--format", "agent")
    assert "last auto update" in agent and "no file changes, index kept" in agent
    assert "最後の自動の更新" in cli("--format", "human")
    assert json.loads(cli("--format", "json"))["auto_update"]["result"] == "skipped"
    is_error, text = mcp_server.call_tool(mcp_server._State(str(ws / ".cf" / "config.toml")), "status", {})
    assert not is_error and "last auto update" in text and len(text.encode("utf-8")) < HARD_MAX_BYTES


def test_status_says_when_it_is_off_or_never_ran(built):
    from context_foundation import mcp_server

    ws, cfg, corpus = built
    config = str(ws / ".cf" / "config.toml")
    _, text = mcp_server.call_tool(mcp_server._State(config), "status", {})
    assert "auto update" not in text                                  # 一度も走っていなければ、何も出さない
    write_config(ws, corpus, "[index]\nauto_update = false\n")
    _, text = mcp_server.call_tool(mcp_server._State(config), "status", {})
    assert "auto update at session start: off" in text
