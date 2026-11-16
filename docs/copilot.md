# Copilot CLI・アプリで使う

GitHub Copilot CLI と、GitHub Copilot のデスクトップアプリ(以下、アプリ)
から、この道具の検索を呼べるようにする手順と、確かめた範囲をまとめます。
Visual Studio Code は、今回の確認と手順の対象にしていません。

確度の表記: 【公式】一次文書で確かめた / 【実測】手元で動かして確かめた /
【未確認】確かめていない。確かめた日は 2026-11-16、Copilot CLI は 1.0.112
です。

## 入れ方は 2 つ

このリポジトリは、Copilot のプラグイン `context-foundation` で、リポジトリ
自身がそのプラグインを載せた Marketplace です(`plugin.json`、
`.github/plugin/marketplace.json`)。プラグインには、skill
`context-search` と、セッションの始めの hook が入っています。

| 入れ方 | 向いている人 | Python の依存の置き場 |
| --- | --- | --- |
| GitHub の Marketplace から | アプリや CLI で使う人、チームに配るとき | 利用者ごとの置き場(`ctxf.py setup` が作る) |
| リポジトリを取得して `register-copilot.bat` | 手元で試す人、道具を直す人 | 取得したフォルダの `.venv`(`setup.bat` が作る) |

どちらも、Python 3.11 以上が `python` という名前で使えることが前提です。
hook と skill は、`python` で入口の `ctxf.py` を動かします。`python` が
無い PC では、hook は何も足さず、skill は検索できません。

## GitHub の Marketplace から入れる

### アプリ

1. サイドバーの Customize を開き、Marketplace の選択の隣にある設定から、
   GitHub のリポジトリ `noxitro/context-foundation` を足す【公式】
2. Customize の Browse で `context-foundation` を探し、Install を押す
   【公式】
3. 下の「最初の用意」と「索引を作る」を済ませる
4. 索引を作った作業フォルダで、新しいセッションを始める

アプリの文書は、プラグインが skill と hook を足すと説明しています【公式】。
アプリでの実際の動作は、まだ確かめていません【未確認】。確かめるときは、
名前の分からないものを探す質問(例: 「〜に相当する処理はどこ?」)をして、
途中の道具の呼び出しに skill `context-search` と `ctxf.py search` の実行が
出ること、回答に `source:path:行-行` の形の根拠が出ることを見ます。

### CLI

```text
copilot plugin marketplace add noxitro/context-foundation
copilot plugin install context-foundation@context-foundation
```

GitHub から入れたプラグインは、Copilot の設定フォルダの
`installed-plugins\context-foundation\context-foundation` に写されます
【公式】。写されるのは、リポジトリで git が追跡しているファイルだけです
(同じ作者の cxxref を同じ方法で入れて、追跡しているファイル 150 個と
写されたファイル 150 個が一致し、`.git` は無いことを確かめました【実測】)。
仮想環境やモデルは、プラグインに入りません。

更新は `copilot plugin update context-foundation` です【公式】。プラグインを
更新すると、ソースはそのまま新しいものになります。Python の依存の一覧が
変わったときだけ、もう一度「最初の用意」が要ります(そうと知らせます)。

### 最初の用意(setup)

検索には、Python のパッケージ(PyPI から約 200 MB)と、埋め込みモデル
(Hugging Face から約 490 MB)が要ります。これを、プラグインのフォルダの外の
利用者ごとの置き場(`%LOCALAPPDATA%\context-foundation`。環境変数
`CF_HOME` で変えられる)に、1 回だけ置きます。

- エージェントに頼む: 用意ができていないと、skill がそう知らせ、エージェント
  は利用者に尋ねてから `ctxf.py setup` を動かします
- 自分で動かす: 次の 1 行です(`<プラグインのフォルダ>` は上の場所)

```text
python -I "<プラグインのフォルダ>\ctxf.py" setup
```

