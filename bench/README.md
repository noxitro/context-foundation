# bench — エージェントを使う手法を Copilot CLI か Claude Code で回す

## 目的

同じ質問を GitHub Copilot CLI(`agent_run.py`)か Claude Code
(`claude_run.py`)に解かせて、検索の道具ごとに、見つけた場所・回答・
時間・トークン・費用・道具の使われ方を記録します。比べる相手には、この
道具のほかに、言語サーバー(clangd などの LSP)と
[Serena](https://github.com/oraios/serena)(言語サーバーを使う記号の道具の
MCP サーバー)も入れられます。結果は
[評価の仕様](../docs/evaluation-data-spec.md)の `results.jsonl` の形で
書くので、検索だけの手法の結果と同じ要約に並べられます。

**実行のたびに費用がかかります。** 実コーパスで全部の質問を流す前に、
回数と見込みの費用を利用者と確かめてください。全体の手順は
[比較の手順書](../docs/comparison-runbook.md)にあります。

## 手法

どの手法も、同じモデル・同じ質問文・同じ回答の書式
([answer-format.txt](answer-format.txt))・同じ道具の一式です。違うのは、
検索の道具だけです。

| 手法 | 足すもの | Copilot CLI | Claude Code |
| --- | --- | :---: | :---: |
| `agent_grep` | 無し(既存の手順) | ○ | ○ |
| `agent_grep_cxxref` | cxxref の skill と hook(`--cxxref` で場所を渡す) | ○ | — |
| `agent_cf` | この道具の skill と、セッションの始めの 1 文(hook)。0.2 までの既定の形 | ○ | — |
| `agent_cf_mcp` | この道具の MCP サーバーと、検索を先に使うように促す始めの 1 文(hook)。0.3 からの、プラグインの既定の形 | ○ | ○ |
| `agent_lsp` | 言語サーバーの道具(Copilot CLI は `lsp`、Claude Code は `LSP`) | ○ | ○(手元の PC だけ) |
| `agent_cf_mcp_lsp` | `agent_cf_mcp` と `agent_lsp` の両方 | ○ | ○(手元の PC だけ) |
| `agent_serena` | Serena の MCP サーバーと、Serena 自身の始めの指示 | ○ | ○ |
| `agent_cf_mcp_serena` | `agent_cf_mcp` と `agent_serena` の両方 | ○ | ○ |

手法の定義は [methods.py](methods.py) にあり、2 つの流し手と、比べる道具
(`compare.py`)が同じものを使います。

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

## 言語サーバーと Serena を用意する

どちらも、コーパスのソースを言語サーバー(C・C++ では clangd)に読ませて、
定義・参照・記号の一覧を引けるようにする道具です。同じ質問で、この道具
(意味とキーワードの検索)と比べるために入れます。

1. **clangd を用意する。** PATH に置きます。パッケージの管理から入れるか
   (`winget install LLVM.LLVM`、`apt install clangd`)、PyPI の
   `clangd`(`pip install clangd`。clangd の配布物を包んだもの)を使います
2. **コンパイルの設定(`compile_commands.json`)を、ソースのルートに作る。**
   無いと、clangd はヘッダーとマクロを解けず、参照の多くを見落とします。
   作り方はコーパスによります。Godot は
   `scons platform=linuxbsd compiledb=yes compiledb_gen_only=yes`(Windows
   では `platform=windows`)でビルドせずに作れます(scons は
   `pip install scons`)。O3DE は CMake の `CMAKE_EXPORT_COMPILE_COMMANDS=ON`
   で作ります。O3DE のように 1 ファイルの誤りが多いコードでは、clangd の
   誤りの上限を外します([計測の一覧](../docs/benchmarks.md)の「難しい
   質問での測り直し」)
3. **LSP の手法:** Copilot CLI には `--lsp-config <lsp-config.json>`
   (Copilot CLI の言語サーバーの設定)を、Claude Code には
   `--lsp-command "clangd --background-index"` を渡します。Claude Code の
   流し手は、その回だけのプラグイン(`.lsp.json`)を作って `--plugin-dir`
   で渡します。**Claude Code は、クラウドのセッション(claude.ai の Claude
   Code)の中では、プラグインの言語サーバーを起こしません**(道具 `LSP` が
   出ない)。LSP の手法は、手元の PC で流してください
