# context-foundation(日本語の説明)

大きなコードベースと設計文書から、AI エージェントが自然文の質問で根拠の
箇所を探せるようにする、検索の PoC(試作)です。

## 目的

「識別子が分からないまま、場所を探す」質問に答えるための道具です。

- 別のエンジンの用語や、日本語の自然文で、該当するコードを探す
  (例: 「〜に相当する処理はどこ?」)
- 設計文書の記述から、その実装を探す。コードから、説明している文書を探す
- 似た実装がほかに無いかを探す

識別子が分かっている参照の検索(この関数はどこから呼ばれているか)は、
grep や、同じ作者の cxxref(clang の索引)が向いています。この道具は
その補完で、置き換えではありません。

この道具がするのは検索だけです。順位つきの passage(ファイルのパス・行の
範囲・本文・外側のスコープ・シグネチャ)を返します。回答を作ることと、
コードを変えることは、呼び出す側のエージェントに任せます。

索引づくりも埋め込みの計算も、この PC の中で行います。この道具が、ソースの
中身を外部のサービスに送ることはありません(下の「データの扱い」)。

## 状態

PoC です。動かしたのは、合成のコーパスと、公開されているコードだけです。
公開されているコード(Godot)で既存の手順と比べたところ、事前に決めた
目安には届かず、既存の手順との差も出ていませんでした。そのあと、
エージェントへの出し方を MCP サーバーに変え(0.3)、同じ質問で比べ直すと、
回答の合格率は既存の手順とほぼ同じ(75.9% と 74.7%)で、時間は 35%、費用は
32% 下がりました。回答の合格率は、事前に決めた答えの要点では、どちらも
目安の 80% に届いていません(結果を見たあとに、要点が質問の聞いている
内容かを点検し直すと、93.1% と 88.5% で、どちらも届きます)。
Godot での判定には、点検後の数字を採りました。エージェントで使う形は、
Godot では目安を満たしています。質問の文をそのまま検索語にする、検索
だけの使い方は、満たしていません。使う人自身のコーパスでの比較(効果が
あるかの判断)は、まだ行っていません。比較の手順は
[docs/comparison-runbook.md](docs/comparison-runbook.md) にあります。
これまでに測ったものと、まだ測っていないものは、
[docs/benchmarks.md](docs/benchmarks.md) にまとめています。

公開のコード(O3DE)を題材にした評価例では、検索だけの手法は、仕様の
しきい値(上位 5 件に正解がある質問が 80% 以上)に届いていません(既定の
hybrid で 54%)。質問と正解、結果は
[examples/o3de](examples/o3de/README.md) にあります。この評価例では、
エージェントがこの検索を使って答えるときの正しさは、まだ測っていません。

対象は、まず GitHub Copilot CLI と、GitHub Copilot のデスクトップアプリ
です。Visual Studio Code は後回しで、手順も確認もまだありません。

## 前提

- Windows(ほかの OS でも Python の部分は動きますが、.bat は Windows 用です)
- Python 3.11 以降(3.13 で確かめています)
- Copilot から使うときは、GitHub Copilot CLI(1.0.112 で確かめています)

git は要りません。Perforce の作業フォルダや、ただのフォルダでも、同じように
索引を作れます。

## 使い始める

入れ方は 2 つです。詳しい手順と確かめた範囲は [docs/copilot.md](docs/copilot.md)
にあります。

**Copilot のプラグインとして入れる(アプリ・CLI で使う人)**: このリポジトリは
Copilot の Marketplace です。アプリでは Customize に GitHub のリポジトリ
`noxitro/context-foundation` を Marketplace として足し、プラグイン
`context-foundation` を入れます。CLI では次の 2 行です。

```text
copilot plugin marketplace add noxitro/context-foundation
copilot plugin install context-foundation@context-foundation
```

その後、最初に 1 回だけ用意(`ctxf.py setup`。依存のパッケージとモデルを、
利用者ごとの置き場に取得する)をし、作業フォルダで索引を作ります。どちらも、
エージェントに頼めば、利用者に尋ねてから動かします。

**リポジトリを取得して使う(手元で試す人)**: どれもダブルクリックで動きます。

