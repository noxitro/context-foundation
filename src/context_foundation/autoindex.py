"""セッションの始まりの差分更新(0.4.6 から)。

Copilot のセッションが始まると、hook(入口 ctxf.py の session-hint)が、索引のある作業フォルダでだけ、
`ctxf auto-index` を切り離したプロセスで始める(hook はすぐに返る)。ここでは:

  1. 安い確かめ: 名前の決まり(include・exclude)を通ったファイルを、大きさと更新日時だけで歩き(中身は読まない)、
     索引の files 表と skipped_files 表に比べる。設定・道具の版・分割の作り方も比べる。何も変わっていなければ、
     索引を書き直さずに終わる
  2. 変わっていれば、`ctxf index` と同じ差分更新(中身の同じファイルの passage と埋め込みを、前の索引から写す)。
     途中のファイルに書いて最後に置き換えるので、検索中に流しても安全。止まったときは、前の索引をそのまま残す
  3. 結果を .cf/auto-index.json に残す(status に出す)。経過は .cf/auto-index.log(hook が毎回書き直す)

同時に 1 つだけ: .cf/auto-index.lock(O_EXCL で作り、中に pid を書く。hook が作って、このプロセスに渡す)。
手動の索引づくり(<索引>.tmp-<pid> のプロセスが生きている)の間は始めない。何が起きても例外を外に出さない
(セッションを止めない)。
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
from contextlib import closing
from pathlib import Path

from . import __version__
from .config import Config

LOCK_NAME = "auto-index.lock"
LOG_NAME = "auto-index.log"
STATE_NAME = "auto-index.json"
# hook がロックを取って、このプロセスを始めたときに立てる環境変数(ロックを取り直さず、終わりに消す)
LOCKED_ENV = "CF_AUTO_INDEX_LOCKED"
# ロックの決まり(入口 ctxf.py の _auto_index_lock_held と同じ)。中身(pid)をまだ書いていないロックは、この秒数
# までは使われているとみなす。pid の生死が分からないときと、pid が生きていても(別のプロセスが同じ pid を使って
# いることがある)、この秒数より古いロックは古いとみなす
EMPTY_LOCK_GRACE_SECONDS = 60
UNKNOWN_LOCK_STALE_SECONDS = 6 * 3600
LOCK_MAX_AGE_SECONDS = 24 * 3600
MESSAGE_MAX_CHARS = 200


def _say(s: str) -> None:
    """経過を書く(hook から始めたときは、標準エラーが .cf/auto-index.log)。"""
    try:
        sys.stderr.write(f"[{time.strftime('%H:%M:%S')}] {s}\n")
        sys.stderr.flush()
    except (OSError, ValueError):
        pass


def _short(s: str) -> str:
    s = (s.strip().splitlines() or [""])[0]
    return s if len(s) <= MESSAGE_MAX_CHARS else s[:MESSAGE_MAX_CHARS - 1] + "…"


# ---- ロック ----

def _lock_pid(text: str) -> int | None:
    text = text.strip()
    return int(text) if text.isascii() and text.isdigit() and len(text) <= 10 else None


def lock_is_stale(path: Path) -> bool:
    """ロックが古い(作ったプロセスがもう無い)か。読めなければ古いとはみなさない(消さない)。"""
    from .sysinfo import pid_alive

    try:
        age = time.time() - path.stat().st_mtime
        text = path.read_text(encoding="ascii", errors="replace")
    except OSError:
        return False
    pid = _lock_pid(text)
    if pid is None:
        return age > EMPTY_LOCK_GRACE_SECONDS
    if age > LOCK_MAX_AGE_SECONDS:
        return True
    alive = pid_alive(pid)
    return alive is False or (alive is None and age > UNKNOWN_LOCK_STALE_SECONDS)


def _take_lock(path: Path) -> bool:
    """ロックを O_EXCL で作り、この pid を書く。古いロックは、消す役の印(<ロック>.break。これも O_EXCL)を取った
    ときだけ、もう一度確かめてから消して作り直す(入口 ctxf.py の _take_auto_index_lock と同じ)。"""
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    try:
        fd = os.open(path, flags, 0o644)
    except FileExistsError:
        fd = None
    except OSError:
        return False
    if fd is None:
        if not lock_is_stale(path):
            return False
        breaker = path.with_name(path.name + ".break")
        try:
            os.close(os.open(breaker, flags, 0o644))
        except FileExistsError:
            try:
                if time.time() - breaker.stat().st_mtime > EMPTY_LOCK_GRACE_SECONDS:
                    breaker.unlink()
            except OSError:
                pass
            return False
        except OSError:
            return False
        try:
            if path.exists() and not lock_is_stale(path):
                return False
            path.unlink(missing_ok=True)
            fd = os.open(path, flags, 0o644)
        except OSError:
            return False
        finally:
            try:
                breaker.unlink()
            except OSError:
                pass
    try:
        os.write(fd, str(os.getpid()).encode("ascii"))
    finally:
        os.close(fd)
    return True


def release_hook_lock(cf_dir: Path) -> None:
    """hook がロックを取って始めたのに、設定を読めずに run まで来なかったとき、ロックを返す。"""
    if os.environ.get(LOCKED_ENV) == "1":
        _release_lock(cf_dir / LOCK_NAME, True)


def _release_lock(path: Path, from_hook: bool) -> None:
    """自分のロック(中の pid がこのプロセスか、hook が始めた親(Windows の仮想環境の python.exe は、本体を子として
    始める))だけを消す。"""
    try:
        text = path.read_text(encoding="ascii", errors="replace")
    except OSError:
        return
    pid = _lock_pid(text)
    mine = {os.getpid()}
    if hasattr(os, "getppid"):
        mine.add(os.getppid())
    if (pid in mine) or (pid is None and not text.strip() and from_hook):
        try:
            path.unlink()
        except OSError:
            pass


def running_index_build(dest: Path) -> int | None:
    """手動の索引づくり(<索引>.tmp-<pid> のプロセスが生きている)があれば、その pid。"""
    from .sysinfo import pid_alive

    head = dest.name + ".tmp-"
    try:
        names = [p.name for p in dest.parent.iterdir() if p.name.startswith(head)]
    except OSError:
        return None
    for name in names:
        pid = _lock_pid(name[len(head):])
        if pid is None or pid == os.getpid():
            continue
        if pid_alive(pid) is not False:        # 分からないときも、動いているとみなす(_remove_dead_tmp と同じ)
            return pid
    return None


# ---- 安い確かめ ----

def check_current(cfg: Config) -> tuple[str | None, int]:
    """索引が、今のファイルと設定に合っているか。合っていれば (None, 確かめたファイルの数)、合っていなければ
    (理由, 確かめたファイルの数)。中身は読まない(大きさと更新日時だけ)。

    名前の決まりを通ったファイルの集まりが、索引の files 表と skipped_files 表(大きさや中身で除いたファイル)を
    合わせたものと、パス・大きさ・更新日時まで同じなら、合っている。中身を読まないと決まらない規則(生成物・
    バイナリ・空など)で除いたファイルも、大きさと更新日時を比べるので、変われば更新する(取りこぼさない側)。
    読めずに除いたファイルは、開けるようになっていれば更新する。迷うもの(表が無い古い索引・置き換えられずに
    残った索引・stat できないファイル)は、合っていないとみなす。"""
    from .indexer import _pending_path, chunking_signature, config_digest
    from .ingest import IngestStats, iter_candidates
    from .store import IndexError_, open_readonly, read_meta

    dest = cfg.index_path
    if not dest.is_file():
        return "索引がありません", 0
    if _pending_path(dest).is_file():
        return "置き換えられなかった索引(.new)があります", 0
    known: dict[tuple[str, str], tuple] = {}
    try:
        with closing(open_readonly(dest)) as db:
            meta = read_meta(db)
            if meta.get("tool_version") != __version__:
                return f"道具の版が変わりました({meta.get('tool_version')} → {__version__})", 0
            if meta.get("config_digest") != config_digest(cfg):
                return "設定(config.toml)が変わりました", 0
            signature = json.loads(json.dumps(chunking_signature(cfg)))
            if meta.get("chunking") != signature:
                return "分割の作り方が変わりました", 0
            for sid, path, size, mtime_ns in db.execute("SELECT source_id, path, size, mtime_ns FROM files"):
                known[(sid, path)] = (size, mtime_ns, "")
            for sid, path, size, mtime_ns, reason in db.execute(
                    "SELECT source_id, path, size, mtime_ns, reason FROM skipped_files"):
                known[(sid, path)] = (size, mtime_ns, reason)
    except (IndexError_, sqlite3.Error, ValueError) as e:
        return f"索引を読めません({_short(str(e))})", 0

    checked = 0
    for src in cfg.sources:
        try:
            for c in iter_candidates(src, IngestStats(), cfg.sources, count_excluded=False):
                checked += 1
                got = known.pop((c.source_id, c.path), None)
                where = f"{c.source_id}:{c.path}"
                if got is None:
                    return f"増えたファイルがあります({where} など)", checked
                if c.size is None and got[0] is None and got[2] == "unreadable":
                    continue                # 前も今も stat できない
                if c.size is None or got[0] is None or (c.size, c.mtime_ns) != got[:2]:
                    return f"変わったファイルがあります({where} など)", checked
                if got[2] == "unreadable":
                    try:
                        with open(c.os_path, "rb"):
                            pass
                    except OSError:
                        continue
                    return f"読めなかったファイルが読めるようになりました({where} など)", checked
        except OSError as e:          # ルートが無い(FileNotFoundError)など
            return f"source {src.id} を歩けません({_short(str(e))})", checked
    if known:
        sid, path = next(iter(known))
        return f"消えたファイルがあります({sid}:{path} など)", checked
    return None, checked


# ---- 状態 ----

def state_path(cfg: Config) -> Path:
    return cfg.cf_dir / STATE_NAME


def _write_state(cfg: Config, state: dict) -> None:
    p = state_path(cfg)
    tmp = p.with_name(p.name + f".tmp-{os.getpid()}")
    try:
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
        for attempt in range(20):        # Windows では、status などが読んでいる間は置き換えに失敗する
            try:
                os.replace(tmp, p)
                return
            except PermissionError:
                if attempt == 19:
                    raise
                time.sleep(0.05)
    except OSError:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


def read_state(cfg: Config) -> dict | None:
    """status に出す、自動の更新の様子。設定で切ってあれば enabled が偽。一度も走っていなければ None
    (切ってあるときを除く)。走っている途中で止められたものは state を stopped にする。例外は出さない
    (status を止めない)。"""
    try:
        return _read_state(cfg)
    except Exception:
        return None


def _read_state(cfg: Config) -> dict | None:
    from .sysinfo import pid_alive

    enabled = bool(cfg.index.auto_update)
    try:
        raw = json.loads(state_path(cfg).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raw = None
    if not isinstance(raw, dict):
        return None if enabled else {"enabled": False}
    out = {"enabled": enabled}
    for k in ("state", "result", "message", "started", "finished", "seconds", "pid"):
        if raw.get(k) is not None:
            out[k] = raw[k]
    if out.get("state") == "running":
        pid = out.get("pid")
        if not isinstance(pid, int) or pid_alive(pid) is False:
            out["state"] = "stopped"
    return out


# ---- 走らせる ----

def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def run(cfg: Config) -> str:
    """自動の差分更新を 1 回流す。結果(skipped・updated・failed・busy・off)を返す。例外は外に出さない。"""
    from_hook = os.environ.get(LOCKED_ENV) == "1"
    lock = cfg.cf_dir / LOCK_NAME
    if not cfg.index.auto_update:
        _say("設定で切ってあります([index] auto_update = false)。何もしません")
        if from_hook:
            _release_lock(lock, from_hook)
        return "off"
    if not from_hook and not _take_lock(lock):
        _say(f"ほかの自動の更新が動いています({lock})。何もしません")
        return "busy"
    if os.name != "nt":
        try:
            os.nice(10)                  # セッションの邪魔をしない(Windows は hook が低い優先度で始める)
        except (AttributeError, OSError):
            pass
    t0 = time.time()
    state = {"state": "running", "pid": os.getpid(), "started": _now()}
    result, message = "failed", ""
    try:
        _write_state(cfg, state)
        _say(f"自動の差分更新を始めます: {cfg.config_path}")
        pid = running_index_build(cfg.index_path)
        if pid is not None:
            result, message = "busy", f"手動の索引づくり(pid {pid})が動いていたので、始めませんでした"
        else:
            reason, checked = check_current(cfg)
            if reason is None:
                result = "skipped"
                message = f"変わったファイルはありません({checked:,} ファイルを確かめました)"
            else:
                _say(f"更新します: {reason}")
                message = _update(cfg, reason)
                result = "updated"
    except BaseException as e:           # 何が起きても止めない(前の索引はそのまま残る)
        message = _short(str(e) or type(e).__name__) + "(前の索引はそのまま使えます)"
    finally:
        seconds = round(time.time() - t0, 1)
        state.update({"state": "done", "finished": _now(), "seconds": seconds, "result": result,
                      "message": message})
        _say(f"{result}: {message}({seconds} 秒)")
        _write_state(cfg, state)
        _release_lock(lock, from_hook)
    return result


def _update(cfg: Config, reason: str) -> str:
    """差分更新。前の索引に埋め込みが無ければ、keyword だけで作る(勝手に長い埋め込みを始めない)。
    前の索引に埋め込みがあってモデルが無ければ、索引を書く前に止まる(ModelUnavailable。前の索引は残る)。"""
    from .indexer import build_index, previous_had_embeddings

    use_embedding = None if previous_had_embeddings(cfg.index_path) else False
    res = build_index(cfg, use_embedding=use_embedding, require_embedding=False, progress=False, log=_say,
                      reuse=True)
    st, tm = res["stats"], res["timings"]
    return (f"{reason}。{st['files']:,} ファイル(分け直し {tm.get('chunked_files', 0):,}・"
            f"埋め込み {tm.get('embedded_passages', 0):,} passage)で作り直しました")
