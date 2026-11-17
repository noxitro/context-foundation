"""sdist の中身が、git で追跡しているファイルと合うかを確かめる(CI の package の job から呼ぶ)。

  python .github/scripts/check_sdist.py <sdist の .tar.gz>

git で無視している手元のファイル(第三者の資料、公開しない計測、.cf の索引、手元の設定)が入っていたら、
または追跡しているファイルが MANIFEST.in から漏れていたら、その一覧を出して 1 で終わる。
"""
import subprocess
import sys
import tarfile

# setuptools が作るもの
GENERATED = {"PKG-INFO", "setup.cfg"}
# 追跡しているが、テストにも配布にも要らないもの
NOT_SHIPPED = {".gitattributes", ".gitignore", ".markdownlint-cli2.yaml"}


def main(path):
    with tarfile.open(path) as t:
        shipped = {m.name.split("/", 1)[1] for m in t.getmembers() if m.isfile() and "/" in m.name}
    shipped = {p for p in shipped if p not in GENERATED and ".egg-info/" not in p}
    out = subprocess.run(["git", "ls-files"], capture_output=True, text=True, encoding="utf-8", check=True).stdout
    tracked = set(out.splitlines()) - NOT_SHIPPED
    extra, missing = sorted(shipped - tracked), sorted(tracked - shipped)
    for p in extra:
        print("in the sdist, but not tracked by git: " + p)
    for p in missing:
        print("tracked by git, but not in the sdist (add it to MANIFEST.in): " + p)
    if extra or missing:
        return 1
    print("the sdist holds the %d tracked files" % len(shipped))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
