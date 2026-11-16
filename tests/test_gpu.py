"""索引づくりの埋め込みを GPU で計算する指定(設定の device、用意の --gpu)。

GPU の実物は使わない。確かめるのは、どの実行先を選ぶか・使えないときにどうするか・依存の読み替えだけ。
実物での速さと、CPU との結果の一致は、手元で測って docs/design.md に書いている。
"""
from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

import context_foundation.embed as embed
from context_foundation.config import ConfigError, EmbeddingConfig, load_config

from conftest import write_config

REPO = Path(__file__).resolve().parents[1]


def launcher():
    spec = importlib.util.spec_from_file_location("ctxf_gpu_test", REPO / "ctxf.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class FakeOnnx:
    """OnnxEmbedder の代わり。作られたときの引数を覚える。broken に入っている実行先では、作れない。"""
    made: list[tuple[str, int]] = []
    broken: set[str] = set()

    def __init__(self, spec, threads=0, batch_size=16, provider=embed.CPU_PROVIDER):
        if provider in self.broken:
            raise RuntimeError("the driver is too old\nmore detail")
        self.provider, self.batch_size = provider, batch_size
        FakeOnnx.made.append((provider, batch_size))


@pytest.fixture
def fake_onnx(monkeypatch):
    monkeypatch.delenv("CF_TEST_EMBEDDER")
    monkeypatch.setattr(embed, "OnnxEmbedder", FakeOnnx)
    FakeOnnx.made, FakeOnnx.broken = [], set()
    return FakeOnnx


def test_device_setting_is_checked(tmp_path):
    assert load_config(write_config(tmp_path)).embedding.device == "auto"
    assert load_config(write_config(tmp_path, extra="[embedding]\ndevice = 'gpu'\n")).embedding.device == "gpu"
    with pytest.raises(ConfigError, match="device"):
        load_config(write_config(tmp_path, extra="[embedding]\ndevice = 'fast'\n"))


def test_index_building_uses_the_gpu_when_one_is_available(fake_onnx, monkeypatch):
    monkeypatch.setattr(embed, "gpu_provider", lambda: "DmlExecutionProvider")
    emb = embed.make_embedder(EmbeddingConfig(), for_index=True)
    assert (emb.provider, emb.batch_size) == ("DmlExecutionProvider", embed.GPU_BATCH_SIZE)
    assert embed.make_embedder(EmbeddingConfig(batch_size=64), for_index=True).batch_size == 64
    # 検索(質問の埋め込み)は、いつも CPU。device = "cpu" なら、索引づくりも CPU
    assert embed.make_embedder(EmbeddingConfig()).provider == embed.CPU_PROVIDER
    emb = embed.make_embedder(EmbeddingConfig(device="cpu"), for_index=True)
    assert (emb.provider, emb.batch_size) == (embed.CPU_PROVIDER, 8)


def test_auto_falls_back_to_the_cpu_and_gpu_refuses(fake_onnx, monkeypatch):
    notes: list[str] = []
    monkeypatch.setattr(embed, "gpu_provider", lambda: None)             # GPU の実行先が無い onnxruntime
    assert embed.make_embedder(EmbeddingConfig(), for_index=True, log=notes.append).provider == embed.CPU_PROVIDER
    assert notes == []
    with pytest.raises(embed.ModelUnavailable, match="GPU の実行先がありません"):
        embed.make_embedder(EmbeddingConfig(device="gpu"), for_index=True)

    monkeypatch.setattr(embed, "gpu_provider", lambda: "DmlExecutionProvider")   # 実行先はあるが、始められない
    fake_onnx.broken = {"DmlExecutionProvider"}
    emb = embed.make_embedder(EmbeddingConfig(), for_index=True, log=notes.append)
    assert emb.provider == embed.CPU_PROVIDER and emb.batch_size == 8
    assert len(notes) == 1 and "CPU で計算します" in notes[0] and "the driver is too old" in notes[0]
    assert "more detail" not in notes[0]                                  # 知らせるのは 1 行目だけ
    with pytest.raises(embed.ModelUnavailable, match="読み込めません"):
        embed.make_embedder(EmbeddingConfig(device="gpu"), for_index=True)


def test_gpu_provider_reads_what_onnxruntime_offers(monkeypatch):
    import onnxruntime as ort
    monkeypatch.setattr(ort, "get_available_providers", lambda: ["CUDAExecutionProvider", "CPUExecutionProvider"])
    assert embed.gpu_provider() == "CUDAExecutionProvider"
    monkeypatch.setattr(ort, "get_available_providers",
                        lambda: ["CUDAExecutionProvider", "DmlExecutionProvider", "CPUExecutionProvider"])
    assert embed.gpu_provider() == "DmlExecutionProvider"                 # 並んでいる順(GPU_PROVIDERS)で選ぶ
    monkeypatch.setattr(ort, "get_available_providers", lambda: ["AzureExecutionProvider", "CPUExecutionProvider"])
    assert embed.gpu_provider() is None


def test_setup_swaps_only_the_onnxruntime_package_for_the_gpu():
    ctxf = launcher()
    deps = ["numpy>=2.0", "onnxruntime>=1.20", "onnxruntime-extensions>=0.1", "tokenizers>=0.21"]
    assert ctxf.install_requirements(deps, gpu=False) == deps
    assert ctxf.install_requirements(deps, gpu=True) == [
        "numpy>=2.0", "onnxruntime-directml>=1.20", "onnxruntime-extensions>=0.1", "tokenizers>=0.21"]
    assert ctxf.install_requirements(["onnxruntime"], gpu=True) == ["onnxruntime-directml"]
    # 指定が無ければ、前の setup の選び方を引き継ぐ(依存が変わって入れ直すときに、GPU 用が外れない)
    assert ctxf.wants_gpu(["--gpu"], {}) and not ctxf.wants_gpu(["--cpu"], {"gpu": True})
    assert ctxf.wants_gpu([], {"gpu": True}) and not ctxf.wants_gpu([], {}) and not ctxf.wants_gpu(["--no-model"], {})
    # いまの依存の一覧に、読み替える相手がある
    assert any(d.startswith("onnxruntime-directml") for d in ctxf.install_requirements(ctxf.dependencies(), gpu=True))


def test_setup_rejects_conflicting_gpu_options_before_doing_anything(tmp_path):
    env = {"CF_HOME": str(tmp_path / "home"), "SYSTEMROOT": __import__("os").environ.get("SYSTEMROOT", "")}
    r = subprocess.run([sys.executable, "-I", str(REPO / "ctxf.py"), "setup", "--gpu", "--cpu"], capture_output=True,
                       text=True, encoding="utf-8", env=env, timeout=60)
    assert r.returncode == 2 and "cannot be used together" in r.stdout
    assert not (tmp_path / "home" / "runtime" / "venv").exists()      # 何も作っていない
    r = subprocess.run([sys.executable, "-I", str(REPO / "ctxf.py"), "setup", "--help"], capture_output=True,
                       text=True, encoding="utf-8", env=env, timeout=60)
    assert "--gpu" in r.stdout and "onnxruntime-directml" in r.stdout
    if sys.platform != "win32":
        r = subprocess.run([sys.executable, "-I", str(REPO / "ctxf.py"), "setup", "--gpu"], capture_output=True,
                           text=True, encoding="utf-8", env=env, timeout=60)
        assert r.returncode == 2 and "Windows only" in r.stdout
        assert not (tmp_path / "home" / "runtime" / "venv").exists()