| 置くもの | 場所 | 大きさ |
| --- | --- | --- |
| 仮想環境(依存のパッケージ) | `%LOCALAPPDATA%\context-foundation\runtime\venv` | 約 180 MB |
| 依存の一覧の覚え書き | `...\runtime\deps.json` | 小さい |
| 埋め込みモデル | `%LOCALAPPDATA%\context-foundation\models` | 約 490 MB |

- `setup --no-model` でモデルを取らずに済ませると、keyword(BM25)だけの
  検索になります
- `setup --help` は、何も取得せずに説明だけを出します
- 途中で止まった仮想環境は、次の `setup` で作り直します。2 つの `setup`
  が同時に走らないよう、排他します
- 使われる python と置き場は `python -I "<…>\ctxf.py" where` で見られます

### 索引を作る

作業フォルダ(Copilot を起動するフォルダ)で、検索したいフォルダを並べて
設定を作り、索引を作ります。エージェントに頼んでも、自分で動かしても
構いません。

```text
python -I "<プラグインのフォルダ>\ctxf.py" init "<エンジンのソース>" "<設計文書>"
python -I "<プラグインのフォルダ>\ctxf.py" index
```

最初の索引づくりは、約 8,000 ファイルで 30 分ほどかかります(ほとんどが
埋め込み)。2 回目からは、変わっていない passage の埋め込みを使い回すので、
変わったファイルの分だけで済みます([design.md](design.md) の「差分更新」)。

## リポジトリを取得して使う

1. `setup.bat`: 取得したフォルダに `.venv` を作り、依存を入れる。モデルを
   取得するかを尋ねる
2. `index.bat`: 作業フォルダと検索したいフォルダを尋ねて、索引を作る
3. `register-copilot.bat`: このフォルダを手元の Marketplace として足し、
   プラグインを入れる(中身は `ctxf copilot register`)

```text
copilot plugin marketplace add <このフォルダ>
copilot plugin install context-foundation@context-foundation
```

- 手元のフォルダの Marketplace から入れたプラグインは、写されずに、その
  フォルダから直接読まれます。Copilot CLI は「It is loaded live from …
  nothing was copied」と出します【実測】。このフォルダを更新すれば、次の
  セッションから新しい内容で動き、`.venv` もこのフォルダのものを使います
- 同じ名前の Marketplace やプラグインが、ほかの場所(GitHub や別の
  フォルダ)から入っているときは、何も変えずに知らせて止まります
- 0.1 の登録の形(`copilot skill add` と、`hooks\context-foundation.json`)
  が残っていれば、確認のうえで外します
- 解除は `unregister-copilot.bat`(`ctxf copilot unregister`)です。この
  フォルダから入れたものだけを外します。状態は `ctxf copilot status` で
  見られます
- フォルダのパスに `& | < > ^ % ( ) ! "` があると、npm で入れた Copilot CLI
  (`copilot.cmd`)に正しく渡せないので、登録を断ります

## 出し方: skill と hook(既定)、MCP サーバーは追加

| 出し方 | 中身 | 入れ方 |
| --- | --- | --- |
| skill と hook(既定) | `skills/context-search`。モデルが必要なときに読み、検索の CLI を 1 回呼ぶ。hook は、索引のある作業フォルダでだけ、skill の使いどころを 1 文で知らせる | プラグイン |
| MCP サーバー | ツールは `search` と `status` だけ。読み取り専用 | `register-copilot.bat --mcp` |

skill を既定にした理由:

- 同じ作者の cxxref の計測で、MCP サーバーの形は、ツールの定義と使い方の
  指示を、使わない回でもモデルを呼ぶたびに送っていた(1 回あたり約 2,100
  トークン)。skill の形では、常に送るのは説明文だけだった
  <https://github.com/noxitro/cxxref/blob/main/docs/benchmarks.md>
- この道具での実測(合成のコーパス、各 1 回)では、モデルを呼ぶ 1 回目の
  入力の上乗せは、skill と始めの 1 文で +358、MCP サーバーで +365 トークン
  と、ほとんど差が無かった。それでも、索引の無い作業フォルダでは skill の
  上乗せが説明文だけになること、結果の読み方を skill の本文に書けること、
  cxxref と同じ形にそろうことから、skill にした

