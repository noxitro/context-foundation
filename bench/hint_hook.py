"""Copilot CLI の sessionStart の hook: 用意した文を、そのまま始めの文脈として渡す(標準ライブラリだけ)。

  python -I bench/hint_hook.py <文のファイル>

agent_run.py が、Serena を足す手法で使う(Serena 自身の始めの指示と、この道具の始めの 1 文をつないだもの)。
何が起きても 0 で終わる(hook はセッションを止めない)。
"""
import json
import sys

if __name__ == "__main__":
    try:
        sys.stdin.read()
        with open(sys.argv[1], encoding="utf-8") as f:
            text = f.read().strip()
        if text:
            sys.stdout.write(json.dumps({"additionalContext": text}))
    except BaseException:
        pass
    sys.exit(0)
