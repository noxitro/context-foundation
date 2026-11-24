"""context-foundation の入口(標準ライブラリだけで動く)。

  python -I ctxf.py setup                初回の用意: 依存の入った仮想環境(埋め込みモデルは入れない。有無だけを出す)
  python -I ctxf.py search "<質問>"      検索(init / index / status などのコマンドも同じ形で渡す)
  python -I ctxf.py session-hint         Copilot の sessionStart の hook 用(標準入力に JSON)
  python -I ctxf.py where                使う python と置き場、モデル、索引を出す(うまく動かないときの確認)

埋め込みモデル(Hugging Face の OSS)は、setup では取得しない。使ってよいかは利用者の環境(ライセンス・利用の条件・
ネットワーク)で確かめる必要があるので、setup は「インストールされていません」と入手先を出すだけに
する。取得は、利用者が確かめたうえで `model download` を自分で実行する。pip の依存は setup が入れる。

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
# 確かめた最も新しい版。3.15 には onnxruntime の配布物(wheel)がまだ無く、pip が入れられない。
# onnxruntime が出したら、CI の版の並び(.github/workflows/ci.yml)と pyproject の requires-python と一緒に上げる
MAX_PYTHON = (3, 14)
SETUP_SIZE_NOTE = "about 200 MB of Python packages from PyPI"


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


def _base_python_missing(venv):
    """仮想環境を作った python(pyvenv.cfg の home)が、もう無いか(別の版に置き換わったときも含む)。読めない・
    書き方が違うときは False(今までどおり使えるものとして扱う)。プロセスは起こさない(hook を遅くしないため)。"""
    try:
        with open(os.path.join(venv, "pyvenv.cfg"), encoding="utf-8", errors="replace") as f:
            lines = f.read().splitlines()
    except OSError:
        return False
    cfg = {}
    for line in lines:
        key, sep, value = line.partition("=")
        if sep:
            cfg.setdefault(key.strip().lower(), value.strip())
    home = cfg.get("home")
    if not home:
        return False
    if not os.path.isdir(home):
        return True
    if os.name != "nt":
        # 仮想環境の bin/python は元の python へのリンクなので、元が消えればリンクの先が無くなる(_venv_python が
        # 見つけない)。OS の更新で python3 が別の版(3.12 から 3.13 など)を指すようになると、リンクは切れないが、
        # 仮想環境の lib/python3.12 は読まれない。リンクの先の名前に版があれば、作ったときの版と比べる
        return _venv_version_changed(venv, cfg.get("version") or cfg.get("version_info") or "")
    if os.path.isfile(os.path.join(home, "python.exe")):
        return False
    try:                          # 中を読めないフォルダ(Microsoft Store の Python など)は、あるものとみなす
        names = os.listdir(home)
    except OSError:
        return False
    return "python.exe" not in (n.lower() for n in names)


def _venv_version_changed(venv, version):
    """Windows 以外: 仮想環境の bin/python のリンクの先(python3.13 など)の版が、pyvenv.cfg の版(3.12.3)と違うか。
    どちらかの版が分からなければ False。"""
    made = re.match(r"(\d+)\.(\d+)", version)
    real = re.fullmatch(r"python(\d+)\.(\d+)", os.path.basename(os.path.realpath(os.path.join(venv, "bin", "python"))))
    return bool(made and real) and made.groups() != real.groups()


LOCAL_VENV = "local .venv"
PYTHON_LOST = "set up with a Python that is no longer installed"
# 子(--run)が、要るパッケージを読めずに終わったときの終了コード(仮想環境の python が別の版に替わったときなど)
VENV_IMPORT_FAILED = 104
PACKAGES_LOST = "set up in a virtual environment whose packages can no longer be loaded (its Python was replaced)"


def find_python():
    """(python のパス, どこのものか, 理由)。使える python が無ければ、パスは None で、理由に次にすべきことを入れる。
    隣の .venv が壊れているときは、どこのものかを "local .venv" にして返す(直すコマンドが違うため)。"""
    env = os.environ.get("CF_PYTHON")
    if env:
        return env, "CF_PYTHON", ""
    local_venv = os.path.join(HERE, ".venv")
    local = _venv_python(local_venv)
    if local:
        # 隣の .venv が壊れていても、利用者ごとの置き場へは落ちない(直すのは setup.bat。文面もそれを案内する)
        if _base_python_missing(local_venv):
            return None, LOCAL_VENV, PYTHON_LOST
        return local, LOCAL_VENV, ""
    venv = os.path.join(runtime_dir(), "venv")
    py = _venv_python(venv)
    if py is None:
        return None, "", "not set up"
    if _base_python_missing(venv):
        return None, "", PYTHON_LOST
    stamp = _read_stamp()
    if not stamp.get("deps_hash"):
        return None, "", "not set up completely (the last setup did not finish)"
    deps = dependencies()
    if deps is None or stamp["deps_hash"] != deps_hash(deps):
        return None, "", "set up for an older version (its dependencies changed)"
    return py, "runtime", ""


def entry_command():
    """この入口の呼び方。子のプロセスには環境変数 CF_ENTRY で渡し、文面が案内するコマンドに使う
    (`ctxf` は PATH に無く、プラグインの置き場の仮想環境には入っていないため)。"""
    return 'python -I "' + os.path.join(HERE, "ctxf.py") + '"'


def setup_command():
    return entry_command() + " setup"


def repair_command(kind=""):
    """用意をやり直すコマンド。隣の .venv(リポジトリを取得した人)は setup.bat で作り直す(`ctxf.py setup` は
    利用者ごとの置き場に作るだけで、隣の .venv が先に選ばれ続けるため)。"""
    if kind != LOCAL_VENV:
        return setup_command()
    if os.name == "nt":
        return '"' + os.path.join(HERE, "setup.bat") + '"'
    return 'cd "' + HERE + '" && python3 -m venv --clear .venv && .venv/bin/python -m pip install -e .'


NEW_SESSION_NOTE = ("A Copilot session that is already running does not get the search tool after setup: "
                    "start a new session (the context-search skill works meanwhile).")
# setup の終わりに出す(setup の文面は日本語。上の NEW_SESSION_NOTE は、エージェント向けの英語の文に入る)
SETUP_NEW_SESSION_NOTE = ("動いている Copilot のセッションには、setup の後も検索の道具が出ません。新しいセッションを"
                          "始めてください(それまでは、skill context-search が使えます)。")
# Copilot CLI の拡張の /context-setup(利用者が打つ)。隣の .venv のときは直らないので、そのときは書かない
SLASH_SETUP_NOTE = ("In Copilot CLI with extensions on, the user can instead type /context-setup, which runs the same "
                    "setup without the agent.")
# 隣の .venv が壊れているときは、控えの skill も同じ python で動くので、勧めない
LOCAL_VENV_NEW_SESSION_NOTE = ("A Copilot session that is already running does not get the search tool after setup: "
                               "start a new session.")


def not_ready_message(reason, kind=""):
    return ("context-foundation is " + reason + " on this PC. Search with grep meanwhile. To set it up, ask the user "
            "first (it downloads " + SETUP_SIZE_NOTE + " and takes a few minutes), then run: " + repair_command(kind)
            + "\n"
            + (LOCAL_VENV_NEW_SESSION_NOTE if kind == LOCAL_VENV else SLASH_SETUP_NOTE + " " + NEW_SESSION_NOTE) + "\n")


SESSION_HINT = (
    "This workspace has a context-foundation index (.cf folder: the code and design docs, searchable by meaning "
    "and keywords). When you need to find where something is implemented or documented and do not know its exact "
    "name or file, load the context-search skill before grepping. For a known identifier, grep as usual.")
# MCP サーバーも入っているとき(プラグインと一緒に入る)の 1 文。検索の道具を、grep より先に呼ぶように促す。
# 「名前を知っているなら grep」とは書かない: 公開のコードでは、モデルが名前を覚えているつもりで grep から
# 始め、検索を 1 回も使わなかった(docs/benchmarks.md)。検索は keyword も見るので、識別子でも引ける
SESSION_HINT_MCP = (
    "This workspace has a context-foundation index (.cf folder: the code and design docs, searchable by meaning "
    "and keywords). If the question already names the exact function, class or macro, grep that name first. "
    "Otherwise, to find where something is implemented or documented, call the context-foundation-search tool "
    "first, before grepping: describe what the code does in English and add identifiers you expect. One call "
    "returns ranked passages with source:path:start-end and their text: use them to find the right place, not as "
    "the whole answer. Before answering, cover related code (the other variant, the caller, the next step): each "
    "top passage lists the other names in its file ('same file also has'); open the relevant ones, and grep only "
    "for what they do not cover. If that tool is not available, load the context-search skill instead.")


def session_hint_not_ready(reason, kind=""):
    """索引はあるが、検索の道具が使えないときの 1 文。理由(find_python の理由)をそのまま入れる。隣の .venv が
    壊れているときは、skill も同じ python で動くので使えない。直すコマンド(setup.bat)を書く。"""
    if kind == LOCAL_VENV:
        return ("This workspace has a context-foundation index (.cf folder), but the search tool is " + reason +
                " on this PC. Search with grep meanwhile. To repair it, ask the user to run " + repair_command(kind) +
                "; after that, the search tool appears in a new session.")
    return ("This workspace has a context-foundation index (.cf folder), but the search tool is " + reason +
            " on this PC. If you need to find code or docs without knowing their names, load the context-search "
            "skill: it says how to set the tool up (ask the user first). After setup, the search tool appears in a "
            "new session.")


def find_index(cwd):
    """cwd かその親にある索引(.cf/index.db)のパス。無ければ None。"""
    d = os.path.abspath(cwd) if isinstance(cwd, str) and cwd else None
    while d:
        path = os.path.join(d, ".cf", "index.db")
        if os.path.isfile(path):
            return path
        parent = os.path.dirname(d)
        if parent == d:
            return None
        d = parent
    return None


INDEX_AGE_ENV = "CF_INDEX_AGE_WARN_DAYS"
INDEX_AGE_DEFAULT_DAYS = 7


def index_age_note(path, now=None):
    """索引が作ってから INDEX_AGE_DEFAULT_DAYS 日(環境変数 CF_INDEX_AGE_WARN_DAYS。0 で出さない)以上たって
    いれば、そのことの 1 文。そうでなければ空。古さは索引のファイルの更新日時で見る(作り直しはファイルを置き
    換えるので、作った時刻になる)。ソースが変わったかは見ない(フォルダを歩くと、セッションの始まりが遅くなる
    ため)。sqlite も開かない(Windows で、作り直しの置き換えを妨げないため)。何が起きても空を返す。"""
    try:
        try:
            limit = int(os.environ.get(INDEX_AGE_ENV, "").strip())
        except ValueError:                             # 無い・読めない値は既定の日数
            limit = INDEX_AGE_DEFAULT_DAYS
        if limit <= 0:
            return ""
        days = int(((time.time() if now is None else now) - os.stat(path).st_mtime) // 86400)
        if days < limit:
            return ""
        return (" Note: this index was built %d day%s ago. Files added since then do not appear in search results, "
                "so for recently added code, grep as well, and tell the user once that rebuilding the index "
                "(/context-index in Copilot CLI) brings it up to date." % (days, "" if days == 1 else "s"))
    except (OSError, ValueError, OverflowError):
        return ""


def session_hint(mcp=False):
    """Copilot の sessionStart の hook から呼ばれる。索引のある作業フォルダでだけ、skill の使いどころを 1 文で
    知らせる(説明文だけでは、skill を読まずに grep から始めるモデルがあるため)。索引が無ければ何も足さない。
    mcp が真なら(`session-hint --mcp`。MCP サーバーも入れているとき)、検索の道具を先に呼ぶように促す。
    仮想環境の python は起動しない(セッションの始まりを遅くしないため)。何が起きても 0 で終わる。"""
    try:
        raw = sys.stdin.buffer.read() if sys.stdin is not None else b""
        try:
            data = json.loads(raw.decode("utf-8", "replace") or "{}")
        except ValueError:
            data = {}
        cwd = data.get("cwd") if isinstance(data, dict) else None
        index = find_index(cwd or os.getcwd())
        if index is not None:
            py, kind, reason = find_python()
            hint = ((SESSION_HINT_MCP if mcp else SESSION_HINT) + index_age_note(index) if py is not None
                    else session_hint_not_ready(reason, kind))
            sys.stdout.write(json.dumps({"additionalContext": hint}))
    except BaseException:        # hook はセッションを止めない
        pass
    return 0


def _say(text):
    sys.stdout.write(text + "\n")
    sys.stdout.flush()


SETUP_USAGE = """使い方: python -I ctxf.py setup [--no-model] [--gpu | --cpu]

