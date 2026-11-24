"""context-foundation: コードと設計文書から、自然文の質問で根拠の箇所を探す検索の道具。

検索だけを提供する(回答の生成とコードの変更は、呼び出す側のエージェントに任せる)。
ソースの中身は手元から出さない(埋め込みも手元で計算する)。
"""

__version__ = "0.4.15"

# 索引の形式の版。検索で読む表の形や語の分け方を変えたら上げる(古い索引は作り直しを求める)。
# 索引づくりのときだけ読む表(file_chunking: 分割の使い回しの控え)を足すだけなら上げない。その表の無い
# 古い索引は、検索にはそのまま使え、次の索引づくりで分割を使い回さないだけ(indexer.py の _ChunkReuse)。
INDEX_SCHEMA_VERSION = 1


def command_hint(args: str) -> str:
    """文面で案内するコマンド(`<入口> index` など)。入口 ctxf.py から動いているときは、ctxf.py が環境変数
    CF_ENTRY に自分の呼び方(python -I "<…>/ctxf.py")を入れる。無ければ、今の python でこのパッケージを動かす形
    (`ctxf` は PATH に無いことが多く、プラグインの置き場の仮想環境には入っていないため)。-P を付けて、今のフォルダ
    (案内のコマンドは作業フォルダで打たれる)を sys.path に入れない(そこにある同じ名前のモジュールを読まないため)。"""
    import os
    import sys
    entry = os.environ.get("CF_ENTRY") or f'"{sys.executable}" -P -m context_foundation'
    return f"{entry} {args}"
