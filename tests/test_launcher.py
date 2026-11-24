"""入口(ctxf.py)の用意と知らせ: 使える Python の版、pip の失敗の原因、CPU 用と GPU 用の入れ替え、壊れた仮想環境、
文面が案内するコマンド、where の確認、.bat と skill の取り次ぎ、パッケージと CI の形。

用意(setup)そのものは PyPI からの取得が要るので、pip とプロセスの起動を差し替えて確かめる。
"""
from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

from context_foundation import copilot_reg

REPO = Path(__file__).resolve().parents[1]


def load_launcher(monkeypatch, cf_home: Path, path: Path = REPO / "ctxf.py"):
    monkeypatch.setenv("CF_HOME", str(cf_home))
    monkeypatch.delenv("CF_PYTHON", raising=False)
    # main() は CF_ENTRY を書く。テストの後に元へ戻るよう、monkeypatch に覚えさせてから外す
    monkeypatch.setenv("CF_ENTRY", "")
    monkeypatch.delenv("CF_ENTRY")
    spec = importlib.util.spec_from_file_location("ctxf_launcher_test", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def plugin_copy(tmp_path: Path, with_src: bool = False) -> Path:
    """隣に .venv の無い、プラグインとして入れたばかりの形の写し。"""
    plugin = tmp_path / "plugin"
    plugin.mkdir()
    for name in ("ctxf.py", "pyproject.toml"):
        shutil.copy2(REPO / name, plugin / name)
    if with_src:
        shutil.copytree(REPO / "src", plugin / "src", ignore=shutil.ignore_patterns("__pycache__", "*.egg-info"))
        shutil.copytree(REPO / "skills", plugin / "skills", ignore=shutil.ignore_patterns("__pycache__"))
    return plugin


def base_env(tmp_path: Path, **extra) -> dict:
    env = {"CF_HOME": str(tmp_path / "home"), "SYSTEMROOT": os.environ.get("SYSTEMROOT", ""),
           "CF_TEST_EMBEDDER": "hash"}
    env.update(extra)
    return env


def run(cmd, env, cwd=None, stdin="") -> subprocess.CompletedProcess:
    return subprocess.run([str(c) for c in cmd], capture_output=True, text=True, encoding="utf-8", env=env, cwd=cwd,
                          input=stdin, timeout=120)


class FakeChild:
    """subprocess.Popen の代わり(入口の main が起こす子)。wait() は results を順に返す(例外なら投げる)。"""

    def __init__(self, *results):
        self.results = list(results)
        self.started: list[tuple] = []
        self.killed = False

    def __call__(self, *args, **kwargs):
        self.started.append((args, kwargs))
        return self

    def wait(self, timeout=None):
        r = self.results.pop(0)
        if isinstance(r, BaseException):
            raise r
        return r

    def kill(self):
        self.killed = True


def current_deps_hash() -> str:
    spec = importlib.util.spec_from_file_location("ctxf_for_hash", REPO / "ctxf.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.deps_hash(mod.dependencies())


def fake_runtime(home: Path, base_home: Path | None, deps_hash: str | None = None) -> Path:
    """利用者ごとの置き場に、仮想環境の形と覚え書きを作る(python は空のファイル)。base_home は pyvenv.cfg の home。
    deps_hash を省くと、今の依存の一覧に合う覚え書きにする。"""
    venv = home / "runtime" / "venv"
    py = venv / "Scripts" / "python.exe"
    py.parent.mkdir(parents=True)
    py.write_bytes(b"")
    if base_home is not None:
        (venv / "pyvenv.cfg").write_text(f"home = {base_home}\ninclude-system-site-packages = false\n",
                                         encoding="utf-8")
    stamp = {"deps_hash": deps_hash or current_deps_hash()}
    (home / "runtime" / "deps.json").write_text(json.dumps(stamp), encoding="utf-8")
    return py


# --- 使える Python の版(INST-1 / PKG-1) ---

def test_setup_refuses_a_python_newer_than_the_tested_range(monkeypatch, tmp_path, capsys):
    ctxf = load_launcher(monkeypatch, tmp_path / "home")
    monkeypatch.setattr(sys, "version_info", (3, 15, 2, "final", 0))
    started = []
    monkeypatch.setattr(ctxf.subprocess, "call", lambda *a, **k: started.append(a) or 0)
    assert ctxf._setup([]) == 2
    out = capsys.readouterr().out
    assert "3.15.2 は使えません" in out and "onnxruntime" in out and "3.11 から 3.14" in out
    assert "この PC には見つかりませんでした" in out
    assert ("py -3.14 -I" if os.name == "nt" else "python3.14 -I") in out and 'ctxf.py" setup' in out
    assert "ネットワーク" not in out
    assert started == [] and not (tmp_path / "home" / "runtime" / "venv").exists()   # 何も作らない


def _unsupported_setup(monkeypatch, tmp_path, found):
    """3.15 で setup を起こしたときの形。find_supported_python は found を返し、子の起動は覚えるだけにする。"""
    ctxf = load_launcher(monkeypatch, tmp_path / "home")
    monkeypatch.delenv(ctxf.SETUP_REDIRECTED, raising=False)
    monkeypatch.setattr(sys, "version_info", (3, 15, 0, "final", 0))
    monkeypatch.setattr(ctxf, "find_supported_python", lambda: found)
    children = []
    monkeypatch.setattr(ctxf, "_run_child", lambda cmd, env=None: children.append((cmd, env)) or 0)
    monkeypatch.setattr(ctxf.subprocess, "call", lambda *a, **k: pytest.fail("must not create a venv itself"))
    return ctxf, children


def test_setup_with_an_unsupported_python_runs_itself_again_with_a_supported_one(monkeypatch, tmp_path, capsys):
    """`python` が 3.15 でも、この PC に 3.14 などがあれば、それで setup をやり直す(利用者が py -3.14 を打たなくてよい)。
    排他と仮想環境づくりは子がする(親は置き場に何も作らない)。"""
    exe = str(tmp_path / "py314" / "python.exe")
    ctxf, children = _unsupported_setup(monkeypatch, tmp_path, (exe, "3.14.2"))
    assert ctxf.setup(["--no-model", "--cpu"]) == 0
    (cmd, env), = children
    assert cmd == [exe, "-I", os.path.abspath(ctxf.__file__), "setup", "--no-model", "--cpu"]
    assert env[ctxf.SETUP_REDIRECTED] == "1"
    out = capsys.readouterr().out
    assert "3.15.0" in out and "使えない" in out and "代わりに Python 3.14.2 で setup をします: " + exe in out
    assert not (tmp_path / "home" / "runtime").exists()


def test_a_found_python_that_cannot_be_started_is_named_not_reported_missing(monkeypatch, tmp_path, capsys):
    """見つけた版を起動できなかったときは、その版で自分で動かすコマンドを出す(「見つからない」とは言わない)。"""
    exe = str(tmp_path / "py314" / "python.exe")
    ctxf, children = _unsupported_setup(monkeypatch, tmp_path, (exe, "3.14.2"))

    def cannot_start(cmd, env=None):
        raise PermissionError("blocked")
    monkeypatch.setattr(ctxf, "_run_child", cannot_start)
    assert ctxf.setup([]) == 2
    out = capsys.readouterr().out
    assert "起動できませんでした(blocked)" in out and f'"{exe}" -I ' in out and "見つかりませんでした" not in out
    assert not (tmp_path / "home" / "runtime").exists()


def test_setup_with_an_unsupported_python_and_no_other_says_what_to_install(monkeypatch, tmp_path, capsys):
    ctxf, children = _unsupported_setup(monkeypatch, tmp_path, None)
    assert ctxf.setup([]) == 2 and children == []
    assert "この PC には見つかりませんでした" in capsys.readouterr().out


def test_a_redirected_setup_does_not_search_again(monkeypatch, tmp_path, capsys):
    """子が(思いがけず)対応しない版だったときに、探し直して起こし合い続けない。"""
    ctxf, children = _unsupported_setup(monkeypatch, tmp_path, ("x", "3.14.0"))
    monkeypatch.setenv(ctxf.SETUP_REDIRECTED, "1")
    assert ctxf.setup([]) == 2 and children == []


def test_an_argument_error_is_reported_before_another_python_is_searched(monkeypatch, tmp_path, capsys):
    ctxf, children = _unsupported_setup(monkeypatch, tmp_path, ("x", "3.14.0"))
    assert ctxf.setup(["--bogus"]) == 2 and children == []
    assert "知らない指定です" in capsys.readouterr().out


def test_python_candidates_are_newest_first_and_use_the_py_launcher_on_windows(monkeypatch, tmp_path):
    ctxf = load_launcher(monkeypatch, tmp_path / "home")
    on_path = {"python3.12": "/bin/python3.12", "python3.14": "/bin/python3.14", "py": "C:/Windows/py.exe"}
    monkeypatch.setattr(ctxf, "which_on_path", on_path.get)
    want = [["/bin/python3.14"], ["/bin/python3.12"]]
    assert ctxf.python_candidates() == (
        [["/bin/python3.14"], ["C:/Windows/py.exe", "-3.14"], ["C:/Windows/py.exe", "-3.13"],
         ["/bin/python3.12"], ["C:/Windows/py.exe", "-3.12"], ["C:/Windows/py.exe", "-3.11"]]
        if os.name == "nt" else want)


def test_probe_accepts_this_python_and_rejects_an_unsupported_or_broken_one(monkeypatch, tmp_path):
    """実物の python で確かめる。版が範囲の外のもの・起動しないもの・出力が違うものは使わない。"""
    ctxf = load_launcher(monkeypatch, tmp_path / "home")
    found = ctxf._probe_python([sys.executable])
    assert found is not None and found[1] == "%d.%d.%d" % sys.version_info[:3]
    assert os.path.samefile(found[0], sys.executable)
    fake = tmp_path / "fake.py"
    fake.write_text("print('/opt/python3.15'); print('3.15.0')\n", encoding="utf-8")
    assert ctxf._probe_python([sys.executable, str(fake)]) is None        # 範囲の外の版
    fake.write_text("import sys; sys.exit(9009)\n", encoding="utf-8")
    assert ctxf._probe_python([sys.executable, str(fake)]) is None        # 起動しない(Store のショートカットなど)
    assert ctxf._probe_python([str(tmp_path / "no-such-python")]) is None
    monkeypatch.setattr(ctxf, "python_candidates", lambda: [[str(tmp_path / "no-such-python")], [sys.executable]])
    assert ctxf.find_supported_python() == found


def test_supported_python_range_agrees_everywhere(monkeypatch, tmp_path):
    ctxf = load_launcher(monkeypatch, tmp_path / "home")
    lo, hi = ctxf.MIN_PYTHON, ctxf.MAX_PYTHON
    project = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    assert project["requires-python"] == ">=%d.%d,<%d.%d" % (lo + (hi[0], hi[1] + 1))
    ci = (REPO / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    matrix = re.findall(r'"(\d+\.\d+)"', re.search(r"^\s*python: \[(.*)\]", ci, re.M).group(1))
    assert matrix == ["%d.%d" % (lo[0], m) for m in range(lo[1], hi[1] + 1)]       # テストは範囲のすべての版で
    # 文書が「CI で確かめる」と書く版は、CI の版の並びにある(範囲より新しい版は、使えない版として書かれる)
    for name in ("CHANGELOG.md", "README.md", "readme-ja.md", "docs/copilot.md"):
        for line in (REPO / name).read_text(encoding="utf-8").splitlines():
            if re.search(r"(?<![A-Za-z])CI(?![A-Za-z])", line):
                named = {v for v in re.findall(r"(?<![\d.])3\.\d+(?!\d)", line) if int(v.split(".")[1]) <= hi[1]}
                assert named <= set(matrix), f"{name}: {line}"
    bat =(REPO / "setup.bat").read_text(encoding="utf-8")
    newest_first = " ".join("%d.%d" % (hi[0], m) for m in range(hi[1], lo[1] - 1, -1))
    assert f"for %%v in ({newest_first}) do" in bat                       # py -3 ではなく、使える版を新しい順に
    assert "(%d, %d) <= sys.version_info[:2] <= (%d, %d)" % (lo + hi) in bat


# --- pip の失敗の原因(INST-4) ---

def test_pip_failure_hint_names_the_likely_cause(monkeypatch, tmp_path):
    ctxf = load_launcher(monkeypatch, tmp_path / "home")
    no_build = ctxf.pip_failure_hint("ERROR: No matching distribution found for onnxruntime>=1.20")
    assert "配布物を見つけられませんでした" in no_build and ".".join(map(str, sys.version_info[:2])) in no_build
    assert "3.11 から 3.14" in no_build
    long_path = ctxf.pip_failure_hint("ERROR: Could not install packages due to an OSError: [Errno 2] No such file "
                                      "or directory: 'C:\\x\\y.py'\nHINT: ... Long Path support ...")
    assert "長すぎる" in long_path and "CF_HOME" in long_path and "ネットワーク" not in long_path
    net = ctxf.pip_failure_hint("WARNING: Retrying ... NewConnectionError ... Max retries exceeded")
    assert "ネットワーク" in net and "プロキシ" in net
    assert "ネットワーク" not in ctxf.pip_failure_hint("ERROR: something else")


def test_a_failed_pip_install_reports_the_cause_and_writes_no_stamp(monkeypatch, tmp_path, capsys):
    ctxf = load_launcher(monkeypatch, tmp_path / "home")
    monkeypatch.setattr(ctxf, "_pip", lambda pip, args: (1, "ERROR: No matching distribution found for tree-sitter"))
    monkeypatch.setattr(ctxf, "_healthy", lambda py, check: True)
    assert ctxf._install("py", ctxf.dependencies(), {}, False) == 1
    out = capsys.readouterr().out
    assert "pip が失敗しました(終了コード 1)" in out and "配布物を見つけられませんでした" in out
    assert not (tmp_path / "home" / "runtime" / "deps.json").exists()


def test_setup_warns_when_the_runtime_path_is_too_long_for_windows(monkeypatch, tmp_path, capsys):
    deep = tmp_path / ("d" * 120)
    ctxf = load_launcher(monkeypatch, deep)
    monkeypatch.setattr(ctxf, "long_paths_disabled", lambda: True)
    monkeypatch.setattr(ctxf.subprocess, "call", lambda *a, **k: 1)        # 仮想環境は作らずに止める
    assert ctxf._setup(["--no-model"]) == 1
    out = capsys.readouterr().out
    assert "パスが長く" in out and "CF_HOME" in out


# --- CPU 用と GPU 用の入れ替え(INST-2) ---

def gpu_swap_env(monkeypatch, tmp_path, pip_results):
    ctxf = load_launcher(monkeypatch, tmp_path / "home")
    deps = ctxf.dependencies()
    ctxf._write_stamp(deps, gpu=False)                                    # 今は CPU 用が入っている
    calls = []

    def fake_pip(pip, args):
        calls.append(args)
        return pip_results.pop(0)

    uninstalled = []
    monkeypatch.setattr(ctxf, "_pip", fake_pip)
    monkeypatch.setattr(ctxf, "_healthy", lambda py, check: True)
    monkeypatch.setattr(ctxf.subprocess, "call", lambda cmd, *a, **k: uninstalled.append(cmd) or 0)
    return ctxf, deps, calls, uninstalled


def test_gpu_switch_keeps_the_cpu_build_when_directml_cannot_be_installed(monkeypatch, tmp_path, capsys):
    ctxf, deps, calls, uninstalled = gpu_swap_env(
        monkeypatch, tmp_path, [(1, "ERROR: No matching distribution found for onnxruntime-directml>=1.20")])
    assert ctxf._install("py", deps, ctxf._read_stamp(), True) == 1
    assert "--dry-run" in calls[0] and any(a.startswith("onnxruntime-directml") for a in calls[0])
    assert uninstalled == []                                              # 今のものは外していない
    assert ctxf._read_stamp()["gpu"] is False and ctxf._read_stamp()["deps_hash"] == ctxf.deps_hash(deps)
    out = capsys.readouterr().out
    assert "何も変えていません" in out and "onnxruntime(CPU 版) のまま" in out


def test_gpu_switch_puts_the_cpu_build_back_when_the_install_fails(monkeypatch, tmp_path, capsys):
    ctxf, deps, calls, uninstalled = gpu_swap_env(
        monkeypatch, tmp_path, [(0, ""), (1, "ERROR: HTTP error 503"), (0, "")])
    assert ctxf._install("py", deps, ctxf._read_stamp(), True) == 1
    assert len(uninstalled) == 1 and "uninstall" in uninstalled[0]
    assert any(a.startswith("onnxruntime-directml") for a in calls[1])   # GPU 用を入れようとして
    assert any(re.match(r"onnxruntime(?!-)", a) for a in calls[2])       # 失敗したので CPU 用に戻した
    assert ctxf._read_stamp()["gpu"] is False and ctxf._read_stamp()["deps_hash"] == ctxf.deps_hash(deps)
    assert "onnxruntime(CPU 版) に戻した" in capsys.readouterr().out


def test_gpu_switch_that_cannot_restore_says_how_to_get_the_cpu_build(monkeypatch, tmp_path, capsys):
    ctxf, deps, _, _ = gpu_swap_env(monkeypatch, tmp_path, [(0, ""), (1, ""), (1, "")])
    assert ctxf._install("py", deps, ctxf._read_stamp(), True) == 1
    assert "setup --cpu" in capsys.readouterr().out
    assert ctxf._read_stamp() == {}                                       # 中途半端な状態を「用意済み」と言わない


# --- 用意の後の新しいセッション(INST-5)と、文面が案内するコマンド(DOC-2) ---

def test_setup_done_says_a_new_session_is_needed(monkeypatch, tmp_path, capsys):
    ctxf = load_launcher(monkeypatch, tmp_path / "home")
    ctxf._write_stamp(ctxf.dependencies(), gpu=False)
    monkeypatch.setattr(ctxf, "_venv_python", lambda venv: "py")
    monkeypatch.setattr(ctxf, "_healthy", lambda py, check: True)
    assert ctxf._setup(["--no-model"]) == 0
    out = capsys.readouterr().out
    assert "用意ができました" in out and "新しいセッション" in out and 'ctxf.py" init <フォルダ>' in out
    assert "new session" in ctxf.not_ready_message("not set up")


def test_setup_does_not_download_the_model_and_says_it_is_not_installed(monkeypatch, tmp_path, capsys):
    """埋め込みモデル(PyPI の外の OSS)は、setup では取得しない。無ければ、そう知らせ、入手先と取得のコマンドを出す。"""
    ctxf = load_launcher(monkeypatch, tmp_path / "home")
    ctxf._write_stamp(ctxf.dependencies(), gpu=False)
    monkeypatch.setattr(ctxf, "_venv_python", lambda venv: "py")
    monkeypatch.setattr(ctxf, "_healthy", lambda py, check: True)
    started = []
    monkeypatch.setattr(ctxf.subprocess, "call", lambda cmd, *a, **k: started.append(cmd) or 0)
    monkeypatch.setattr(ctxf, "_setup_model_info", lambda py: {
        "state": "missing", "name": "intfloat/multilingual-e5-small", "license": "MIT", "size_mb": 487,
        "url": "https://huggingface.co/intfloat/multilingual-e5-small", "folder": "/m", "own_folder": False})
    assert ctxf._setup([]) == 0
    out = capsys.readouterr().out
    assert not any("download" in map(str, cmd) for cmd in started), started
    assert "埋め込みモデル: インストールされていません(intfloat/multilingual-e5-small、ライセンス MIT、約 487 MB" in out
    assert "https://huggingface.co/intfloat/multilingual-e5-small" in out and "keyword(BM25)だけ" in out
    assert 'ctxf.py" model download' in out and "用意ができました" in out


def test_setup_model_report_for_each_state(monkeypatch, tmp_path):
    ctxf = load_launcher(monkeypatch, tmp_path / "home")
    assert ctxf.model_report({"state": "installed", "name": "m", "folder": "/m"}).strip() == \
        "埋め込みモデル: 入っています(m、/m)"
    assert "使いません" in ctxf.model_report({"state": "off", "config": "/w/.cf/config.toml"})
    assert "model info" in ctxf.model_report({"state": "unknown", "error": "x"})
    own = ctxf.model_report({"state": "missing", "name": "m", "license": "", "folder": "/mine", "own_folder": True})
    assert "インストールされていません" in own and "model_dir のフォルダ: /mine" in own and "モデルのファイルを置きます" in own
    assert "model download" not in own


def test_setup_model_check_runs_without_downloading(monkeypatch, tmp_path, outside_any_workspace):
    """setup が仮想環境の python で動かす確認(MODEL_SETUP_CHECK)は、モデルの有無を読むだけ。"""
    ctxf = load_launcher(monkeypatch, tmp_path / "home")
    monkeypatch.setenv("CF_MODEL_CACHE", str(tmp_path / "no-models"))
    info = ctxf._setup_model_info(sys.executable)
    assert info["state"] == "missing" and info["name"] == "intfloat/multilingual-e5-small", info
    assert info["license"] == "MIT" and info["url"].startswith("https://huggingface.co/") and info["size_mb"] > 400
    assert not (tmp_path / "no-models").exists()


def test_messages_name_a_runnable_command_when_started_through_the_launcher(tmp_path, outside_any_workspace):
    plugin = plugin_copy(tmp_path, with_src=True)
    entry = f'python -I "{plugin / "ctxf.py"}"'
    env = base_env(tmp_path, CF_PYTHON=sys.executable)
    ws = tmp_path / "ws"
    (ws / "docs").mkdir(parents=True)
    (ws / "docs" / "a.md").write_text("# a\n", encoding="utf-8")
    r = run([sys.executable, "-I", plugin / "ctxf.py", "search", "anything"], env, cwd=ws)
    assert r.returncode == 3 and "No context-foundation index" in r.stdout and "index.bat" not in r.stdout
    r = run([sys.executable, "-I", plugin / "ctxf.py", "init", "docs"], env, cwd=ws)
    assert r.returncode == 0 and f"`{entry} index`" in r.stdout and ".p4ignore" in r.stdout
    r = run([sys.executable, "-I", plugin / "ctxf.py", "model", "info"], env, cwd=ws)
    assert r.returncode == 0 and f"`{entry} model download`" in r.stdout
    assert "`ctxf " not in r.stdout and "setup.bat" not in r.stdout


def test_fallback_command_does_not_import_modules_from_the_current_folder(monkeypatch, tmp_path,
                                                                           outside_any_workspace):
    """CF_ENTRY が無いとき(pip で入れた ctxf)の案内のコマンドを、作業フォルダで打っても、そこにある同じ名前の
    モジュール(tomllib など)を読まない。"""
    import shlex

    from context_foundation import command_hint
    monkeypatch.delenv("CF_ENTRY", raising=False)
    argv = shlex.split(command_hint("status"), posix=os.name != "nt")
    if os.name == "nt":
        argv = [a.strip('"') for a in argv]
    env = {k: v for k, v in os.environ.items() if k != "CF_ENTRY"}
    env.update(CF_HOME=str(tmp_path / "home"), CF_TEST_EMBEDDER="hash", PYTHONIOENCODING="utf-8")
    results = []
    for name, planted in (("empty", False), ("planted", True)):
        ws = tmp_path / name
        ws.mkdir()
        if planted:
            (ws / "tomllib").mkdir()
            (ws / "tomllib" / "__init__.py").write_text(
                'open("pwned.txt", "w").write("x")\nraise ImportError("stop here")\n', encoding="utf-8")
        r = run(argv, env, cwd=ws)
        results.append((r.returncode, r.stdout.replace(str(ws), "<ws>")))
        assert not (ws / "pwned.txt").exists() and "stop here" not in r.stderr
    assert results[0] == results[1] and results[0][0] == 3


def test_launcher_help_lists_its_own_commands(tmp_path):
    plugin = plugin_copy(tmp_path, with_src=True)
    r = run([sys.executable, "-I", plugin / "ctxf.py", "--help"], base_env(tmp_path, CF_PYTHON=sys.executable))
    assert r.returncode == 0
    head, _, rest = r.stdout.partition("Every other command goes to the tool")
    assert "setup [--no-model]" in head and "where" in head and "usage: ctxf" in rest


# --- 仮想環境を作った Python が消えた(MCP-5)・依存が変わった(CP-2) ---

def test_runtime_venv_with_missing_base_python_reports_not_set_up(indexed_workspace, tmp_path):
    plugin = plugin_copy(tmp_path)
    fake_runtime(tmp_path / "home", tmp_path / "uninstalled-python")
    env = base_env(tmp_path)
    r = run([sys.executable, "-I", plugin / "ctxf.py", "where"], env)
    assert r.returncode == 4 and "no longer installed" in r.stdout
    r = run([sys.executable, "-I", plugin / "ctxf.py", "session-hint", "--mcp"], env,
            stdin=json.dumps({"cwd": str(indexed_workspace)}))
    hint = json.loads(r.stdout)["additionalContext"]
    assert "no longer installed" in hint and "context-search skill" in hint and "new session" in hint
    r = run([sys.executable, "-I", plugin / "ctxf.py", "serve"], env,
            stdin=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}) + "\n" +
            json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}) + "\n")
    replies = [json.loads(line) for line in r.stdout.splitlines()]
    assert r.returncode == 0 and replies[1]["result"] == {"tools": []} and "no longer installed" in r.stderr


