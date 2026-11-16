"""context-foundation の入口(標準ライブラリだけで動く)。

  python -I ctxf.py setup [--no-model]   初回の用意: 依存の入った仮想環境と、埋め込みモデル
  python -I ctxf.py search "<質問>"      検索(init / index / status などのコマンドも同じ形で渡す)
  python -I ctxf.py session-hint         Copilot の sessionStart の hook 用(標準入力に JSON)
  python -I ctxf.py where                使う python と置き場を出す

Python の依存(約 200 MB)は、次のどちらかに置く。プラグインの中には置かない(プラグインを更新しても、
依存が変わらなければ作り直さないため。プラグインのフォルダは、更新のたびに置き換わる)。

  1. この入口の隣の .venv            リポジトリを取得して使う人(setup.bat が作る)
  2. 利用者ごとの置き場の runtime      プラグインとして入れた人(`ctxf.py setup` が作る)。
                                        %LOCALAPPDATA%\\context-foundation\\runtime(環境変数 CF_HOME で変えられる)

パッケージのソースは、この入口の隣の src から読む(プラグインを更新すると、そのまま新しいソースで動く)。
2 の仮想環境は、作ったときの依存の一覧(pyproject.toml の dependencies)を覚えておき、一覧が変わったら
「もう一度 setup を」と知らせる。環境変数 CF_PYTHON で、使う python を差し替えられる。
"""
import hashlib
import json
import os
import re
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, "src")
PYPROJECT = os.path.join(HERE, "pyproject.toml")
MIN_PYTHON = (3, 11)
SETUP_SIZE_NOTE = "about 200 MB of Python packages from PyPI and a 490 MB embedding model from Hugging Face"


def data_home():
    env = os.environ.get("CF_HOME")
    if env:
        return env
    if os.name == "nt" and os.environ.get("LOCALAPPDATA"):
        return os.path.join(os.environ["LOCALAPPDATA"], "context-foundation")
    base = os.environ.get("XDG_DATA_HOME") or os.path.join(os.path.expanduser("~"), ".local", "share")
    return os.path.join(base, "context-foundation")


def runtime_dir():
    return os.path.join(data_home(), "runtime")


def _venv_python(venv):
    for rel in (os.path.join("Scripts", "python.exe"), os.path.join("bin", "python")):
        p = os.path.join(venv, rel)
        if os.path.isfile(p):
            return p
    return None


def dependencies():
    """pyproject.toml の [project] dependencies(依存の一覧)。読めなければ None。"""
    try:
        import tomllib
    except ImportError:          # Python 3.10 以前: この入口だけは動かせるよう、配列を簡単に読む
        try:
            with open(PYPROJECT, encoding="utf-8") as f:
                text = f.read()
        except OSError:
            return None
        m = re.search(r"^dependencies\s*=\s*\[(.*?)^\]", text, re.S | re.M)
        return re.findall(r'"([^"]+)"', re.sub(r"#[^\n]*", "", m.group(1))) if m else None
    try:
        with open(PYPROJECT, "rb") as f:
            return list(tomllib.load(f)["project"]["dependencies"])
    except (OSError, KeyError, ValueError):
        return None


def deps_hash(deps):
    return hashlib.sha256(json.dumps(sorted(deps), ensure_ascii=False).encode("utf-8")).hexdigest()[:16]


def _stamp_path():
    return os.path.join(runtime_dir(), "deps.json")


