# Copilot CLI・アプリで使う

GitHub Copilot CLI と、GitHub Copilot のデスクトップアプリ(以下、アプリ)
から、この道具の検索を呼べるようにする手順と、確かめた範囲をまとめます。
Visual Studio Code は、今回の確認と手順の対象にしていません。

確度の表記: 【公式】一次文書で確かめた / 【実測】手元で動かして確かめた /
【未確認】確かめていない。確かめた日は 2026-11-16、Copilot CLI は 1.0.112
です。

## 出し方は 2 つあり、既定は skill

| 出し方 | 中身 | 登録 |
| --- | --- | --- |
| skill(既定) | `skills/context-search`。モデルが必要なときに読み、検索の CLI を 1 回呼ぶ | `register-copilot.bat` |
| MCP サーバー | ツールは `search` と `status` だけ。読み取り専用 | `register-copilot.bat --mcp` |

どちらも同じ索引を引き、同じ形の結果(順位つきの passage)を返します。

### skill を既定にした理由

判断の材料は 2 つです。

1. 同じ作者の cxxref の計測(Copilot CLI、公開されている資料)。MCP
   サーバーの形では、ツールの定義と使い方の指示を、使わない回でもモデルを
   呼ぶたびに送っていた(1 回あたり約 2,100 トークン)。skill の形にすると、
   常に送るのは説明文だけになり、関係のない作業での費用は、道具を入れない
   ときと変わらなかった。
   <https://github.com/noxitro/cxxref/blob/main/docs/benchmarks.md>
2. この道具での実測(合成のコーパス、同じ質問、同じ道具の一式、各 1 回)。
   モデルを呼ぶ 1 回目の入力は、道具なしの 10,543 トークンに対して、skill
   とセッションの始めの 1 文で +358、MCP サーバーで +365 だった。

2 のとおり、この道具では、索引のある作業フォルダでの上乗せに差がほとんど
ありません(MCP のツールが 2 つだけで、説明も短いためです)。それでも skill
を既定にしたのは、次の理由です。

- 索引の無い作業フォルダでは、skill の上乗せは説明文だけになる。始めの
  1 文は、索引のある作業フォルダでしか足さない。MCP サーバーは、どの
  作業フォルダでも同じ定義を送る
- 結果の読み方や、見つからないときの振る舞いは、skill の本文に書ける。
  本文は、skill が読まれたときだけ送られる
- cxxref と同じ形(skill と hook)にそろう。登録のしかたと考え方が 1 つで
  済む

skill の形の弱みも、実測で分かっています。

- 検索のたびに、モデルの呼び出しが 1 回増える(skill を読む回)
- 検索のたびに CLI を起動するので、埋め込みモデルの読み込みに毎回 1〜2 秒
  かかる。MCP サーバーは起動したままなので、2 回目からは数十ミリ秒
- shell の許可が要る(下の「許可」)
- skill を読むかどうかはモデルが決める。Claude Haiku 5.5 は、説明文だけ
  では読まずに grep から始めた。セッションの始めの 1 文を足すと読んだ。
  MCP のツールは、何も足さなくても呼ばれた

どちらが良いかは、実コーパスで両方を測って決めるのが確実です。計測の
道具は、両方の形に対応しています(`agent_cf` と `agent_cf_mcp`。
[comparison-runbook.md](comparison-runbook.md))。

## 登録する

`register-copilot.bat` をダブルクリックします(中身は
`ctxf copilot register`)。Copilot CLI の利用者設定のフォルダ
(`%USERPROFILE%\.copilot`。環境変数 `COPILOT_HOME` があれば、そのフォルダ)
に、次のものを足します。

| 足すもの | 場所 | 中身 |
| --- | --- | --- |
| skill のフォルダ | `settings.json` の `skillDirectories` | このリポジトリの `skills` フォルダのパスを 1 行 |
| hook | `hooks\context-foundation.json` | セッションの始めに `ctxf.py session-hint` を動かす定義 |
| MCP サーバー(`--mcp` のときだけ) | `mcp-config.json` | `context-foundation` という名前のサーバー |

