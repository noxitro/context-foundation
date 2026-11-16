"""skill のフォルダに置く、入口への取り次ぎ(標準ライブラリだけで動く)。

  python -I "<この skill のフォルダ>/ctxf.py" search "<質問>"

プラグインの根にある入口(2 つ上の ctxf.py)を、同じ引数で動かす。skill の文から入口を「このフォルダの
2 つ上」と書くと、エージェントがパスを組み立て間違えて検索が失敗する回があった(Godot での計測で、skill を
読んだ 50 回のうち 15 回)。このファイルがあれば、skill のフォルダに名前を足すだけで呼べる。
"""
import os
import runpy
import sys

ENTRY = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "ctxf.py")

if __name__ == "__main__":
    if not os.path.isfile(ENTRY):
        sys.stderr.write(f"context-foundation: the entry script is missing ({ENTRY}). "
                         "Reinstall the plugin, and search with grep meanwhile.\n")
        sys.exit(2)
    sys.argv[0] = ENTRY
    runpy.run_path(ENTRY, run_name="__main__")