def test_runtime_venv_whose_base_python_exists_is_used(monkeypatch, tmp_path):
    home = tmp_path / "home"
    ctxf = load_launcher(monkeypatch, home)
    monkeypatch.setattr(ctxf, "HERE", str(tmp_path / "plugin"))
    base = Path(sys.executable).parent if os.name == "nt" else Path(sys.base_prefix) / "bin"
    py = fake_runtime(home, base)
    assert ctxf.find_python()[:2] == (str(py), "runtime")


def test_a_dependency_change_is_named_in_the_hint_and_the_server_error(indexed_workspace, tmp_path):
    """プラグインの更新で依存が変わったとき: 「まだ用意していない」ではなく、古い版の用意だと知らせる。"""
    plugin = plugin_copy(tmp_path)
    fake_runtime(tmp_path / "home", None, deps_hash="0000000000000000")
    env = base_env(tmp_path)
    r = run([sys.executable, "-I", plugin / "ctxf.py", "session-hint", "--mcp"], env,
            stdin=json.dumps({"cwd": str(indexed_workspace)}))
    hint = json.loads(r.stdout)["additionalContext"]
    assert "older version" in hint and "new session" in hint
    r = run([sys.executable, "-I", plugin / "ctxf.py", "serve"], env,
            stdin=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                              "params": {"name": "search", "arguments": {}}}) + "\n")
    (reply,) = [json.loads(line) for line in r.stdout.splitlines()]
    assert "older version" in reply["error"]["message"] and "setup" in reply["error"]["message"]
    assert "older version" in r.stderr


