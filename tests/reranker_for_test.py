"""テスト用の reranker(渡された passage を逆順にするだけ)。差し込み口が働くことを確かめる。"""


def reverse(query, passages):
    return list(reversed(passages))
