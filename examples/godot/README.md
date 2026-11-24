# Godot での評価例

公開のゲームエンジン Godot を題材にした、評価データの実例です。
[評価データの仕様](../../docs/evaluation-data-spec.md)の形式で書いた質問と、
正解の場所が入っています。[O3DE の評価例](../o3de/README.md)と同じく、次の
2 つを兼ねます。

- この道具の検索の質を、だれでも同じ題材で測り直せるようにするもの
- エージェントを使う手法(既存の手順・この道具・言語サーバー・Serena)を、
  O3DE とは別のコードで比べるための題材

## 入っているもの

| ファイル | 中身 |
| --- | --- |
| `cases.jsonl` | 質問 32 件と、正解の場所(ファイルと行の範囲)と、正しい回答が示すべき点 |
| `config.example.toml` | 題材のコーパスを指す設定の見本 |

Godot のソースの本文は入っていません。入っているのは、質問、正解の場所
(パスと行番号)、その理由と、正しい回答が示すべき点を自分の言葉で書いた
文、注釈のときの覚え書き(`notes`)です。

[計測の一覧](../../docs/benchmarks.md)にある「Godot での評価」は、この評価例
より前に、別の版(コミット `30caae98b7`)と、公開していない質問で行った
ものです。この評価例の質問とは別物なので、数字は並べません。

## 題材

- Godot 4.7.2-stable(コミット `ed1daf0bf001b61586d9930840f2f1394092c079`)。
  ライセンスは MIT(<https://github.com/godotengine/godot>)
- コードは、リポジトリ全体から、外部のコード(`thirdparty`)とテスト
  (`tests`)を除いたもの(`source_id` は `godot`)。クラスの説明文
  (`doc/classes` の XML)は、別の source(`godot-docs`)にして入れる。
  既定では XML は索引に入らないので、設定の見本で `include` を書いている
- 正解の場所は、`scene`(シーンと GUI)、`servers` と `drivers`(描画・
  音・ドライバー)、`core` と `modules/gdscript`(基盤とスクリプト)、
  クラスの説明文にある。`platform` には無い

## 質問

| 分け方 | 件数 | 使い道 |
| --- | ---: | --- |
| 評価用(`split` が `eval`) | 24 | 判定に使う。結果を見て調整しない |
| 調整用(`tune`) | 8 | 結果を見ながら調整してよい |

評価用の 24 件の内訳:

| 種類(`category`) | 件数 |
| --- | ---: |
| `natural_language_only`(名前を知らずに、動きを言葉で聞く) | 9 |
| `identifier_known`(名前が分かっている) | 3 |
| `doc_to_code`(クラスの説明文に書いてあることの実装を探す) | 3 |
| `code_to_doc`(コードを説明している説明文を探す) | 3 |
| `similar_implementation`(同じ種類の別の実装を探す) | 4 |
| `unanswerable`(コーパスに答えが無い) | 2 |

- 質問の言語は、日本語 16 件、英語 8 件(調整用の 8 件は、すべて日本語の
  `natural_language_only`)
- 区域ごとに、case_id の頭をそろえてある。`GDS`(シーン、評価用 9 件)、
  `GDV`(描画・音・ドライバー、8 件)、`GDC`(基盤とスクリプト、7 件)。
  `bench/compare.py summary --by area` で、区域ごとの内訳が出る
- `natural_language_only` の質問は、ほかのエンジン(Unity・Unreal)や
  言語(C#・Python)の言い方で、Godot の名前を使わずに聞く。どれも、
  決める処理だけでなく、その下の層の実装・呼び出し元・別の版(描画の
  もう 1 つのバックエンドなど)まで答えないと、要点を満たさない
- 答えの要点は、どの質問も 3 つ(英語)。根拠の場所は、評価用で 1 問
  あたり平均 4.3 か所・約 260 行

## 正解の付け方

- 付けたのは AI(Claude のエージェント)。区域ごとに 1 つずつ、3 つの
  エージェントが質問と正解を書いた。grep とファイルの読み取りだけを
  使い、この道具も言語サーバーも Serena も使っていない
- 書いたエージェントとは別のエージェントが、全 32 件をソースと照らして
  点検した。134 か所の範囲は、どれも関数かクラスの説明の項目の切れ目と
  合っていた。12 件を直した(足りない根拠の場所を 10 か所足した、答えの
  要点のうち、質問が聞いていない 3 つと、細部の誤り 2 つを直した、名前を
  使わない質問に GDScript の語(`await`)が入っていた 1 件を言い換えた)
- 答えの無い 2 件は、作ったエージェントと点検したエージェントの両方が、
  それらしい語で検索して実装が無いことを確かめた(探した語は `notes`)
- 行の範囲がコーパスにあることは、`ctxf validate --check-corpus` で確かめた
- 人の目での確認はしていない【未確認】

## 再現のしかた

このリポジトリの外の、作業用のフォルダで行います(`<ここ>` は、この
リポジトリのフォルダ)。Godot を取得して、作業フォルダに設定を置きます。

```text
git clone --depth 1 --branch 4.7.2-stable https://github.com/godotengine/godot.git
mkdir godot-eval\.cf
copy <ここ>\examples\godot\config.example.toml godot-eval\.cf\config.toml
```

`godot-eval\.cf\config.toml` の `<godot>` を、取得したフォルダに書き換えて、
索引を作り、正解の場所を検査して、検索だけの評価を回します。

```text
cd godot-eval
ctxf index --revision godot-4.7.2-stable
ctxf validate --cases <ここ>\examples\godot\cases.jsonl --check-corpus
ctxf eval --cases <ここ>\examples\godot\cases.jsonl --out .cf\eval\run-01
```

エージェントを使う手法(言語サーバーと Serena を含む)で比べるときは、先に
ソースのルートに `compile_commands.json` と Serena のプロジェクトを作ります
([bench の README](../../bench/README.md)の「言語サーバーと Serena を用意
する」)。Godot の `compile_commands.json` は、ビルドせずに作れます
(scons は `pip install scons`)。

```text
cd godot
scons platform=windows compiledb=yes compiledb_gen_only=yes
uvx --from serena-agent==2.1.0 serena project create . --language cpp --index
```

Claude Code で 6 つの手法を交互に流す設定の見本は
[bench/arms.claude.example.json](../../bench/arms.claude.example.json) です。

## 測った結果

<!-- 検索だけの評価の結果は、索引ができたあとに書く -->