@pytest.mark.skipif(os.name != "nt", reason="終了コード 103 は Windows の仮想環境の python.exe のもの")
def test_a_venv_python_that_cannot_find_its_base_falls_back_to_not_ready(monkeypatch, tmp_path, capsys):
    ctxf = load_launcher(monkeypatch, tmp_path / "home")
    monkeypatch.setattr(ctxf, "find_python", lambda: ("py.exe", "runtime", ""))
    monkeypatch.setattr(ctxf.subprocess, "Popen", FakeChild(103))
    monkeypatch.setattr(sys, "argv", ["ctxf.py", "search", "x"])
    assert ctxf.main() == 4
    assert "no longer installed" in capsys.readouterr().out


def test_a_broken_venv_next_to_the_launcher_names_setup_bat(indexed_workspace, tmp_path):
    """リポジトリを取得した人の .venv(setup.bat が作る)の python が消えたとき: 「使える」と言わず、直すコマンドは
    setup.bat(`ctxf.py setup` は利用者ごとの置き場に作るだけで、隣の .venv が先に選ばれ続ける)。"""
    plugin = plugin_copy(tmp_path)
    venv = plugin / ".venv"
    (venv / "Scripts").mkdir(parents=True)
    (venv / "Scripts" / "python.exe").write_bytes(b"")
    (venv / "pyvenv.cfg").write_text(f"home = {tmp_path / 'uninstalled-python'}\n", encoding="utf-8")
    fake_runtime(tmp_path / "home", Path(sys.executable).parent)       # 置き場は使える状態でも、こちらは選ばない
    repair = "setup.bat" if os.name == "nt" else "venv --clear .venv"
    env = base_env(tmp_path)
    r = run([sys.executable, "-I", plugin / "ctxf.py", "where"], env)
    assert r.returncode == 4 and "no longer installed" in r.stdout and repair in r.stdout, r.stdout
    assert 'ctxf.py" setup' not in r.stdout
    r = run([sys.executable, "-I", plugin / "ctxf.py", "session-hint", "--mcp"], env,
            stdin=json.dumps({"cwd": str(indexed_workspace)}))
    hint = json.loads(r.stdout)["additionalContext"]
    assert "no longer installed" in hint and repair in hint and "new session" in hint
    assert 'ctxf.py" setup' not in hint
    r = run([sys.executable, "-I", plugin / "ctxf.py", "search", "x"], env)
    assert r.returncode == 4 and repair in r.stdout and 'ctxf.py" setup' not in r.stdout
    r = run([sys.executable, "-I", plugin / "ctxf.py", "serve"], env,
            stdin=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}) + "\n" +
            json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                        "params": {"name": "search", "arguments": {}}}) + "\n")
    replies = [json.loads(line) for line in r.stdout.splitlines()]
    assert replies[0]["result"] == {"tools": []}
    assert repair in r.stderr and 'ctxf.py" setup' not in r.stderr
    # コマンドの直後に句点を付けない(POSIX の直し方 `pip install -e .` が `-e ..` と読めてしまう)
    message = replies[1]["error"]["message"]
    assert repair in message and "-e .." not in r.stderr and "-e .." not in message