def _read_stamp():
    try:
        with open(_stamp_path(), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def find_python():
    """(python のパス, どこのものか, 理由)。使える python が無ければ、パスは None で、理由に次にすべきことを入れる。"""
    env = os.environ.get("CF_PYTHON")
    if env:
        return env, "CF_PYTHON", ""
    local = _venv_python(os.path.join(HERE, ".venv"))
    if local:
        return local, "local .venv", ""
    py = _venv_python(os.path.join(runtime_dir(), "venv"))
    if py is None:
        return None, "", "not set up"
    stamp = _read_stamp()
    if not stamp.get("deps_hash"):
        return None, "", "not set up completely (the last setup did not finish)"
    deps = dependencies()
    if deps is None or stamp["deps_hash"] != deps_hash(deps):
        return None, "", "set up for an older version (its dependencies changed)"
    return py, "runtime", ""


def setup_command():
    return 'python -I "' + os.path.join(HERE, "ctxf.py") + '" setup'


def not_ready_message(reason):
    return ("context-foundation is " + reason + " on this PC. Search with grep meanwhile. To set it up, ask the user "
            "first (it downloads " + SETUP_SIZE_NOTE + " and takes a few minutes), then run: " + setup_command() + "\n")


SESSION_HINT = (
    "This workspace has a context-foundation index (.cf folder: the code and design docs, searchable by meaning "
    "and keywords). When you need to find where something is implemented or documented and do not know its exact "
    "name or file, load the context-search skill before grepping. For a known identifier, grep as usual.")
SESSION_HINT_NOT_READY = (
    "This workspace has a context-foundation index (.cf folder), but the search tool is not set up on this PC yet. "
    "If you need to find code or docs without knowing their names, load the context-search skill: it says how to "
    "set the tool up (ask the user first).")


def has_index(cwd):
    """cwd かその親に、索引(.cf/index.db)があるか。"""
    d = os.path.abspath(cwd) if isinstance(cwd, str) and cwd else None
    while d:
        if os.path.isfile(os.path.join(d, ".cf", "index.db")):
            return True
        parent = os.path.dirname(d)
        if parent == d:
            return False
        d = parent
    return False


def session_hint():
    """Copilot の sessionStart の hook から呼ばれる。索引のある作業フォルダでだけ、skill の使いどころを 1 文で
    知らせる(説明文だけでは、skill を読まずに grep から始めるモデルがあるため)。索引が無ければ何も足さない。
    仮想環境の python は起動しない(セッションの始まりを遅くしないため)。何が起きても 0 で終わる。"""
    try:
        raw = sys.stdin.buffer.read() if sys.stdin is not None else b""
        try:
            data = json.loads(raw.decode("utf-8", "replace") or "{}")
        except ValueError:
            data = {}
        cwd = data.get("cwd") if isinstance(data, dict) else None
        if has_index(cwd or os.getcwd()):
            ready = find_python()[0] is not None
            sys.stdout.write(json.dumps({"additionalContext": SESSION_HINT if ready else SESSION_HINT_NOT_READY}))
    except BaseException:        # hook はセッションを止めない
        pass
    return 0


def _say(text):
    sys.stdout.write(text + "\n")
    sys.stdout.flush()


SETUP_USAGE = """usage: python -I ctxf.py setup [--no-model]

Creates a virtual environment with the Python packages (about 200 MB from PyPI) under
the runtime folder, and downloads the embedding model (about 490 MB from Hugging Face).
--no-model skips the model (keyword-only search). Ask the user before running it.
"""
IMPORT_CHECK = "import numpy, onnxruntime, tokenizers, tree_sitter, jsonschema"
LOCK_STALE_SECONDS = 6 * 3600        # プロセスの生死が分からないときに、古いとみなすまでの時間


def _pid_alive(pid):
    """そのプロセスが動いているか。分からなければ None。"""
    if pid <= 0:
        return False
    if os.name == "nt":
        try:
            import ctypes
            k32 = ctypes.windll.kernel32
            h = k32.OpenProcess(0x1000, False, pid)       # PROCESS_QUERY_LIMITED_INFORMATION
            if not h:
                return False
            try:
                code = ctypes.c_ulong()
                if not k32.GetExitCodeProcess(h, ctypes.byref(code)):
                    return None
                return code.value == 259                  # STILL_ACTIVE
            finally:
                k32.CloseHandle(h)
        except (AttributeError, OSError):
            return None
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except OSError:
        return None


def _lock_is_stale(lock):
    try:
        with open(lock, encoding="ascii", errors="replace") as f:
            pid = int(f.read().strip() or "0")
    except (OSError, ValueError):
        pid = 0
    alive = _pid_alive(pid)
    if alive is False:
        return True
    return alive is None and time.time() - os.path.getmtime(lock) > LOCK_STALE_SECONDS


def _write_stamp(deps):
    os.makedirs(runtime_dir(), exist_ok=True)
    tmp = _stamp_path() + ".tmp-%d" % os.getpid()
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"deps_hash": deps_hash(deps), "dependencies": deps,
                   "python": sys.version.split()[0]}, f, ensure_ascii=False, indent=1)
    os.replace(tmp, _stamp_path())


def _healthy(py, check):
    try:
        return subprocess.call([py, "-I", "-c", check], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) == 0
    except OSError:
        return False


