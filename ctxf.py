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
import subprocess
import sys

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
    except ImportError:          # Python 3.10 以前
        return None
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
    deps = dependencies()
    if deps is not None and _read_stamp().get("deps_hash") != deps_hash(deps):
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
        data = json.loads(sys.stdin.buffer.read().decode("utf-8", "replace") or "{}")
    except ValueError:
        data = {}
    cwd = data.get("cwd") if isinstance(data, dict) else None
    try:
        if has_index(cwd or os.getcwd()):
            ready = find_python()[0] is not None
            sys.stdout.write(json.dumps({"additionalContext": SESSION_HINT if ready else SESSION_HINT_NOT_READY}))
    except Exception:            # hook はセッションを止めない
        pass
    return 0


def _say(text):
    sys.stdout.write(text + "\n")
    sys.stdout.flush()


def setup(argv):
    """利用者ごとの置き場に、依存の入った仮想環境を作り(依存が変わっていれば入れ直す)、埋め込みモデルを取得する。"""
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
    if py is None:
        _say("  creating the virtual environment ...")
        r = subprocess.call([sys.executable, "-m", "venv", venv])
        py = _venv_python(venv)
        if r != 0 or py is None:
            _say("  could not create the virtual environment (exit code %s)" % r)
            return 1
    if _read_stamp().get("deps_hash") == deps_hash(deps):
        _say("  Python packages: already installed")
    else:
        _say("  installing the Python packages from PyPI (about 200 MB the first time) ...")
        r = subprocess.call([py, "-m", "pip", "install", "--quiet", "--disable-pip-version-check", *deps])
        if r != 0:
            _say("  pip failed (exit code %s). Check the network or the proxy settings, then run setup again." % r)
            return 1
        os.makedirs(rt, exist_ok=True)
        with open(_stamp_path(), "w", encoding="utf-8") as f:
            json.dump({"deps_hash": deps_hash(deps), "dependencies": deps,
                       "python": sys.version.split()[0]}, f, ensure_ascii=False, indent=1)
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