@pytest.mark.skipif(os.name == "nt", reason="仮想環境の bin/python が元の python へのリンクになるのは、Windows 以外")
def test_a_venv_whose_python_link_now_points_at_another_version_is_not_ready(indexed_workspace, monkeypatch, tmp_path):
    """OS の更新で python3 が別の版を指すようになった仮想環境(リンクは切れず、lib/python3.12 は読まれない)。"""
    base = tmp_path / "base-bin"
    base.mkdir()
    (base / "python3.99").write_bytes(b"")
    venv = tmp_path / "home" / "runtime" / "venv"
    (venv / "bin").mkdir(parents=True)
    try:
        (venv / "bin" / "python").symlink_to(base / "python3.99")
    except OSError as e:
        pytest.skip(f"リンクを作れない: {e}")
    (tmp_path / "home" / "runtime" / "deps.json").write_text(json.dumps({"deps_hash": current_deps_hash()}),
                                                             encoding="utf-8")
    cfg = venv / "pyvenv.cfg"
    cfg.write_text(f"home = {base}\nversion = 3.12.3\n", encoding="utf-8")
    plugin = plugin_copy(tmp_path)
    env = base_env(tmp_path)
    r = run([sys.executable, "-I", plugin / "ctxf.py", "where"], env)
    assert r.returncode == 4 and "no longer installed" in r.stdout, r.stdout
    r = run([sys.executable, "-I", plugin / "ctxf.py", "session-hint", "--mcp"], env,
            stdin=json.dumps({"cwd": str(indexed_workspace)}))
    assert "no longer installed" in json.loads(r.stdout)["additionalContext"]
    ctxf = load_launcher(monkeypatch, tmp_path / "home")
    monkeypatch.setattr(ctxf, "HERE", str(plugin))
    cfg.write_text(f"home = {base}\nversion = 3.99.1\n", encoding="utf-8")         # 同じ版なら使える
    assert ctxf.find_python()[1] == "runtime"
    cfg.write_text(f"home = {base}\n", encoding="utf-8")                          # 版が分からなければ使える
    assert ctxf.find_python()[1] == "runtime"


