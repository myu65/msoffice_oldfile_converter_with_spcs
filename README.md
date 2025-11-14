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

### Snow CLI の connection.toml 設定
`snow` CLI v2 の接続情報は `~/.snowflake/connection.toml` に保存します。`ci_push.py` や `ci_spcs.py` の `--connection YOUR_CONNECTION` オプションで指定する名前と合わせて、以下のようにプロファイルを作成してください。

```toml
[connections.YOUR_CONNECTION]
account = "xxxxxx-xy12345"
user = "YOUR_USER"
role = "YOUR_ROLE"
warehouse = "YOUR_WH"
database = "SNOWFLAKE_LEARNING_DB"
schema = "DATA_TEST"
authenticator = "SNOWFLAKE"
```

データベースとスキーマも適当です。トライアルアカウントに適当につくっています。

キー認証を使う場合は、同じセクションに `private_key_path` と `private_key_passphrase` (必要であれば) を追記します。例:

```toml
private_key_path = "/home/you/.snowflake/rsa_key.p8"
private_key_passphrase = "env:SNOWFLAKE_PRIVATE_KEY_PASSPHRASE"
```

設定後に `snow connection list` や `snow sql -q "SELECT 1" --connection YOUR_CONNECTION` で疎通を確認しておくと安心です。

### 1. イメージをビルド & push
```bash
uv run python ci_push.py \
  --repo SNOWFLAKE_LEARNING_DB.DATA_TEST.DOC_TOOLS \
  --image lo-convert --tag latest \
  --build \
  --put-spec --spec-path specs/lo_convert.yaml \
  --stage @DOC_STAGE --spec-dest specs/lo_convert.yaml \
  --connection YOUR_CONNECTION
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
  --connection YOUR_CONNECTION
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
  --connection YOUR_CONNECTION
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
  --connection YOUR_CONNECTION
```

- 自動生成されたジョブ名を使う場合は、`job` 実行時のログに表示された名前を指定してください
- Job 実行時と同じ `--database` / `--schema` を指定すると、ログ取得前に適切なコンテキストへ切り替わります
- LibreOffice 変換ジョブは `--container lo`、olmOCR ジョブは `--container olmocr` を指定してください

たぶん、JOB動いてる間しか見れない。

### olmOCR Markdown OCR ジョブ (新規)

