"""context-foundation の入口(標準ライブラリだけで動く)。

  python -I ctxf.py search "<質問>"
  python -I ctxf.py session-hint      (Copilot CLI の sessionStart の hook 用。標準入力に JSON)

このファイルの隣の仮想環境(.venv。setup.bat が作る)の python で、パッケージの CLI を起動し、
出力と終了コードをそのまま返す。skill はこのファイルを呼ぶ(どの python から呼ばれても、依存の入った
仮想環境で動くようにするため)。環境変数 CF_PYTHON で、使う python を差し替えられる。
"""
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))


def venv_python():
    env = os.environ.get("CF_PYTHON")
    if env:
        return env
    for rel in (os.path.join(".venv", "Scripts", "python.exe"), os.path.join(".venv", "bin", "python")):
        p = os.path.join(HERE, rel)
        if os.path.isfile(p):
            return p
    return None


SESSION_HINT = (
    "This workspace has a context-foundation index (.cf folder: the code and design docs, searchable by meaning "
    "and keywords). When you need to find where something is implemented or documented and do not know its exact "
    "name or file, load the context-search skill before grepping. For a known identifier, grep as usual.")


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
    """Copilot CLI の sessionStart の hook から呼ばれる。索引のある作業フォルダでだけ、skill の使いどころを 1 文で
    知らせる(説明文だけでは、skill を読まずに grep から始めるモデルがあるため)。索引が無ければ何も足さない。
    仮想環境の python は起動しない(セッションの始まりを遅くしないため)。何が起きても 0 で終わる。"""
    import json
    try:
        data = json.loads(sys.stdin.buffer.read().decode("utf-8", "replace") or "{}")
    except ValueError:
        data = {}
    cwd = data.get("cwd") if isinstance(data, dict) else None
    if has_index(cwd or os.getcwd()):
        sys.stdout.write(json.dumps({"additionalContext": SESSION_HINT}))
    return 0


def main():
    if sys.argv[1:2] == ["session-hint"]:
        return session_hint()
    py = venv_python()
    if py is None:
        sys.stdout.write("context-foundation is not set up: no .venv next to ctxf.py. "
                         "Ask the user to run setup.bat in " + HERE + ". Search with grep meanwhile.\n")
        return 4
    try:
        return subprocess.call([py, "-I", "-m", "context_foundation"] + sys.argv[1:])
    except OSError as e:
        sys.stdout.write("context-foundation could not start (" + str(e) + "). Search with grep meanwhile.\n")
        return 4


if __name__ == "__main__":
    sys.exit(main())
