# context-foundation(日本語の説明)

大きなコードベースと設計文書から、AI エージェントが自然文の質問で根拠の
箇所を探せるようにする、検索の道具です。

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

PoC(試作)の段階は終えました。0.4 は、Copilot のプラグインとして入り、
Copilot CLI のセッションの中で用意と索引づくりができ、公開されている
C++ のゲームエンジン 2 つで、既存の手順(grep とファイルを読む道具だけ)と
比べてあります。効果の検証は、公開のエンジンで行います。使う人自身の
コードでは比べません。

今の形(0.3 からの、MCP サーバーと始めの 1 文)で、評価用の質問を 3 回
ずつ流した結果です(Copilot CLI、モデルは Claude Haiku 5.5。回答は、事前に
決めた答えの要点で AI が採点。目安は合格率 80%)【実測】:

| エンジン | 回答の合格率(この道具 / 既存の手順) | 時間の中央値 | 費用の中央値 | 判定 |
| --- | --- | --- | --- | --- |
| Godot(29 問、0.3) | 93.1% / 88.5%(点検後の要点) | 35% 減 | 32% 減 | 目安を満たす |
| O3DE(27 問、0.4.9) | 95.1% / 90.1% | 12% 減 | 14% 減 | 品質は満たす。時間と費用の得は 1 割ほど |

- O3DE では、0.4.7 の形だと時間と費用は 3 割ほど減ったものの、検索の
  passage だけで答えてファイルを開かず、近くにある関連の要点を落として
  合格率が 77.8% に下がりました。0.4.9 では、上位の passage に同じファイルの
  ほかの関数の名前を添え、答える前に関連の処理を見るように促しました。
  合格率は既存の手順を下回らなくなり、時間と費用の得は 1 割ほどに小さく
  なりました。数字は、既存の手順と同じ時間帯に交互に流した計測のものです
  (別の日の回答と比べた計測では 4% 減と 3% 減で、日によって動きます)。
  品質を落とさないことを
  先に守る、という判断です([docs/production.md](docs/production.md))
- Godot は 0.3 の形での数字で、0.4.9 ではまだ測り直していません【未確認】
- O3DE では、下の層の実装や呼び出し先まで答えないと満点にならない、
  難しめの 40 問も作って測りました(質問と要点は AI が作って点検。人の
  確認は未了)。0.4.9 は既存の手順を、Claude Sonnet 5.5 で 82.5% 対
  72.5%(時間 10% 減、費用 11% 減)、Claude Code を代わりのエージェントに
  した Haiku で 90.0% 対 85.0% と、どちらも上回る向きでした。差の 95% の
  区間が 0 をまたがないのは、答えの要点の名前を数える指標で、AI の採点
  では区間が 0 をまたぎます。検索の当たりを上げる(埋め込みモデルの
  granite)、言語サーバー(clangd)を道具として渡す、は答えを良くしません
  でした([docs/benchmarks.md](docs/benchmarks.md))
- Godot の合格率は、結果を見たあとに答えの要点を点検し直した数字です。
  事前の要点では、どちらも目安に届きません(75.9% と 74.7%)。判定には
  点検後の数字を採りました。0.2 までの形(skill)は、どちらのエンジンでも
  効果が出ませんでした
- 質問の文をそのまま検索語にする、検索だけの使い方は、目安(上位 5 件に
  正解がある質問が 80%)に届きません(Godot 50%、O3DE 54%)。検索の時間は、
  公開されていない大きいエンジンのコードの一部(約 24.5 万 passage)でも、
  目安の 3 秒を大きく下回ります
- 検証の限界: 公開のエンジンで、モデルが識別子を一部覚えている(既存の
  手順に有利)。主な計測のモデルは Claude Haiku 5.5(難しめの質問では
  Sonnet 5.5 でも測った)、採点は AI

質問と正解、結果は [examples/o3de](examples/o3de/README.md) に、
これまでに測ったものと、まだ測っていないものは
[docs/benchmarks.md](docs/benchmarks.md) にあります。自分のコードで比べる
ときの手順は [docs/comparison-runbook.md](docs/comparison-runbook.md) です。

0.4.0 では、本番で使うための直し(安全・取り込み・止まったときの続き・
出力の上限・用意の失敗の知らせ方など)を入れました。何が変わったか、
更新のあとに何をするかは [CHANGELOG.md](CHANGELOG.md) にあります。直しを
手元で動かして確かめたのは Windows と Python 3.13 で、ほかの OS と Python
の版は CI のテストで確かめています。Copilot での 0.4.0 の動作は、まだ
確かめていません(Copilot の側で確かめたのは 0.3 まで)。0.4.1 では、索引を
作るスラッシュコマンド `/context-index` を足しました。0.4.2 では、Copilot
CLI 1.0.113 で読み込まれなかったのを直しました。Windows の Copilot CLI
1.0.113 と 1.0.112 で、索引づくりと作り直しが、モデルを呼ばずに動くことを
確かめました【実測】。0.4.3 では、用意(setup)をするスラッシュコマンド
`/context-setup` を足し、入れるところから索引づくりまでを Copilot CLI の
中だけで進められるようにしました。0.4.4 では、初めての `/context-index` を
フォルダを並べずに打つと、索引に入れるフォルダを一覧から選べるように
しました(どちらも Copilot CLI 1.0.114 で確かめました【実測】)。0.4.7 では、
索引の無い作業フォルダで、MCP サーバーが道具を出さないようにしました。
アプリでは、まだ試していません【未確認】。0.4.9 では、上で書いた、関連の
処理を見てから答えるための直しを入れました。