- skill は、`copilot skill add <フォルダ>` で登録します。フォルダを写さず、
  その場所を登録するだけです【公式・実測】。このリポジトリを更新すれば、
  登録し直さなくても新しい内容が使われます。リポジトリのフォルダを動かした
  ときは、登録し直します
- hook は、作業フォルダかその親に索引(`.cf\index.db`)があるときだけ、
  skill の使いどころを 1 文で知らせます。索引が無ければ何も足しません
- 解除は `unregister-copilot.bat`(`ctxf copilot unregister`)です。足した
  ものだけを消します。今の状態は `ctxf copilot status` で見られます

プラグインの形(`copilot plugin install <フォルダ>`)にしなかった理由:
手元のフォルダを直接入れる形は、入れるときに「将来は使えなくなる」という
警告が出ます。また、フォルダ全体(仮想環境も)が Copilot の設定フォルダに
写されます【実測】。チームで配るときは、マーケットプレイスに載せる形を
別に用意する必要があります(今回は作っていません)。

## アプリで使う

アプリは Copilot CLI の上に作られていて、リポジトリや CLI に設定した skill
と MCP サーバーは、アプリでもそのまま使えると書かれています【公式】。
そのため、上の登録だけで、アプリからも使えるはずです。

アプリでの確認の手順(自分では確かめていません【未確認】):

1. `register-copilot.bat` で登録する
2. アプリを起動し直す
3. サイドバーの Customize を開き、Skills に `context-search` が出ている
   ことを確かめる(`--mcp` で登録したときは、MCP の Installed に
   `context-foundation` が出ていること)
4. 索引を作った作業フォルダを開いて、新しいセッションを始める
5. 名前の分からないものを探す質問をする(例: 「〜に相当する処理はどこ?」)
6. 途中の道具の呼び出しに、skill `context-search` と、`ctxf.py search`
   の実行が出ること、回答に `source:path:行-行` の形の根拠が出ることを見る

アプリについて確かめていない点:

- hook(セッションの始めの 1 文)が、アプリでも働くか。公式文書は、hook を
  CLI の機能として説明しています。アプリの文書では、hook はプラグインの
  部品としてだけ触れられています
- shell の許可を尋ねる画面が、アプリでどう出るか

## 確かめる(Copilot CLI)

1. `copilot skill list` に `context-search` が出ることを確かめる
2. 索引を作った作業フォルダで `copilot` を起動する
3. 名前の分からないものを探す質問をする
4. skill が読まれ、`python -I "<このリポジトリ>\ctxf.py" search "…"` が
   1 回実行され、passage が返ることを見る

### 許可

skill は、shell で `python` を起動します。入口の `ctxf.py` は作業フォルダの
外にあるので、対話のセッションでは、最初の実行のときに許可を尋ねられます。

確認のできない実行(`copilot -p`)では、次の指定で通りました【実測】。

```text
copilot -p "<質問>" --allow-tool "shell(python:*)" --add-dir <このリポジトリ>
```

`shell(python:*)` は、`python` で始まる命令をすべて通します。この道具の
呼び出しだけを通す指定ではありません。計測では、書き込みを断る指定
(`--deny-tool write`)と組み合わせています。

MCP サーバーの形では、shell の許可は要りません。ツールの許可は
`--allow-tool context-foundation` です。

## 確かめたこと

Copilot CLI 1.0.112 を、確認のできない実行(`copilot -p`)で 5 回動かし
ました。モデルは Claude Haiku 5.5 です。Copilot の契約のモデルではなく、
利用者自身の Anthropic の API のキーを使う設定(BYOK)で動かしました。
題材は `tests/fixtures` の合成のコーパス、設定フォルダは毎回空のものです。