検索に要る Python のパッケージ(PyPI から約 200 MB)を入れた仮想環境を、利用者ごとの置き場に作ります。
動かす前に、利用者に確かめてください。
埋め込みモデル(Hugging Face の OSS)は入れません。使ってよいかは、利用者の環境(ライセンス・利用の条件・
ネットワーク)によるので、setup は、入っているかと入手先を知らせるだけです。利用者が確かめたうえで、自分で
`model download` を実行します。モデルが無ければ、検索は keyword だけです。
--no-model: モデルの有無も知らせません。
--gpu: (Windows)onnxruntime の代わりに onnxruntime-directml を入れ、索引づくりの埋め込みを GPU(DirectX 12 の
GPU)で計算します。選び方は次の setup に引き継ぎます。--cpu で戻します。
setup には Python 3.11 から 3.14 が要ります(3.15 からは、まだ onnxruntime の配布物がありません)。
"""
SETUP_OPTIONS = ("--no-model", "--gpu", "--cpu")
ORT_CPU, ORT_GPU = "onnxruntime", "onnxruntime-directml"
GPU_CHECK = "import onnxruntime as o; raise SystemExit(0 if 'DmlExecutionProvider' in o.get_available_providers() else 1)"
IMPORT_CHECK = "import numpy, onnxruntime, tokenizers, tree_sitter, jsonschema"
LOCK_STALE_SECONDS = 6 * 3600        # プロセスの生死が分からないときに、古いとみなすまでの時間


def _pid_alive(pid):
    """そのプロセスが動いているか。分からなければ None。"""
    if pid <= 0 or pid > 0xFFFFFFFF:   # プロセスの番号になりえない値(Windows の ctypes は、32 ビットを超えると例外)
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
    except (ProcessLookupError, OverflowError):
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


def _write_stamp(deps, gpu=False):
    os.makedirs(runtime_dir(), exist_ok=True)
    tmp = _stamp_path() + ".tmp-%d" % os.getpid()
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"deps_hash": deps_hash(deps), "dependencies": deps, "gpu": bool(gpu),
                   "python": sys.version.split()[0]}, f, ensure_ascii=False, indent=1)
    os.replace(tmp, _stamp_path())


def _keep_only_gpu_choice(gpu):
    """覚え書きを「用意が済んでいない」(deps_hash なし)にして、--gpu の選び方だけを残す。仮想環境を作り直す
    途中で pip が失敗したり止められたりしても、次の setup が選び方を引き継げるように。"""
    os.makedirs(runtime_dir(), exist_ok=True)
    tmp = _stamp_path() + ".tmp-%d" % os.getpid()
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"gpu": bool(gpu)}, f)
    os.replace(tmp, _stamp_path())


def wants_gpu(argv, stamp):
    """GPU 用の onnxruntime を入れるか。--gpu か --cpu の指定が無ければ、前の setup の選び方を引き継ぐ。"""
    if "--gpu" in argv:
        return True
    if "--cpu" in argv:
        return False
    return bool(stamp.get("gpu"))


def install_requirements(deps, gpu):
    """pip に渡す依存の一覧。gpu なら、onnxruntime を onnxruntime-directml に読み替える(版の条件はそのまま)。

    2 つは同じ名前のモジュール(onnxruntime)を入れるので、両方を入れることはできない。"""
    if not gpu:
        return list(deps)
    return [ORT_GPU + d[len(ORT_CPU):] if re.match(ORT_CPU + r"(?![A-Za-z0-9_.-])", d) else d for d in deps]


def _healthy(py, check):
    try:
        return subprocess.call([py, "-I", "-c", check], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) == 0
    except OSError:
        return False


def _import_error(py):
    """仮想環境の python で依存を読んだときの誤りの 1 行(読めれば空)。"""
    try:
        p = subprocess.run([py, "-I", "-X", "utf8", "-c", IMPORT_CHECK], stdout=subprocess.DEVNULL,
                           stderr=subprocess.PIPE, timeout=120)
    except (OSError, subprocess.SubprocessError) as e:
        return str(e)
    if p.returncode == 0:
        return ""
    lines = [x.strip() for x in (p.stderr or b"").decode("utf-8", "replace").splitlines() if x.strip()]
    return lines[-1] if lines else "終了コード %s" % p.returncode


# _install の終わり方: pip は 0 で終わったが、入れたパッケージを読めない(Python を同じフォルダのまま別の版に
# 置き換えた・パッケージが壊れた。pip は dist-info だけを見て「入っている」とみなすので、入れ直さない)
INSTALLED_BUT_BROKEN = 5


def setup(argv):
    """利用者ごとの置き場に、依存の入った仮想環境を作る(依存が変わっていれば入れ直す)。埋め込みモデルは取得せず、
    有無だけを知らせる。
    引数を検査してから始める(--help で、何も取得せずに説明だけ出す)。2 つの setup が同時に走らないよう排他する。"""
    if any(a in ("-h", "--help") for a in argv):
        sys.stdout.write(SETUP_USAGE)
        return 0
    unknown = [a for a in argv if a not in SETUP_OPTIONS]
    if unknown:
        sys.stdout.write("知らない指定です: " + " ".join(unknown) + "\n" + SETUP_USAGE)
        return 2
    if "--gpu" in argv and "--cpu" in argv:
        sys.stdout.write("--gpu と --cpu は一緒に指定できません\n" + SETUP_USAGE)
        return 2
    if "--gpu" in argv and os.name != "nt":
        sys.stdout.write("--gpu が入れる onnxruntime-directml は、Windows にしかありません。ほかの OS では、GPU を使える "
                         "onnxruntime を、仮想環境に自分で入れてください。\n")
        return 2
    if not python_supported(sys.version_info) and not os.environ.get(SETUP_REDIRECTED):
        found = find_supported_python()
        if found is not None:
            exe, ver = found
            _say("Python %s(%s)は使えないので、代わりに Python %s で setup をします: %s"
                 % ("%d.%d.%d" % tuple(sys.version_info[:3]), sys.executable, ver, exe))
            # 子(対応する版)が排他と仮想環境づくりをする。子が見つけ直さないよう印を付ける
            env = dict(os.environ, **{SETUP_REDIRECTED: "1"})
            try:
                return _run_child([exe, "-I", os.path.abspath(__file__), "setup"] + list(argv), env=env)
            except OSError as e:
                # 見つけた版はあるので「見つからない」とは言わない(python_range_message は言う)
                _say("  起動できませんでした(%s)。その Python で、自分で setup を実行してください: \"%s\" -I \"%s\" setup"
                     % (e, exe, os.path.abspath(__file__)))
                return 2
    os.makedirs(runtime_dir(), exist_ok=True)
    lock = os.path.join(runtime_dir(), "setup.lock")
    try:
        if os.path.exists(lock) and _lock_is_stale(lock):
            os.remove(lock)                  # 前の setup が途中で止まった(打ち切られた)ときの残り
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        _say("別の setup が動いています(" + lock + ")。終わるのを待ってから、もう一度 setup を実行してください。"
             "動いている setup が無いなら、そのファイルを消してください。")
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


# setup を、対応する版の python で起こし直したことの印(子が、さらに探し直さないため)
SETUP_REDIRECTED = "CF_SETUP_REDIRECTED"
# 探す python が、仮想環境を作れるか(Debian 系は python3-venv を入れないと ensurepip が無い)も一緒に確かめる
PYTHON_PROBE = "import sys, venv, ensurepip; print(sys.executable); print('%d.%d.%d' % sys.version_info[:3])"


def python_supported(version_info):
    return MIN_PYTHON <= tuple(version_info[:2]) <= MAX_PYTHON


def python_candidates():
    """setup に使えそうな python の起動のしかたを、新しい版から順に返す。PATH の python3.14 など(Windows の
    Python install manager も、この名前を PATH に置く)と、Windows では py ランチャーの py -3.14 など。"""
    py = which_on_path("py") if os.name == "nt" else None
    out = []
    for minor in range(MAX_PYTHON[1], MIN_PYTHON[1] - 1, -1):
        exe = which_on_path("python%d.%d" % (MAX_PYTHON[0], minor))
        if exe:
            out.append([exe])
        if py:
            out.append([py, "-%d.%d" % (MAX_PYTHON[0], minor)])
    return out


def _probe_python(cmd):
    """cmd が、対応する版の、仮想環境を作れる python なら (実行ファイルのパス, 版)。でなければ None。
    標準入力は渡さない(入っていない版を py が「入れますか」と聞いて止まらないように)。"""
    try:
        p = subprocess.run(cmd + ["-I", "-c", PYTHON_PROBE], stdin=subprocess.DEVNULL, capture_output=True,
                           text=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return None
    lines = (p.stdout or "").strip().splitlines()
    if p.returncode != 0 or len(lines) != 2 or not re.fullmatch(r"\d+\.\d+\.\d+", lines[1]):
        return None
    exe, ver = lines[0].strip(), lines[1]
    if not exe or not python_supported(tuple(int(x) for x in ver.split(".")[:2])):
        return None
    return exe, ver


def find_supported_python():
    """この PC にある、setup に使える python のうち、いちばん新しい版の (実行ファイルのパス, 版)。無ければ None。"""
    for cmd in python_candidates():
        found = _probe_python(cmd)
        if found is not None:
            return found
    return None


def python_range_message():
    """この python では setup ができないときの文面。使える版と、その版で setup を流すコマンドを書く。"""
    want = "%d.%d" % MAX_PYTHON
    this = "%d.%d.%d" % tuple(sys.version_info[:3])
    cmd = ("py -" + want if os.name == "nt" else "python" + want) + ' -I "' + os.path.join(HERE, "ctxf.py") + '" setup'
    why = ("(検索に要る onnxruntime の、この版向けの配布物がまだありません)" if sys.version_info[:2] > MAX_PYTHON
           else "")
    manager = ("(Python install manager なら `py install %s`)" % want) if os.name == "nt" else ""
    return ("Python %s は使えません%s。setup には Python %d.%d から %s が要りますが、この PC には見つかりませんでした。"
            "https://www.python.org/downloads/ から Python %s を入れて%s、もう一度 setup を実行してください"
            "(入れた Python で実行するなら: %s)\n"
            "(`python` そのものは、そのままで構いません。プラグインはそれでこの入口を起動し、入口は setup が作った"
            "仮想環境を使います)" % ((this, why) + MIN_PYTHON + (want, want, manager, cmd)))


# 仮想環境の中で、いちばん深いファイルまでの文字数(onnxruntime の中のもの。実測 143 に余裕を足す)。
# Windows の長いパスが無効(既定)だと、仮想環境のパスにこれを足して 260 を超えると、pip が入れられない
VENV_DEEPEST = 145
WINDOWS_MAX_PATH = 259


def long_paths_disabled():
    """Windows で、長いパス(260 文字を超えるもの)が使えない設定か。Windows 以外は False。"""
    if os.name != "nt":
        return False
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\FileSystem") as k:
            return winreg.QueryValueEx(k, "LongPathsEnabled")[0] != 1
    except OSError:              # 値が無い = 既定の無効
        return True


NETWORK_MARKERS = ("newconnectionerror", "proxyerror", "connecttimeout", "readtimeout", "max retries exceeded",
                   "sslerror", "certificate verify failed", "name resolution", "getaddrinfo failed",
                   "connection refused", "connection reset", "timed out")
NO_BUILD_MARKERS = ("no matching distribution", "could not find a version that satisfies",
                    "requires a different python")
LONG_PATH_MARKERS = ("long path", "winerror 206", "filename or extension is too long")


def pip_failure_hint(err):
    """pip の誤りの出力から、利用者が次にすべきことを 1 文で選ぶ(いつも「ネットワーク」とは言わない)。"""
    text = (err or "").lower()
    if any(m in text for m in LONG_PATH_MARKERS) or ("oserror" in text and "no such file or directory" in text):
        return ("仮想環境の中のパスが、Windows には長すぎる見込みです(長いパスが無効)。環境変数 CF_HOME を短い"
                "フォルダ(C:\\cf など)にするか、長いパスを有効にしてから、もう一度 setup を実行してください。")
    if any(m in text for m in NETWORK_MARKERS):
        return ("pip が PyPI に届きませんでした。ネットワークかプロキシの設定(HTTPS_PROXY)を確かめてから、もう一度 "
                "setup を実行してください。")
    if any(m in text for m in NO_BUILD_MARKERS):
        import platform          # ここでだけ読む(hook のたびに起動が遅くならないよう、先頭では読まない)
        return ("pip が、入れられるパッケージの配布物を見つけられませんでした。パッケージの置き場に届かなかった"
                "(ネットワーク・プロキシ・そのパッケージの無い pip の置き場)か、この OS の Python %s(%s)向けの"
                "配布物が無いかです。setup は、Windows x64 と Linux x64 の Python %d.%d から %d.%d で確かめています。"
                % ((sys.version.split()[0], platform.machine() or "不明な CPU") + MIN_PYTHON + MAX_PYTHON))
    return "わけは、上の pip の出力にあります。直してから、もう一度 setup を実行してください。"


def _pip(pip, args):
    """pip を動かす。(終了コード, 誤りの出力)。誤りの出力は、そのまま見せたうえで、原因を選ぶのに使う。"""
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    try:
        p = subprocess.run(pip + args, stderr=subprocess.PIPE, env=env)
    except OSError as e:
        return 1, str(e)
    err = (p.stderr or b"").decode("utf-8", "replace")
    if err:
        sys.stderr.write(err)
        sys.stderr.flush()
    return p.returncode, err


def _ort_requirements(deps, gpu):
    """依存の一覧のうち、onnxruntime(gpu なら onnxruntime-directml に読み替えたもの)の行。"""
    pattern = (ORT_GPU if gpu else ORT_CPU) + r"(?![A-Za-z0-9_.-])"
    return [d for d in install_requirements(deps, gpu) if re.match(pattern, d)]


def _ort_name(gpu):
    return ORT_GPU + " (the GPU build)" if gpu else ORT_CPU + " (the CPU build)"


def _ort_name_ja(gpu):
    """setup の文面に出す名前(where は英語の _ort_name)。"""
    return ORT_GPU + "(GPU 版)" if gpu else ORT_CPU + "(CPU 版)"


def _install(py, deps, stamp, gpu):
    """依存を入れる。成功なら 0。CPU 用と GPU 用を入れ替えるときは、入れられることを確かめてから今のものを外し、
    入れられなかったら前のものに戻す(onnxruntime が無いと、keyword の検索も動かないため)。
    pip が 0 で終わっても入れたものを読めないとき(戻したほうも読めないとき)は INSTALLED_BUT_BROKEN(文面は出さない。
    呼ぶ側が仮想環境を作り直す)。"""
    pip = [py, "-m", "pip", "--disable-pip-version-check"]
    swap = bool(stamp.get("deps_hash")) and bool(stamp.get("gpu")) != gpu
    if swap:
        r, err = _pip(pip, ["install", "--quiet", "--dry-run", "--no-deps", "--ignore-installed",
                            "--only-binary=:all:", *_ort_requirements(deps, gpu)])
        if r != 0:
            _say("  " + _ort_name_ja(gpu) + " は、この PC に入れられません。" + pip_failure_hint(err) +
                 " 何も変えていません。" + _ort_name_ja(not gpu) + " のままで、検索はこれまでどおり動きます。")
            return 1
        # CPU 用と GPU 用は、同じ名前のモジュールを入れる。入れ替えるときは、先に両方を外す
        try:
            os.remove(_stamp_path())
        except OSError:
            pass
        subprocess.call(pip + ["uninstall", "--quiet", "--yes", ORT_CPU, ORT_GPU])
    r, err = _pip(pip, ["install", "--quiet", *install_requirements(deps, gpu)])
    if r == 0 and _healthy(py, IMPORT_CHECK):
        _write_stamp(deps, gpu)
        return 0
    hint = pip_failure_hint(err)
    if swap:
        r2, _ = _pip(pip, ["install", "--quiet", *install_requirements(deps, not gpu)])
        if r2 == 0 and _healthy(py, IMPORT_CHECK):
            _write_stamp(deps, not gpu)
            _say("  pip が %s を入れられませんでした(終了コード %s)。%s %s に戻したので、検索はこれまでどおり動きます。"
                 % (_ort_name_ja(gpu), r, hint, _ort_name_ja(not gpu)))
            return 1
        if r == 0 and r2 == 0:
            return INSTALLED_BUT_BROKEN
        _say("  pip が %s を入れられず(終了コード %s)、%s にも戻せませんでした。%s setup が済むまで、検索は動きません。"
             "`%s --cpu` で CPU 版を入れ直せます。" % (_ort_name_ja(gpu), r, _ort_name_ja(not gpu), hint, setup_command()))
        return 1
    if r == 0:
        return INSTALLED_BUT_BROKEN
    _say("  pip が失敗しました(終了コード %s)。%s" % (r, hint))
    return 1


def _setup(argv):
    report_model = "--no-model" not in argv
    if not python_supported(sys.version_info):
        _say(python_range_message())
        return 2
    deps = dependencies()
    if not deps:
        _say("依存の一覧を読めません: " + PYPROJECT)
        return 2
    rt = runtime_dir()
    venv = os.path.join(rt, "venv")
    _say("== context-foundation の用意(setup)")
    _say("  利用者ごとの置き場: " + rt)
    if long_paths_disabled() and len(os.path.abspath(venv)) + VENV_DEEPEST > WINDOWS_MAX_PATH:
        _say("  注意: 置き場のパスが長く(%d 文字)、Windows の長いパスが無効なので、pip が「No such file or directory」"
             "で失敗することがあります。そのときは、CF_HOME を短いフォルダ(C:\\cf など)にするか、長いパスを有効にして"
             "から、もう一度 setup を実行してください。" % len(os.path.abspath(venv)))
    # GPU の選び方は、覚え書きを消す(仮想環境を作り直す)前に 1 回だけ決める。作り直しで --gpu の選び方を失わないため
    gpu = wants_gpu(argv, _read_stamp())
    py = _venv_python(venv)
    if py is not None and not _healthy(py, "import pip"):
        _say("  仮想環境が壊れています(前の setup が途中で止まったか、その Python が消された)。作り直します ...")
        py = None
    for attempt in (1, 2):
        if py is None:
            _say("  仮想環境を作ります ...")
            try:
                _keep_only_gpu_choice(gpu)
            except OSError:
                pass
            r = subprocess.call([sys.executable, "-m", "venv", "--clear", venv])
            py = _venv_python(venv)
            if r != 0 or py is None:
                _say("  仮想環境を作れませんでした(終了コード %s)" % r)
                return 1
        stamp = _read_stamp()
        if stamp.get("deps_hash") == deps_hash(deps) and bool(stamp.get("gpu")) == gpu and _healthy(py, IMPORT_CHECK):
            _say("  Python のパッケージ: 入っています" + ("(onnxruntime-directml、GPU 用)" if gpu else ""))
            break
        _say("  Python のパッケージを PyPI から入れます(初めては約 200 MB)...")
        r = _install(py, deps, stamp, gpu)
        if r == 0:
            _say("  Python のパッケージ: 入れました" + ("(onnxruntime-directml、GPU 用)" if gpu else ""))
            break
        if r != INSTALLED_BUT_BROKEN:
            return 1
        why = _import_error(py)
        if attempt == 2:
            _say("  pip はパッケージを入れましたが、読み込めません(%s)。フォルダ %s を消してから、もう一度 setup を実行"
                 "してください。それでも直らなければ、Python を入れ直してください。" % (why, venv))
            return 1
        # pip は dist-info だけを見て「入っている」とみなすので、入れ直さない。仮想環境ごと作り直す(1 回だけ)
        _say("  入れたパッケージを読み込めません(%s。仮想環境の Python が入れ替わった見込み)。仮想環境を作り直します ..."
             % why)
        py = None
    if gpu and not _healthy(py, GPU_CHECK):
        _say("  注意: onnxruntime-directml は入りましたが、この PC では DirectML を使えないと報告しています。"
             "索引づくりは CPU で計算します。")
    # 埋め込みモデルは取得しない(OSS を利用者の PC に勝手に入れない)。有無と入手先だけを出す
    if report_model:
        _say(model_report(_setup_model_info(py)))
    _say("用意ができました。次は、作業フォルダで索引を作ります: " + entry_command()
         + " init <フォルダ> [<フォルダ> ...] の後、同じ形で index\n" + SETUP_NEW_SESSION_NOTE)
    return 0


def serve_not_ready(reason="not set up", kind=""):
    """用意(setup)が済んでいない PC で、MCP サーバーとして起動されたとき。

    プラグインは MCP サーバーも一緒に登録するので、用意の前でも起動される。ここで誤りの文を標準出力に書いて
    終わると、エージェントの側では「サーバーが壊れている」としか見えない。そこで、道具を 1 つも出さない
    サーバーとして答え続ける(用意のしかたは、始めの 1 文と skill が伝える)。標準ライブラリだけで動く。
    理由は、標準エラー(ホストの MCP のログに残る)と、道具を呼ばれたときの誤りの文に書く。
    """
    # コマンドの直後に句点を置かない(POSIX の直し方は `pip install -e .` で終わり、`-e ..` と読めてしまう)
    text = ("context-foundation is " + reason + " on this PC. Ask the user, then run: " + repair_command(kind) +
            "\nThe search tool appears in a new session after that.")
    sys.stderr.write(text + "\n")
    sys.stderr.flush()
    stdin, stdout = sys.stdin.buffer, sys.stdout.buffer
    while True:
        line = stdin.readline()
        if not line:
            return 0
        try:                                          # NaN・Infinity は JSON ではない。深い入れ子の行でも落ちない
            msg = json.loads(line.decode("utf-8"), parse_constant=_not_json)
        except (ValueError, RecursionError):
            continue
        if not isinstance(msg, dict) or msg.get("id") is None or not msg.get("method"):
            continue                                  # 通知には答えない
        method, params = msg["method"], msg.get("params")
        reply = {"jsonrpc": "2.0", "id": msg["id"]}
        if method == "initialize":
            want = params.get("protocolVersion") if isinstance(params, dict) else None
            reply["result"] = {"protocolVersion": want if isinstance(want, str) else "2025-06-18",
                               "capabilities": {"tools": {"listChanged": False}},
                               "serverInfo": {"name": "context-foundation", "version": "0"}}
        elif method == "ping":
            reply["result"] = {}
        elif method == "tools/list":
            reply["result"] = {"tools": []}
        else:
            reply["error"] = {"code": -32601, "message": text}
        try:                                          # 標準出力には JSON の行だけを書く(id が 1e999 などなら答えない)
            out = json.dumps(reply, allow_nan=False).encode("utf-8")
        except (ValueError, RecursionError):
            continue
        stdout.write(out + b"\n")
        stdout.flush()


def _not_json(name):
    raise ValueError("not JSON: " + name)


def which_on_path(name):
    """PATH の項目だけを順に見て、実行ファイル name を探す。shutil.which と違い、Windows でも今のフォルダを見ない
    (作業フォルダに置かれた python.bat などを起動しないため)。空の項目と相対の項目も飛ばす。"""
    exts = [""]
    if os.name == "nt":
        pathext = [e for e in os.environ.get("PATHEXT", ".COM;.EXE;.BAT;.CMD").split(os.pathsep) if e]
        if os.path.splitext(name)[1].lower() not in [e.lower() for e in pathext]:
            exts = pathext
    for folder in os.environ.get("PATH", "").split(os.pathsep):
        if not folder or not os.path.isabs(folder):
            continue
        for ext in exts:
            p = os.path.join(folder, name + ext)
            if os.path.exists(p) and not os.path.isdir(p) and os.access(p, os.X_OK):
                return p
    return None


# PATH の `python` で Python が起動しないときの直し方(where と setup.bat が出す)
PATH_PYTHON_FIX = ("The Copilot MCP server and session hook start with `python`, so Copilot gets no search tool. "
                   "Add Python to PATH (the python.org installer has the option \"Add python.exe to PATH\").")
STORE_ALIAS_NOTE = ("This is the Windows shortcut that offers Python from the Microsoft Store, not Python. "
                    + PATH_PYTHON_FIX + " Or turn off python.exe under Settings > Apps > Advanced app settings > "
                    "App execution aliases, so that the Python on PATH is found.")


def is_store_alias(exe):
    """Windows の、Microsoft Store へ案内する python.exe(%LOCALAPPDATA%\\Microsoft\\WindowsApps の)か。
    Store から入れた Python もここに置かれるので、起動しなかったときにだけ使う。"""
    return os.name == "nt" and os.path.basename(os.path.dirname(exe)).lower() == "windowsapps"


def _python_version(exe):
    """その python の版("3.13.1")。起動できなければ None。"""
    try:
        p = subprocess.run([exe, "-I", "-c", "import sys; print('%d.%d.%d' % sys.version_info[:3])"],
                           capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return None
    out = (p.stdout or "").strip()
    return out if p.returncode == 0 and re.fullmatch(r"\d+\.\d+\.\d+", out) else None


# where から、仮想環境の python で動かす: 今のフォルダの設定で使うモデルが、手元にあるか
MODEL_CHECK = r"""
import sys
sys.path.insert(0, sys.argv[1])
try:
    from context_foundation.config import EmbeddingConfig, find_config, load_config
    from context_foundation.embed import ModelUnavailable, model_files, model_folder, resolve_model
    path = find_config(None)
    emb = load_config(path).embedding if path else EmbeddingConfig()
    if not emb.enabled:
        print("not used (embedding is off in " + str(path) + ")")
        raise SystemExit(0)
    spec = resolve_model(emb)
    by_use = getattr(spec, "model_dir_source", "") == "user"
    try:
        model_files(spec)
        print("present: " + spec.name + "  " + str(model_folder(spec)) + ("  (set with `model use`)" if by_use else ""))
    except ModelUnavailable:
        print("missing: " + spec.name + "  " + str(model_folder(spec)) +
              ("  (the folder set with `model use` no longer has the model files. Check it and run "
               "`model use \"<folder>\"` again, or `model forget`. Without it, search is keyword-only)" if by_use else
               "  (not installed. Setup does not install it; to use it, check its source and licence, then run "
               "`model download`, or `model use \"<folder>\"` if it is already on this PC. Without it, search is "
               "keyword-only)"))
