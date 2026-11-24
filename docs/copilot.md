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
`.github/plugin/marketplace.json`)。プラグインには、読み取り専用の MCP
サーバー(`mcp.json`)、セッションの始めの hook、控えの skill
`context-search` が入っています。

| 入れ方 | 向いている人 | Python の依存の置き場 |
| --- | --- | --- |
| GitHub の Marketplace から | アプリや CLI で使う人、チームに配るとき | 利用者ごとの置き場(`ctxf.py setup` が作る) |
| リポジトリを取得して `register-copilot.bat` | 手元で試す人、道具を直す人 | 取得したフォルダの `.venv`(`setup.bat` が作る) |

どちらも、`python` という名前のコマンドで Python が起動できることが前提です。
MCP サーバー・hook・skill は、どれも `python` で入口の `ctxf.py` を
動かします。skill は、skill のフォルダに置いた取り次ぎ
(`skills/context-search/ctxf.py`)を呼び、取り次ぎが同じ入口を動かします
(skill の文に「2 つ上のフォルダ」と書くと、エージェントがパスを組み立て
間違える回があったため)。`python` が無い PC では、MCP サーバーは起動
できず、hook は何も足さず、skill は検索できません。どれも、何も知らせ
ません(下の「うまく動かないとき」)。

- 用意(setup)に使う Python は 3.11 から 3.14 です。3.15 以降には、検索に
  要る onnxruntime の配布物がまだありません。`python` が 3.15 以降でも、
  この PC に 3.11 から 3.14 があれば、setup はその版(新しいもの)で
  自分を動かし直します(PATH の `python3.14` などと、Windows の
  `py -3.14` などを探します)。無ければ、3.14 を入れるよう知らせます。
  `python` はそのままで構いません(入口は、setup が作った仮想環境の
  Python で道具を動かします)
- python.org のインストーラーは、既定では `python` を PATH に足しません
  (`py` だけが入ります)。入れるときに「Add python.exe to PATH」を選ぶか、
  あとから PATH に足します。足さないと、`python` は Microsoft Store へ
  案内する Windows のショートカット(`WindowsApps\python.exe`)になり、
  Python は起動しません。`where`・`setup.bat`・登録は、そうと名指しして知らせます
- Linux と macOS では、`python3` しか無いことが多いです。プラグインは
  `python` を起動するので、`python` という名前でも起動できるようにして
  ください(Debian・Ubuntu なら python-is-python3 のパッケージ、ほかは
  PATH の通ったフォルダに `python` という名前のリンク)。シェルの alias
  は効きません(Copilot は、対話のシェルを通さずに起動します)。Linux と
  macOS でプラグインとして動かすことは、確かめていません【未確認】