4. **Serena の手法:** Serena のプロジェクトを、ソースのルートに作ります
   (`.serena` フォルダを、そのフォルダに書きます。この道具の索引からは
   `exclude` で外してください)。記号の索引も、ここで作っておきます
   (作らないと、1 回目の質問で言語サーバーの立ち上げを待つことになり、
   その時間が手法の時間に入ります)

   ```text
   uvx --from serena-agent==2.1.0 serena project create <ソースのルート> ^
     --language cpp --index
   ```

   できた `<ソースのルート>/.serena/project.yml` の `read_only` を `true` に
   して、書き込みの道具を出さないようにします。流し手には
   `--serena-project <ソースのルート>` を渡します(source が 1 つなら省け
   ます)

Serena は、手を加えない既定の形で比べます。

- 版は `serena-agent==2.1.0` に固定しています(`--serena-command` で
  変えられます)
- 文脈(context)は、Copilot CLI では `copilot-cli`、Claude Code では
  `claude-code`(どちらも Serena に入っているもの)
- モードは `planning`(書き込みの道具を外す)と `benchmark`(記憶の道具を
  外し、尋ね返さずに最後まで進める)。どちらも Serena に入っているもの
- 道具の出し方(1 つの操作に 1 つの道具か、`serena_repl` に Python を
  渡す形か)は、Serena の設定のままです。2.1.0 の既定は `serena_repl` です
- 始めの指示は、Serena の `print-system-prompt` が出すものを、そのまま
  渡します(Copilot CLI では sessionStart の hook、Claude Code ではシステム
  プロンプト)。この道具の始めの 1 文と同じ扱いです
- Serena の設定の置き場(`SERENA_HOME`)は、流しごとに分けます(既定は
  `<結果のフォルダ>/serena-home`。`--serena-home` で変えられます)

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
| `--no-hint` | この道具を足す手法で、セッションの始めの 1 文を足さない |
| `--lsp-config` | LSP の手法で使う、Copilot CLI の lsp-config.json |
| `--serena-project`、`--serena-command`、`--serena-home` | Serena の手法(上の「言語サーバーと Serena を用意する」) |
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
  検索が返した passage(`search_result`)、Serena や言語サーバーが返した
  記号の範囲(`symbol_result`)、回答で根拠に挙げた場所(`cited`)を、
  出てきた順に。ファイル全体を開いたときは、1 行目から最後の行まで
- `answer`、`task_time_seconds`、`status`(`completed`・`timeout`・
  `gave_up`)
- `input_tokens`・`output_tokens`・`cache_read_tokens`・
  `cache_write_tokens`、`ai_units`、`cost_usd`、`tool_calls`、`model_calls`
- `tools_by_name`: 道具の名前ごとの呼び出しの数(LSP や Serena を実際に
  使ったかは、ここで見る)
- `returned_bytes`(道具の結果の大きさの合計)と `offloaded_results`
  (大きすぎてモデルに渡らなかった結果の数)
- `answer_correctness` は空です。人が採点して埋めます

## 手法を比べる道具

docs/benchmarks.md の数字は、次の道具で出しています。

| ファイル | 役目 |
| --- | --- |
| `interleave.py` | いくつかの手法を、質問ごとに順番を入れ替えて交互に流す。手法ごとの設定(流し手・python・作業フォルダ・手法・結果のフォルダ・引数)は JSON に書く |
| `grade_pack.py` | 採点の束を作る(手法と回を伏せて混ぜる)。採点(`score-<n>.json`)を結果に戻す |
| `compare.py` | 手法ごとの品質(合格の数・点の平均・識別子の指標・根拠の当たり・最後まで通らなかった回)、時間・費用・トークン、道具の種類ごとの使われ方と、2 つの手法の差の、質問を単位にした 95% 区間 |
| `claude_run.py` | Copilot CLI の代わりに、Claude Code(`claude -p`)をエージェントにして流す。費用は Claude Code の利用枠から出る |

```text
python bench\interleave.py arms.json --cases cases.jsonl --split eval --trials 2
python bench\grade_pack.py pack results.jsonl cases.jsonl grade --seed 1
(採点して grade\score-<n>.json を置く)
python bench\grade_pack.py merge results.jsonl grade results.scored.jsonl
python bench\compare.py summary results.scored.jsonl cases.jsonl --by category
python bench\compare.py cost results.scored.jsonl cases.jsonl
python bench\compare.py tools results.scored.jsonl cases.jsonl
python bench\compare.py paired results.scored.jsonl cases.jsonl agent_cf_mcp agent_grep
```