except SystemExit:
    raise
except Exception as e:
    print("unknown (" + type(e).__name__ + ": " + (str(e).splitlines() or [""])[0] + ")")
"""


# setup から、仮想環境の python で動かす: 既定のモデル(今のフォルダの設定があればそのモデル)が手元にあるか。
# 取得はしない。1 行の JSON を書く
MODEL_SETUP_CHECK = r"""
import json, sys
sys.path.insert(0, sys.argv[1])
out = {}
try:
    from context_foundation.config import EmbeddingConfig, find_config, load_config
    from context_foundation.embed import MODEL_PRESETS, ModelUnavailable, model_files, model_folder, resolve_model
    path = find_config(None)
    emb = load_config(path).embedding if path else EmbeddingConfig()
    if not emb.enabled:
        out = {"state": "off", "config": str(path)}
    else:
        spec = resolve_model(emb)
        p = MODEL_PRESETS.get(spec.name)
        size = sum(p.files[n][0] for n in (spec.onnx_file, spec.tokenizer_file) if n in p.files) if p else 0
        out = {"name": spec.name, "license": spec.license, "size_mb": round(size / 1e6),
               "url": "" if spec.model_dir else "https://huggingface.co/" + spec.repo,
               "folder": str(model_folder(spec)), "own_folder": bool(spec.model_dir),
               "dir_source": spec.model_dir_source}
        try:
            model_files(spec)
            out["state"] = "installed"
        except ModelUnavailable:
            out["state"] = "missing"
