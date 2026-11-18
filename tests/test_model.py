"""埋め込みモデルの実物を使うテスト(モデルが手元に無ければ飛ばす。CI はモデルを取らないので、いつも飛ぶ)。

ほかのテストは、語のハッシュで作るテスト用の埋め込み(HashEmbedder)を使う。ONNX で計算する道筋(質問と
passage の接頭辞、埋め草を除いた平均の pooling、長さの切り詰め)は、ここでだけ確かめる。
手元で流すには、先に `ctxf.py setup`(か `model download`)でモデルを取得しておく: pytest -m model
"""
from __future__ import annotations

import numpy as np
import pytest

from context_foundation.config import EmbeddingConfig
from context_foundation.embed import ModelUnavailable, OnnxEmbedder, model_files, resolve_model

pytestmark = pytest.mark.model


@pytest.fixture(scope="module")
def e5() -> OnnxEmbedder:
    spec = resolve_model(EmbeddingConfig())        # 既定のモデル(multilingual-e5-small)
    try:
        model_files(spec)
    except ModelUnavailable:
        pytest.skip("埋め込みモデルが手元にありません(取得すると流れます)")
    return OnnxEmbedder(spec)


def cos(a, b) -> float:
    return float(np.dot(a, b))                     # どちらも長さ 1


def test_vectors_have_the_model_dimension_and_unit_length(e5):
    q = e5.embed_query("where are GPU buffers pooled and reused")
    p = e5.embed_passages(["BufferPool keeps released GPU buffers and hands them out again.", "short"])
    assert q.shape == (384,) and p.shape == (2, 384)
    assert np.allclose(np.linalg.norm(p, axis=1), 1.0, atol=1e-5) and abs(np.linalg.norm(q) - 1.0) < 1e-5


def test_query_and_passage_get_their_own_prefix(e5):
    text = "release a buffer back to the pool"
    assert e5.spec.query_prefix == "query: " and e5.spec.passage_prefix == "passage: "
    q, p = e5.embed_query(text), e5.embed_passages([text])[0]
    assert np.allclose(q, e5._run(["query: " + text])[0], atol=1e-5)
    assert np.allclose(p, e5._run(["passage: " + text])[0], atol=1e-5)
    assert not np.allclose(q, p, atol=1e-3)        # 接頭辞が入れ替わったり落ちたりすると、ここで分かる


def test_padding_does_not_change_the_vector(e5):
    """長さの違う文を同じ束で計算しても、短い文のベクトルは 1 つで計算したときと同じ(埋め草を平均に入れない)。"""
    short = "passage: buffer pool"
    long = "passage: " + "the job scheduler hands work to idle worker threads and balances the queues " * 6
    together = e5._run([short, long])
    assert np.allclose(together[0], e5._run([short])[0], atol=1e-4)
    assert np.allclose(together[1], e5._run([long])[0], atol=1e-4)


def test_long_text_is_cut_at_the_token_limit(e5):
    long = "passage: " + "voice mixer channel gain " * 400          # 512 トークンを大きく超える
    assert len(e5.tok.encode(long).ids) == e5.spec.max_tokens == 512
    assert np.allclose(e5._run([long])[0], e5._run([long + " and more words after the limit" * 50])[0], atol=1e-5)


def test_a_related_passage_ranks_above_an_unrelated_one(e5):
    q = e5.embed_query("where are released GPU buffers kept and reused")
    related, unrelated = e5.embed_passages([
        "BufferPool keeps GPU buffers that were released and hands them out again instead of allocating new ones.",
        "The voice mixer adds the audio channels together and applies the master gain."])
    assert cos(q, related) > cos(q, unrelated)