def test_a_child_that_cannot_import_its_packages_falls_back_to_not_ready(monkeypatch, tmp_path, capsys):
    """子(--run)が、要るパッケージを読めずに VENV_IMPORT_FAILED で終わったとき(OS を問わない): serve は道具の無い
    サーバーとして答え、ほかのコマンドは用意をやり直す文面を出す。"""
    ctxf = load_launcher(monkeypatch, tmp_path / "home")
    monkeypatch.setattr(ctxf, "find_python", lambda: ("py", "runtime", ""))
    monkeypatch.setattr(ctxf.subprocess, "Popen", lambda *a, **k: FakeChild(ctxf.VENV_IMPORT_FAILED))
    monkeypatch.setattr(sys, "argv", ["ctxf.py", "search", "x"])
    assert ctxf.main() == 4
    assert "packages can no longer be loaded" in capsys.readouterr().out
    import io
    request = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}) + "\n"
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(request.encode("utf-8"))))
    out = io.BytesIO()
    monkeypatch.setattr(sys, "stdout", io.TextIOWrapper(out, encoding="utf-8"))
    monkeypatch.setattr(sys, "argv", ["ctxf.py", "serve"])
    assert ctxf.main() == 0
    assert json.loads(out.getvalue())["result"] == {"tools": []}
    assert "packages can no longer be loaded" in capsys.readouterr().err
    monkeypatch.setattr(ctxf, "find_python", lambda: ("py", "CF_PYTHON", ""))       # 自分で選んだ python はそのまま
    monkeypatch.setattr(sys, "argv", ["ctxf.py", "search", "x"])
    assert ctxf.main() == ctxf.VENV_IMPORT_FAILED


def test_ctrl_c_waits_for_the_child_and_returns_its_exit_code(monkeypatch, tmp_path):
    """Ctrl+C は子にも届く: 入口は子を止めずに後始末を待ち、子の終了コード(130)を返す(traceback を出さない)。
    2 回目の Ctrl+C では、子を止めてから上げる(親のいない子を残さない)。子は標準入力・出力を受け継ぐ(serve のため。引数は変えない)。"""
    ctxf = load_launcher(monkeypatch, tmp_path / "home")
    monkeypatch.setattr(ctxf, "find_python", lambda: ("py", "runtime", ""))
    monkeypatch.setattr(sys, "argv", ["ctxf.py", "index"])
    child = FakeChild(KeyboardInterrupt(), 130)
    monkeypatch.setattr(ctxf.subprocess, "Popen", child)
    assert ctxf.main() == 130
    assert not child.killed and child.results == []
    assert child.started == [((["py", "-I", os.path.abspath(ctxf.__file__), "--run", "index"],), {})]
    child = FakeChild(KeyboardInterrupt(), KeyboardInterrupt(), -9)
    monkeypatch.setattr(ctxf.subprocess, "Popen", child)
    with pytest.raises(KeyboardInterrupt):
        ctxf.main()
    assert child.killed and child.results == []


def _broken_venv_setup(monkeypatch, tmp_path, loads_after_rebuild, gpu=False):
    """用意が済んだ形(覚え書きは今の依存に合う)だが、入れたパッケージを読めない仮想環境(Python を同じフォルダの
    まま別の版に置き換えた形)。pip は「入っている」とみなして 0 で終わる。"""
    ctxf = load_launcher(monkeypatch, tmp_path / "home")
    ctxf._write_stamp(ctxf.dependencies(), gpu=gpu)
    made = []
    ctxf.pip_calls = []
    monkeypatch.setattr(ctxf, "_venv_python", lambda venv: "py")
    monkeypatch.setattr(ctxf, "_healthy", lambda py, check: check == "import pip" or (bool(made) and loads_after_rebuild))
    monkeypatch.setattr(ctxf.subprocess, "call", lambda cmd, *a, **k: made.append(cmd) or 0)
    monkeypatch.setattr(ctxf, "_pip", lambda pip, args: ctxf.pip_calls.append(list(args)) or (0, ""))
    monkeypatch.setattr(ctxf, "_import_error", lambda py: "ImportError: numpy._core._multiarray_umath")
    return ctxf, made


def test_setup_recreates_a_venv_whose_installed_packages_cannot_be_loaded(monkeypatch, tmp_path, capsys):
    ctxf, made = _broken_venv_setup(monkeypatch, tmp_path, loads_after_rebuild=True)
    assert ctxf._setup(["--no-model"]) == 0
    out = capsys.readouterr().out
    assert len(made) == 1 and "venv" in made[0] and "--clear" in made[0]          # 1 回だけ作り直す
    assert "読み込めません(ImportError: numpy._core._multiarray_umath" in out and "用意ができました" in out
    assert "pip が失敗しました(終了コード 0)" not in out
    assert ctxf._read_stamp()["deps_hash"] == ctxf.deps_hash(ctxf.dependencies())


def test_recreating_a_broken_venv_keeps_the_gpu_choice(monkeypatch, tmp_path, capsys):
    """作り直しの前に覚え書きを消しても、前の setup で選んだ --gpu は引き継ぐ(CPU 用に黙って戻さない)。"""
    ctxf, made = _broken_venv_setup(monkeypatch, tmp_path, loads_after_rebuild=True, gpu=True)
    assert ctxf._setup(["--no-model"]) == 0
    out = capsys.readouterr().out
    assert len(made) == 1
    assert ctxf._read_stamp()["gpu"] is True
    assert any("onnxruntime-directml" in a for a in ctxf.pip_calls[-1])
    assert "入れました(onnxruntime-directml、GPU 用)" in out


def test_a_rebuild_that_stops_midway_still_keeps_the_gpu_choice(monkeypatch, tmp_path):
    """作り直しの途中で pip が止まっても(Ctrl+C やネットワークの失敗)、次の setup は --gpu の選び方を引き継ぐ。"""
    ctxf, made = _broken_venv_setup(monkeypatch, tmp_path, loads_after_rebuild=True, gpu=True)

    first = ctxf._install

    def stopped(py, deps, stamp, gpu):
        if not made:                                   # 作り直す前は、ふつうに入れて「読めない」に気づく
            return first(py, deps, stamp, gpu)
        raise KeyboardInterrupt                        # 作り直した後の pip を止める

    monkeypatch.setattr(ctxf, "_install", stopped)
    with pytest.raises(KeyboardInterrupt):
        ctxf._setup(["--no-model"])
    stamp = ctxf._read_stamp()
    assert len(made) == 1 and not stamp.get("deps_hash") and ctxf.wants_gpu([], stamp) is True


def test_the_launcher_waits_for_the_child_in_short_steps(monkeypatch, tmp_path):
    """Windows の wait() は時間を決めないと Ctrl+C で割り込めないので、入口は短い間隔で待ち直す。"""
    ctxf = load_launcher(monkeypatch, tmp_path / "home")
    calls = []

    class Slow:
        def wait(self, timeout=None):
            calls.append(timeout)
            if len(calls) < 3:
                raise ctxf.subprocess.TimeoutExpired("ctxf", timeout)
            return 7

    assert ctxf._wait(Slow()) == 7
    assert calls == [0.25, 0.25, 0.25]


def test_setup_says_the_packages_cannot_be_loaded_when_recreating_does_not_help(monkeypatch, tmp_path, capsys):
    ctxf, made = _broken_venv_setup(monkeypatch, tmp_path, loads_after_rebuild=False)
    assert ctxf._setup(["--no-model"]) == 1
    out = capsys.readouterr().out
    assert len(made) == 1                                                         # 作り直すのは 1 回だけ
    assert "pip はパッケージを入れましたが、読み込めません" in out and "を消してから" in out
    assert "pip が失敗しました(終了コード 0)" not in out and "用意ができました" not in out
    assert not ctxf._read_stamp().get("deps_hash")                               # 「用意済み」とは言わない