def setup(argv):
    """利用者ごとの置き場に、依存の入った仮想環境を作り(依存が変わっていれば入れ直す)、埋め込みモデルを取得する。
    引数を検査してから始める(--help で、何も取得せずに説明だけ出す)。2 つの setup が同時に走らないよう排他する。"""
    if any(a in ("-h", "--help") for a in argv):
        sys.stdout.write(SETUP_USAGE)
        return 0
    unknown = [a for a in argv if a != "--no-model"]
    if unknown:
        sys.stdout.write("unknown option: " + " ".join(unknown) + "\n" + SETUP_USAGE)
        return 2
    os.makedirs(runtime_dir(), exist_ok=True)
    lock = os.path.join(runtime_dir(), "setup.lock")
    try:
        if os.path.exists(lock) and _lock_is_stale(lock):
            os.remove(lock)                  # 前の setup が途中で止まった(打ち切られた)ときの残り
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        _say("Another setup is running (" + lock + "). Wait for it to finish, then run setup again. "
             "If no setup is running, delete that file.")
        return 3
    try:
        os.write(fd, str(os.getpid()).encode("ascii"))
        os.close(fd)
        return _setup(argv)
    finally:
        try:
            os.remove(lock)
        except OSError:
            pass


def _setup(argv):
    want_model = "--no-model" not in argv
    if sys.version_info < MIN_PYTHON:
        _say("Python %d.%d or later is required (this is %s). Install it from https://www.python.org/downloads/ "
             "and run setup again." % (MIN_PYTHON + (sys.version.split()[0],)))
        return 2
    deps = dependencies()
    if not deps:
        _say("Cannot read the dependency list from " + PYPROJECT)
        return 2
    rt = runtime_dir()
    venv = os.path.join(rt, "venv")
    _say("== context-foundation setup")
    _say("  runtime folder: " + rt)
    py = _venv_python(venv)
    if py is not None and not _healthy(py, "import pip"):
        _say("  the virtual environment is broken (an earlier setup stopped, or its Python was removed); recreating ...")
        py = None
    if py is None:
        _say("  creating the virtual environment ...")
        try:
            os.remove(_stamp_path())
        except OSError:
            pass
        r = subprocess.call([sys.executable, "-m", "venv", "--clear", venv])
        py = _venv_python(venv)
        if r != 0 or py is None:
            _say("  could not create the virtual environment (exit code %s)" % r)
            return 1
    if _read_stamp().get("deps_hash") == deps_hash(deps) and _healthy(py, IMPORT_CHECK):
        _say("  Python packages: already installed")
    else:
        _say("  installing the Python packages from PyPI (about 200 MB the first time) ...")
        r = subprocess.call([py, "-m", "pip", "install", "--quiet", "--disable-pip-version-check", *deps])
        if r != 0 or not _healthy(py, IMPORT_CHECK):
            _say("  pip failed (exit code %s). Check the network or the proxy settings, then run setup again." % r)
            return 1
        _write_stamp(deps)
        _say("  Python packages: installed")
    if want_model:
        _say("  embedding model: checking (downloads about 490 MB the first time) ...")
        r = subprocess.call([py, "-I", os.path.abspath(__file__), "--run", "model", "download"])
        if r != 0:
            _say("  the model download failed; search still works keyword-only. Run setup again to retry.")
            return 1
    _say("Setup is done. Next, build an index in the workspace: "
         'python -I "' + os.path.join(HERE, "ctxf.py") + '" init <folder> [<folder> ...], then the same with index')
    return 0


def where():
    py, kind, reason = find_python()
    _say("ctxf.py:   " + os.path.abspath(__file__))
    _say("python:    " + (py + "  (" + kind + ")" if py else "none: " + reason))
    _say("runtime:   " + runtime_dir())
    _say("setup:     " + setup_command())
    return 0 if py else 4


def run_inner(argv):
    """仮想環境の python で動いているとき: 隣の src からパッケージを読み、CLI を動かす。"""
    sys.path.insert(0, SRC)
    from context_foundation.cli import main as cli_main
    return cli_main(argv)


def main():
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    argv = sys.argv[1:]
    if argv[:1] == ["--run"]:
        return run_inner(argv[1:])
    if argv[:1] == ["session-hint"]:
        return session_hint()
    if argv[:1] == ["setup"]:
        return setup(argv[1:])
    if argv[:1] == ["where"]:
        return where()
    py, _kind, reason = find_python()
    if py is None:
        sys.stdout.write(not_ready_message(reason))
        return 4
    try:
        return subprocess.call([py, "-I", os.path.abspath(__file__), "--run"] + argv)
    except OSError as e:
        sys.stdout.write("context-foundation could not start (" + str(e) + "). Search with grep meanwhile.\n")
        return 4


if __name__ == "__main__":
    sys.exit(main())