`compare.py` の表:

| 表 | 中身 |
| --- | --- |
| `summary` | 回数、`timeout`・`gave_up` の数、合格の数(0.8 以上)、点の平均、識別子の指標、根拠の当たり(回答が根拠に挙げた場所が正解の場所に重なった回の割合。`seen` は、開いた・道具が返した場所も数える)。`--by category`・`language`・`area`(case_id の最後の `-` より前)で、組ごとにも出す |
| `cost` | 時間・費用・入力と出力とキャッシュのトークン・モデルの呼び出し・道具の呼び出し・道具の結果の大きさの中央値と、費用の合計 |
| `tools` | 道具の種類(検索・LSP・Serena・grep・glob・読む・shell・skill)ごとの、1 回あたりの呼び出しの平均と、使った回の割合。道具を渡しても使われない、を見分ける |
| `paired` | 採点役の点・識別子の指標・根拠の当たり・時間・費用の、A − B の差と 95% 区間 |

どの表も `--format markdown`(文書に貼る表)と `--format json` で出せます。

Claude Code で 6 つの手法を交互に流す設定の見本は
[arms.claude.example.json](arms.claude.example.json) です(パスを自分の
ものに書き換えて使います)。

`interleave.py` は、手法ごとに結果のフォルダを分けて書きます。比べる前に、
手法の名前(`method`)が重ならないように、1 つの `results.jsonl` に
まとめてください(同じ `agent_cf_mcp` でもコードや索引が違うときは、
まとめるときに名前を付け替えます)。

守ること:

- **手法どうしは、交互に流した回答でだけ比べる。** 同じ手法でも、流した
  日によって識別子の指標が 0.03 ほど動いた。別の日の回答は、伏せて採点
  し直しても比べられない
- **物差しは 2 つ見る。** 採点役(AI)は束ごとに厳しさが違う。同じ回答を
  `--seed` を変えて 2 回採点し、平均をとると、ぶれが減る。識別子の指標は
  ぶれないが、名前を挙げずに正しく説明した回答を拾えない
- **差は区間で見る。** 22 問 × 2 回くらいでは、数 % の差は見分けられ
  ない。調整用の質問で見えた改善は、評価用の質問で確かめる
- 言語サーバーは、`agent_lsp`・`agent_cf_mcp_lsp` の手法で足す。0.4.10
  までと同じく、`agent_run.py` でほかの手法に `--lsp-config` を渡すと、
  その手法にも道具 `lsp` を足す(手法の名前は変わらないので、比べるときは
  名前を付け替える)
- 道具を渡しても、エージェントが使うとは限らない。`compare.py tools` で、
  LSP や Serena を使った回の割合を見てから、手法の差を読む

## 注意

- 記録には、質問・回答・読んだ本文が入ります。公開の場所に置かないで
  ください(このリポジトリの `bench/runs` と `bench/local` は、git が
  無視します)
- 1 回ごとの結果は、モデルの気まぐれで変わります。1 問 3 回でも、小さな
  差は読み取れません
- `agent_cf` は、この道具を置いたフォルダも、エージェントが読める場所
  として渡します(skill が検索の CLI を呼ぶため)。そのフォルダに質問や
  正解があると、エージェントから読めます。このリポジトリには、公開の
  評価例(`examples/o3de`)が入っています。その質問で測るときは、
  評価例とテストを除いた写しから動かしてください(`git archive` で
  書き出して、`examples` と `tests` を消す)
- Serena と LSP の手法を確かめたのは、Claude Code 2.1.331 と Serena 2.1.0、
  clangd 22.1.1 の、合成のコーパスでの試し流しだけです
  ([計測の一覧](../docs/benchmarks.md)の「Serena と LSP を足した比較の
  試し流し」)。Copilot CLI では、まだ流していません【未確認】
- 確かめたのは、Copilot CLI 1.0.112 と Claude Haiku 5.5(BYOK)です。
  合成のコーパスでの 5 回のほかに、公開されているコード 2 つで、
  `agent_grep` と `agent_cf` を流しました
  ([計測の一覧](../docs/benchmarks.md))。`--no-custom-instructions` を
  付けた実行は、そのうちの O3DE での 166 回です。`agent_grep_cxxref` は、
  実行していません
