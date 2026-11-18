"""skill のフォルダに置く、入口への取り次ぎ(標準ライブラリだけで動く)。

  python -I "<この skill のフォルダ>/ctxf.py" search "<質問>"

プラグインの根にある入口(2 つ上の ctxf.py)を、同じ引数で動かす。skill の文から入口を「このフォルダの
2 つ上」と書くと、エージェントがパスを組み立て間違えて検索が失敗する回があった(Godot での計測で、skill を
読んだ 50 回のうち 15 回)。このファイルがあれば、skill のフォルダに名前を足すだけで呼べる。

search と status は、--format の指定が無ければ agent の形にする(CLI は、出力が端末かどうかで形を選ぶので、
ホストの shell が擬似端末を渡すと、人が読む形になってしまう。skill の文が説明するのは agent の形)。
"""
import os
import runpy
import sys

ENTRY = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "ctxf.py")
AGENT_FORMAT_COMMANDS = ("search", "status")


def with_agent_format(argv):
    """search と status に --format が無ければ、--format agent を足した引数を返す。"""
    given = any(a == "--format" or a.startswith("--format=") for a in argv[1:])
    if argv[:1] and argv[0] in AGENT_FORMAT_COMMANDS and not given:
        return argv[:1] + ["--format", "agent"] + argv[1:]
    return list(argv)


if __name__ == "__main__":
    if not os.path.isfile(ENTRY):
        sys.stderr.write(f"context-foundation: the entry script is missing ({ENTRY}). "
                         "Reinstall the plugin, and search with grep meanwhile.\n")
        sys.exit(2)
    sys.argv = [ENTRY] + with_agent_format(sys.argv[1:])
    runpy.run_path(ENTRY, run_name="__main__")