| 回 | 形 | 質問 | 使われた道具 | モデルの呼び出し | 入力トークン | 時間 |
| --- | --- | --- | --- | ---: | ---: | ---: |
| 1 | skill(始めの 1 文なし) | F001 | grep 2、view 3(skill は読まれず) | 3 | 38,761 | 17.4 秒 |
| 2 | skill(始めの 1 文あり) | F002 | skill 1、shell 1 | 3 | 36,705 | 12.0 秒 |
| 3 | MCP(道具の一式が少ない設定) | F002 | grep 1、search 1、view 1 | 3 | 25,106 | 12.6 秒 |
| 4 | 道具なし(既存の手順) | F002 | grep 1、view 1 | 3 | 37,185 | 10.6 秒 |
| 5 | MCP | F002 | grep 2、search 1、view 1 | 4 | 56,653 | 13.6 秒 |

- 回 2 で、hook が 1 文を足し、モデルが skill を読み、検索の CLI を 1 回
  呼び、返った passage(約 5.4 KB)から答えて、その場所を根拠に挙げた
  ことを、セッションの記録で確かめました【実測】
- 回 3 と 5 で、MCP の `search` が呼ばれ、結果が回答に使われました【実測】
- 回 3 は、渡した道具の一式がほかの回より少ないので、入力トークンは
  比べられません。回 2・4・5 は、同じ一式です
- 各 1 回だけの結果で、コーパスも 13 ファイルと小さいので、どの形が
  安いかは、ここからは言えません。確かめたのは、仕組みが通ることです
- 登録と解除のコマンドは、空の設定フォルダ(`COPILOT_HOME`)に向けて動かし、
  上の表のとおりに足され、消えることを確かめました【実測】。利用者のふだんの
  設定には、登録していません

## 確かめていないこと

- アプリでの動作(上の「アプリで使う」)
- 対話のセッションで、登録した skill と hook が働くこと(確かめたのは、
  同じ skill のフォルダと同じ hook を、空の設定フォルダに置いた形です)
- Claude Haiku 5.5 のほかのモデルで、skill が読まれるか
- Visual Studio Code

## 組み込みの意味検索について

Copilot の側に意味検索があるなら、それを使えば済みます。公式文書で確かめた
範囲では、最初の対象(CLI とアプリ)には載っておらず、Visual Studio Code の
ものは、対応する場所に置いていない作業フォルダではファイルを外部に送ります。
詳しくは [evaluation-data-spec.md](evaluation-data-spec.md) の
「Why built-in semantic search is not the default」にまとめています。

## 出典

公式文書(GitHub Docs。2026-11-16 に取得):

- アプリの概要(CLI の上に作られている、クラウドで動かすセッションがある)
  <https://docs.github.com/en/copilot/concepts/copilot-surfaces/github-copilot-app>
- アプリの設定(skill と MCP サーバーは CLI のものが使える、プラグイン)
  <https://docs.github.com/en/copilot/how-tos/github-copilot-app/customize-github-copilot-app>
- skill の置き場所と `copilot skill add`
  <https://docs.github.com/en/copilot/how-tos/copilot-cli/customize-copilot/add-skills>
- CLI のコマンドとオプション、道具の一覧、許可の書き方、環境変数
  <https://docs.github.com/en/copilot/reference/copilot-cli-reference/cli-command-reference>
- hook の置き場所と、セッションの始めの hook の入出力
  <https://docs.github.com/en/copilot/reference/hooks-reference>
- MCP サーバーの登録
  <https://docs.github.com/en/copilot/how-tos/copilot-cli/customize-copilot/add-mcp-servers>
- プラグインの作り方と、手元のフォルダからの導入
  <https://docs.github.com/en/copilot/how-tos/copilot-cli/customize-copilot/plugins-creating>
- 自分のキーで使うモデルの設定(BYOK)
  <https://docs.github.com/en/copilot/how-tos/copilot-cli/customize-copilot/add-llm-models>
- 大きな道具の結果の扱い(既定で 20 KiB を超えるとファイルに退かせる)
  <https://docs.github.com/en/copilot/concepts/prompting-and-context/context-management>
