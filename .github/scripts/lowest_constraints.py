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


class NoFloor(ValueError):
    """版の指定はあるのに、下限を決められない(この一覧から黙って落とさないため)。"""


def lowest(requirement: str) -> str | None:
    """'numpy>=2.0,<3' → 'numpy==2.0'、'x~=1.2' と 'x==1.2' → 'x==1.2'。版の指定が無ければ None。
    版の指定があるのに下限を決められない('x>1.0'、'x<2' など)ときは NoFloor。"""
    m = re.match(r"\s*([A-Za-z0-9_.-]+)\s*([^;]*)", requirement)       # ; から後ろは環境の印(版の指定ではない)
    if not m:
        return None
    spec = m.group(2).strip()
    if not spec:
        return None
    floor = re.search(r"(?:>=|~=|==)\s*([0-9][0-9A-Za-z.]*)", spec)
    if not floor:
        raise NoFloor(requirement)
    return f"{m.group(1)}=={floor.group(1)}"


def main() -> int:
    deps = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))["project"]["dependencies"]
    for d in deps:
        try:
            pin = lowest(d)
        except NoFloor:
            sys.stderr.write(f"cannot pin the lowest version of {d!r}: write its lower bound with >=\n")
            return 1
        if pin:
            sys.stdout.write(pin + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