skill の形の弱み(実測):

- 検索のたびに、モデルの呼び出しが 1 回増える(skill を読む回)
- 検索のたびに CLI を起動するので、1 回に 1.5〜2 秒かかる(約 7 万 passage
  の索引で、中央値 2.0 秒)。MCP サーバーは起動したままなので、2 回目から
  は 10〜30 ミリ秒([design.md](design.md) の「規模と時間」)
- shell の許可が要る(下の「許可」)
- skill を読むかどうかはモデルが決める。Claude Haiku 5.5 は、説明文だけ
  では読まずに grep から始めた。始めの 1 文を足すと読んだ。MCP のツールは、
  何も足さなくても呼ばれた

## 許可

skill は、shell で `python` を起動します。入口の `ctxf.py` は作業フォルダの
外にあるので、対話のセッションでは、最初の実行のときに許可を尋ねられます。
確認のできない実行(`copilot -p`)では、`--allow-tool "shell(python:*)"`
で通りました【実測】。この指定は、`python` で始まる命令をすべて通します。
計測では、書き込みを断る指定(`--deny-tool write`)と組み合わせています。

MCP サーバーの形では、shell の許可は要りません。ツールの許可は
`--allow-tool context-foundation` です。

## 確かめたこと

### 0.2(プラグインの形)

- 手元のフォルダを Marketplace として足して入れると、写されずにそのフォルダ
  から読まれた。設定フォルダは 25 KB のままで、`.venv` は写らなかった
  【実測】
- GitHub から入れたプラグインには、git が追跡しているファイルだけが写る
  (上の「CLI」)【実測】
- `.venv` の無い git の複製(GitHub から入れたときと同じ状態)で、
  `ctxf.py setup` → `init` → `index` → `search` と hook の 1 文が、利用者
  ごとの置き場の仮想環境で動いた。`setup` は 19 秒だった(パッケージと
  モデルは手元のキャッシュにあった。初めての PC では取得の時間がかかる)
  【実測】
- 空の設定フォルダ(`COPILOT_HOME`)にプラグインを入れ、`copilot -p` を
  1 回動かした(モデルは Claude Haiku 5.5。利用者自身の Anthropic の API の
  キーを使う設定 BYOK)。題材は合成のコーパス、質問は日本語で「GPU の
  バッファを確保して使い回している処理はどこ」。次の流れを、セッションの
  記録で確かめた【実測】
  - セッションの始めの hook(プラグインの `hooks.json`。`command` の形で
    Windows でも動いた)が 1 文を足した
  - モデルが、プラグインの skill `context-search` を読んだ
  - モデルが英語の語を足した検索語で `ctxf.py search` を 1 回動かした
  - 返った passage の場所(`engine:Render/BufferPool.cpp:28-51` など)を
    根拠に挙げて答えた
  - モデルの呼び出し 3 回、入力 87,485 トークン(うちキャッシュの読み
    56,087、書き 31,388)、出力 945 トークン、API の時間 5.7 秒

### 0.1(skill のフォルダを登録する形)

Copilot CLI 1.0.112 を `copilot -p` で 5 回動かしました(BYOK の Claude
Haiku 5.5、合成のコーパス、毎回空の設定フォルダ)。

| 回 | 形 | 質問 | 使われた道具 | モデルの呼び出し | 入力トークン | 時間 |
| --- | --- | --- | --- | ---: | ---: | ---: |
| 1 | skill(始めの 1 文なし) | F001 | grep 2、view 3(skill は読まれず) | 3 | 38,761 | 17.4 秒 |
| 2 | skill(始めの 1 文あり) | F002 | skill 1、shell 1 | 3 | 36,705 | 12.0 秒 |
| 3 | MCP(道具の一式が少ない設定) | F002 | grep 1、search 1、view 1 | 3 | 25,106 | 12.6 秒 |
| 4 | 道具なし(既存の手順) | F002 | grep 1、view 1 | 3 | 37,185 | 10.6 秒 |
| 5 | MCP | F002 | grep 2、search 1、view 1 | 4 | 56,653 | 13.6 秒 |