def test_an_import_error_in_the_tool_itself_keeps_its_traceback(tmp_path):
    """道具そのものの import の誤り(名前の取り違えなど)は、「用意をやり直す」(終了コード 104)にしない。"""
    plugin = plugin_copy(tmp_path, with_src=True)
    glossary = plugin / "src" / "context_foundation" / "glossary.py"
    glossary.write_text(glossary.read_text(encoding="utf-8") + "\nfrom .config import no_such_name\n",
                        encoding="utf-8")
    env = base_env(tmp_path)
    r = run([sys.executable, "-I", plugin / "ctxf.py", "--run", "search", "x"], env, cwd=tmp_path)
    assert r.returncode not in (0, 104) and "Traceback" in r.stderr and "no_such_name" in r.stderr, r.stderr
    request = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}) + "\n"
    r = run([sys.executable, "-I", plugin / "ctxf.py", "--run", "serve"], env, stdin=request)
    assert r.returncode not in (0, 104) and "Traceback" in r.stderr and r.stdout == "", r.stderr


def test_the_not_ready_server_writes_only_json_and_survives_bad_lines(tmp_path):
    """用意の前に答えるサーバー(入口の中の、標準ライブラリだけのもの): NaN・Infinity の id にも JSON でない行を
    書かず、深い入れ子の 1 行でも落ちない。"""
    plugin = plugin_copy(tmp_path)
    lines = [json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}),
             '{"jsonrpc": "2.0", "id": NaN, "method": "ping"}',
             '{"jsonrpc": "2.0", "id": 1e999, "method": "ping"}',
             '{"jsonrpc": "2.0", "id": -Infinity, "method": "ping"}',
             "[" * 200_000,
             "not json",
             json.dumps({"jsonrpc": "2.0", "id": 2, "method": "ping"})]
    r = run([sys.executable, "-I", plugin / "ctxf.py", "serve"], base_env(tmp_path), stdin="\n".join(lines) + "\n")

    def strict(name):
        raise ValueError(name)
    replies = [json.loads(line, parse_constant=strict) for line in r.stdout.splitlines()]
    assert r.returncode == 0, r.stderr
    assert [x["id"] for x in replies] == [1, 2] and replies[1]["result"] == {}


def test_serve_ends_before_reading_stdin_when_a_package_cannot_be_imported(tmp_path):
    plugin = plugin_copy(tmp_path, with_src=True)
    server = plugin / "src" / "context_foundation" / "mcp_server.py"
    server.write_text("raise ModuleNotFoundError(\"No module named 'numpy'\")\n", encoding="utf-8")
    env = base_env(tmp_path)
    request = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}) + "\n"
    r = run([sys.executable, "-I", plugin / "ctxf.py", "--run", "serve"], env, stdin=request)
    assert r.returncode == 104 and r.stdout == "" and "numpy" in r.stderr, r.stderr
    r = run([sys.executable, "-I", plugin / "ctxf.py", "--run", "search", "x"], env, cwd=tmp_path)
    assert r.returncode != 104                      # search はこのモジュールを読まない


# --- where(DOC-3) ---

def test_where_reports_python_on_path_model_and_index(indexed_workspace, tmp_path, outside_any_workspace):
    plugin = plugin_copy(tmp_path, with_src=True)
    # PATH は python の無い空のフォルダだけにする(PATH を渡さないと、Linux では /bin:/usr/bin を探す道具がある)
    empty = tmp_path / "no-python-here"
    empty.mkdir()
    env = base_env(tmp_path, CF_PYTHON=sys.executable, COPILOT_SESSION_CWD=str(indexed_workspace), PATH=str(empty))
    env.pop("CF_TEST_EMBEDDER")
    r = run([sys.executable, "-I", plugin / "ctxf.py", "where"], env, cwd=tmp_path)
    assert r.returncode == 0, r.stdout + r.stderr
    lines = dict(line.split(":", 1) for line in r.stdout.splitlines() if ":" in line)
    assert "none" in lines["PATH python"] and "Add Python to PATH" in lines["PATH python"]
    assert "(CF_PYTHON)" in lines["python"]
    assert lines["model"].strip().startswith("missing: intfloat/multilingual-e5-small")
    assert lines["index"].strip().startswith("none")
    assert str(indexed_workspace / ".cf" / "index.db") in lines["index (COPILOT_SESSION_CWD)"]
    # PATH に python があれば、その絶対パスと版を出す
    env["PATH"] = os.pathsep.join([str(empty), os.path.dirname(sys.executable)])
    r = run([sys.executable, "-I", plugin / "ctxf.py", "where"], env, cwd=tmp_path)
    line = next(x for x in r.stdout.splitlines() if x.startswith("PATH python:"))
    found = line.split(":", 1)[1].split("  (")[0].strip()
    assert os.path.normcase(os.path.dirname(found)) == os.path.normcase(os.path.dirname(sys.executable)), line
    assert "(Python 3." in line


def test_where_names_the_microsoft_store_shortcut(monkeypatch, tmp_path, capsys):
    """PATH の `python` が起動しない Store のショートカットなら、そうと名指しして直し方を出す。"""
    ctxf = load_launcher(monkeypatch, tmp_path / "home")
    stub = os.path.join(str(tmp_path), "WindowsApps", "python.exe")
    monkeypatch.setattr(ctxf, "which_on_path", lambda name: stub)
    monkeypatch.setattr(ctxf, "_python_version", lambda exe: None)
    monkeypatch.setattr(ctxf, "is_store_alias", lambda exe: exe == stub)
    ctxf.where()
    out = capsys.readouterr().out
    assert "does not start" in out and "Microsoft Store" in out and "App execution aliases" in out
    monkeypatch.setattr(ctxf, "is_store_alias", lambda exe: False)
    ctxf.where()
    out = capsys.readouterr().out
    assert "Add Python to PATH" in out and "Microsoft Store" not in out


@pytest.mark.skipif(os.name != "nt", reason="the Store shortcut exists on Windows only")
def test_is_store_alias_on_windows(monkeypatch, tmp_path):
    ctxf = load_launcher(monkeypatch, tmp_path / "home")
    assert ctxf.is_store_alias(str(tmp_path / "Microsoft" / "WindowsApps" / "python.exe"))
    assert not ctxf.is_store_alias(str(tmp_path / "Python314" / "python.exe"))


def test_is_store_alias_is_false_off_windows(monkeypatch, tmp_path):
    ctxf = load_launcher(monkeypatch, tmp_path / "home")
    if os.name != "nt":
        assert not ctxf.is_store_alias("/home/u/WindowsApps/python")


@pytest.mark.parametrize("case", ["config-not-utf-8", "non-ascii-model-folder"])
def test_where_model_line_shows_non_ascii_text_as_it_is(tmp_path, outside_any_workspace, case):
    """where の model の行(仮想環境の python の子が書く)は、日本語の設定の誤りやパスを、化けずにそのまま出す
    (子を -I で起こすので、PYTHONIOENCODING は効かない)。"""
    plugin = plugin_copy(tmp_path, with_src=True)
    empty = tmp_path / "no-python-here"
    empty.mkdir()
    ws = tmp_path / "ws"
    (ws / ".cf").mkdir(parents=True)
    config = "# 設定ファイル\n[[source]]\nid = 'ws'\nroot = '.'\n"
    home = tmp_path / ("日本 home" if case == "non-ascii-model-folder" else "home")
    if case == "config-not-utf-8":
        (ws / ".cf" / "config.toml").write_bytes(config.encode("cp932"))
    else:
        (ws / ".cf" / "config.toml").write_text(config, encoding="utf-8")
    env = base_env(tmp_path, CF_PYTHON=sys.executable, PATH=str(empty), CF_HOME=str(home))
    env.pop("CF_TEST_EMBEDDER")
    r = run([sys.executable, "-I", plugin / "ctxf.py", "where"], env, cwd=ws)
    line = next(x for x in r.stdout.splitlines() if x.startswith("model:"))
    assert "�" not in line and "exit code" not in line, line
    if case == "config-not-utf-8":
        assert "設定ファイルが UTF-8 ではありません" in line, line
    else:
        assert line.split(":", 1)[1].strip().startswith("missing:") and "日本 home" in line, line