本番で使うときの手順・設定・運用・限界は
[docs/production.md](docs/production.md) にまとめてあります。

対象は、まず GitHub Copilot CLI と、GitHub Copilot のデスクトップアプリ
です。Visual Studio Code は後回しで、手順も確認もまだありません。

## 前提と、対応する環境

| もの | 対応する範囲 | 確かめたもの |
| --- | --- | --- |
| OS | Windows。Linux でも Python の部分は動く(`.bat` は Windows 用) | Windows 11(手元)。Windows と Ubuntu(CI のテスト) |
| Python | 3.11 から 3.14 | 3.13(手元)。3.11・3.12・3.13・3.14(CI のテスト) |
| Copilot から使うとき | GitHub Copilot CLI と、そのデスクトップアプリ | CLI 1.0.112(0.3 まで)。拡張のコマンドは CLI 1.0.112〜1.0.114。アプリは未確認 |

- Copilot から使うときは、`python` という名前のコマンドで Python が起動
  できることも要ります(MCP サーバーと hook は `python` で起動します)。
  python.org のインストーラーは、既定では `python` を PATH に足しません。
  Linux と macOS の `python3` しか無い PC では、`python` という名前でも
  起動できるようにします([docs/copilot.md](docs/copilot.md) の「入れ方は
  2 つ」)
- 3.15 以降には、検索に要る onnxruntime の配布物がまだありません。
  `python` が 3.15 以降でも、この PC に 3.14 以前があれば、用意(setup)は
  その版で動きます(無ければ、3.14 を入れるよう知らせます)
- 依存のパッケージは、版の範囲(下限と、壊れたことのあるものの上限)だけを
  決めていて、版を固定した一覧(lock)は持ちません。PyPI の新しい版で
  壊れたことには、CI の週 1 回の実行で気づくようにしています

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

その後、最初に 1 回だけ用意(`ctxf.py setup`。依存のパッケージを、利用者
ごとの置き場に取得する)をし、作業フォルダで索引を作ります。どちらも、
エージェントに頼めば、利用者に尋ねてから動かします。埋め込みモデルは、
用意では入れません(下の「埋め込みモデルは自動では入れません」)。

Copilot CLI なら、すべてセッションで打つコマンドで済みます(ターミナルは
要りません。詳しくは [docs/copilot.md](docs/copilot.md) の「Copilot CLI の
中だけで始める」)。

```text
/plugin marketplace add noxitro/context-foundation
/plugin install context-foundation@context-foundation
/settings experimental on
```

続けて、新しいセッションで `/context-setup`(用意。モデルが無ければ、
この PC に置いたモデルのフォルダを尋ねる)、`/context-index`
(索引。初めは、索引に入れるフォルダを一覧から選ぶ)と打ち、もう一度新しい
セッションを始めます。2 つのコマンドは、モデルを呼ばずに裏で動きます。
3 行目は、この 2 つ(Copilot CLI の拡張。実験的機能)を読み込むための
設定です。

**リポジトリを取得して使う(手元で試す人)**: どれもダブルクリックで動きます。

| 順 | ファイル | すること |
| --- | --- | --- |
| 1 | `setup.bat` | 初回の用意。仮想環境(`.venv`)を作り、依存するパッケージを入れる。埋め込みモデルは入れず、インストールされているかと入手先を出す |
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
- ソースが変わったら(同期したら)、もう一度 `index.bat` を実行します。
  中身の変わらないファイルの分割と、変わっていない passage の埋め込みは
  前の索引から使い回すので、ほぼ変わったファイルの分だけの時間で済みます
  (差分更新。ファイルを読む時間は、毎回すべての分かかります)。索引の後に変わった・消えた
  ファイルを指す結果には、その旨の印が付きます。索引の後に増えたファイルは、
  作り直すまで検索に出ません。無人で流すコマンドと、タスク スケジューラの
  例は [docs/copilot.md](docs/copilot.md) の「索引を更新する」にあります
- `.cf` フォルダは、git には入りません(中に、全部を無視する `.gitignore`
  を置きます)。Perforce では、`.cf` を `.p4ignore` に足してください(索引
  には、ソースの本文の写しが入ります)

取得するもの:

