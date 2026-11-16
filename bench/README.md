# bench — エージェントを使う手法を Copilot CLI で回す

## 目的

同じ質問を GitHub Copilot CLI に解かせて、検索の道具ごとに、見つけた場所・
回答・時間・トークン・費用を記録します。結果は
[評価の仕様](../docs/evaluation-data-spec.md)の `results.jsonl` の形で
書くので、検索だけの手法の結果と同じ要約に並べられます。

**実行のたびに費用がかかります。** 実コーパスで全部の質問を流す前に、
回数と見込みの費用を利用者と確かめてください。全体の手順は
[比較の手順書](../docs/comparison-runbook.md)にあります。

## 手法

どの手法も、同じモデル・同じ質問文・同じ回答の書式
([answer-format.txt](answer-format.txt))・同じ道具の一式です。違うのは、
検索の道具だけです。

| 手法 | 足すもの |
| --- | --- |
| `agent_grep` | 無し(既存の手順) |
| `agent_grep_cxxref` | cxxref の skill と hook(`--cxxref` で場所を渡す) |
| `agent_cf` | この道具の skill と、セッションの始めの 1 文(hook)。0.2 までの既定の形 |
| `agent_cf_mcp` | この道具の MCP サーバーと、検索を先に使うように促す始めの 1 文(hook)。0.3 からの、プラグインの既定の形 |

- 道具の一式は `view`・`grep`・`glob`・`skill` と shell です。道具の定義は、
  使わない回でもモデルを呼ぶたびに送られるので、手法によって一式が違うと、
  それだけで入力トークンに差が出ます
- 許可は、読み取りを通し、書き込みを断ります。shell は `python` の起動
  だけを通します(skill が検索の CLI を呼ぶため)。作業フォルダは書き換え
  ません。git でもとに戻す処理は無いので、git の無い作業フォルダでも
  使えます
- 1 回ごとに、空の設定フォルダ(`COPILOT_HOME`)で新しいセッションを
  始めます。ふだん登録している MCP サーバーやプラグインは混ざりません。
  ただし、`~/.agents/skills` に置いた個人の skill は、空の設定フォルダでも
  読まれます(どの手法にも同じだけ入ります)

## 指示のファイル(AGENTS.md など)

Copilot CLI は、作業フォルダと、そこから版管理のルートまでのフォルダにある
`AGENTS.md`・`CLAUDE.md`・`GEMINI.md`・`.github/copilot-instructions.md`
などを、指示として読みます【公式】。中身は、モデルを呼ぶたびに入力に入る
ので、トークンにも振る舞いにも混ざります。

- 作業フォルダの側にあるものは、どの手法にも同じだけ入ります。実際の
  使い方でも読まれるものなので、既定では、そのまま読ませます
- この道具を置いたフォルダ(`agent_cf` だけが `--add-dir` で渡します)と、
  cxxref を置いたフォルダにあるものは、手法によって条件が変わります。
  渡したフォルダの指示のファイルが読まれるかは、確かめていません【未確認】
- この道具は、実行の前に、見つけた指示のファイルを「置き場の種類:名前」で
  知らせ、`runs.jsonl` の手法ごとの設定(`custom_instructions` と
  `instruction_files`)に残します。絶対パスは残しません
- `--no-custom-instructions` を付けると、どの手法でも読ませません(Copilot
  CLI の同じ名前のオプションを渡します)。手法の間で条件をそろえたいときは、
  これを付けます
- 読ませるかどうかは、走らせる前に決めて、全部の手法で同じにします
- この道具そのものの動作確認は、指示のファイルの無い作業フォルダ(合成の
  コーパスを写したフォルダなど)で行います。このリポジトリの中を作業
  フォルダにすると、リポジトリに置いた指示のファイルが混ざります

出典:
<https://docs.github.com/en/copilot/how-tos/copilot-cli/customize-copilot/add-custom-instructions>

## 使い方

```text
python bench\agent_run.py --config <作業フォルダ> --cases cases.jsonl ^
  --method agent_cf --out <結果のフォルダ> --run-id <名前> --dry-run
```