def test_session_hint_does_not_import_modules_only_where_and_setup_need(indexed_workspace, tmp_path):
    """hook はセッションの始めに毎回動く。where と setup だけが使う重いモジュール(shutil・platform)を読まない
    (時間ではなく import の有無を見るので、機械の速さに左右されない)。"""
    plugin = plugin_copy(tmp_path)
    fake_runtime(tmp_path / "home", Path(sys.executable).parent)
    r = run([sys.executable, "-I", "-X", "importtime", plugin / "ctxf.py", "session-hint", "--mcp"], base_env(tmp_path),
            stdin=json.dumps({"cwd": str(indexed_workspace)}))
    assert r.returncode == 0 and "additionalContext" in r.stdout
    imported = {line.rsplit("|", 1)[-1].strip() for line in r.stderr.splitlines() if line.startswith("import time:")}
    assert "json" in imported                                  # -X importtime の出力を読めている
    assert not imported & {"shutil", "platform"}, imported & {"shutil", "platform"}


def _planted_python(folder: Path) -> Path:
    """呼ばれると印のファイル pwned.txt を書く、偽の python(Windows は .bat、ほかは実行できるスクリプト)。"""
    if os.name == "nt":
        p = folder / "python.bat"
        p.write_text('@echo off\r\necho hijacked> "%~dp0pwned.txt"\r\necho 3.13.1\r\n', encoding="ascii", newline="")
    else:
        p = folder / "python"
        p.write_text('#!/bin/sh\necho hijacked > "$(dirname "$0")/pwned.txt"\necho 3.13.1\n', encoding="ascii",
                     newline="")
        p.chmod(0o755)
    return p


def test_path_walker_skips_the_current_folder_and_relative_entries(monkeypatch, tmp_path):
    ws = tmp_path / "ws"
    (ws / "rel").mkdir(parents=True)
    _planted_python(ws)
    _planted_python(ws / "rel")
    monkeypatch.chdir(ws)
    launcher = load_launcher(monkeypatch, tmp_path / "home")
    for which in (launcher.which_on_path, copilot_reg.which_on_path):
        monkeypatch.setenv("PATH", os.pathsep.join(["", ".", "rel", str(tmp_path / "missing")]))
        assert which("python") is None
        monkeypatch.setenv("PATH", os.pathsep.join(["rel", str(ws / "rel")]))
        found = which("python")
        assert found and Path(found).parent == ws / "rel" and os.path.isabs(found)


