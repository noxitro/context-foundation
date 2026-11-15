"""ファイルを passage に分ける。言語ごとに方法を振り分ける。

  c / cpp / csharp → 構文の単位(tree-sitter)。解析できなかった範囲は行で分ける
  markdown         → 見出しの単位
  そのほか          → 行で分ける
"""
from __future__ import annotations

from ..config import ChunkConfig
from .base import Chunk, ChunkStats, chunk_lines

__all__ = ["Chunk", "ChunkStats", "chunk_file"]


def chunk_file(text: str, language: str, cfg: ChunkConfig) -> tuple[list[Chunk], ChunkStats]:
    if language in ("c", "cpp", "csharp"):
        from .code import chunk_code
        return chunk_code(text, language, cfg)
    if language == "markdown":
        from .markdown import chunk_markdown
        return chunk_markdown(text, cfg)
    return chunk_lines(text, cfg)