| 順 | ファイル | すること |
| --- | --- | --- |
| 1 | `setup.bat` | 初回の用意。仮想環境(`.venv`)を作り、依存するパッケージを入れる。埋め込みモデルを取得するかを尋ねる |
| 2 | `index.bat` | 索引を作る。作業フォルダと、検索したいフォルダを尋ねる |
| 3 | `search.bat` | 検索を試す。質問を入れると、結果が出る |
| 4 | `register-copilot.bat` | このフォルダを手元の Marketplace として足し、プラグインを入れる(Copilot CLI とアプリの両方に効く) |

- **作業フォルダ**は、Copilot を起動するフォルダです。ここに `.cf` という
  フォルダができ、設定(`config.toml`)と索引(`index.db`)が入ります
- **検索したいフォルダ**は、いくつでも指定できます(エンジンのソース、
  参考にするアプリ、設計文書など)。作業フォルダの外にあっても構いません
- 索引づくりの時間は、ほとんどが埋め込みの計算です。GPU(Windows の
  DirectX 12 の GPU)で計算すると、6〜7 倍速くなります。入れ方は
  [docs/design.md](docs/design.md) の「GPU で計算する」にあります
- ソースが変わったら、もう一度 `index.bat` を実行します。変わっていない
  passage の埋め込みは前の索引から使い回すので、変わったファイルの分だけの
  時間で済みます(差分更新)。索引の後に変わったファイルを指す結果には、
  その旨の印が付きます
- `.cf` フォルダは、git には入りません(中に、全部を無視する `.gitignore`
  を置きます)。Perforce では、`.cf` を `.p4ignore` に足してください

`setup.bat` が取得するもの:

| もの | 入手先 | 大きさ | ライセンス |
| --- | --- | --- | --- |
| Python のパッケージ(tree-sitter と C・C++・C# の文法、numpy、onnxruntime、tokenizers、huggingface_hub、jsonschema、pytest と、その依存) | PyPI | 仮想環境で約 190 MB | MIT・BSD・Apache-2.0 など |
| 埋め込みモデル `intfloat/multilingual-e5-small`(ONNX と tokenizer) | Hugging Face | 約 490 MB | MIT |

時間の目安(16 コアの CPU で測ったもの。GPU は使いません):

- 最初の索引づくりは、約 1,300 ファイル(16 万行)で 3 分、約 8,000 ファイル
  (143 万行)で 30 分ほどです。ほとんどが、埋め込みの計算です
- 2 回目からは、変わったファイルの分だけ埋め込み直します(約 120 ファイル・
  950 passage の例で、作り直し 19 秒に対して、5 ファイルを変えたときは
  1.3 秒)
- 索引の大きさは、ソースの 2〜3 倍です(143 万行で 135 MB)
- 検索は、1 回 2 秒ほどです(コマンドの起動と、モデルの読み込みを含む)

埋め込みモデルは、取得しなくても使えます。そのときは、keyword(BM25)だけの
検索になります。モデルの置き場は `%LOCALAPPDATA%\context-foundation\models`
です(環境変数 `CF_HOME` で、置き場の親を変えられます)。インターネットに
出られない PC では、ほかの PC で取得したこのフォルダを写せば使えます。

## 検索の方法

| 方法 | 中身 | 向いている質問 |
| --- | --- | --- |
| `keyword` | BM25。識別子を分けた語(`BufferPool` → buffer, pool)でも当たる | 識別子や、コードに出てくる語が分かっているとき |
| `semantic` | 手元の埋め込みで、意味の近さを見る | 日本語の質問で英語のコードを探すなど、語が一致しないとき |
| `hybrid`(既定) | 2 つの順位をまとめる | どちらか分からないとき |

日本語の質問で英語のコードを探すとき、そのままでは keyword にほとんど語が
当たりません。そこで、検索の前に、用語の対応表で質問に英語の語を足します
(足した語は、結果の note に出ます)。

- 既定の対応表: 日本語の一般の語(描画 → render draw、確保 → allocate
  など)。エージェントがすでに英語の語を足した質問には当てません
- 作業フォルダの対応表: `.cf\glossary.tsv`。エンジンに固有の語や、別の
  エンジンの用語との対応を、チームで書きます。1 行 1 語で、`<語><タブ><足す語>`
  です(例: `AudioMixer<タブ>mixer bus volume`)

公開のゲームエンジン(O3DE)を題材に、行の範囲で正解を付けた質問で測ると、
既定の hybrid で上位 5 件に正解があった割合は、評価用の 24 問で 54%、
日本語だけで聞く調整用の 22 問で 55%(対応表なしでは 9%)でした。名前を
知らずに言葉で聞く質問は、まだ弱いです(評価用の 9 問で 22%)。質問と
正解と測り方は [examples/o3de](examples/o3de/README.md) に、試したことと
数字の詳しいことは [docs/design.md](docs/design.md) の「公開の評価例での
計測」にあります。

## コマンド

`.bat` の中身は、次のコマンドです。作業フォルダ(かその下)で実行すると、
親へさかのぼって `.cf` を探します。

```text
.venv\Scripts\python -m context_foundation <コマンド>
python -I <このフォルダかプラグインのフォルダ>\ctxf.py <コマンド>
```

2 行目の入口(`ctxf.py`)は、使える python を探して同じコマンドを動かします
(環境変数 `CF_PYTHON`、隣の `.venv`、利用者ごとの置き場の順)。入口だけの
コマンドは `setup`(依存とモデルを利用者ごとの置き場に用意する。
`--no-model`・`--help`)と `where`(使う python と置き場を出す)です。

| コマンド | すること |
| --- | --- |
| `init <フォルダ>...` | 作業フォルダに設定(`.cf\config.toml`)を作る。対話なし。`--force` で作り直す(前の設定は `.bak` に残す) |
| `index` | 索引を作る。変わっていない passage の埋め込みは前の索引から使い回す。`--full` ですべて埋め込み直す、`--no-embed` で keyword だけ、`--download` でモデルを先に取得、`--revision <版>` でコーパスの版を記録 |
| `search "<質問>"` | 検索する。`--mode`、`--top-k`、`--source <id>`、`--format agent\|human\|json` |
| `status` | 索引の版・件数・作成日時・作成にかかった時間、構文で分けられなかった割合と、その多いファイル |
| `eval --cases <cases.jsonl> --out <フォルダ>` | 評価を回して、要約を書く |
| `validate --cases <cases.jsonl>` | 評価データの形式を検査する |
| `serve` | 読み取り専用の MCP サーバー(stdio) |
| `model download` / `model info` | 埋め込みモデルの取得と確認 |
| `copilot register` / `unregister` / `status` | このフォルダを手元の Marketplace としてプラグインを入れる・外す・状態を見る |

`search` の出力は、端末では人が読む形、そうでないとき(エージェントが
呼んだとき)は、エージェント向けの短い形になります。エージェント向けの形は、
上位の passage は本文ごと、残りは場所だけを返し、全体を 12,000 バイト
(設定で変更可。上限 18,000)に収めます。

## 設定ファイル

`.cf\config.toml` です。`index.bat` が最初に作ります。

```toml
[[source]]
id = 'engine'                 # 検索結果に出る名前(英数字と . _ -)
root = 'D:\work\engine'       # 絶対パスか、作業フォルダからの相対パス
exclude = ['**/ThirdParty/**']  # 既定の除外に足す

[[source]]
id = 'docs'
root = 'docs'
include = ['**/*.md']         # 省くと、既定の拡張子

[embedding]
enabled = true                # false にすると keyword だけ
# device = 'cpu'              # 索引づくりの埋め込みを GPU で計算しない(既定は auto: 使えれば GPU)

[search]
mode = 'hybrid'
top_k = 8
# glossary = 'glossary.tsv'   # 用語の対応表(省くと .cf\glossary.tsv があれば使う)
# builtin_glossary = false    # 既定の対応表を使わない
# preload = false             # MCP サーバーが、起動のときにモデルとベクトルを先に読み込まない
```

- 既定で入るのは、C・C++・C#・Markdown と、そのほかの文字のファイル
  (Python、シェーダー、設定ファイルなど)です
- 既定で除くのは、版管理やビルドの出力のフォルダ(`.git`、`build`、
  `bin`、`obj`、`node_modules` など)、バイナリ、生成物(先頭に自動生成の
  印があるもの、極端に長い行があるもの)、1 MB を超えるファイルです
- 除いた数は、`status` に理由ごとに出ます
- そのほかの項目(分割の大きさ、埋め込みモデルの差し替え、順位の重み)は
  [docs/design.md](docs/design.md) にあります

## Copilot から使う

プラグインを入れると、Copilot CLI とアプリの両方から使えます。入れ方、
解除のしかた、確かめた範囲は [docs/copilot.md](docs/copilot.md) にあります。

入れた後は、索引のある作業フォルダで Copilot を起動し、ふつうに質問する
だけです。プラグインには、読み取り専用の MCP サーバー(道具は `search` と
`status`)が入っています。セッションの始めの hook が、索引のある作業
フォルダでだけ「探すときは、検索を grep より先に」と 1 文で知らせ、モデルが
検索の道具を呼びます。MCP の道具が使えない場面のために、同じ検索を CLI で
呼ぶ skill `context-search` も入っています。

## 評価する

`eval.bat` は、質問の一覧(`cases.jsonl`)に対して keyword・semantic・hybrid
を回し、要約(`summary.md`)を書きます。モデルは呼ばないので、費用は
かかりません。

- 評価データの形式: [docs/evaluation-data-spec.md](docs/evaluation-data-spec.md)
  (英語)
- 実コーパスで、既存の手順と比べる手順:
  [docs/comparison-runbook.md](docs/comparison-runbook.md)
- エージェントを使う手法を Copilot CLI で回す道具: [bench](bench/README.md)。
  こちらは、実行のたびに費用がかかります
- 測ったものと、まだ測っていないものの一覧:
  [docs/benchmarks.md](docs/benchmarks.md)

非公開のコーパスの質問・正解の注釈・検索結果は、コーパスの中身を含み
ます。この公開リポジトリには入れないでください。公開のコーパスを題材に
した実例(質問と正解の場所、測った結果)は
[examples/o3de](examples/o3de/README.md) にあります。

## データの扱い

- 索引(`.cf\index.db`)には、対象のファイルの本文が入ります。作業フォルダの
  中だけに置かれ、この道具がどこかへ送ることはありません
- 埋め込みは、この PC の中で計算します(CPU。索引づくりは、設定によっては
  GPU)。モデルの取得のときに送るのは、取得するファイルの名前だけです
- エージェントから使うときは、話が別です。エージェントが読んだもの(質問、
  検索が返した passage、開いたファイル)は、そのエージェントが使うモデルの
  提供元に送られます。これは、この道具を使わない今の手順でも同じです。
  非公開のコーパスでエージェントを動かす前に、どの提供元になら送ってよいか
  を決めてください

## テスト

`run-tests.bat` で、すべてのテストが流れます。合成のコーパス
(`tests/fixtures`)で、分割・検索・評価・MCP サーバー・コマンドを通しで
確かめます。埋め込みモデルが無くても通ります。

GitHub Actions では、次の 3 つを回します(`.github/workflows`)。

| ワークフロー | いつ | すること |
| --- | --- | --- |
| CI | プルリクエストと main への push | テスト(Windows・Linux、Python 3.11 と 3.13)、Markdown の検査、リンクの確認 |
| Plugin smoke test | 道具の中身に関わる変更と main への push | まっさらな機械で、`ctxf.py setup` から、実物の埋め込みモデルでの索引づくり・検索・評価・MCP サーバーまでを通す(Windows・Linux) |
| Benchmark (O3DE example) | 手で起動したとき | 公開の評価例([examples/o3de](examples/o3de/README.md))を、GitHub の機械で最初から測り直す |

エージェントを使う比較(Copilot CLI を動かす計測)は、GitHub Actions では
流しません。質問と正解を公開のリポジトリに置かない決まりで、実行の記録も
公開されるためです。

## フォルダの構成

| 場所 | 中身 |
| --- | --- |
| `src/context_foundation` | 道具の本体(取り込み・分割・索引・検索・評価・MCP サーバー) |
| `mcp.json`、`com.github.copilot` | プラグインの MCP サーバーの定義と、セッションの始めの hook |
| `skills/context-search` | MCP の道具が使えないときに、検索の CLI の呼び方を伝える skill |
| `ctxf.py` | MCP サーバー・hook・skill が呼ぶ入口 |
| `bench` | エージェントを使う手法を Copilot CLI で回して記録する道具 |
| `tests` | テストと、合成のコーパス |
| `examples/o3de` | 公開のゲームエンジンを題材にした評価の実例(質問・正解の場所・測った結果) |
| `docs` | 評価の仕様、設計、Copilot の手順、比較の手順書、計測の一覧 |

## ライセンス

[MIT](LICENSE)