@pytest.mark.skipif(os.name != "nt", reason="今のフォルダの実行ファイルを先に見るのは、Windows の探し方だけ")
def test_where_and_register_do_not_run_a_python_planted_in_the_current_folder(monkeypatch, tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    _planted_python(ws)
    (ws / "copilot.cmd").write_text('@echo off\r\necho hijacked> "%~dp0pwned.txt"\r\n', encoding="ascii", newline="")
    real = os.path.dirname(sys.executable)
    # この変数があると shutil.which も今のフォルダを見ない。ふつうの PC には無いので、外して確かめる
    monkeypatch.delenv("NoDefaultCurrentDirectoryInExePath", raising=False)
    env = base_env(tmp_path, CF_PYTHON=sys.executable, PATH=real)
    r = run([sys.executable, "-I", REPO / "ctxf.py", "where"], env, cwd=ws)
    line = next(x for x in r.stdout.splitlines() if x.startswith("PATH python:"))
    assert not (ws / "pwned.txt").exists(), r.stdout
    assert not line.split(":", 1)[1].strip().startswith("."), line
    assert os.path.normcase(os.path.dirname(line.split(":", 1)[1].split("  (")[0].strip())) == os.path.normcase(real)
    monkeypatch.chdir(ws)
    monkeypatch.setenv("PATH", real)
    assert copilot_reg.python_on_path_problem() is None
    # Copilot も、今のフォルダの copilot.cmd を選ばない(PATH には本物の copilot が無い)
    assert copilot_reg.find_copilot() is None
    assert not (ws / "pwned.txt").exists()


def test_pid_alive_says_false_for_a_number_no_process_can_have(monkeypatch, tmp_path):
    """入口の _pid_alive も、パッケージの pid_alive と同じく、プロセスの番号になりえない値で False(例外を出さない)。"""
    from context_foundation.sysinfo import pid_alive
    launcher = load_launcher(monkeypatch, tmp_path / "home")
    for pid in (10 ** 30, 2 ** 32, -1, 0):
        assert launcher._pid_alive(pid) is False, pid
        assert pid_alive(pid) is False, pid


# --- .bat と、フォルダを動かしたり写したりしたとき(INST-3・INST-6) ---

def test_bat_files_run_the_package_next_to_them():
    for name in ("index.bat", "search.bat", "eval.bat", "register-copilot.bat", "unregister-copilot.bat",
                 "setup.bat"):
        text = (REPO / name).read_text(encoding="utf-8")
        assert '-I "%~dp0ctxf.py" --run ' in text, name
        assert "-m context_foundation" not in text, name


def test_setup_bat_checks_the_venv_before_installing(monkeypatch, tmp_path):
    """静的な検査: setup.bat の中で、.venv を確かめる行・作り直す行・pip で入れる行がこの順に並ぶこと。入れた後に
    依存を読めるかを確かめ(入口の IMPORT_CHECK と同じもの)、読めなければ 1 回だけ作り直して入れ直すこと。
    初回の用意(wizard setup)が失敗したら、失敗で終わること(.bat を動かしてはいない。壊れた .venv を実際に
    作り直すことは、手で確かめた)。"""
    text = (REPO / "setup.bat").read_text(encoding="utf-8")
    check = text.index('".venv\\Scripts\\python.exe" -c "import pip')
    assert text.index("-m venv --clear .venv") > check
    assert text.index("-m venv --clear .venv") < text.index("call :install || goto :failed")
    ctxf = load_launcher(monkeypatch, tmp_path / "home")
    body = text.split("\n:importable\n", 1)[1].split("\n:", 1)[0]
    assert f'".venv\\Scripts\\python.exe" -I -c "{ctxf.IMPORT_CHECK}"' in body
    at = text.index("call :install || goto :failed")
    for step in ("call :importable || (", "-m venv --clear .venv", "call :install || goto :failed",
                 "call :importable || (", "goto :failed", "--run wizard setup || goto :failed"):
        at = text.index(step, at + 1)                 # この順に並ぶ(見つからなければ ValueError)


def test_a_copied_folder_runs_and_registers_its_own_code(tmp_path):
    """写したフォルダの入口は、写した先の src を読む(入れたときのフォルダの src ではなく)。"""
    copy = plugin_copy(tmp_path, with_src=True)
    init = copy / "src" / "context_foundation" / "__init__.py"
    init.write_text(init.read_text(encoding="utf-8").replace('__version__ = "', '__version__ = "copy-'),
                    encoding="utf-8")
    code = ("import runpy, sys; sys.argv = [sys.argv[1], '--run', '--version']; "
            "runpy.run_path(sys.argv[0], run_name='__main__')")
    r = run([sys.executable, "-I", "-c", code, copy / "ctxf.py"], base_env(tmp_path))
    assert "context-foundation copy-" in r.stdout, r.stdout + r.stderr
    # -I では PYTHONIOENCODING が効かないので、パスは UTF-8 のバイトで書く(一時フォルダのパスに日本語があっても読める)
    code = ("import sys; sys.path.insert(0, sys.argv[1]); import context_foundation.copilot_reg as r; "
            "sys.stdout.buffer.write(str(r.REPO_ROOT).encode('utf-8'))")
    r = run([sys.executable, "-I", "-c", code, copy / "src"], base_env(tmp_path))
    assert Path(r.stdout.strip()) == copy.resolve()


# --- skill の取り次ぎ(MCP-10) ---

def test_skill_relay_defaults_to_agent_format(indexed_workspace, tmp_path):
    spec = importlib.util.spec_from_file_location("relay", REPO / "skills" / "context-search" / "ctxf.py")
    relay = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(relay)
    assert relay.with_agent_format(["search", "q"]) == ["search", "--format", "agent", "q"]
    assert relay.with_agent_format(["status"]) == ["status", "--format", "agent"]
    assert relay.with_agent_format(["search", "--format", "json", "q"]) == ["search", "--format", "json", "q"]
    assert relay.with_agent_format(["search", "--format=human", "q"]) == ["search", "--format=human", "q"]
    assert relay.with_agent_format(["init", "docs"]) == ["init", "docs"]
    # 取り次ぎが、実際に with_agent_format を通して入口へ渡すこと(引数を書き出すだけの偽の入口で確かめる。
    # 本物の入口では、標準出力がパイプなら CLI が自分で agent の形を選ぶので、結線が無くても通ってしまう)
    fake = tmp_path / "fake-plugin"
    (fake / "skills" / "context-search").mkdir(parents=True)
    (fake / "ctxf.py").write_text("import json, sys\nprint(json.dumps(sys.argv[1:]))\n", encoding="utf-8")
    shutil.copy2(REPO / "skills" / "context-search" / "ctxf.py", fake / "skills" / "context-search" / "ctxf.py")
    for given, passed in ((["search", "q"], ["search", "--format", "agent", "q"]),
                          (["search", "--format", "json", "q"], ["search", "--format", "json", "q"])):
        r = run([sys.executable, "-I", fake / "skills" / "context-search" / "ctxf.py", *given], base_env(tmp_path))
        assert json.loads(r.stdout) == passed, r.stdout + r.stderr
    plugin = plugin_copy(tmp_path, with_src=True)
    r = run([sys.executable, "-I", plugin / "skills" / "context-search" / "ctxf.py", "search", "buffer pool"],
            base_env(tmp_path, CF_PYTHON=sys.executable), cwd=indexed_workspace)
    assert r.returncode == 0 and r.stdout.startswith("context-foundation search:")


# --- モデルの取得の失敗(PKG-4・INST-4) ---

def test_model_download_failure_is_one_message_with_the_cause(monkeypatch, tmp_path):
    import huggingface_hub

    from context_foundation.config import EmbeddingConfig
    from context_foundation.embed import ModelUnavailable, download_model, resolve_model

    def too_long(**kw):                 # huggingface_hub 0.30〜0.33 が、長いパスで落ちるときの形
        partial = os.path.join(kw["local_dir"], ".cache", "huggingface", "download", "onnx",
                               "H" * 28 + "=." + "0" * 64 + ".incomplete")
        raise FileNotFoundError(2, "No such file or directory", "m" * 300 if len(partial) < 260 else partial)

    def offline(**kw):                  # プロキシで止められたとき(ファイルの名前は付かない)
        raise ConnectionError("Cannot reach https://huggingface.co")

    spec = resolve_model(EmbeddingConfig(cache_dir=str(tmp_path / "m")))
    monkeypatch.setattr(huggingface_hub, "hf_hub_download", too_long)
    with pytest.raises(ModelUnavailable) as e:
        download_model(spec, log=lambda s: None)
    msg = str(e.value)
    assert "FileNotFoundError" in msg and "\n" not in msg                  # 素の traceback ではなく、1 つの文
    if os.name == "nt":
        assert "CF_HOME" in msg and "260" in msg and "ネットワーク" not in msg
    monkeypatch.setattr(huggingface_hub, "hf_hub_download", offline)
    with pytest.raises(ModelUnavailable) as e:
        download_model(spec, log=lambda s: None)
    assert "ConnectionError" in str(e.value) and "ネットワーク" in str(e.value) and "CF_HOME" not in str(e.value)


# --- パッケージ(PKG-5)・依存の範囲(SEC-5・PKG-4)・CI(PKG-3) ---

def test_register_from_an_installed_package_says_it_needs_the_source_folder(monkeypatch, tmp_path, capsys):
    from types import SimpleNamespace

    # wheel で入れたときの形: site-packages には入口・skill・Marketplace の定義のどれも無い
    site = tmp_path / "site-packages"
    site.mkdir()
    monkeypatch.setattr(copilot_reg, "REPO_ROOT", site)
    monkeypatch.setattr(copilot_reg, "SKILLS_DIR", site / "skills")
    monkeypatch.setattr(copilot_reg, "LAUNCHER", site / "ctxf.py")

    def no_marketplace(*a):
        raise OSError("marketplace.json がありません")

    monkeypatch.setattr(copilot_reg, "marketplace_name", no_marketplace)
    monkeypatch.setattr(copilot_reg, "find_copilot", lambda: "copilot.exe")
    monkeypatch.setattr(copilot_reg, "_run", lambda *a: pytest.fail("Copilot を呼んではいけない"))
    assert copilot_reg.main(SimpleNamespace(action="register", mcp=False, yes=True)) == 2
    err = capsys.readouterr().err
    assert "pip で入れたパッケージからは登録できません" in err and "Marketplace の定義" not in err


def test_register_warns_when_python_is_not_on_path(monkeypatch, tmp_path, capsys):
    """登録そのものは済ませ、そのあと `python` が PATH に無いことを知らせる(Copilot の呼び出しは差し替える)。"""
    from types import SimpleNamespace

    monkeypatch.setenv("PATH", str(tmp_path))          # python の無いフォルダだけ
    monkeypatch.setenv("COPILOT_HOME", str(tmp_path / "copilot-home"))     # 利用者の設定には触れない
    assert "PATH" in copilot_reg.python_on_path_problem()
    calls = []
    monkeypatch.setattr(copilot_reg, "find_copilot", lambda: "copilot.exe")
    monkeypatch.setattr(copilot_reg, "_list", lambda *a: [])
    monkeypatch.setattr(copilot_reg, "_run", lambda *a: calls.append(a[1:]) or (0, "", ""))
    monkeypatch.setattr(copilot_reg, "show_status", lambda *a: None)
    monkeypatch.setattr(copilot_reg, "mcp_registered", lambda *a: (False, ""))
    assert copilot_reg.main(SimpleNamespace(action="register", mcp=False, yes=True)) == 0
    out = capsys.readouterr().out
    assert ("plugin", "marketplace", "add", str(copilot_reg.REPO_ROOT)) in calls
    assert "`python` が PATH にありません" in out and "Python を PATH に足してください" in out


def test_sdist_carries_what_the_tests_read():
    """静的な検査: MANIFEST.in が、テストの読むものを名前か拡張子で入れ、フォルダごと(graft)は入れないこと。
    sdist の中身が git で追跡しているファイルと合うことは、CI の package の job が sdist を作って確かめる
    (.github/scripts/check_sdist.py)。"""
    manifest = (REPO / "MANIFEST.in").read_text(encoding="utf-8")
    assert not re.search(r"^\s*graft\b", manifest, re.M)       # 無視しているファイルまで入る
    for line in ("recursive-include tests ", "recursive-include .github ", "skills/context-search/ctxf.py",
                 "com.github.copilot/hooks/hooks.json", "com.github.copilot/extensions/context-foundation/lib.mjs", "docs/design.md", "bench/agent_run.py", "prune bench/local",
                 "prune bench/runs", ".cf/*"):
        assert line in manifest, line
    assert all(name in manifest for name in ("ctxf.py", "plugin.json", "mcp.json"))
    ci = (REPO / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert "check_sdist.py" in ci


def test_dependencies_have_upper_bounds_where_a_major_version_broke_things():
    deps = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))["project"]["dependencies"]
    by_name = {re.match(r"[A-Za-z0-9_.-]+", d).group(0): d for d in deps}
    for name, bound in (("numpy", "<3"), ("onnxruntime", "<2"), ("huggingface_hub", "<3"), ("jsonschema", "<5")):
        assert bound in by_name[name], by_name[name]
    assert ">=0.36" in by_name["huggingface_hub"]          # 0.36 より前は、長いパスで取得が失敗する


def test_ci_runs_weekly_and_at_the_lowest_declared_versions():
    spec = importlib.util.spec_from_file_location("lowest", REPO / ".github" / "scripts" / "lowest_constraints.py")
    lowest = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(lowest)
    assert lowest.lowest("numpy>=2.0,<3") == "numpy==2.0" and lowest.lowest("tree-sitter>=0.25") == "tree-sitter==0.25"
    assert lowest.lowest("pytest") is None
    for name in ("ci.yml", "smoke.yml"):
        assert "schedule:" in (REPO / ".github" / "workflows" / name).read_text(encoding="utf-8")
    ci = (REPO / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert "lowest_constraints.py" in ci and "-c " in ci
    assert "package-ecosystem: github-actions" in (REPO / ".github" / "dependabot.yml").read_text(encoding="utf-8")
