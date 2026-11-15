"""context-foundation の入口(標準ライブラリだけで動く)。

  python -I ctxf.py search "<質問>"

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


def main():
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