- 控えの skill の文は、PowerShell で動かすこと(`\` 区切りのパス、
  `[Console]::OutputEncoding` の指定)を前提に書いています。Windows の
  ほかでは、エージェントが読み替えることになります

## GitHub の Marketplace から入れる

### アプリ

1. サイドバーの Customize を開き、Marketplace の選択の隣にある設定から、
   GitHub のリポジトリ `noxitro/context-foundation` を足す【公式】
2. Customize の Browse で `context-foundation` を探し、Install を押す
   【公式】
3. 下の「最初の用意」と「索引を作る」を済ませる
4. 索引を作った作業フォルダで、新しいセッションを始める

アプリの文書は、プラグインが skill と hook を足すと説明しています【公式】。
プラグインの MCP サーバーがアプリでも読み込まれるかは、文書に書かれて
いません【未確認】。アプリでの実際の動作は、まだ確かめていません【未確認】。

確かめるときは、索引のある作業フォルダで新しいセッションを始め、名前の
分からないものを探す質問(例: 「〜に相当する処理はどこ?」)をして、次の
3 つを見ます。既定の出し方は MCP の道具なので、skill が読まれることは、
確かめる目安にしません(skill は控えです)。

1. 道具の一覧に、`context-foundation-search` が出る
2. 質問のときに、それが呼ばれ、結果が「No context-foundation index」で
   ない(passage が返る)
3. 回答に、`source:path:行-行` の形の根拠が出る

1 が出なければ、MCP サーバーが起動していません。`python` と setup を
疑います(下の「うまく動かないとき」)。2 が「No context-foundation
index」なら、アプリがサーバーに作業フォルダ(環境変数
`COPILOT_SESSION_CWD`)を渡していない見込みがあります。そのときは、控えの
skill(`ctxf.py search`)で検索が動くかを見ます。skill は、作業フォルダで
動くシェルから呼ばれるので、作業フォルダを受け取れます。

### CLI

```text
copilot plugin marketplace add noxitro/context-foundation
copilot plugin install context-foundation@context-foundation
```

Copilot CLI のセッションの中でも、`/plugin marketplace add …` と
`/plugin install …` で同じことができます(下の「Copilot CLI の中だけで
始める」)。

GitHub から入れたプラグインは、Copilot の設定フォルダの
`installed-plugins\context-foundation\context-foundation` に写されます
【公式】。写されるのは、リポジトリで git が追跡しているファイルだけです
(同じ作者の cxxref を同じ方法で入れて、追跡しているファイル 150 個と
写されたファイル 150 個が一致し、`.git` は無いことを確かめました【実測】)。
仮想環境やモデルは、プラグインに入りません。

更新は `copilot plugin update context-foundation` です【公式】。プラグインを
更新すると、ソースはそのまま新しいものになります。版ごとに、更新のあとに
すること(setup のやり直し・索引の作り直し・新しいセッション)は
[CHANGELOG.md](../CHANGELOG.md) にあります。

- Python の依存の一覧が変わったときだけ、もう一度「最初の用意」が要り
  ます。道具が「set up for an older version (its dependencies changed)」
  と知らせます。0.3.1 から 0.4.0 への更新では要ります
- setup のあとは、Copilot の新しいセッションを始めます。動いている
  セッションには、検索の道具は出ません
- 更新は、GitHub の main の先頭を取ってきます。git のタグはまだ打って
  いないので、前の版に戻したいときは、下の「チームの Marketplace に
  載せる」のように、コミットの sha で固定します

### Copilot CLI の中だけで始める

入れるところから検索できるところまで、Copilot CLI のセッションで打つ
コマンドだけで進められます。ターミナルのコマンドも、プラグインのフォルダの
パスも要りません。前提は、`python` という名前で Python 3.11〜3.14 が起動
できることです(上の「入れ方は 2 つ」)。

```text
/plugin marketplace add noxitro/context-foundation
/plugin install context-foundation@context-foundation
/settings experimental on
```

1. 上の 3 行を打つ。3 行目は、拡張(`/context-setup` と `/context-index`)
   を読み込むための設定です(拡張は Copilot CLI の実験的機能)。打つと、
   CLI がセッションを読み込み直します
2. 新しいセッションを始め、`/context-setup` と打つ。Python のパッケージを
   入れます。埋め込みモデルは入れず、インストールされていないことと入手先を
   知らせます(下の「`/context-setup`」と「埋め込みモデルは自動では入れない」)
3. 作業フォルダで `/context-index` と打ち、出てきた一覧で索引に入れる
   フォルダを選ぶ(`/context-index <フォルダ> ...` と並べても構いません)。
   索引を作ります(下の「`/context-index`」)
4. 新しいセッションを始める。そこから、エージェントが grep より先に検索の
   道具を使います

2 と 3 は、モデルを呼びません。1 から 3 を、Copilot CLI 1.0.114 で確かめ
ました【実測】(下の「確かめたこと」の「0.4.3」)。

### 最初の用意(setup)

検索には、Python のパッケージ(PyPI から約 200 MB)が要ります。意味での
検索には、さらに埋め込みモデル(Hugging Face から約 490 MB)が要ります。
これを、プラグインのフォルダの外の利用者ごとの置き場
(`%LOCALAPPDATA%\context-foundation`。環境変数 `CF_HOME` で変えられる)に、
1 回だけ置きます。setup が入れるのは Python のパッケージだけです(下の
「埋め込みモデルは自動では入れない」)。

- Copilot CLI で打つ: `/context-setup`(下の「`/context-setup`」)。拡張を
  読み込んでいれば、これがいちばん簡単です
- エージェントに頼む: 用意ができていないと、skill がそう知らせ、エージェント
  は利用者に尋ねてから `ctxf.py setup` を動かします
- 自分で動かす: 次の 1 行です(`<プラグインのフォルダ>` は上の場所)

```text
python -I "<プラグインのフォルダ>\ctxf.py" setup
```

置くものと場所の一覧は、下の「保存されるものと消し方」にあります。

- 終わったら、Copilot の新しいセッションを始めます。動いているセッション
  には、検索の道具は出ません(そのセッションでは、控えの skill が動きます)
- setup は、埋め込みモデルを取得しません。終わりに、インストールされて
  いるかを出し、無ければ入手先・ライセンス・取得のコマンドを出します。
  `setup --no-model` は、その知らせも出しません
- `setup --help` は、何も取得せずに説明だけを出します
- setup に使う Python は 3.11 から 3.14 です。範囲の外の Python で動かすと、
  何もせずに止まり、使う版の起動のしかたを示します(上の「入れ方は 2 つ」)
- `setup --gpu`(Windows)は、onnxruntime の代わりに onnxruntime-directml を
  入れます。索引づくりの埋め込みを、GPU で計算するようになります
  ([design.md](design.md) の「GPU で計算する」)。`setup --cpu` で戻します。
  切り替えるときは、入れ替える先を入れられるかを先に確かめ、入れられな
  ければ何も変えずに止まります(今のものが残り、検索はそのまま動きます)
- 途中で止まった仮想環境は、次の `setup` で作り直します。2 つの `setup`
  が同時に走らないよう、排他します(前の `setup` のプロセスが終わっていれば、
  打ち切られたときの残りとみなして引き継ぎます)
- pip が失敗すると、最後の 1 行に、見込みの原因を出します(パスが長すぎる、
  PyPI に届かない、この Python 向けの配布物が無い)
- Windows で長いパス(260 文字を超える)を使えない設定のとき、置き場の
  パスが長いと、pip が「No such file or directory」で失敗します。setup は、
  pip の前にそう知らせます。`CF_HOME` を短いフォルダ(例 `C:\cf`)にするか、
  Windows の長いパスを有効にしてから、やり直します
- 使われる python と置き場は `python -I "<…>\ctxf.py" where` で見られます

### 埋め込みモデルは自動では入れない

埋め込みモデル(既定は `intfloat/multilingual-e5-small`、MIT)は、PyPI の外
(Hugging Face)から取得する OSS です。pip のパッケージと違い、使ってよいか
は利用する環境(ライセンス、利用の条件、ネットワーク)で確かめる必要が
あるので、利用者の PC では勝手に入れません。

- `ctxf.py setup`・`/context-setup`・`setup.bat`・`index.bat` は、モデルを
  取得しません(尋ねもしません)。無ければ「embedding model: not installed」
  (`.bat` では「埋め込みモデルがインストールされていません」)と、入手先と
  取得のコマンドを出します
- 検索と索引づくりも取得しません。無ければ keyword(BM25)だけで動き、
  結果の note や索引づくりのログに、インストールされていないことが出ます
- エージェントは、モデルを取得するコマンドを自分では動かしません(skill に
  そう書いてあります)
- 取得するのは、利用者が入手先とライセンスを確かめたうえで、自分で次を
  実行したときだけです(`ctxf.py index --download` も同じく、利用者が
  明示的に頼むもの)

```text
python -I "<プラグインのフォルダ>\ctxf.py" model download
```

モデルをすでにこの PC に置いているときは、取得せずに、そのフォルダを
指定します。モデルを 1 回読み込んで確かめてから、利用者ごとの設定
(利用者ごとの置き場の `settings.json`)に覚え、すべての作業フォルダで
使います。外すときは `model forget` です。

```text
python -I "<プラグインのフォルダ>\ctxf.py" model use "<フォルダ>"
```

### skill `context-search-setup`(用意を対話で進める)

セッションで「context-foundation をセットアップして」「手元のモデルを
使いたい」などと頼むと、モデルがこの skill を読み、利用者に 1 つずつ
尋ねながら進めます。

1. `where` で今の状態を見て、知らせる(何も入れない)。パッケージが
   入っていればパッケージを、モデルもあればモデルを飛ばす
2. パッケージ(PyPI から約 200 MB)を入れてよいかを尋ね(Windows では CPU
   か GPU かも)、よければ `setup --no-model` を動かす。リポジトリを取得
   した人の `.venv` が壊れているときは、`where` の `setup:` の行のコマンド
   (`setup.bat` など)を自分で動かすよう頼み、終わったら 1 からやり直す
3. 手元に置いた埋め込みモデルのフォルダを尋ね、`model use` で確かめて
   覚える。見つからなければ、何が足りないかを伝えて、もう一度尋ねる。
   大きさや次元が控えの値と違えば、使ってよいかを尋ねる。飛ばすことも
   できる(keyword だけになる)
4. `where` で確かめる
5. 索引を作るかを尋ね、検索するフォルダを尋ねて `init` を動かす。索引
   づくりそのもの(大きなコードでは数十分)は、`/context-index` か、利用者
   の端末で動かすよう勧める(頼まれれば、ここで動かす)
6. 新しいセッションで検索の道具が出ることを伝える

`model use` などの結果は、英語の 1 行(`result: saved` など)と終了コード
でも分かるようにしてあります(端末の文字コードで日本語が読めないとき
のため)。

モデルは取得しません(`model download` を動かさないと skill に書いて
あります)。`/context-setup` と違い、モデルを呼ぶのでトークンを使います。

### `/context-setup`(用意をするスラッシュコマンド)

`/context-index` と同じ拡張が足すコマンドです。Copilot CLI のセッションで
打つと、モデルを呼ばずに、上の `ctxf.py setup` を裏で動かします。
トークンを使いません。

```text
/context-setup
/context-setup no-model
/context-setup gpu
/context-setup help
```

- 1 行目がふつうの形です。済んでいれば、確かめるだけで終わります(手元で
  1 秒)。依存の一覧が変わった版に更新したときも、同じく打ちます
- 埋め込みモデルは入れません。終わりの知らせで、インストールされていない
  ことと、利用者がターミナルで実行する取得のコマンドを出します(上の
  「埋め込みモデルは自動では入れない」)
- `no-model`・`gpu`・`cpu` は、`setup` の `--no-model`・`--gpu`・`--cpu`
  と同じです(`--` を付けても構いません)。`help` は、何もせずに説明だけを
  出します。`gpu` と `cpu` を一緒に指定すると、断ります
- 利用者が自分で打つので、打ったことを同意とみなします。エージェントから
  は呼べません(大きな取得を、利用者の知らないうちに始めないため)
- 裏で動かすので、作業を続けられます。段階(仮想環境・パッケージ)が
  変わったときと、同じ段階が 1 分続くたびに、経過の秒数を出します。
  出力は、利用者ごとの置き場の
  `context-setup.log` に書きます(毎回書き直します)
- 終わると、かかった時間と、次にすること(作業フォルダに索引が無ければ
  `/context-index`、あれば新しいセッション)を出します。失敗したときは、
  ログの最後の数行(pip が届かない、Python の版が合わないなど、わけと
  直し方が書いてある)を添えます
- `/context-index` と `/context-setup` は、同時には動かしません。どちらかが
  動いている間に打つと、「途中です」と断ります(用意は、索引づくりが使う
  仮想環境を作り直すことがあるため)
- リポジトリを取得して `register-copilot.bat` で登録した人(プラグインの
  隣に `.venv` がある)は、`.venv` が先に使われるので、このコマンドは何も
  せずに `setup.bat` を案内します
- Python は、MCP サーバーと hook と同じく、PATH の `python` で動かします。
  `python` が 3.15 以降なら、この PC の 3.14 以前の版で動かし直します。
  無ければ断って、入れる版を知らせます(上の「入れ方は 2 つ」)
- 用意が済んでも、動いているセッションには検索の道具は出ません。
  `/context-index` は、同じセッションのまま打てます

### インターネットに出られない PC・PyPI のミラー

setup は、Python のパッケージを pip で取得します(モデルは、利用者が
`model download` を実行したときに Hugging Face から取得します)。pip は
環境変数の設定を読むので、setup を動かす前に設定しておけば、そのまま
効きます。

- プロキシ: `HTTPS_PROXY`(pip と `model download` の両方に効きます)
- ミラー(PyPI の写し): `PIP_INDEX_URL`
- インターネットに出られない PC: 出られる PC で、同じ OS と同じ Python の
  版(3.13 なら 3.13)を使って、依存を 1 つのフォルダに取得しておき、
  それを写します

```text
python -m pip download -d <wheels> "tree-sitter>=0.25" "tree-sitter-c>=0.23" ^
  "tree-sitter-cpp>=0.23" "tree-sitter-c-sharp>=0.23" "numpy>=2.0,<3" ^
  "onnxruntime>=1.20,<2" "tokenizers>=0.21" "huggingface_hub>=0.36,<3" ^
  "jsonschema>=4.20,<5"
```

依存の一覧は `pyproject.toml` の `dependencies` が正です。版を上げて一覧が
変わったら、取得し直します。写した PC では、次のように設定してから setup
を動かします。

```text
set PIP_NO_INDEX=1
set PIP_FIND_LINKS=<wheels>
python -I "<プラグインのフォルダ>\ctxf.py" setup
```

(コマンド プロンプトの書き方です。PowerShell なら `$env:PIP_NO_INDEX = "1"`
のように書きます。)

モデルは、ほかの PC の置き場の `models` フォルダを、同じ場所に写せば使え
ます(setup の終わりに「embedding model: installed」と出ます)。写す前に、
この PC で使ってよいかを確かめてください。置き場の仮想環境
(`runtime\venv`)は、写しても使えません。中に、もとの Python の場所が
書かれているためです。

Windows と Python 3.13 で、このフォルダ(約 39 MB)を `PIP_NO_INDEX` と
`PIP_FIND_LINKS` で渡して `setup --no-model` を動かすと、PyPI に行かずに
依存が入り、検索(モデルが無いので keyword)が動きました【実測】。Linux では
確かめていません【未確認】。

### 索引を作る

作業フォルダ(Copilot を起動するフォルダ)で、検索したいフォルダを並べて
設定を作り、索引を作ります。エージェントに頼んでも、自分で動かしても
構いません。Copilot CLI では、セッションで `/context-index` と打つのが
いちばん簡単です(下の「`/context-index`」)。

```text
python -I "<プラグインのフォルダ>\ctxf.py" init "<エンジンのソース>" "<設計文書>"
python -I "<プラグインのフォルダ>\ctxf.py" index
```

最初の索引づくりは、約 8,000 ファイルで 30 分ほどかかります(ほとんどが
埋め込み)。2 回目からは、変わっていない passage の埋め込みを使い回すので、
変わったファイルの分だけで済みます([design.md](design.md) の「差分更新」)。

- 索引(`.cf\index.db`)には、対象のファイルの本文の写しが入ります。git
  には入りません(`init` が、全部を無視する `.cf\.gitignore` を置きます)。
  Perforce では、`.cf` を `.p4ignore` に足してください(`init` もそう
  知らせます)。足さないと、`p4 reconcile` などで追加の候補に挙がります
- 秘密を持ちやすい名前のファイル(`.env`、`*.pem`、`*.key` など)は、既定で
  除きます。名前だけで見るので、ほかのファイルの中に書かれた秘密は索引に
  入ります。除く規則は [readme-ja.md](../readme-ja.md) の「設定ファイル」
- 大きいコードの最初の索引づくりは、エージェントに任せず、自分の端末で
  動かすのがよいです(数十分かかり、その間エージェントは待つだけになり
  ます)。skill の文も、そう勧めるよう書いています
- `.cf` は、自分で作ります。取ってきたリポジトリに `.cf` が入っていたら、
  使わずに消して、作り直してください。設定は、作業フォルダの外のフォルダ
  (source のルート、モデルのフォルダ、用語の対応表)も指せるためです
  ([design.md](design.md) の「置き場所と、設定の探し方」)
- 途中で止めても(Ctrl+C など)、次の `index` は、計算済みの埋め込みを
  使い回して続きから計算します。計算した埋め込みは、1,024 passage ごとに
  `.cf\index.db.partial` に書いておきます(プロセスを強制終了したときは、
  最後に書いた後の分だけ計算し直します)。`--full` は、これも使わずに
  すべて埋め込み直します

### `/context-index`(索引を作るスラッシュコマンド)

プラグインの拡張(`com.github.copilot\extensions\context-foundation\extension.mjs`)
が足すコマンドです。Copilot CLI のセッションで打つと、モデルを呼ばずに、
上の `init` と `index` を裏で動かします。トークンを使いません。

```text
/context-index "<エンジンのソース>" "<設計文書>"
/context-index
/context-index full
```

- 1 行目は、初めて(`.cf` が無いとき)の形です。並べたフォルダで作業
  フォルダに設定を作り(`init`)、続けて索引を作ります。相対パスは作業
  フォルダから数えます。空白を含むパスは `"…"` で囲みます。`full` という
  名前のフォルダは `./full` と書きます
- 初めてでフォルダを並べずに打つと(2 行目の形)、作業フォルダの直下の
  フォルダを一覧にしたダイアログを出します。チェックを付けたものを索引に
  入れます(Space で付け外し、Enter で決める、Esc でやめる)。初めは全部に
  チェックが付いています。版管理・ビルドの出力・依存物のフォルダ
  (`.git`・`node_modules`・`build`・`bin` など、索引に入らない名前)と、
  名前が `.` で始まるものは、一覧に出しません。フォルダへのリンク・
  ジャンクションは出します。一覧は名前の順で 40 個までで、それより多い
  ときは、出していない数を一覧の説明に書きます。「. (作業フォルダ全体)」を
  選ぶと、直下のファイルも含めて作業フォルダごと入れます。CLI が足す
  「Other」には、一覧に無いフォルダ(作業フォルダの外も可)のパスを
  書き込めます(一覧のフォルダと同じものを書いても、2 度は入れません)。
  直下にフォルダが無ければ、作業フォルダ全体で作るかを
  尋ねます。ダイアログを出せないホストでは、並べて打つ使い方を出します
- 2 行目は、2 回目からの形でもあります。作業フォルダから親へさかのぼって
  `.cf\config.toml` を探し、その設定で `index` を動かします。変わって
  いない passage の埋め込みは使い回します。設定があるのにフォルダを
  並べると、断ります(設定を黙って書き換えないため)。検索するフォルダを
  変えるときは、`.cf\config.toml` の `[[source]]` を直します
- 3 行目は、すべて埋め込み直す形です(`index --full`)
- 用意(setup)が済んでいない・古くなっているときは、何も作らずに止まり、
  `/context-setup` とターミナルのコマンドを案内します。setup は大きな取得
  (約 700 MB)をするので、このコマンドからは動かしません
- 裏で動かすので、作業を続けられます。動いている間は、進み具合(ファイルの
  取り込み、埋め込みの何 % と残りの時間)をときどき出します。同時に動かせる
  のは 1 つだけです。出力は `.cf\context-index.log` に書きます(毎回書き
  直します)
- 終わると、できたこと(かかった時間)か、失敗のわけを出します。わけは
  終了コードごとです(下の「索引を更新する」の終了コード)。埋め込みモデルが
  無く、keyword だけの索引になったときも、そう出します
- 初めての索引を作ったセッションでは、エージェントには始めに「索引が無い」
  と伝わっています。新しいセッションからは、hook が検索を先に使うよう
  知らせます。今のセッションでも、索引ができると MCP サーバーが道具の
  一覧の変化を知らせ、次の依頼から検索の道具が出ます(下の「出し方」の
  「索引の無い作業フォルダ」)【実測】
- Python は、MCP サーバーと hook と同じく、PATH の `python` で動かします
- エージェントからは呼べません。利用者が自分で打つので、打ったことを同意と
  みなします。MCP の道具に索引づくりを足さなかったのは、MCP サーバーを
  読み取り専用に保つためと、最初の索引づくりが長く(約 8,000 ファイルで
  30 分ほど)、道具の呼び出しの時間切れにかかりやすいためです

**Copilot CLI の拡張は実験的機能で、実験的機能を有効にしたときだけ読み込まれ
ます。** セッションで `/settings experimental on` と打つ(設定の
`"experimental": true` に残り、次からも有効)か、`copilot --experimental` で
起動します【公式: `copilot help extensions`】。Copilot CLI 1.0.114 では、
`/settings experimental on` だけ(機能フラグ無し)で、一覧に
`/context-setup` と `/context-index` が出ました【実測】。1.0.112 と 1.0.113
では、機能フラグ `EXTENSIONS` を有効にして動かしました【実測】:
`%USERPROFILE%\.copilot\settings.json` に
`"feature_flags": { "enabled": ["EXTENSIONS"] }` を足す(すでに
`feature_flags` があれば、`enabled` の配列に `"EXTENSIONS"` を足す)か、
環境変数 `COPILOT_CLI_ENABLED_FEATURE_FLAGS=EXTENSIONS` を設定してから
起動します。拡張はセッションが始まってから読み込まれるので、入力欄に
`/context` と打って一覧に出てから実行します(一覧に出る前に送ると、ただの
依頼としてモデルに送られます)(下の「確かめたこと」の「0.4.2」「0.4.3」)。

**Copilot CLI 1.0.113 では、0.4.2 より前の版の `/context-index` は読み込まれ
ません。** 1.0.113 から、CLI が拡張に渡す SDK の名前が
`@microsoft/copilot-sdk` に変わり、前の名前(`@github/copilot-sdk`)では
読み込めなくなったためです。コマンドの置き場も変わりました。0.4.2 は、
どちらの名前と置き場でも動きます。一覧に `/context-index` が出ないときは、
`/extensions manage` を開きます。`context-foundation:context-foundation` に
「failed」と出ていれば、`copilot plugin update context-foundation` で
0.4.2 以降にして、新しいセッションを始めます。

GitHub Copilot app は、拡張がセッションに出す知らせのうち、エラーしか表示
しません(cxxref で確かめたこと)。そのため、アプリで動いていると分かった
ときは、結果をダイアログにも出します。ダイアログのボタンは、どれを押しても
閉じるだけです。進み具合はダイアログにしないので、アプリでは出ません。
出し方は環境変数 `CF_DISPLAY` で変えられます: `auto`(既定。アプリのとき
だけダイアログにも出す)、`dialog`(いつもダイアログにも出す)、`timeline`
(ダイアログを出さない)。このプラグインのアプリでの動作(ダイアログが
出るか)は、まだ確かめていません【未確認】。アプリのランタイム
(1.0.106-8)は、前の名前の SDK を渡します。

## 索引を更新する

索引は、`index` を流したときの内容のままです。ソースを同期したら(Perforce
の `p4 sync`、`git pull` など)、もう一度流します。Copilot CLI のセッション
では、`/context-index` と打っても同じです。

- 索引の後に増えたファイルは、作り直すまで検索に出ません。変わった
  ファイルや消えたファイルを指す結果には印が付きます(`[changed since
  index]`、`[file no longer exists]`)が、増えたファイルは結果に出ない
  ので、印も付きません
- どれだけ古いかは、`status --check` で数えられます。索引の後に変わった・
  増えた・消えたファイルの数と、その例を出します。フォルダを歩いて
  ファイルを読むので、大きいコーパスでは数十秒かかります(既定の
  `status` と検索では行いません)
- `status --check` が数えるのは、今の版の取り込みの規則で作り直すと変わる
  ファイルです。ほかの版(0.3.1 など)で作った索引では、規則の違いの分も、
  ファイルは変わっていないのに、増えた・消えたに数えます。そのときは、
  索引を作った道具の版と、そのことも出します。一度 `index` を流すと
  消えます
- 2 回目からの索引づくりは、変わったファイルの分だけを分け直し、変わった
  passage の分だけを埋め込みます。中身の変わらないファイルは、前の索引の
  passage を写します(0.4.5 から。0.4.4 までは、毎回すべてのファイルを分け
  直していました。約 19,000 ファイルで 185 秒)。ファイルは毎回すべて読み、
  本文で見分けます。仕組みは [design.md](design.md) の「分割を写す」
- Copilot のセッションを開いたまま流して構いません。検索は、検索のたびに
  索引を開き直すので、流し終わった後の検索から新しい索引を使います。
  Windows で、ちょうど検索中で置き換えられないときは、少し待って繰り
  返し、それでもだめなら、できあがった索引を `.cf\index.db.new` に残して
  終了コード 1 で止まります。次の `index` は、そこから埋め込みを使い
  回します

対話なしで流すコマンドです。作業フォルダを `--config` で渡すので、どこから
動かしても構いません。`--quiet` は、途中の進み具合を出しません(終わりの
要約は出ます)。

```text
python -I "<プラグインのフォルダ>\ctxf.py" index --config "<作業フォルダ>" --quiet
```

リポジトリを取得した人は、`"<取得したフォルダ>\.venv\Scripts\python.exe" -I
"<取得したフォルダ>\ctxf.py" --run index --config "<作業フォルダ>" --quiet`
です。

終了コードは、0 が成功、1 が索引を置き換えられなかった(上のとおり)、2 が
設定の誤り・source のルートが無い・入るファイルが無い・モデルを読めない
(埋め込みのある索引を keyword だけで置き換えないため)・モデルを取得
できなかった(`--download`)、3 が `.cf` が見つからない(`--config` を
付けないとき)、4 が用意ができていない(setup が済んでいない、プラグインの
更新で依存の一覧が変わった、仮想環境のもとの Python が消えたなど)、130 が
途中で止めた、です。2 と 4 のときは、前の索引がそのまま使えます。4 は、
setup をやり直すまで続きます。4 は入口(`python -I "<…>\ctxf.py" index
…`)から動かしたときで、上の `--run` の形では、要るパッケージを読めない
ときは 104、Windows で `.venv` のもとの Python が消えたときは 103 で終わり
ます。

毎日決まった時刻に流すなら、Windows のタスク スケジューラに登録します
(例は毎朝 7 時。`<…>` は自分の場所に置き換えます)。この例は、登録して
動かすところまでは確かめていません【未確認】。

```text
schtasks /Create /TN "context-foundation index" /SC DAILY /ST 07:00 ^
  /TR "python -I \"<プラグインのフォルダ>\ctxf.py\" index --config \"<作業フォルダ>\" --quiet"
```

- タスクは、ログオンしている利用者として動かします(置き場は利用者ごとの
  `%LOCALAPPDATA%` にあるため)
- 同期の後に流すのが一番よいので、同期をスクリプトにしているなら、その
  最後に上の 1 行を足すほうが確かです
- ドライブが外れていて source のルートが無い日は、何も書かずに終了
  コード 2 で止まり、前の索引が残ります

## リポジトリを取得して使う

1. `setup.bat`: 取得したフォルダに `.venv` を作り、依存を入れる。埋め込み
   モデルは入れず、インストールされているかと入手先を出す
2. `index.bat`: 作業フォルダと検索したいフォルダを尋ねて、索引を作る
3. `register-copilot.bat`: このフォルダを手元の Marketplace として足し、
   プラグインを入れる(中身は `copilot register`。コマンドの呼び方は
   [readme-ja.md](../readme-ja.md) の「コマンド」)

`.bat` は、`.venv` の python で `ctxf.py --run <コマンド>` を動かします。
フォルダを動かしたり写したりしても、そのまま動きます(写したフォルダは、
写したフォルダの中のコードを動かし、登録します)。

リポジトリを更新して依存の一覧(`pyproject.toml`)が変わったときは、
`setup.bat` をもう一度実行します(`.venv` の依存は自動では確かめません)。
`setup.bat` は、壊れた `.venv` や、範囲の外の Python(3.11 から 3.14 の
外)で作った `.venv` を作り直します。入れた後に要るパッケージを読み込める
かも確かめ、読めなければ `.venv` を 1 回だけ作り直して入れ直します(それ
でも読めなければ、終了コード 1 で止まります)。`.venv` のもとの Python が
消えたときと、`.venv` の python が要るパッケージを読めなくなったときは、
入口が気づいて、道具を出さずに「`setup.bat` を動かす」よう知らせます
(利用者ごとの置き場には切り替えません)。

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
- 解除は `unregister-copilot.bat`(中身は `copilot unregister`)です。この
  フォルダから入れたものだけを外します。状態は `copilot status` で
  見られます
- 登録は、取得したリポジトリのフォルダからだけできます。pip で入れた
  パッケージ(`ctxf` のコマンドだけがあるもの)からは、skill と入口が
  無いので、知らせて止まります
- 登録のあと、PATH の `python` が無いか起動しないときは、そう知らせます
  (MCP サーバーと hook は `python` で起動するため)。登録は止めません
- フォルダのパスに `& | < > ^ % ( ) ! "` があると、npm で入れた Copilot CLI
  (`copilot.cmd`)に正しく渡せないので、登録を断ります

## 出し方: MCP サーバーと hook(既定)、skill は控え

| 出し方 | 中身 | 入れ方 |
| --- | --- | --- |
| MCP サーバーと hook(既定) | ツールは `search` と `status` だけ。読み取り専用。hook は、索引のある作業フォルダでだけ、検索を grep より先に使うように 1 文で知らせる | プラグイン(`mcp.json` と `hooks.json`) |
| skill(控え) | `skills/context-search`。MCP の道具が使えない場面で、検索の CLI を 1 回呼ぶ。最初の用意(setup)のしかたも、ここに書いてある | プラグイン |
| skill(用意) | `skills/context-search-setup`。用意を、利用者に尋ねながら進める(パッケージ、手元に置いたモデルのフォルダ) | プラグイン |
| 拡張(`/context-setup`・`/context-index`) | 利用者が打つ、用意をするコマンドと、索引を作る・作り直すコマンド。モデルを呼ばない。Copilot CLI の実験的機能を有効にすると読み込まれる(上の「`/context-index`」) | プラグイン(`com.github.copilot\extensions`) |

0.2 までは、skill と hook が既定で、MCP サーバーは追加でした。0.3 で入れ
替えました。理由は、Godot の調整用の 7 問を 3 回ずつ流した結果です
(Claude Haiku 5.5。数字は [benchmarks.md](benchmarks.md) の「出し方を MCP
サーバーと始めの 1 文に変えたとき」)【実測】。

- skill の形では、モデルが skill を読まない回が残った(21 回のうち 5 回)。
  読んだ回も、skill を読む分だけモデルの呼び出しが 1 回増え、検索の
  たびに CLI を起動して 1.5〜2 秒かかった。既存の手順(grep だけ)より、
  速くも安くもならなかった
- MCP の形に、始めの 1 文を足すと、21 回すべてで検索が使われた。モデルの
  呼び出しは中央値で 5 回から 2 回に、入力トークンは半分以下になり、時間は
  10%、費用は 14% 下がった。変えたあとに評価用の 29 問で比べると、回答の
  合格率はほぼ同じで、時間は 35%、費用は 32% 下がった
- MCP の道具を置くだけでは、使われなかった(17 回のうち 0 回)。モデルは、
  覚えている識別子で grep から始めた。そこで、始めの 1 文には「検索を
  grep より先に」と書いている

MCP の形で気を付けること:

- 道具の定義は、使わない回でも、モデルを呼ぶたびに送られる。索引の無い
  作業フォルダでも送られる。合成のコーパスでの実測では、モデルを呼ぶ
  1 回目の入力の上乗せは、skill と始めの 1 文で +358、MCP サーバーで +365
  トークンだった(0.2 のときの計測)
- サーバーは、セッションごとに起動したままになる。索引のある作業
  フォルダでは、起動のときに埋め込みモデルとベクトルを読み込む。最初の
  検索は、読み込みを待つと 1.2 秒、先に読み込んであれば 0.03 秒だった。
  持ち続けるメモリは、約 7 万 passage の索引で 0.9 GB ほど【実測】。
  セッションをたくさん開く人は、設定の `[search] preload = false` で、
  最初の検索まで読み込みを遅らせられる
- Copilot CLI は、プラグインに入っている MCP サーバーを、プラグインの
  フォルダを作業フォルダにして起動する。利用者の作業フォルダは、環境変数
  `COPILOT_SESSION_CWD` で渡る(MCP の roots は空で返る)。サーバーは、
  その場所から `.cf` を探す【実測。公式文書には、作業フォルダの記載が無い】
- 道具の名前は、Copilot CLI では `context-foundation-search` と
  `context-foundation-status` になる【実測】
- 最初の用意(`ctxf.py setup`)が済んでいない PC では、サーバーは道具を
  1 つも出さずに起動したままになる。始めの 1 文が、理由(「not set up」
  「set up for an older version」など)と、skill を読めば用意のしかたが
  分かることを知らせる。サーバーは、理由と setup のコマンドを、標準誤差に
  書く(ホストの MCP のログに残る。コマンドで 1 行を終え、次の行に新しい
  セッションのことを書く)。setup を済ませても、同じセッション
  には道具は出ない。新しいセッションから出る
- `.cf` か索引の無い作業フォルダでは、起動の挨拶の指示は「No
  context-foundation index in this workspace; search with grep.」の 1 文
  になり、道具の一覧は空で返す(0.4.7 から)。道具の定義は、使わない回でも
  毎回モデルに送られ、2 つで 381 トークンになるため(Copilot CLI 1.0.114 の
  `/context` で数えた)【実測】。サーバーは 3 秒おきに索引の有無を見て、
  変わったら `notifications/tools/list_changed` で知らせる。Copilot CLI
  1.0.114 は、知らせを受けると、次にモデルを呼ぶときに一覧を取り直す。
  そのため、セッションの途中で `/context-index` で索引を作ると、次の依頼
  から検索の道具が出る【実測】。プロセス(サーバーは約 60 MB)は、索引の
  無い作業フォルダでも起動したまま。重いもの(numpy・onnxruntime・モデル)は、
  検索を呼ばれるまで読まない
- 8,192 バイトを超える質問は、誤りとして断る
- `status` の道具は、source ごとに、どのフォルダかを出す(`sources: engine
  = corpus/engine (…)`)。検索の結果も、source のルートが作業フォルダ
  そのものでないときは、`roots:` の行でルートを出す。作業フォルダの外の
  ルートは、どちらにも絶対パスで出るので、そのパス(利用者の名前を含む
  こともある)がモデルに送られる。検索の結果では、検索のたびに送られる。
  避けるには、ルートを作業フォルダの中に置く
- リポジトリを取得して使う人の `register-copilot.bat --mcp` は、要らなく
  なった(指定しても、何もしない)

skill の形(控え)の弱み【実測】:

- 検索のたびに、モデルの呼び出しが 1 回増える(skill を読む回)
- 検索のたびに CLI を起動するので、1 回に 1.5〜2 秒かかる(約 7 万 passage
  の索引で、中央値 2.0 秒)
- shell の許可が要る(下の「許可」)
- skill を読むかどうかは、モデルが決める

## 許可

MCP の道具は、対話のセッションでは、最初に呼ぶときに許可を尋ねられます
(`copilot help permissions`)【公式】。確認のできない実行(`copilot -p`)
では、`--allow-tool context-foundation` で通りました【実測】。shell の
許可は要りません。

控えの skill は、shell で `python` を起動します。入口の `ctxf.py` は作業フォルダの
外にあるので、対話のセッションでは、最初の実行のときに許可を尋ねられます。
確認のできない実行(`copilot -p`)では、`--allow-tool "shell(python:*)"`
で通りました【実測】。この指定は、`python` で始まる命令をすべて通します。
計測では、書き込みを断る指定(`--deny-tool write`)と組み合わせています。

## 検索の限界

- 検索は、上位の passage(既定 8 件。MCP の道具では最大 20 件)だけを返します。ほかにも
  当たる場所があるか、当たったものが質問と関係があるかは、知らせません。
  識別子が分かっていて、使われている場所をすべて挙げたいとき(この関数は
  どこから呼ばれているか)は、grep か、cxxref のような参照の索引を使います。
  無い識別子を聞いても、意味の近い passage が返ります
- エージェントへの始めの 1 文と道具の説明は、「検索を grep より先に」
  「検索の語は英語で」と書いています。日本語の設計文書は、日本語の語が
  検索に入らないと、順位が下がりやすいです(控えの skill の文は、利用者の
  言葉を後ろに残すよう書いています)
- この 2 つは、まだ直していません。始めの 1 文と道具の説明は、Godot で
  エージェントの計測をした形そのもので、変えるには測り直しが要るため
  です

## うまく動かないとき

最初に、作業フォルダ(Copilot を起動するフォルダ)で次の 1 行を動かします。
入口の Python、PATH の `python`、使う仮想環境と置き場、モデルの有無、
今のフォルダ(と `COPILOT_SESSION_CWD`)から見える索引を、1 回で出します。

```text
python -I "<プラグインのフォルダ>\ctxf.py" where
```

| 症状 | 見るもの | 直し方 |
| --- | --- | --- |
| 道具の一覧に `context-foundation-search` が出ない | `where` の `PATH python:`。`copilot mcp list` に `context-foundation` があるか | `python` が無い・起動しないなら、PATH に足す(上の「入れ方は 2 つ」)。あれば、下の 2 行を見る |
| 道具が出ず、始めの 1 文が「the search tool is not set up …」「the search tool is set up for …」「the search tool is set up with …」 | `where` の `python:` の行の理由と `setup:` の行 | 理由が「not set up」「set up for an older version」「set up with a Python that is no longer installed」なら、`setup:` の行のコマンドを動かし、新しいセッションを始める。プラグインとして入れた人は `ctxf.py setup`(Copilot CLI なら `/context-setup` でも同じ)、リポジトリを取得した人(隣の `.venv`)は `setup.bat` |
| 道具が出ず、検索のコマンドが「set up in a virtual environment whose packages can no longer be loaded」(終了コード 4) | `where` の `model:` の行。この理由は、`where` の `python:` の行(仮想環境の python のパスが出る)にも始めの 1 文にも出ない。`model:` が `unknown (ImportError: …)` などになる。MCP のログにも出る | `setup:` の行のコマンドを動かし、新しいセッションを始める。setup は、読めないパッケージに気づくと、仮想環境を 1 回作り直して入れ直す。それでも直らなければ、文面のとおり仮想環境のフォルダ(リポジトリを取得した人は `.venv`)を消してから、もう一度 setup |
| setup の後も道具が出ない | 同じセッションのままか | 新しいセッションを始める。動いているセッションには出ない(控えの skill は動く) |
| 検索が「No context-foundation index」 | `where` の `index:` と `index (COPILOT_SESSION_CWD):` | 作業フォルダで `init` と `index`。アプリで、索引はあるのにこう出るなら、作業フォルダが渡っていない見込み(上の「アプリ」) |
| 結果の note が「Fell back to keyword」 | note の理由。`where` の `model:` | 「embedding model is not available」なら、モデルがインストールされていない。入手先とライセンスを確かめ、使ってよければ利用者が `model download` を実行する(setup では入らない)。「embedded with different settings」なら `index` で作り直す |
| 結果に `[changed since index]` が多い・新しいファイルが出ない | `status --check` | `index` を流す(上の「索引を更新する」) |
| setup が pip で失敗する | 最後の 1 行の原因 | 長いパスなら `CF_HOME` を短く。届かないなら `HTTPS_PROXY` か `PIP_INDEX_URL`(上の「インターネットに出られない PC・PyPI のミラー」) |
| `index` が終了コード 1 で止まる | 文面の `index.db.new` | 索引を開いているセッションを閉じてから、もう一度 `index` |
| 日本語が文字化けする(控えの skill) | PowerShell の出力の文字コード | `[Console]::OutputEncoding = [Text.Encoding]::UTF8` を 1 回動かす |

`status`(`--format human` で人が読む形)は、索引の作成日時、source ごとの
フォルダ、除いたファイルの数(理由ごと)、既定の名前で除いたフォルダの中に
あった索引に入りうるファイルの数、使える検索(`hybrid / keyword /
semantic` か keyword だけか)を出します。

不具合を見つけたら、[readme-ja.md](../readme-ja.md) の「不具合の報告」の
とおりに知らせてください。

## 保存されるものと消し方

| 場所 | もの | 中身 |
| --- | --- | --- |
| 作業フォルダの `.cf` | `config.toml`、`config.toml.bak`(`init --force` の前の設定)、`glossary.tsv`(書いたとき)、`.gitignore` | 設定 |
| 同上 | `index.db` | 索引。対象のファイルの本文の写しが入る。大きさはソースの 2〜3 倍 |
| 同上 | `index.db.partial`、`index.db.new`、`index.db.tmp-<番号>` | 索引づくりの途中の埋め込み、置き換えられなかった索引、作っている途中の索引。次の `index` が使い回すか消す |
| 同上 | `context-index.log` | `/context-index` の出力(進み具合と結果)。毎回書き直す |
| 同上 | `eval\run-*`(`eval.bat` の既定の出力先) | 評価の結果。検索結果(ソースの場所の一覧)が入る |
| 利用者ごとの置き場 | `runtime\venv`(約 180 MB)、`runtime\deps.json`、`runtime\setup.lock`(setup の最中だけ) | `ctxf.py setup` が作る仮想環境と、その覚え書き |
| 同上 | `context-setup.log` | `/context-setup` の出力(setup の出力)。毎回書き直す |
| 同上 | `models`(約 490 MB) | 埋め込みモデル |
| 同上 | `state.json` | `.bat` の対話が覚える、作業フォルダと `cases.jsonl` の場所 |
| Copilot の設定フォルダ(`~/.copilot`。`COPILOT_HOME` で変わる) | `installed-plugins\context-foundation\context-foundation` | GitHub から入れたプラグインの写し |
| 取得したリポジトリのフォルダ | `.venv`(約 190 MB) | `setup.bat` が作る仮想環境 |

利用者ごとの置き場の場所です。環境変数 `CF_HOME` を設定すると、どの OS
でも、そのフォルダ 1 つにまとまります(モデルだけは `CF_MODEL_CACHE` で
別にできます)。

| OS | 仮想環境(`runtime`) | モデル(`models`)と `state.json` |
| --- | --- | --- |
| Windows | `%LOCALAPPDATA%\context-foundation\runtime` | `%LOCALAPPDATA%\context-foundation\models`、`...\state.json` |
| Linux・macOS | `~/.local/share/context-foundation/runtime`(`XDG_DATA_HOME` があればその下) | `~/.cache/context-foundation/models`、`~/.cache/context-foundation/state.json` |

Linux と macOS で 2 か所に分かれるのは、0.1 のモデルの置き場を引き継いで
いるためです。

外すときは、次の順です(GitHub の Marketplace から入れたとき)。

1. `copilot plugin uninstall context-foundation`
2. `copilot plugin marketplace remove context-foundation`
3. 利用者ごとの置き場を消す(上の表の場所。Windows なら
   `%LOCALAPPDATA%\context-foundation`)
4. 各作業フォルダの `.cf` を消す(索引には、ソースの本文の写しが入って
   いるので、残したくなければ)

リポジトリを取得して入れたときは、1 と 2 の代わりに
`unregister-copilot.bat` を動かし、取得したフォルダごと消します(`.venv`
もその中です)。プラグインを外しても、3 と 4 は残ります。

## 確かめたこと

### 0.4.9(関連の処理を見てから答える)

確かめた日は 2026-11-19 です。Windows 11、Copilot CLI 1.0.114 を
`copilot -p` で動かし(空の設定フォルダ、BYOK の Claude Haiku 5.5、
指示のファイルを読ませない設定)、O3DE の評価用の 27 問を 3 回ずつ流し
ました。

- 81 回とも最後まで通り、検索の道具が使われ、上位の passage に添えた
  同じファイルの名前(`same file also has: ...`)が結果に出た【実測】
- 道具の説明と始めの 1 文は 0.4.7 より長くなった(始めの 1 文は
  840 文字)。それを含めても、入力トークンの中央値は既存の手順より
  5% 少なかった【実測】(数字と品質は docs/benchmarks.md の「0.4.9 の
  直し」)
- アプリでは、まだ試していない【未確認】

### 0.4.7(索引の無い作業フォルダでは、道具を出さない)

確かめた日は 2026-11-19 です。Windows 11、Copilot CLI 1.0.114 の対話の
画面を、疑似端末から操作しました(空の設定フォルダ、BYOK の Claude Haiku
5.5、`--plugin-dir` で git の作業フォルダから読む)。

- 直す前は、索引の無い作業フォルダでも、`/context` の「MCP Tools」に
  `context-foundation-search`(304 トークン)と `context-foundation-status`
  (77 トークン)が出ていた。セッションの始まりの文脈 33k トークンの約 1%
  【実測】
- 直した後は、索引の無い作業フォルダの `/context` に、context-foundation の
  道具が出なくなった【実測】
- 同じセッションで `/context-index` で索引を作ると、サーバーは 3 秒の
  うちに索引に気づいて知らせを送った。CLI は、知らせを受けた時点では一覧を
  取り直さず、次の依頼(モデルの呼び出し)の時に取り直した。サーバーは
  それに道具 2 つで答えた【実測。サーバーに一時的な記録を足して確かめた】。
  この確かめで、モデルを 1 回呼んだ(入力 33,047 トークン、うちキャッシュ
  への書き込み 33,043、出力 83。Haiku 5.5 の単価で約 0.004 ドル)
- 索引の無い作業フォルダでの負担(直す前と同じ): MCP サーバーは Python の
  プロセス 3 つで約 61 MB、起動して挨拶に答えるまで約 240 ms(裏で起動
  する)。セッションの始めの hook は 56 ms で、何も出さない。拡張は
  プロセス 1 つで約 73 MB。どれも、重いもの(numpy・onnxruntime・モデル)は
  読まない【実測】

確かめていないこと: アプリでの動作。知らせを受けた後、次の依頼でモデルが
実際に検索の道具を使ったか(道具の一覧が取り直されたことまでを確かめた)
【未確認】。

### 0.4.4(初めての `/context-index` で、フォルダを選ぶ)

確かめた日は 2026-11-18 です。0.4.3 と同じ環境(Windows 11、Copilot CLI
1.0.114、空の設定フォルダ、疑似端末、BYOK の Claude Haiku 5.5)で、
`src`・`docs`・`node_modules` のある作業フォルダで動かしました。

- `/context-index` を並べずに打つと、「Copilot needs information.」の
  ダイアログに、`docs` と `src` がチェック付きで出た。`node_modules` は
  出なかった。CLI は、選択肢の最後に「Other (type your answer)」を足した
  【実測】
- Esc で閉じると「索引づくりをやめました」と出し、`.cf` を作らなかった。
  そのまま Enter で決めると、`docs` と `src` の設定と索引ができた【実測】
- `docs` のチェックを Space で外し、「Other」にほかの作業フォルダの
  `../ws/docs` を書き込んで決めると、`src` と、作業フォルダの外の
  `docs`(絶対パス)の設定と索引ができた【実測】
- この間、モデルの呼び出しは 0 回だった【実測】

確かめていないこと: アプリでの動作(ダイアログの出方)、直下にフォルダが
無い作業フォルダでの確かめのダイアログ(単体テストでは、候補が空になる
ことだけを確かめた)【未確認】。

### 0.4.3(`/context-setup` を足す)

確かめた日は 2026-11-18 です。Windows 11、Python 3.13 で、Copilot CLI
1.0.114 の対話の画面を、疑似端末から操作しました。Copilot の設定フォルダ
(`COPILOT_HOME`)と利用者ごとの置き場(`CF_HOME`)は、どちらも空の
フォルダにしました。モデルは BYOK(Anthropic の Claude Haiku 5.5)です。
0.4.3 は `--plugin-dir` で git の作業フォルダから読み、ほかのプラグインは
読ませていません(`COPILOT_PLUGIN_DIR_ONLY`)。題材は、テスト用の合成の
11 ファイル(コードと文書)の作業フォルダです。アプリは動かしていません。

- 実験的機能が無効のままでは、一覧に `/context-setup` も `/context-index`
  も出なかった。`/settings experimental on` と打つと、CLI がセッションを
  読み込み直し、設定に `"experimental": true` が残った。次に起動した
  セッションでは、機能フラグ `EXTENSIONS` が無くても、一覧に 2 つが出た
  【実測】
- 用意の無い PC で `/context-index src docs` と打つと、何も作らずに、
  `/context-setup` とターミナルのコマンドを案内した【実測】
- `/context-setup` で、仮想環境・パッケージ・モデルの取得の段階が順に出て、
  32 秒で「用意ができました」と出た。続けて、同じセッションで
  `/context-index src docs` が、埋め込みつきの索引を作った(2 秒)【実測】。
  32 秒と短いのは、この PC に pip の取得の控え(キャッシュ)があったためと
  みられる【推測】。初めての PC では、取得の分だけ長くなる
- 済んだ後にもう一度 `/context-setup` と打つと、1 秒で終わり、作業フォルダに
  索引があるので新しいセッションを始めるよう案内した。`/context-setup gpu
  cpu` は断り、`/context-setup help` は説明だけを出した【実測】
- この間、モデルの呼び出しは 0 回だった。5 つのセッションの記録に、利用者の
  依頼もモデルの応答も 1 件も無い【実測】
- 同じ設定フォルダで、セッションの中から `/plugin marketplace add
  noxitro/context-foundation` と `/plugin install
  context-foundation@context-foundation` を打つと、GitHub に
  ログインしていなくても入った。次のセッションで、入れたプラグイン(0.4.2)の
  `/context-index` が一覧に出た【実測】
- 偽の SDK で、`/context-setup` の最中に `/context-index` ともう 1 つの
  `/context-setup` を呼ぶと、どちらも「用意(setup)の途中です」と断った。
  プラグインの隣に `.venv` があると、何もせずに `setup.bat` を案内した
  【実測】。実物の画面では、用意が 1 秒で終わったので、断る場面は作れな
  かった
- モデルの取得の % は、ログに出なかった(huggingface_hub は、端末でない
  出力には進み具合を書かない)。そのため、経過の秒数だけを出す【実測】

確かめていないこと: アプリでの動作、Copilot CLI 1.0.113 以前での
`/context-setup`、初めての PC での取得の時間、失敗(PyPI に届かない、
Python 3.15 など)の文の実物の画面での出方(単体テストで文の形だけを
確かめた)【未確認】。

### 0.4.2(`/context-index` を Copilot CLI 1.0.113 に合わせる)

確かめた日は 2026-11-18 です。Windows 11、Python 3.13 で、Copilot CLI
1.0.113 と 1.0.112 の対話の画面を動かしました。画面は疑似端末から操作し
(キーを送り、画面の文字を読む)、0.4.2 は `--plugin-dir` で git の作業
フォルダから読みました(Marketplace から入れた 0.4.1 も、同時に読まれて
います)。題材は、合成の 5 ファイル(コード 3、文書 2)の作業フォルダです。
アプリは動かしていません。

- 1.0.113 で、入力欄に `/context-i` と打つと、一覧に `/context-index` が
  出た【実測】
- `.cf` の無い作業フォルダで、フォルダを並べずに打つと、使い方を出し、
  `.cf` を作らなかった【実測】
- `/context-index src docs` で、設定と索引ができた(2 秒)。続けて
  `/context-index` で作り直し(1 秒)、`/context-index full` ですべて埋め
  込み直した(1 秒)。始めた知らせと、できた知らせが画面に出た。この 3 回
  の間、モデルの呼び出しは 0 回だった(Copilot の設定フォルダの
  `session-store.db` の `assistant_usage_events` に、そのセッションの行が
  無い)【実測】
- 同じセッションで、検索の道具で探すよう頼むと、MCP の `search` が呼ばれ、
  hybrid で 5 件を返し、`src/render/BufferPool.cpp:1-13` を答えた。モデル
  の呼び出しは、この質問の 2 回だけだった【実測】
- 1.0.112 でも、拡張は動いた(`/extensions manage` で running)。一覧に
  出て、作り直しが動いた。ただし、0.4.1 も running で、同じ名前の
  コマンドを足すので、どちらが受けたかは分からない【実測】
- 確かめていないこと: 進み具合の表示(小さい題材ですぐ終わるため出ない)、
  続けて 2 回打ったときに断ること、用意(setup)の無い PC での動き。
  どれも、本物の CLI では試していない(偽の SDK では 0.4.1 で確かめた)
  【未確認】

### 0.4.1(`/context-index`)

確かめた日は 2026-11-18 です。初めは、Copilot の SDK を偽物(コマンドを
受け取り、知らせを画面に出すだけ)に差し替えて、Linux(Node.js 22、
Python 3.13)で動かしました。そのあと、Windows 11 の Copilot CLI で動かすと、
1.0.113 では読み込まれませんでした(0.4.2 で直した)。

- Copilot CLI 1.0.113 では、一覧に `/context-index` が出なかった。
  `/extensions manage` で、この拡張は「failed」だった。同じ SDK の名前を
  使う、ほかのプラグインの拡張(cxxref など)も、同じく「failed」だった
  【実測】
- わけは、CLI が拡張の SDK を差し替える仕組み(配布物の
  `preloads\extension_sdk_resolver.mjs`)が向ける名前が、1.0.113 から
  `@microsoft/copilot-sdk` だけになったこと。手元に残っていた配布物で
  比べると、1.0.106-8 から 1.0.112 まで(アプリのランタイムを含む)は
  `@github/copilot-sdk` だけだった。各版の仕組みを読み込んだ Node.js で、
  2 つの名前を読み込み比べ、同じ結果になった【実測】。1.0.113 の変更履歴
  には、この変更は書かれていない
- Copilot CLI 1.0.112 では、この拡張は動いた(running)【実測】
- 下は、偽の SDK で確かめたこと

- `.cf` の無い作業フォルダで、フォルダを並べずに打つと、使い方を出して
  何も作らなかった。用意(setup)の無い PC では、フォルダを並べても、
  setup のコマンドを出して何も作らなかった【実測】
- 用意のある PC で、フォルダを並べると、設定と索引ができた。2 回目は
  作り直しになり、`full` では `index --full` を動かした。設定があるのに
  フォルダを並べると断った。続けて 2 回打つと、2 つ目は断った【実測】
- 実物のモデルで、合成の 1,500 ファイルの初めての索引(103 秒)を作り、
  埋め込みの進み具合が約 1 割ごとに出た。モデルの無い PC では、keyword
  だけの索引になったことを出した【実測】
- プラグインの通しの確かめ(`.github/scripts/plugin_smoke.py`)に、偽の
  SDK で `/context-index` を動かす段を足し、通った【実測】

### 0.4.0(本番に向けた直し)

確かめた日は 2026-11-17 です。Copilot は動かしていません(道具の側だけ
です)。手元で動かしたのは Windows 11 と Python 3.13 です(ほかに、WSL の
Ubuntu 24.04 と Python 3.12 でテストだけを流しました)。

- 道具の側の直しは、テスト(合成のコーパス)と、手元の Windows で動かして
  確かめた【実測】
- 索引が新しく、source のルートが作業フォルダそのものなら、エージェント
  向けの検索の出力(CLI の `--format agent` と MCP の `search`)、道具の
  名前・説明・引数、索引のある作業フォルダの起動の挨拶の指示、始めの
  1 文は、0.3.1 とバイト単位で同じだった(合成のコーパスと埋め込みの
  代わりのもので比べた。semantic と hybrid は、実物のモデルでも比べた)
  【実測】
- 0.3.1 で作った索引は、作り直さずに検索できた【実測】
- テストは、WSL の Ubuntu 24.04 と Python 3.12 でも流した【実測】
- 上の「インターネットに出られない PC・PyPI のミラー」の手順で、PyPI に
  行かずに setup が通った【実測】

### 0.3(MCP サーバーを同梱した形)

- 空の設定フォルダ(`COPILOT_HOME`)に、手元のフォルダからプラグインを
  入れ、Godot の索引のある作業フォルダで `copilot -p` を 1 回動かした
  (BYOK の Claude Haiku 5.5)。`copilot mcp list` に、プラグインの
  サーバーとして `context-foundation` が出た。セッションの始めの hook が
  1 文を足し、モデルが `context-foundation-search` を呼び、8 件の passage
  が返り、場所を根拠に挙げて答えた【実測】
- 直す前は、同じ流れで、検索が「索引が無い」と返った。サーバーの作業
  フォルダが、プラグインのフォルダだったため。上の「気を付けること」の
  とおりに直した【実測】
- MCP サーバーの形での回答の正しさ・時間・費用は、Godot の評価用の
  29 問で、既存の手順と比べた。回答の合格率はほぼ同じで、時間は 35%、
  費用は 32% 下がった([benchmarks.md](benchmarks.md) の「0.3 の形での、
  評価用の質問での比較」)
- GitHub の Marketplace(`noxitro/context-foundation`)から、0.3.0 を空の
  設定フォルダに入れて、同じ作業フォルダで 1 回動かした。ファイルが
  写される形でも、始めの 1 文 → `context-foundation-search` を 1 回 →
  場所つきの回答、まで通った。モデルの呼び出しは 2 回。Python の依存は、
  利用者ごとの置き場の仮想環境(0.2 のときに `setup` で作ったもの)が
  そのまま使われた【実測】

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
- GitHub の Marketplace(`noxitro/context-foundation`)から、この
  リポジトリ自身のプラグイン(0.2.0)を入れた。空の設定フォルダと、
  ほかのプラグインも入っているふだんの設定の両方で確かめた。git が追跡
  している 82 ファイルが写り、`setup` → `init` → `index` → `search` と
  hook の 1 文が動いた【実測】
- ふだんの設定で、公開されているコード(O3DE)の索引(約 7 万 passage)の
  ある作業フォルダから、`copilot -p` を 1 回動かした(BYOK の Claude
  Haiku 5.5、日本語の質問)。hook の 1 文 → skill → 検索 1 回 → 根拠の
  場所つきの回答まで通った。モデルの呼び出し 3 回、入力 83,446 トークン
  (うちキャッシュの読み 52,043、書き 31,393)、出力 1,078 トークン、API の
  時間 6.5 秒【実測】

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

- アプリでの動作(上の「アプリ」)。プラグインの MCP サーバーが、アプリでも
  同じ環境変数で作業フォルダを受け取るか
- 対話のセッションでの、hook の 1 文と skill の動作(確かめたのは
  `copilot -p`。対話のセッションで MCP の検索の道具が呼ばれることは、
  0.4.2 の確かめで見た)
- Claude Haiku 5.5 のほかのモデルで、skill が読まれるか
- `python` が無い PC、Microsoft Store 版の Python での動作
- Linux と macOS で、プラグインとして動かすこと(`python3` しか無い PC を
  含む)。Python の部分のテストは、CI で Ubuntu でも回している
- 0.4.0 の直しを、Python 3.11・3.14 で手元で動かすこと(CI のテスト
  だけ)。Linux と 3.12 は、WSL でテストを流しただけ
- 0.4.0 の Copilot での動作(Copilot は動かしていない)。setup の後に
  新しいセッションで道具が出ること、`.cf` の無い作業フォルダでの起動の
  挨拶
- `/context-index` と `/context-setup` の、アプリでの動作(結果がダイアログに
  出るか)。Copilot CLI での、`/context-index` の進み具合の表示・続けて
  打ったときに断ること(上の「0.4.2」)。実験的機能と機能フラグが無いと
  読み込まれないことは、1.0.114 で確かめた(上の「0.4.3」)
- 本当のネットワークの共有フォルダ(UNC)の上の作業フォルダ(確かめたのは、
  自分の PC の管理用の共有だけ)
- タスク スケジューラでの、索引の定期の更新(上の「索引を更新する」)
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
  "version": "0.4.10"
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