`--dry-run` は、実行されるコマンドを出すだけです。実行するときは、
`--dry-run` を外して `--yes` を付けます。

| 引数 | 意味 |
| --- | --- |
| `--config` | 作業フォルダ(`.cf\config.toml` のある場所) |
| `--cases` | 質問の一覧 |
| `--method` | 上の表の手法 |
| `--out` | 結果を書くフォルダ(非公開の場所) |
| `--case <id>` | この質問だけを流す(繰り返せる) |
| `--trials` | 1 問あたりの回数(既定 3) |
| `--model`、`--provider` | モデルと提供元(下の「モデル」) |
| `--time-limit` | 1 回の上限(秒。既定 600) |
| `--price KIND=USD` | 100 万トークンあたりの単価(費用の見積もり用) |
| `--no-hint` | `agent_cf` と `agent_cf_mcp` で、セッションの始めの 1 文を足さない |
| `--no-custom-instructions` | 指示のファイル(`AGENTS.md` など)を、どの手法でも読ませない |
| `--split` | 使う質問(既定 `eval`) |

止まっても、同じコマンドで続きから流せます(`results.jsonl` にある回は
飛ばします)。

### モデル

- `--provider copilot --model <名前>`: Copilot の契約のモデル。AI credits
  を消費します。1 回の上限は `--max-ai-credits` で付けられます
- `--provider <名前> --model <id>`(既定は `anthropic` と
  `claude-haiku-5-5`): 自分のキーで使う設定(BYOK)。Copilot CLI の
  `providers.json` に登録した提供元とモデルを使います。その提供元の料金が
  かかります。キーは、この道具は読みません(場所を Copilot CLI に渡す
  だけです)

BYOK のときは AI credits が記録されないので、`--price` で単価を渡すと、
トークンから費用を見積もります。

```text
--price input=0.10 --price output=0.50 ^
--price cache_read=0.01 --price cache_write=0.125
```

質問と、エージェントが読んだ本文は、選んだ提供元に送られます。

## 記録するもの

`<結果のフォルダ>` にできるもの:

| ファイル | 中身 |
| --- | --- |
| `results.jsonl` | 1 回につき 1 行 |
| `runs.jsonl` | 実行ごとの情報(Copilot CLI の版、モデル、上限、回数、単価、手法ごとの道具・許可・指示のファイルの扱い) |
| `logs\<回>.md` | やり取りの書き出し |
| `logs\<回>.usage.json` | Copilot CLI が書く使用量 |
| `homes\<回>\` | その回の設定フォルダ(セッションの記録が入る) |

1 行に入るもの:

- `retrieved_evidence`: エージェントが開いたファイルと行(`opened`)、
  検索が返した passage(`search_result`)、回答で根拠に挙げた場所
  (`cited`)を、出てきた順に。ファイル全体を開いたときは、1 行目から
  最後の行まで
- `answer`、`task_time_seconds`、`status`(`completed`・`timeout`・
  `gave_up`)
- `input_tokens`・`output_tokens`・`cache_read_tokens`・
  `cache_write_tokens`、`ai_units`、`cost_usd`、`tool_calls`、`model_calls`
- `returned_bytes`(道具の結果の大きさの合計)と `offloaded_results`
  (大きすぎてモデルに渡らなかった結果の数)
- `answer_correctness` は空です。人が採点して埋めます

## 注意

- 記録には、質問・回答・読んだ本文が入ります。公開の場所に置かないで
  ください(このリポジトリの `bench/runs` と `bench/local` は、git が
  無視します)
- 1 回ごとの結果は、モデルの気まぐれで変わります。1 問 3 回でも、小さな
  差は読み取れません
- 確かめたのは、Copilot CLI 1.0.112 と Claude Haiku 5.5(BYOK)、合成の
  コーパスでの 5 回だけです。`agent_grep_cxxref` は、実行していません。
  `--no-custom-instructions` を付けた実行も、していません(`--dry-run` で、
  オプションが渡ることだけを確かめました)
