# soffice container tools

LibreOffice のヘッドレス実行環境を Docker/Snowflake Container Services (SPCS) で扱えるようにしたセットです。`convert.py` で古い Office 形式 (doc/ppt/xls) をモダン形式や PDF にまとめて変換できます。神 Excel も一度 PDF/modern 形式を経由させて Cortex `AI_PARSE_DOCUMENT` などに渡せます。

## できること
- `convert.py` を使って、doc/ppt/xls を docx/pptx/xlsx へ一括変換
- オプションで PDF も同時生成（Calc 系は UNO を使って横 1 ページに収める出力が可能）
- Debian ベースの Docker イメージに LibreOffice / python3-uno / 日本語フォントを同梱
- Snowflake SPCS へイメージを push & Job Service を実行するためのスクリプト (`ci_push.py`, `ci_spcs.py`)

## リポジトリ構成 (抜粋)
- `convert.py` : 変換ロジック本体。`soffice` CLI と UNO を併用
- `dockerfile` : LibreOffice 一式を入れたコンテナ定義
- `specs/lo_convert.yaml` : SPCS Job Service 用の specification
- `ci_push.py` : Docker イメージの build/push + spec をステージへ PUT
- `ci_spcs.py` : Compute Pool 作成・Job 実行・ログ取得
- `in/`, `out/` : ローカル検証用のサンプル入出力

## convert.py の使い方
```bash
uv run python convert.py \
  --inputs "in/**/*" \
  --out-dir out \
  --passthrough-modern \
  --also-pdf \
  --calc-fit-export
```

主なオプション:
- `--inputs` : glob パターンを複数指定可。マッチしたファイルのみ対象
- `--out-dir` : 出力ルート
- `--passthrough-modern` : 既にモダン形式 (docx/pptx/xlsx/od*) はコピー
- `--also-pdf` : PDF も出力
- `--calc-fit-export` : Calc 系は UNO 経由で横 1 ページに収める PDF を生成

UNO 利用のため `python3-uno` が必要です (Docker イメージ内には同梱済み)。

## Docker での利用
```bash
# イメージをビルド
uv run python ci_push.py --build \  # build だけなら connection など不要
  --repo dummy.dummy.dummy \
  --image lo-convert --tag local \
  --context .

# ローカル実行例 (in/out をマウント)
docker run --rm \
  -v "$(pwd)/in:/in" \
  -v "$(pwd)/out:/out" \
  lo-convert:local \
  python3 /usr/local/bin/convert.py \
    --inputs "/in/**/*" \
    --out-dir /out \
    --passthrough-modern \
    --also-pdf \
    --calc-fit-export
```

## Snowflake SPCS で回す
前提:
- `uv` あるいは Python 3.11 環境
- `snow` CLI v2 (pyproject の依存に含む)
- SPCS が利用可能な Snowflake アカウントと適切なロール/権限
- ステージ `@DOC_STAGE` に以下のディレクトリを用意
  - `raw/` : 入力ファイルを置く
  - `convert/out/` : 出力先 (Job で out ボリュームとしてマウント)

### 1. イメージをビルド & push
```bash
uv run python ci_push.py \
  --repo SNOWFLAKE_LEARNING_DB.DATA_TEST.DOC_TOOLS \
  --image lo-convert --tag latest \
  --build \
  --put-spec --spec-path specs/lo_convert.yaml \
  --stage @DOC_STAGE --spec-dest specs/lo_convert.yaml \
  --connection XVRALMC-UX32060
```
- 既存イメージを使い spec だけ差し替えるときは `--skip-image` を付けて実行
- Snowflake CLI の connection プロファイル名は環境に合わせて調整
- `--put-spec` を付けると spec ファイルを stage にアップロード
- Job 実行時にも spec は自動で PUT されるため、ここでの `--put-spec` は手動で事前配置したい場合のみ使用

### 2. Compute Pool 作成
```bash
uv run python ci_spcs.py pool \
  --name LO_POOL_XS \
  --size XS \
  --min-nodes 1 --max-nodes 1 \
  --connection XVRALMC-UX32060
```
既存のプールを使う場合はこのステップ不要です。

### 3. Job Service 実行
```bash
uv run python ci_spcs.py job \
  --pool LO_POOL_XS \
  --stage @DOC_STAGE \
  --spec specs/lo_convert.yaml \
  --database SNOWFLAKE_LEARNING_DB \
  --schema DATA_TEST \
  --sync \
  --connection XVRALMC-UX32060
```
- `--spec` にローカルのテンプレートを渡すと、ジョブ名 + タイムスタンプ + ランダム ID 付きの spec が毎回ステージに PUT されます
- PUT 先は `@DOC_STAGE/<spec パス>` なので、テンプレートをサブディレクトリに置くとその構造が反映されます（例: `specs/lo_convert.yaml` → `@DOC_STAGE/specs/...`）
- `--name` を省略すると `DOC_JOB_YYYYMMDDHHMMSS` 形式で自動生成され、実際のジョブ名は実行時に表示されます
- `--database` / `--schema` は Job 実行・ログ取得に使うステージ（および関連オブジェクト）が存在する DB/スキーマを指定してください
- `--sync` を外すと非同期実行
- 実行後、出力はステージ `@DOC_STAGE/convert/out/` に保存されます

### 4. ログ確認
```bash
uv run python ci_spcs.py logs \
  --name <実行時に表示されたジョブ名> \
  --container lo \
  --database SNOWFLAKE_LEARNING_DB \
  --schema DATA_TEST \
  --connection XVRALMC-UX32060
```

- 自動生成されたジョブ名を使う場合は、`job` 実行時のログに表示された名前を指定してください
- Job 実行時と同じ `--database` / `--schema` を指定すると、ログ取得前に適切なコンテキストへ切り替わります

## 開発メモ
- `dockerfile` は `ja_JP.UTF-8` ロケールと Noto CJK フォントを設定しているので日本語文書でも文字化けしにくい
- LibreOffice CLI での変換が失敗した際はログにフィルター名などが出力されます
- Calc PDF を UNO で吐き出す際は既存の改ページや印刷範囲を尊重します

公開用に調整しているので、追加の改善や要望があれば Issue/PR へどうぞ。