- 回 3 は、渡した道具の一式がほかの回より少ないので、入力トークンは比べ
  られません。回 2・4・5 は、同じ一式です
- 各 1 回だけの結果で、コーパスも小さいので、どの形が安いかは言えません。
  確かめたのは、仕組みが通ることです
- 作業フォルダは版管理の外で、Copilot CLI が指示として読むファイル
  (`AGENTS.md` など)は置いていません

## 確かめていないこと

- アプリでの動作(上の「アプリ」)
- 対話のセッションでの動作(確かめたのは `copilot -p`)
- Claude Haiku 5.5 のほかのモデルで、skill が読まれるか
- `python` が無い PC、Microsoft Store 版の Python での動作
- GitHub の Marketplace からこのリポジトリ自身を入れること(この変更が
  公開されてから確かめる)
- Visual Studio Code

## チームの Marketplace に載せる

チームや個人の Marketplace の `marketplace.json` には、GitHub の場所で
載せられます【公式】。版を固定するなら、`sha` に 40 文字のコミットの
ハッシュを書きます。

```json
{
  "name": "context-foundation",
  "source": { "source": "github", "repo": "noxitro/context-foundation" },
  "description": "Find code and design docs by meaning and keywords, locally.",
  "version": "0.2.0"
}
```

## 組み込みの意味検索について

Copilot の側に意味検索があるなら、それを使えば済みます。公式文書で確かめた
範囲では、最初の対象(CLI とアプリ)には載っておらず、Visual Studio Code の
ものは、対応する場所に置いていない作業フォルダではファイルを外部に送ります。
詳しくは [evaluation-data-spec.md](evaluation-data-spec.md) の
「Why built-in semantic search is not the default」にまとめています。

## 出典

公式文書(GitHub Docs。2026-11-16 に取得):

- アプリの概要(CLI の上に作られている)
  <https://docs.github.com/en/copilot/concepts/copilot-surfaces/github-copilot-app>
- アプリの設定(Customize、プラグインと Marketplace、skill と hook)
  <https://docs.github.com/en/copilot/how-tos/github-copilot-app/customize-github-copilot-app>
- プラグインと Marketplace の形
  <https://docs.github.com/en/copilot/reference/plugin-and-marketplace-reference>
- CLI のプラグインのコマンド、置き場所、`marketplace.json` の書き方
  <https://docs.github.com/en/copilot/reference/copilot-cli-reference/cli-plugin-reference>
- プラグインを探して入れる
  <https://docs.github.com/en/copilot/how-tos/copilot-cli/customize-copilot/plugins-finding-installing>
- プラグインの作り方
  <https://docs.github.com/en/copilot/how-tos/copilot-cli/customize-copilot/plugins-creating>
- skill の置き場所
  <https://docs.github.com/en/copilot/how-tos/copilot-cli/customize-copilot/add-skills>
- CLI のコマンドとオプション、道具の一覧、許可の書き方、環境変数
  <https://docs.github.com/en/copilot/reference/copilot-cli-reference/cli-command-reference>
- hook の入出力
  <https://docs.github.com/en/copilot/reference/hooks-reference>
- MCP サーバーの登録
  <https://docs.github.com/en/copilot/how-tos/copilot-cli/customize-copilot/add-mcp-servers>
- 自分のキーで使うモデルの設定(BYOK)
  <https://docs.github.com/en/copilot/how-tos/copilot-cli/customize-copilot/add-llm-models>
- 指示のファイル(`AGENTS.md` など)が読まれる場所
  <https://docs.github.com/en/copilot/how-tos/copilot-cli/customize-copilot/add-custom-instructions>
- 大きな道具の結果の扱い(既定で 20 KiB を超えるとファイルに退かせる)
  <https://docs.github.com/en/copilot/concepts/prompting-and-context/context-management>
