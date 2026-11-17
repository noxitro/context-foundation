"""pyproject の依存(dependencies)の下限を、そのまま固定する制約の一覧(pip の -c に渡す形)を書き出す。

CI の「下限の版」の実行で使う。宣言した下限(>=)が本当に動くかを確かめるため(新しい版だけを入れていると、
下限が知らないうちに嘘になる)。標準ライブラリだけで動く(Python 3.11 以上)。

usage: python lowest_constraints.py > lowest.txt
"""
from __future__ import annotations

import re
import sys
import tomllib
from pathlib import Path

PYPROJECT = Path(__file__).resolve().parents[2] / "pyproject.toml"


def lowest(requirement: str) -> str | None:
    """'numpy>=2.0,<3' → 'numpy==2.0'。下限が無ければ None。"""
    m = re.match(r"\s*([A-Za-z0-9_.-]+)\s*(.*)$", requirement)
    if not m:
        return None
    floor = re.search(r">=\s*([0-9][0-9A-Za-z.]*)", m.group(2))
    return f"{m.group(1)}=={floor.group(1)}" if floor else None


def main() -> int:
    deps = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))["project"]["dependencies"]
    for d in deps:
        pin = lowest(d)
        if pin:
            sys.stdout.write(pin + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