| もの | 入手先 | 大きさ | ライセンス | 入れるもの |
| --- | --- | --- | --- | --- |
| Python のパッケージ(tree-sitter と C・C++・C# の文法、numpy、onnxruntime、tokenizers、huggingface_hub、jsonschema、pytest と、その依存) | PyPI | 仮想環境で約 190 MB | MIT・BSD・Apache-2.0 など | `setup.bat`・`ctxf.py setup`・`/context-setup` |
| 埋め込みモデル `intfloat/multilingual-e5-small`(ONNX と tokenizer) | Hugging Face | 約 490 MB | MIT | 利用者が自分で `model download` を実行したときだけ |

時間の目安(16 コアの CPU で測ったもの。GPU は使いません):

- 最初の索引づくりは、約 1,300 ファイル(16 万行)で 3 分、約 8,000 ファイル
  (143 万行)で 30 分ほどです。ほとんどが、埋め込みの計算です
- 2 回目からは、変わったファイルの分だけ埋め込み直します(約 120 ファイル・
  950 passage の例で、作り直し 19 秒に対して、5 ファイルを変えたときは
  1.3 秒)
- 索引の大きさは、ソースの 2〜3 倍です(143 万行で 135 MB)
- 検索は、1 回 2 秒ほどです(コマンドの起動と、モデルの読み込みを含む)

### 埋め込みモデルは自動では入れません

埋め込みモデルは、PyPI の外から取得する OSS です。使ってよいかは、利用
する環境(ライセンス、利用の条件、ネットワーク)で確かめる必要があるので、
`setup.bat`・`ctxf.py setup`・`/context-setup`・`index.bat` は、モデルを
取得しません(取得するかも尋ねません)。インストールされていなければ、次の
ように知らせ、入手先と取得のコマンドを出します。

```text
  embedding model: not installed (intfloat/multilingual-e5-small,
  license MIT, about 487 MB, from https://huggingface.co/...)
    Setup does not install it: ...
    To use it, check the model's source and licence, then run:
    python -I "<…>\ctxf.py" model download
```

(実際は、括弧の中と最後のコマンドが 1 行に続きます。)

`setup.bat` と `/context-setup` は、そのうえで、この PC に置いたモデルの
フォルダを尋ね、確かめてから覚えます(空のままで飛ばせます)。

入手先とライセンスを確かめ、使ってよければ、自分で次を実行します
(リポジトリを取得したなら `.venv\Scripts\python.exe -I ctxf.py --run model
download`)。

```text
python -I "<…>\ctxf.py" model download
```

### 手元に置いたモデルを使う

モデルを、すでにこの PC に置いているとき(ほかの PC から写した、共有の
フォルダから入れた、Hugging Face のキャッシュにある、など)は、取得せずに、
そのフォルダを指定します。

```text
python -I "<…>\ctxf.py" model use "D:\models\multilingual-e5-small"
```

- 渡すのは、`onnx\model.onnx`(フォルダの直下の `model.onnx` でも可)と
  `tokenizer.json` が入ったフォルダです。その中のファイル(`model.onnx`)、
  Hugging Face のキャッシュのフォルダ(`models--intfloat--multilingual-e5-small`)、
  下にモデルが 1 つだけある上のフォルダでも構いません
- 量子化したものなど、別の名前の ONNX を使うときは、その `.onnx` の
  パスを渡します(`tokenizer.json` が、その隣か 1 つ上のフォルダに要り
  ます)。既定の名前の ONNX が無く、隣の ONNX が 1 つだけなら、フォルダを
  渡しても、それを使います
- もう 1 つの既定の候補のモデルなら、`--model
  ibm-granite/granite-embedding-97m-multilingual-r2` をフォルダの前に付け
  ます。最後に `model use` したモデルを、設定に `model` の無い作業フォルダ
  で使います。モデルが変わると、それまでの索引は、作り直すまで keyword
  だけで検索されます(`model use` がそう知らせます)
- モデルを 1 回読み込んで確かめてから、利用者ごとの設定
  (`%LOCALAPPDATA%\context-foundation\settings.json`)に覚えます。この
  PC のすべての作業フォルダに効きます。ファイルは写しも取得もしません
- 確かめて、ファイルの大きさが、そのモデルの控えの値と違えば、覚えずに
  終わります(終了コード 4。別の版か別のモデルのようです)。それでよければ
  `--yes` を付けて、もう一度実行します。埋め込みの次元が違うときは、別の
  モデルなので、`--yes` でも覚えません(終了コード 2)
- `--model` を付けずに渡し直すと、今使っているモデルのフォルダを置き換え
  ます。既定のモデルに戻すときは、`--model intfloat/multilingual-e5-small`
  を付けます
- 外すときは `model forget`(ファイルは消しません)。作業フォルダの
  `.cf\config.toml` の `[embedding]` の `model`・`model_dir`・`onnx_file`
  は、こちらより先に使われます
- `setup.bat` も、モデルが無ければ、最後にこのフォルダを尋ねます(空のまま
  Enter で飛ばせます)
- Copilot のセッションでは、skill `context-search-setup` が、用意を対話で
  通しで進めます。パッケージを入れてよいかを尋ね、手元のモデルのフォルダ
  を尋ねて `model use` で覚え、索引を作るかも尋ねます(モデルは取得しま
  せん)

モデルが無くても使えます。そのときは、keyword(BM25)だけの
検索になります。モデルの置き場は `%LOCALAPPDATA%\context-foundation\models`
です(環境変数 `CF_HOME` で、置き場の親を変えられます。Linux の場所は
[docs/copilot.md](docs/copilot.md) の「保存されるものと消し方」)。

インターネットに出られない PC では、モデルは、ほかの PC で取得したこの
フォルダを写せば使えます。Python のパッケージは、写しただけでは入りません。
出られる PC で `pip download` したものを渡すか、PyPI のミラーを使います
(手順は [docs/copilot.md](docs/copilot.md) の「インターネットに出られない
PC・PyPI のミラー」)。

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

道具のコマンドは、どれも入口の `ctxf.py` を通して、次のどちらかの形で
動かします。この文書とほかの文書で `ctxf <コマンド>` と書いたものは、
この形の略です(`ctxf` という名前のコマンドは、ふつうは PATH にありません)。

| 入れ方 | 形 |
| --- | --- |
| プラグインとして入れた | `python -I "<プラグインのフォルダ>\ctxf.py" <コマンド>` |
| リポジトリを取得した | `"<取得したフォルダ>\.venv\Scripts\python.exe" -I "<取得したフォルダ>\ctxf.py" --run <コマンド>`(`.bat` の中身もこの形) |

作業フォルダ(かその下)で動かすと、親へさかのぼって `.cf` を探します。
ほかの場所から動かすときは、`--config <作業フォルダ>` で渡します(`init`
は `--workspace`)。道具が文面で案内するコマンドも、動かした形に合わせて
出ます。

1 つ目の形の入口は、使える python を探して、同じコマンドを動かします
(環境変数 `CF_PYTHON`、隣の `.venv`、利用者ごとの置き場の順)。2 つ目の
`--run` は、探さずに、その python で道具を動かします。入口だけのコマンドは
次の 3 つで、`python -I "<…>\ctxf.py" --help` で一覧が出ます。

| 入口のコマンド | すること |
| --- | --- |
| `setup` | 依存を、利用者ごとの置き場に用意する。埋め込みモデルは入れず、有無と入手先を出す(`--no-model` で、それも出さない)。`--gpu`・`--cpu`・`--help` |
| `where` | 使う python・PATH の `python`・置き場・モデル・索引を出す(うまく動かないときに最初に見る) |
| `session-hint` | Copilot のセッションの始めの hook が使う |

| コマンド | すること |
| --- | --- |
| `init <フォルダ>...` | 作業フォルダに設定(`.cf\config.toml`)を作る。対話なし。`--force` で作り直す(前の設定は `.bak` に残す) |
| `index` | 索引を作る。変わっていない passage の埋め込みは前の索引から使い回す。`--full` ですべて埋め込み直す(途中までの埋め込み `index.db.partial` も使わない)、`--no-embed` で keyword だけ(前の索引の埋め込みは捨てる。途中までの埋め込みは残し、次に埋め込みつきで作るときに使う)、`--download` でモデルを先に取得、`--revision <版>` でコーパスの版を記録、`--quiet` で途中の進み具合を出さない |
| `search "<質問>"` | 検索する。`--mode`、`--top-k`、`--source <id>`、`--format agent\|human\|json`、`--max-bytes`、`--line-numbers` |
| `status` | 索引の版・件数・作成日時・作成にかかった時間、source ごとのフォルダ、除いたファイルの数(理由ごと)、構文で分けられなかった割合と、その多いファイル。`--check` で、索引の後に変わった・増えた・消えたファイルを数える(フォルダを歩くので時間がかかる) |
| `eval --cases <cases.jsonl> --out <フォルダ>` | 評価を回して、要約を書く |
| `validate --cases <cases.jsonl>` | 評価データの形式を検査する |
| `serve` | 読み取り専用の MCP サーバー(stdio) |
| `model download` / `model info` | 埋め込みモデルの取得と確認(取得は、利用者が自分で実行するもの。setup などからは動かさない) |
| `model use <フォルダ>` / `model forget` | 手元に置いた埋め込みモデルのフォルダを、確かめてから利用者ごとの設定に覚える・外す(`--model <名前>` で既定と違うモデル、`--onnx-file <名前>` で別の名前の ONNX、`--no-check` で読み込みの確かめを省く、`--yes` で確かめて気になる点があっても覚える)。終了コード: 0 覚えた、2 覚えられない、4 気になる点があって覚えていない |
| `copilot register` / `unregister` / `status` | このフォルダを手元の Marketplace としてプラグインを入れる・外す・状態を見る(取得したリポジトリのフォルダからだけ) |

`index` の終了コードは、0 が成功、1 が索引を置き換えられなかった(ほかの
プログラムが開いている。できあがった索引は `.cf\index.db.new` に残る)、
2 が設定の誤り・source のルートが無い・入るファイルが無い・モデルを読め
ない・モデルを取得できなかった(`--download`)、3 が `.cf` が見つからない
(`--config` を付けないとき)、4 が用意ができていない(setup が済んでいない、
プラグインの更新で依存の一覧が変わった、仮想環境のもとの Python が消えた、
仮想環境の python が要るパッケージを読めない。setup をやり直すまで続き
ます)、130 が途中で止めた(Ctrl+C)、です。4 は、入口(`python -I
"<…>\ctxf.py"`)から動かしたときです。`--run` の形(`.bat` と、取得した
フォルダの `.venv` の python で直接動かす形)では、要るパッケージを読めない
ときは 104、Windows で `.venv` のもとの Python が消えたときは 103 で終わり
ます。
埋め込みのある索引は、モデルを読めないときに keyword だけの索引に置き
換えず、止まります(置き換えるなら `--no-embed`)。途中で止めた `index`
は、次の実行で、計算済みの埋め込みを使い回して続きから計算します。

`search` の出力は、端末では人が読む形、そうでないとき(エージェントが
呼んだとき)は、エージェント向けの短い形になります。エージェント向けの形は、
上位の passage は本文ごと、残りは場所だけを返し、全体を 12,000 バイト
(設定で変更可。上限 18,000)に収めます。収まらない場所は、最後に
`(+N more matches, not shown)` とまとめます。収まらないのは、頭や印の
段落など、決まって出す部分に、`More matches` の見出しと件数の行(合わせて
60 バイトほど)を足した大きさが上限を超えるときだけです(上限を最小の
1,000 バイト近くにしたときに起きえます)。

結果の印は 2 つです。`[changed since index]` は、索引の後に変わった
ファイルで、本文と行番号は索引を作ったときのものです。`[file no longer
exists]` は、索引の後に消えたファイルで、エージェント向けの形では本文を
出しません(人が読む形は「※索引の後に消えたファイル」の印を付けて、索引の
ときの本文を出します。JSON は本文に `missing: true` を付けます)。source の
ルートが作業フォルダそのものでないときは、`roots: <id> = <ルート>` の
行で、`source:path` をどこで開けばよいかを出します。

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
# default_excludes = false    # 既定の除外(フォルダの名前・生成物と秘密の名前)を使わない

[embedding]
enabled = true                # false にすると keyword だけ
# device = 'cpu'              # 索引づくりの埋め込みを GPU で計算しない(既定は auto: 使えれば GPU)

[search]
mode = 'hybrid'
top_k = 8
# glossary = 'glossary.tsv'   # 用語の対応表(省くと .cf\glossary.tsv があれば使う)
# builtin_glossary = false    # 既定の対応表を使わない
# preload = false             # MCP サーバーが、起動のときにモデルとベクトルを先に読み込まない
# related = 8                 # 上位 3 件の passage に添える、同じファイルのほかの名前の数(0 で添えない)
```

- 実装の場所を探すのが主な使い方なら、`exclude` にテスト・サンプルの
  フォルダ(`'**/Tests/**'`・`'**/Samples/**'` など)を足すと、検索が
  当たりやすくなります([docs/production.md](docs/production.md) の
  「7. 品質を落とさないために」)

- 既定で入るのは、C・C++・C#・Markdown と、そのほかの文字のファイル
  (Python、シェーダー、設定ファイルなど)です
- 既定で除くフォルダの名前です(大文字と小文字は区別しません)。どの深さに
  あっても除きます
  - `.git`・`.svn`・`.hg`・`.p4`・`.cf`・`.index`・`.cache`・`.venv`・
    `venv`・`__pycache__`・`node_modules`・`.gradle`・`.vs`・`.vscode`・
    `.idea`・`intermediate`・`deriveddatacache`
  - `build`・`out`・`bin`・`obj`・`dist`・`target`・`binaries`・`saved`・
    `packages`。この 9 つは、ソースを置く名前にも使われるので、中に索引に
    入りうるファイルがあれば、その数とフォルダを `index` の終わりと
    `status` に出します
- 既定の名前で除いたフォルダを入れるには、そのフォルダを `root` にした
  `[[source]]` を足すか、その source に `default_excludes = false` を
  書いて、要らないものを `exclude` に並べます
- 既定で除くファイルは、生成物によくある名前(`*.generated.*`、`*.g.cs`、
  `*.min.js`、`moc_*.cpp` など)、秘密を持ちやすい名前(下)、バイナリ、
  文字として読めないもの、1 MB を超えるもの、5,000 文字を超える行がある
  もの(minify したものなど)、先頭の 2,000 文字のコメントの行に自動生成の
  印があるものです。文書(`.md`・`.markdown`・`.mdx`・`.txt`・`.rst`・
  `.adoc`)は、生成物とはみなしません
- 自動生成の印は、コメントの行だけで見ます
  - 「This file was generated」「File automatically generated by」
    「Generated by」「autogenerated」「このファイルは自動生成」
    「自動生成ファイル」などは、先頭の 2,000 文字のどのコメントの行でも、
    書き出しにあれば印です。前に飾りの記号や `NOTE:`・`注意:` のような
    短い前置きがあっても構いません。「Generated by:」(後ろにコロン)は
    印にしません
  - 「auto-generated」だけ、「generated from」「generated with」
    「code generated」「parser generated by」「自動生成され」などは、
    ファイルの頭のコメントのかたまり(最初のコードの行より前。docstring
    の中の行も含みます)のどこかの行の書き出しにあるときだけ印です
  - `@generated`・`<auto-generated`・大文字の `DO NOT EDIT` は、コメントの
    行のどこにあっても印です。小文字の「do not edit」は、同じ行に
    「generated」があるときだけです
  - 頭で開いたブロックコメント(`/* */`、docstring)の中の行も、
    コメントの行として見ます。`'` は VB の拡張子、`<!--` はマークアップ
    (`.xml`・`.html`・`.csproj` など)の拡張子のときだけ、コメントの
    始まりとみなします
  - `#` の後ろが `include`・`import`・`if`・`else`・`define`・`pragma`・
    `warning`・`using` などの語の行は、プリプロセッサの行としてコードの
    行とみなし、印を見ません(頭のコメントのかたまりもそこで終わります)。
    ただし、`#` がコメントの始まりのファイル(`.py`・`.sh`・`.cmake`・
    `.yaml`・`.yml`・`.toml`・`.ini`・`.cfg`・`.rb`・`.ps1` と、
    `Makefile`・`Dockerfile`)では、その語の行もコメントの行として見ます
    (「# WARNING: This file is auto-generated by …」も印になります)
  - 生成物なのに入ってしまうものは、`exclude` に足します。手書きなのに
    除かれるものは、`[index] skip_generated = false` で入ります(印と
    長い行の判定を、すべての source でやめます)
- 秘密を持ちやすい名前: `.env`・`.env.*`・`*.pem`・`*.key`・`*.pfx`・
  `*.p12`・`id_rsa*`・`id_dsa*`・`id_ecdsa*`・`id_ed25519*`・`.npmrc`・
  `.pypirc`・`.netrc`・`_netrc`・`.git-credentials`・`.p4config`・
  `.p4tickets`・`*.tfvars` と、名前に `secret` か `credential` を含む
  yaml・yml・toml・ini・cfg・json。`include` を書いても除きます。名前だけで
  見るので、ほかのファイルの中に書かれた秘密(設定ファイルのパスワード
  など)は索引に入ります。`.gitignore` は読みません
- `default_excludes = false` は、既定のフォルダの名前・生成物の名前・
  秘密を持ちやすい名前を、まとめて外します
- フォルダのリンク(シンボリックリンク、Windows のジャンクション)は
  たどりません。リンクの先を入れるには、そのフォルダ(リンクそのもので
  も構いません)を `root` にした `[[source]]` を足します
- ファイルのリンクは入れません。指す先が source のルートの中なら、指す先の
  ファイルが、もとの場所で(その名前の決まりのとおりに)入ります
- Windows でも、260 文字を超えるパスのファイルを取り込みます
- ほかの source のルートになっているフォルダは、その source の側だけで
  取り込みます
- 除いた数は、`status` に理由ごとに出ます(`excluded`・`not_included`・
  `secret_name`・`too_large`・`binary`・`undecodable`・`generated`・
  `long_lines`・`empty`・`unreadable`・`symlink_inside`・`symlink_outside`・
  `bad_name`。たどらなかったフォルダは `link_dir`・`unreadable_dir`)
- `[index] path` で、索引のファイル名を変えられます。置けるのは `.cf` の
  中だけです
- 0.3.1 まであった `[search] reranker` と `rerank_candidates` は、0.4.0 で
  無くなりました。書いてあると、止まります(作業フォルダの設定から、
  Python のコードを読み込ませないため)
- そのほかの項目(分割の大きさ、埋め込みモデルの差し替え、順位の重み)は
  [docs/design.md](docs/design.md) にあります

### 複数のリポジトリを横断して検索する

検索は、作業フォルダの `.cf` にある 1 つの索引を引きます。いくつもの索引を
まとめて引く仕組みはないので、リポジトリごとに `.cf` を作っても、横断は
できません。ほかのリポジトリは、作業フォルダの設定に `[[source]]` として
並べます。

作業フォルダの下にジャンクションを置いて、ほかのリポジトリを参照している
とき(`external\Engine` など)の例です。

```toml
[[source]]
id = 'App'
root = '.'

[[source]]
id = 'Engine'
root = 'external\Engine'      # ジャンクションそのものを root にする

[[source]]
id = 'Docs'
root = 'external\Docs'
```

- 索引づくりは、source の中のフォルダのリンクをたどりません。設定に
  `root = '.'` だけを書くと、`external` の下のジャンクションの中は入りません
  (`status` に、たどらなかったリンクとして出ます)。ジャンクションごとに
  `[[source]]` が要ります
- `init`(`index.bat`・`/context-index` も)は、これを自分で足します。渡した
  フォルダの 2 段下までにあるジャンクション・フォルダのリンクを探し、
  それぞれを root にした source を足します(0.4.15 から)。`ctxf init .` か
  `ctxf init external` で、上の例と同じ形になります。足さないのは、指す先が
  渡したフォルダの中にあるもの(もとの場所で入る)、渡したフォルダを含む
  もの(上のフォルダを指すもの)、ほかと同じ場所を指す 2 本目、足すほかの
  ジャンクションの先の中を指すもの(外側の source で入る)、既定の名前で
  除くフォルダ(`build`・`.git` など)の中のものと、その名前のもの、指す先の
  無いもの。それより深い場所のジャンクションと、要らないものは、設定を手で
  直します
- 外側の `App` は、`external\Engine` の中を歩きません(ほかの source の
  ルートなので)。同じファイルは 1 回だけ入ります。ただし、同じ実体を
  2 つの source で指すと(ジャンクション 2 本、ジャンクションと実体の
  絶対パス)、両方に入ります。どちらか 1 つにします
- `init` は、ジャンクションのパスを、解決せずに書きます。
  `root = 'external\Engine'` で、呼び名は `Engine` です。指示ファイルに
  書いたパスと、検索結果の `roots:` の行がそろいます(0.4.13 までは、
  ジャンクションの先の絶対パスと、その名前を書いていました)
- ジャンクションが作業フォルダの中を指すとき(`external\Self` → `src`)は、
  そのフォルダは、ジャンクションの source の側で入ります
- 検索の結果は `Engine:Render/BufferPool.cpp:10-42` の形で、どの source
  から来たかが分かります。MCP の `search` は `source_id` で 1 つに絞れます。
  指示ファイルに source ごとの役割(エンジンの本体、設計文書など)を書いて
  おくと、エージェントが絞るときの手がかりになります

いくつものプロジェクトが同じリポジトリ(同じエンジンなど)を参照していて、
プロジェクトごとに索引を作るのが重いときは、索引だけのフォルダ
(`D:\ctx\.cf\config.toml` など)にすべての source を並べ、
`--config D:\ctx` か環境変数 `CF_CONFIG` で渡すこともできます。ただし:

- `--config` と `CF_CONFIG` は、作業フォルダの `.cf` より先に使われます。
  `CF_CONFIG` を設定したプロセス(と、そこから起動した MCP サーバー)では、
  どの作業フォルダでも、その索引を引きます
- 作業中のプロジェクトの変更は、その索引で `index` を流し直すまで入りません
- ルートが作業フォルダの外になるので、`roots:` の行に絶対パスが出ます
  (検索のたびにモデルに送られます)

## Copilot から使う

プラグインを入れると、Copilot CLI とアプリの両方から使えます。入れ方、
解除のしかた、確かめた範囲は [docs/copilot.md](docs/copilot.md) にあります。

入れた後は、索引のある作業フォルダで Copilot を起動し、ふつうに質問する
だけです。プラグインには、読み取り専用の MCP サーバー(道具は `search` と
`status`)が入っています。セッションの始めの hook が、索引のある作業
フォルダでだけ「探すときは、検索を grep より先に」と 1 文で知らせ、モデルが
検索の道具を呼びます。MCP の道具が使えない場面のために、同じ検索を CLI で
呼ぶ skill `context-search` と、用意を対話で進める skill
`context-search-setup`(手元のモデルのフォルダを尋ねて覚え、索引まで進める)も入って
います。用意をするには `/context-setup`、
索引を作る・作り直すには `/context-index` と、セッションで打ちます(拡張。
Copilot CLI の実験的機能で、`/settings experimental on` で読み込まれます)。

- 用意(setup)の後は、Copilot の新しいセッションを始めます。動いている
  セッションには、検索の道具は出ません
- 検索の道具が出ない・効かないときは、[docs/copilot.md](docs/copilot.md)
  の「うまく動かないとき」を見ます。最初に `python -I "<…>\ctxf.py" where`
  を動かします
- 検索は、上位の passage だけを返し、ほかにも当たる場所があるかは知らせ
  ません。識別子が分かっていて、使われている場所をすべて挙げたいときは、
  grep を使います([docs/copilot.md](docs/copilot.md) の「検索の限界」)

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
  中だけに置かれ、この道具がどこかへ送ることはありません。版管理に入れない
  でください(git には入らないようにしてあります。Perforce では `.p4ignore`
  に足します)
- 秘密を持ちやすい名前のファイル(`.env`、鍵など)は、既定で索引に入れ
  ません。ただし、名前だけで見るので、ほかのファイルの中に書かれた秘密は
  入ります(上の「設定ファイル」)
- `.cf` は、自分で作ります。取ってきたリポジトリに `.cf` が入っていたら、
  使わずに消して、作り直してください。設定は、作業フォルダの外のフォルダ
  (source のルート、モデルのフォルダ、用語の対応表)も指せるためです
- 何がどこに保存されるか、プラグインと保存したものの消し方は、
  [docs/copilot.md](docs/copilot.md) の「保存されるものと消し方」にあります
- 埋め込みは、この PC の中で計算します(CPU。索引づくりは、設定によっては
  GPU)。モデルの取得のときに送るのは、取得するファイルの名前だけです
- エージェントから使うときは、話が別です。エージェントが読んだもの(質問、
  検索が返した passage、開いたファイル)は、そのエージェントが使うモデルの
  提供元に送られます。これは、この道具を使わない今の手順でも同じです。
  source のルートが作業フォルダの外にあると、その絶対パス(利用者の名前を
  含むこともあります)も、検索の結果の `roots:` の行と `status` に出て、
  モデルに送られます。避けるには、ルートを作業フォルダの中に置きます。
  非公開のコーパスでエージェントを動かす前に、どの提供元になら送ってよいか
  を決めてください

## テスト

`run-tests.bat` で、すべてのテストが流れます。合成のコーパス
(`tests/fixtures`)で、分割・検索・評価・MCP サーバー・コマンドを通しで
確かめます。埋め込みモデルが無くても通ります。

GitHub Actions では、次の 4 つを回します(`.github/workflows`)。

| ワークフロー | いつ | すること |
| --- | --- | --- |
| CI | プルリクエスト、main への push、毎週月曜、手で起動したとき | テスト(Windows・Linux、Python 3.11・3.12・3.13・3.14)、依存を宣言した下限の版でのテスト(Python 3.11。実物のモデルを使うテストも)、wheel と sdist を作って使う確認(sdist の中身が `git ls-files` と同じか)、Markdown の検査、リンクの確認 |
| Plugin smoke test | 道具の中身に関わる変更、main への push、毎週月曜、手で起動したとき | まっさらな機械で、`ctxf.py setup`(モデルが無いと知らせること)と `model download` から、実物の埋め込みモデルでの索引づくり・検索・評価・MCP サーバーまでを通し、実物のモデルを使うテスト(`pytest -m model`)も流す(Windows・Linux) |
| Secret scan | すべての push、プルリクエスト、手で起動したとき | 秘密情報と、所属・著作権の表示の検査(共通の workflow) |
| Benchmark (O3DE example) | 手で起動したとき | 公開の評価例([examples/o3de](examples/o3de/README.md))を、GitHub の機械で最初から測り直す |

エージェントを使う比較(Copilot CLI を動かす計測)は、GitHub Actions では
流しません。質問と正解を公開のリポジトリに置かない決まりで、実行の記録も
公開されるためです。

毎週の実行は、PyPI の新しい版や新しい Python で壊れたことに気づくため
です。公開リポジトリでは、60 日間動きが無いと、GitHub が毎週の実行を
止めます(Actions の画面で有効に戻します)。失敗の知らせは、cron の行を
最後に変えた人に届きます。GitHub Actions の版の更新は、Dependabot が
知らせます
(`.github/dependabot.yml`)。実物の埋め込みモデルを使うテスト
(`tests/test_model.py`)は、モデルが手元にあるときだけ動き、CI では飛ばし
ます。

## 不具合の報告

不具合や質問は、GitHub の issue
(<https://github.com/noxitro/context-foundation/issues>)に書いてください。
`python -I "<…>\ctxf.py" where` の出力と、道具の版(`--version`)を添えると
早く分かります。

- 公開されていないコードの本文、検索結果、索引、評価の質問と正解は、
  issue に貼らないでください(issue は公開されます)。パスやフォルダの名前
  も、伏せられるものは伏せてください。再現には、合成のコーパス
  (`tests/fixtures`)か、公開されているコードを使います
- 秘密が漏れるおそれのある不具合は、issue に詳しく書かないでください。
  GitHub のリポジトリの Security の画面から非公開で知らせるか、それが
  使えなければ、issue には「非公開で伝えたい不具合がある」とだけ書きます

## フォルダの構成

| 場所 | 中身 |
| --- | --- |
| `src/context_foundation` | 道具の本体(取り込み・分割・索引・検索・評価・MCP サーバー) |
| `mcp.json`、`com.github.copilot` | プラグインの MCP サーバーの定義、セッションの始めの hook、索引を作る拡張(`/context-index`) |
| `skills/context-search` | MCP の道具が使えないときに、検索の CLI の呼び方を伝える skill |
| `skills/context-search-setup` | 用意を、利用者に尋ねながら通しで進める skill(パッケージ、手元に置いたモデルのフォルダ、索引) |
| `ctxf.py` | MCP サーバー・hook・skill が呼ぶ入口 |
| `bench` | エージェントを使う手法を Copilot CLI で回して記録する道具 |
| `tests` | テストと、合成のコーパス |
| `examples/o3de` | 公開のゲームエンジンを題材にした評価の実例(質問・正解の場所・測った結果) |
| `docs` | 評価の仕様、設計、Copilot の手順、比較の手順書、計測の一覧 |
| `CHANGELOG.md` | 版ごとの変更と、更新のあとにすること |

## ライセンス

[MIT](LICENSE)