except Exception as e:
    out = {"state": "unknown", "error": type(e).__name__ + ": " + (str(e).splitlines() or [""])[0]}
print(json.dumps(out, ensure_ascii=False))
"""


def _setup_model_info(py):
    """setup の終わりに出す、埋め込みモデルの有無(取得はしない)。読めなければ state が unknown。"""
    try:
        p = subprocess.run([py, "-I", "-X", "utf8", "-c", MODEL_SETUP_CHECK, SRC], capture_output=True, timeout=120)
        lines = (p.stdout or b"").decode("utf-8", "replace").strip().splitlines()
        info = json.loads(lines[-1]) if lines else {}
    except (OSError, subprocess.SubprocessError, ValueError) as e:
        return {"state": "unknown", "error": str(e)}
    return info if isinstance(info, dict) and info.get("state") else {"state": "unknown",
                                                                      "error": "終了コード %s" % p.returncode}


def model_report(info):
    """setup が出す、埋め込みモデルの行。入っていなければ、入手先と、利用者が自分で取得するコマンドを書く。
    頭の行(「  埋め込みモデル: 」)と、取得のコマンドの行は、/context-setup の拡張が読む(lib.mjs の modelMissing)。"""
    state = info.get("state")
    if state == "installed":
        return "  埋め込みモデル: 入っています(" + info.get("name", "") + "、" + info.get("folder", "") + ")"
    if state == "off":
        return "  埋め込みモデル: 使いません(" + info.get("config", "") + " で、埋め込みを切っています)"
    if state == "unknown":
        return ("  埋め込みモデル: 分かりません(" + info.get("error", "") + ")。`" + entry_command() + " model info` "
                "で確かめられます。")
    by_use = info.get("dir_source") == "user"
    where_from = ("入手先 " + info.get("url", "") if not info.get("own_folder")
                  else "`model use` で覚えたフォルダ: " + info.get("folder", "") if by_use
                  else "[embedding] model_dir のフォルダ: " + info.get("folder", ""))
    about = [info.get("name", ""), "ライセンス " + (info.get("license") or "不明")]
    if info.get("size_mb"):
        about.append("約 %s MB" % info["size_mb"])
    lines = ["  埋め込みモデル: インストールされていません(" + "、".join(about + [where_from]) + ")",
             "    setup では入れません。PyPI の外の OSS で、使ってよいかは、この PC の環境(ライセンス・利用の条件・"
             "ネットワーク)によるためです。入るまで、検索は keyword(BM25)だけです。"]
    if by_use:
        lines.append("    そのフォルダに、モデルのファイルがもうありません。確かめてから、次を実行します: " + entry_command() +
                     ' model use "<フォルダ>"(使うのをやめるなら `model forget`)')
    elif info.get("own_folder"):
        lines.append("    使うには、そのフォルダにモデルのファイルを置きます。")
    else:
        lines.append("    使うには、モデルの入手先とライセンスを確かめてから、次を実行します: " + entry_command() +
                     " model download")
        lines.append("    この PC にモデルが置いてあれば、代わりに次を実行します: " + entry_command() +
                     ' model use "<フォルダ>"')
    return "\n".join(lines)


def _model_state(py):
    # -I は PYTHONIOENCODING を見ないので、子の標準出力は -X utf8 で UTF-8 にする(日本語の設定の誤りやパスを読めるように)
    try:
        p = subprocess.run([py, "-I", "-X", "utf8", "-c", MODEL_CHECK, SRC], capture_output=True, timeout=120)
    except (OSError, subprocess.SubprocessError) as e:
        return "unknown (" + str(e) + ")"
    out = (p.stdout or b"").decode("utf-8", "replace").strip()
    return out or "unknown (exit code %s)" % p.returncode


def _index_state(start):
    path = find_index(start)
    if path is None:
        return "none (no .cf" + os.sep + "index.db in " + os.path.abspath(start) + " or above it)"
    try:
        built = time.strftime("%Y-%m-%d %H:%M", time.localtime(os.path.getmtime(path)))
    except OSError:
        built = "?"
    return path + "  (written " + built + ")"


def where():
    """使う python と置き場、モデル、索引を 1 回で出す(検索の道具が出ない・効かないときに確かめる)。"""
    py, kind, reason = find_python()
    _say("ctxf.py:   " + os.path.abspath(__file__))
    _say("launcher:  " + sys.executable + "  (Python " + sys.version.split()[0] + ")")
    on_path = which_on_path("python")
    if on_path:
        ver = _python_version(on_path)
        _say("PATH python: " + on_path + "  (" + ("Python " + ver if ver else "does not start") + ")")
        if not ver:
            _say("  " + PATH_PYTHON_FIX if not is_store_alias(on_path) else "  " + STORE_ALIAS_NOTE)
    else:
        _say("PATH python: none. " + PATH_PYTHON_FIX)
    _say("python:    " + (py + "  (" + kind + ")" if py else "none: " + reason))
    stamp = _read_stamp()
    rt = runtime_dir()
    if stamp.get("deps_hash"):
        rt += "  (set up with Python " + str(stamp.get("python") or "?") + ", " + _ort_name(bool(stamp.get("gpu"))) + ")"
    _say("runtime:   " + rt)
    _say("model:     " + (_model_state(py) if py else "unknown (needs the setup first)"))
    _say("index:     " + _index_state(os.getcwd()))
    session = os.environ.get("COPILOT_SESSION_CWD")
    if session:
        _say("index (COPILOT_SESSION_CWD): " + _index_state(session))
    _say("setup:     " + repair_command(kind))
    return 0 if py else 4


LAUNCHER_HELP = """context-foundation launcher. Its own commands:
  setup [--no-model] [--gpu | --cpu]   install the Python packages; reports the model (setup --help says more)
  where                                show the Python, runtime folder, model and index in use (troubleshooting)
  session-hint [--mcp]                 used by the Copilot sessionStart hook
