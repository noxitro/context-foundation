"""いくつかの手法を、質問ごとに順番を入れ替えて交互に流す(同じ時間帯に流して、日による答えのぶれを手法どうしで
そろえるため)。手法ごとに、agent_run.py(Copilot CLI)か claude_run.py(Claude Code)を 1 質問ずつ呼ぶ。
止まっても、同じ引数で続きから流せる(どちらの流し手も、results.jsonl にある回は流し直さない)。

  python bench/interleave.py <arms.json> --cases <cases.jsonl> [--split eval] [--trials 2]

arms.json の形(手法の名前 → 設定。runner は agent_run か claude_run。args は、その流し手にそのまま渡す引数):

  {"grep": {"runner": "agent_run", "python": "C:/.../python.exe", "config": "D:/bench/ws",
            "method": "agent_grep", "out": "D:/bench/runs/x-grep", "run_id": "x-grep",
            "args": ["--no-custom-instructions", "--price", "input=0.10", "--price", "output=0.50", "--yes"]},
   "cf":   {"runner": "agent_run", "python": "...", "config": "D:/bench/ws", "method": "agent_cf_mcp",
            "out": "D:/bench/runs/x-cf", "run_id": "x-cf", "args": ["--no-custom-instructions", "--yes"]}}

手法ごとにコードを変えて比べるときは、手法ごとに、そのコードを入れた python と、
"script" に、その版の bench/agent_run.py の場所を書く(省くと、このフォルダのもの)。
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

BENCH = Path(__file__).resolve().parent


def case_ids(cases: str, split: str) -> list[str]:
    rows = [json.loads(x) for x in Path(cases).read_text(encoding="utf-8").splitlines() if x.strip()]
    return [c["case_id"] for c in rows if split == "all" or c.get("split", "eval") == split]


def rotation(names: list[str], i: int) -> list[str]:
    """i 番目の質問で流す順番(1 つずつずらす)。"""
    return [names[(i + k) % len(names)] for k in range(len(names))]


def command(arm: dict, cases: str, split: str, trials: int, cid: str) -> list[str]:
    script = arm.get("script") or str(BENCH / f"{arm['runner']}.py")
    return [arm["python"], script, "--config", arm["config"], "--cases", cases, "--split", split, "--case", cid,
            "--method", arm["method"], "--out", arm["out"], "--run-id", arm["run_id"], "--trials", str(trials),
            *arm.get("args", [])]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("arms")
    ap.add_argument("--cases", required=True)
    ap.add_argument("--split", default="eval", choices=("eval", "tune", "all"))
    ap.add_argument("--trials", type=int, default=2)
    args = ap.parse_args()
    arms = json.loads(Path(args.arms).read_text(encoding="utf-8"))
    names = list(arms)
    ids = case_ids(args.cases, args.split)
    for i, cid in enumerate(ids):
        for name in rotation(names, i):
            arm = arms[name]
            p = subprocess.run(command(arm, args.cases, args.split, args.trials, cid), cwd=arm.get("cwd"),
                               capture_output=True, text=True, encoding="utf-8", errors="replace")
            for line in (p.stdout + p.stderr).splitlines():
                if any(s in line for s in (": timeout", ": gave_up", ": failed", ": error", "Traceback")):
                    print(f"[{name}] {line}", flush=True)
        print(f"== {i + 1}/{len(ids)} {cid} {time.strftime('%H:%M:%S')}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