GPU を含む Compute Pool で [alleninstituteforai/olmocr](https://hub.docker.com/r/alleninstituteforai/olmocr) イメージをそのまま動かして PDF を Markdown に変換する SPCS ジョブ用 spec (`specs/olmocr.yaml`) を追加しました。既存の LibreOffice 変換ジョブとは完全に独立しています。

#### イメージのビルド & プッシュ
- `dockerfile.olmocr` で公式 Docker イメージに SPCS 向けエントリポイントを追加しています
- 例:
  ```bash
  uv run python ci_push.py \
    --repo SNOWFLAKE_LEARNING_DB.DATA_TEST.DOC_TOOLS \
    --image olmocr --tag latest \
    --dockerfile dockerfile.olmocr \
    --context . \
    --build \
    --put-spec --spec-path specs/olmocr.yaml \
    --stage @DOC_STAGE --spec-dest specs/olmocr.yaml \
    --connection YOUR_CONNECTION
  ```
  `--skip-image` を付ければ spec だけ更新可能です

#### モデルのステージ配置
- `ci_ocr_model.py` で Hugging Face からダウンロードし Snowflake stage へ PUT できます
  ```bash
  export HF_TOKEN=...  # 必要な場合
  uv run python ci_ocr_model.py \
    --model allenai/olmOCR-2-7B-1025-FP8 \
    --stage @DOC_MODEL_STAGE/olmocr \
    --connection YOUR_CONNECTION
  ```
- `--model` フラグは複数回指定できるので、複数の Hugging Face リポジトリを一括でステージへ PUT したい場合にも対応できます
- 既に展開済みのディレクトリを使う場合は `--local-dir /path/to/dir` を指定
- Stage 上では `@DOC_MODEL_STAGE/olmocr/<モデル名>/...` の形で配置され、spec の `OCR_MODEL_SUBDIR` と連動します
  - ローカルにダウンロードだけ行いたい場合は `--no-upload` を付けると Snowflake への PUT をスキップできます（`--skip-download` で既存ディレクトリをそのまま使うことも可能）

#### ステージ構成
- `@DOC_STAGE/ocr/in/` : 入力 PDF/画像 (`.pdf/.png/.jpg/.jpeg`)
- `@DOC_STAGE/ocr/workspace/` : ワークスペース（`markdown/` 以下に出力 md）
- `@DOC_MODEL_STAGE/olmocr/<モデル名>/` : モデルファイル群
- spec 内の `OCR_MODEL_SUBDIR` で使用するモデルディレクトリ名、`OCR_WORKERS` で並列度を調整できます（追加引数は `OCR_EXTRA_ARGS` へスペース区切りで指定）

#### ジョブ実行手順
- GPU 対応の Compute Pool を作成（例：`GPU_NV_S` ファミリー相当）
- 入力 PDF を `@DOC_STAGE/ocr/in/` に配置（サブディレクトリも可）
- ジョブ実行:
  ```bash
  uv run python ci_spcs.py job \
    --pool <GPU_POOL_NAME> \
    --stage @DOC_STAGE \
    --spec specs/olmocr.yaml \
    --database SNOWFLAKE_LEARNING_DB \
    --schema DATA_TEST \
    --connection YOUR_CONNECTION \
    --sync
  ```

成功すると `@DOC_STAGE/ocr/workspace/markdown/` に入力ファイルと同じ相対パスで Markdown が並びます。`input_files.txt` が空だった場合はジョブ側で安全に終了します。ログ取得時は `--container olmocr` を指定してください。

### LightOnOCR Markdown OCR (新規)

LightOnAI の LightOnOCR モデルを使って PDF を Markdown に変換し、出力を Parquet 形式でまとめるジョブです。olmOCR と同様に GPU プールが必要です。

#### モデルのステージ配置
- LightOnOCR の重みをステージへ配置する際は `ci_ocr_model.py` の `--model` / `--stage` を差し替えます。
  ```bash
  export HF_TOKEN=...  # 必要な場合
  uv run python ci_ocr_model.py \
    --model LightOnAI/LightOnOCR-1B-1025 \
    --stage @DOC_MODEL_STAGE/lightonocr \
    --connection YOUR_CONNECTION
  ```
  ステージ上では `@DOC_MODEL_STAGE/lightonocr/LightOnOCR-1B-1025/...` のように配置されます。
  ローカル検証用にモデルだけ取得したいときは `--no-upload --local-dir ./models/LightOnOCR-1B-1025` のように実行してください（既存ディレクトリを再利用する場合は `--skip-download` を追加）。

#### ステージ構成
- `@DOC_STAGE/lightonocr/in/` : 入力 PDF
- `@DOC_STAGE/lightonocr/workspace/` : 中間成果物（Markdown ファイルなど）
- `@DOC_STAGE/lightonocr/out/` : Parquet 出力 (`lightonocr_output.parquet`)
- `@DOC_MODEL_STAGE/lightonocr/` : LightOnOCR モデルファイル

#### イメージのビルド & プッシュ
```bash
uv run python ci_push.py \
  --repo SNOWFLAKE_LEARNING_DB.DATA_TEST.DOC_TOOLS \
  --image lightonocr --tag latest \
  --dockerfile dockerfile.lightonocr \
  --context . \
  --build \
  --put-spec --spec-path specs/lightonocr.yaml \
  --stage @DOC_STAGE --spec-dest specs/lightonocr.yaml \
  --connection YOUR_CONNECTION
```

#### ジョブ実行
```bash
uv run python ci_spcs.py job \
  --pool <GPU_POOL_NAME> \
  --stage @DOC_STAGE \
  --spec specs/lightonocr.yaml \
  --database SNOWFLAKE_LEARNING_DB \
  --schema DATA_TEST \
  --connection YOUR_CONNECTION \
  --sync
```

実行後、`@DOC_STAGE/lightonocr/out/lightonocr_output.parquet` に 1 行 1 PDF の Markdown が格納されます（カラム: `source_path`, `page_count`, `markdown`, `page_markdown`, `model_name`, `generated_at`, `error`）。Markdown ファイル群も `@DOC_STAGE/lightonocr/workspace/markdown/` に生成されるため、必要に応じてダウンロードして確認できます。ログ取得時は `--container lightonocr` を指定してください。

#### ローカルテスト (GPU 環境)
モデルをローカルパス `./models/LightOnOCR-1B-1025` に展開済みと仮定して Docker コンテナを実行する例:
```bash
docker run --rm --gpus all \
  -v "$(pwd)/in:/in" \
  -v "$(pwd)/out:/out" \
  -v "$(pwd)/workspace:/workspace" \
  -v "$(pwd)/models:/models" \
  -e OCR_MAX_MODEL_LEN=8192 \
  -e OCR_MAX_BATCH_TOKENS=8192 \
  lightonocr:local
```
`OCR_MAX_BATCH_TOKENS` は `OCR_MAX_MODEL_LEN` 以上になるよう設定してください（デフォルトでは自動的に同じ値になります）。
WSL2 などで GPU を渡す場合は `/usr/lib/wsl/lib` にある CUDA ドライバを優先するようエントリポイントが `LD_LIBRARY_PATH` を自動調整します。値を上書きする場合も同ディレクトリを含めてください。
# End Patch
GPU を検出できない環境（例: WSL 上で NVIDIA ドライバ未設定）では `-e OCR_DEVICE=cpu` を追加すると CPU モードで起動しますが、推論時間が大幅に延びる点に注意してください。

### YomiToku Document OCR (新規)

[kotaro-kinoshita/yomitoku](https://github.com/kotaro-kinoshita/yomitoku) をベースにした日本語向け Document AI/OCR の SPCS ジョブです。文字検出・認識・レイアウト・表構造の 4 モデルをローカルキャッシュから読み込み、PDF や画像を Markdown/HTML/JSON 等に変換します。

#### モデルのステージ配置
- 4 つの Hugging Face リポジトリをまとめて `@DOC_MODEL_STAGE/yomitoku/` 配下へ PUT します
  ```bash
  export HF_TOKEN=...  # 必要に応じて
  uv run python ci_ocr_model.py \
    --stage @DOC_MODEL_STAGE/yomitoku \
    --connection YOUR_CONNECTION \
    --model KotaroKinoshita/yomitoku-text-detector-dbnet-v2 \
    --model KotaroKinoshita/yomitoku-text-recognizer-parseq-middle-v2 \
    --model KotaroKinoshita/yomitoku-layout-parser-rtdtrv2-v2 \
    --model KotaroKinoshita/yomitoku-table-structure-recognizer-rtdtrv2-open-beta
  ```
  それぞれ `@DOC_MODEL_STAGE/yomitoku/<リポジトリ名>/...` というディレクトリ構造で配置され、spec の `YOMITOKU_*_SUBDIR` 環境変数と一致させます。

#### ステージ構成
- `@DOC_STAGE/yomitoku/in/` : 入力 PDF / 画像（`pdf/png/jpg/jpeg/bmp/tif/tiff`）
- `@DOC_STAGE/yomitoku/out/` : `yomitoku` CLI の出力（デフォルトは Markdown / ページ単位ファイル）
- `@DOC_STAGE/yomitoku/workspace/` : 中間成果物（figure 可視化、生成した一時 config など）
- `@DOC_MODEL_STAGE/yomitoku/` : Hugging Face スナップショットを展開したモデル郡

#### イメージのビルド & プッシュ
```bash
uv run python ci_push.py \
  --repo SNOWFLAKE_LEARNING_DB.DATA_TEST.DOC_TOOLS \
  --image yomitoku --tag latest \
  --dockerfile dockerfile.yomitoku \
  --context . \
  --build \
  --put-spec --spec-path specs/yomitoku.yaml \
  --stage @DOC_STAGE --spec-dest specs/yomitoku.yaml \
  --connection YOUR_CONNECTION
```

#### ジョブ実行
```bash
uv run python ci_spcs.py job \
  --pool <GPU_POOL_NAME> \
  --stage @DOC_STAGE \
  --spec specs/yomitoku.yaml \
  --database SNOWFLAKE_LEARNING_DB \
  --schema DATA_TEST \
  --connection YOUR_CONNECTION \
  --sync
```
- デフォルトでは Markdown (`YOMITOKU_FORMAT=md`) + ページ統合 (`YOMITOKU_COMBINE=1`) + 図抽出 (`YOMITOKU_FIGURE=1`) を有効化しています。JSON/HTML/PDF に切り替えたい場合は spec か `ci_spcs.py job --spec` 実行時に環境変数を上書きしてください。
- `YOMITOKU_DEVICE=auto` で GPU を自動検出します。CPU のみで動かす場合は `YOMITOKU_DEVICE=cpu`。
- 追加の CLI オプションは `YOMITOKU_EXTRA_ARGS`（例: `"--pages 1-3 --ignore_meta"`）に半角スペース区切りで渡せます。
- 生成された Markdown/HTML/JSON は `@DOC_STAGE/yomitoku/out/`、図や可視化画像は `@DOC_STAGE/yomitoku/workspace/figures/` に保存されます。ログ取得時は `ci_spcs.py logs --container yomitoku ...` を指定してください。

## 開発メモ
- `dockerfile` は `ja_JP.UTF-8` ロケールと Noto CJK フォントを設定しているので日本語文書でも文字化けしにくい
- LibreOffice CLI での変換が失敗した際はログにフィルター名などが出力されます
- Calc PDF を UNO で吐き出す際は既存の改ページや印刷範囲を尊重します

公開用に調整しているので、追加の改善や要望があれば Issue/PR へどうぞ。