Every other command goes to the tool:"""
# Windows の仮想環境の python.exe が、仮想環境を作った python を見つけられないときの終了コード
VENV_LAUNCHER_LOST = 103


def run_inner(argv):
    """仮想環境の python で動いているとき: 隣の src からパッケージを読み、CLI を動かす。
    .bat から直接 --run で呼ばれたときは、文面が案内するコマンドを、この python とこの入口の形にする。"""
    os.environ.setdefault("CF_ENTRY", '"' + sys.executable + '" -I "' + os.path.abspath(__file__) + '" --run')
    sys.path.insert(0, SRC)
    try:
        from context_foundation.cli import main as cli_main
    except ImportError as e:
        if _own_import_error(e):
            raise
        return _import_failed(e)
    if argv[:1] == ["serve"]:
        return cli_main(argv)    # serve は、標準入力を読む前の import の失敗だけを、自分で VENV_IMPORT_FAILED にする
    try:
        return cli_main(argv)
    except ImportError as e:     # 依存のパッケージを読めない(仮想環境の python が別の版に替わったなど)
        if _own_import_error(e):
            raise
        return _import_failed(e)


def _own_import_error(e):
    """隣の src にある道具そのものの import の誤り(名前の取り違えなど)か。依存を読めないのではないので、
    「用意をやり直す」とは言わずに traceback を残す。"""
    return ((getattr(e, "name", None) or "").split(".")[0] == "context_foundation"
            and os.path.isdir(os.path.join(SRC, "context_foundation")))


def _import_failed(e):
    sys.stderr.write("context-foundation cannot load a package it needs (" + type(e).__name__ + ": " + str(e) + ")\n")
    sys.stderr.flush()
    return VENV_IMPORT_FAILED



def _wait(child):
    """子の終わりを待って、終了コードを返す。Windows の wait() は、時間を決めないと Ctrl+C で割り込めない
    (子が終わるまで KeyboardInterrupt が上がらない)ので、短い間隔で待ち直す。"""
    while True:
        try:
            return child.wait(timeout=0.25)
        except subprocess.TimeoutExpired:
            pass


def _run_child(cmd, env=None):
    """子を起こして終わりを待ち、終了コードを返す。起こせなければ OSError。"""
    child = subprocess.Popen(cmd, **({} if env is None else {"env": env}))
    try:
        return _wait(child)
    except KeyboardInterrupt:
        # Ctrl+C は子にも届いている。子を止めずに後始末(途中までの埋め込みを書き残すなど)を待ち、子の終了コード
        # (130)を返す。2 回目の Ctrl+C では、子を残さないように止めてから上げる
        try:
            return _wait(child)
        except KeyboardInterrupt:
            child.kill()
            child.wait()
            raise


def main():
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    argv = sys.argv[1:]
    if argv[:1] == ["--run"]:
        return run_inner(argv[1:])
    os.environ["CF_ENTRY"] = entry_command()     # 子が出す文面は、この入口の呼び方でコマンドを案内する
    if argv[:1] == ["session-hint"]:
        return session_hint(mcp="--mcp" in argv[1:])
    if argv[:1] == ["setup"]:
        return setup(argv[1:])
    if argv[:1] == ["where"]:
        return where()
    if not argv or argv[0] in ("-h", "--help"):
        _say(LAUNCHER_HELP)
        argv = ["--help"]
    py, kind, reason = find_python()
    if py is None:
        if argv[:1] == ["serve"]:
            return serve_not_ready(reason, kind)
        sys.stdout.write(not_ready_message(reason, kind))
        return 4
    try:
        rc = _run_child([py, "-I", os.path.abspath(__file__), "--run"] + argv)
    except OSError as e:
        sys.stdout.write("context-foundation could not start (" + str(e) + "). Search with grep meanwhile.\n")
        return 4
    lost = None
    if rc == VENV_LAUNCHER_LOST and os.name == "nt":
        lost = PYTHON_LOST       # 仮想環境を作った python が消えた(pyvenv.cfg から前もって気づけなかった)
    elif rc == VENV_IMPORT_FAILED:
        lost = PACKAGES_LOST     # 子が、要るパッケージを読めなかった(理由は子が標準エラーに書いた)
    if lost and kind != "CF_PYTHON":
        # どちらのときも、子は標準入力を読む前に終わっている
        if argv[:1] == ["serve"]:
            return serve_not_ready(lost, kind)
        sys.stdout.write(not_ready_message(lost, kind))
        return 4
    return rc


if __name__ == "__main__":
    sys.exit(main())
