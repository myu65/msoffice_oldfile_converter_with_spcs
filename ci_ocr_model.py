#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ci_ocr_model.py
- Hugging Face から olmOCR モデルスナップショットを取得
- Snowflake Stage へ階層ごと PUT

例:
  uv run python ci_ocr_model.py \
    --model allenai/olmOCR-2-7B-1025-FP8 \
    --stage @DOC_MODEL_STAGE/olmocr \
    --connection YOUR_CONNECTION

環境変数 `HF_TOKEN` を設定するとプライベートモデルにも対応します。
"""

import argparse
import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from huggingface_hub import snapshot_download


def run(cmd):
    printable = " ".join(shlex.quote(str(c)) for c in cmd)
    print(f"+ {printable}")
    subprocess.run(cmd, check=True)


def ensure(cmd_name: str):
    from shutil import which

    if not which(cmd_name):
        print(f"[ERROR] `{cmd_name}` が見つかりません。インストールしてください。", file=sys.stderr)
        sys.exit(1)


def infer_dest(repo_id: str) -> str:
    return repo_id.split("/")[-1]


def build_stage_target(stage: str, dest_prefix: str, rel_parent: Path) -> str:
    parts = [stage.rstrip("/")]
    if dest_prefix:
        parts.append(dest_prefix.strip("/"))
    if rel_parent and rel_parent != Path("."):
        parts.append(rel_parent.as_posix())
    return "/".join(parts)


def make_put_sql(local_path: Path, stage_target: str, overwrite: bool) -> str:
    file_url = f"file://{local_path}"
    file_url_escaped = file_url.replace("'", "''")
    target_escaped = stage_target.replace("'", "''")
    overwrite_flag = "TRUE" if overwrite else "FALSE"
    return f"PUT '{file_url_escaped}' '{target_escaped}' AUTO_COMPRESS=FALSE OVERWRITE={overwrite_flag};"


def upload_directory(root: Path, stage: str, dest_prefix: str, connection: str | None, overwrite: bool):
    files = sorted(p for p in root.rglob("*") if p.is_file())
    if not files:
        print("[WARN] アップロード対象ファイルが見つかりません。", file=sys.stderr)
        return

    for idx, file_path in enumerate(files, start=1):
        rel_parent = file_path.relative_to(root).parent
        stage_target = build_stage_target(stage, dest_prefix, rel_parent)
        sql = make_put_sql(file_path, stage_target, overwrite)
        cmd = ["snow", "sql", "-q", sql]
        if connection:
            cmd += ["--connection", connection]
        print(f"[INFO] ({idx}/{len(files)}) PUT -> {stage_target}")
        run(cmd)

    print("[INFO] Upload completed.")


def main():
    parser = argparse.ArgumentParser(description="Download olmOCR model and upload to Snowflake stage")
    parser.add_argument("--model", default="allenai/olmOCR-2-7B-1025-FP8", help="Hugging Face repo id")
    parser.add_argument("--revision", default=None, help="Specific revision/tag/commit to download")
    parser.add_argument("--stage", default="@DOC_MODEL_STAGE/olmocr", help="Snowflake stage (and optional prefix) to upload into")
    parser.add_argument("--dest-prefix", default=None, help="Destination directory name on stage (defaults to model repo base name)")
    parser.add_argument("--connection", help="snow CLI connection name")
    parser.add_argument("--local-dir", help="Skip download and use existing directory")
    parser.add_argument("--overwrite", action="store_true", help="Pass OVERWRITE=TRUE to PUT commands")
    parser.add_argument("--keep-download", action="store_true", help="Keep downloaded snapshot (in temp dir by default)")
    args = parser.parse_args()

    ensure("snow")

    if args.local_dir:
        root_dir = Path(args.local_dir).resolve()
        if not root_dir.is_dir():
            print(f"[ERROR] --local-dir がディレクトリではありません: {root_dir}", file=sys.stderr)
            sys.exit(2)
    else:
        temp_dir_path = Path(tempfile.mkdtemp(prefix="olmocr_model_"))
        print(f"[INFO] Downloading model {args.model} -> {temp_dir_path}")
        snapshot_download(
            repo_id=args.model,
            revision=args.revision,
            local_dir=temp_dir_path,
            local_dir_use_symlinks=False,
        )
        root_dir = temp_dir_path

    temp_dir_for_cleanup = None if args.local_dir else root_dir

    dest_prefix = args.dest_prefix or infer_dest(args.model)
    print(f"[INFO] Upload destination: {args.stage.rstrip('/')}/{dest_prefix}")

    try:
        upload_directory(root_dir, args.stage, dest_prefix, args.connection, args.overwrite)
    finally:
        if temp_dir_for_cleanup is not None:
            if args.keep_download:
                print(f"[INFO] Download retained at {temp_dir_for_cleanup}")
            else:
                shutil.rmtree(temp_dir_for_cleanup, ignore_errors=True)


if __name__ == "__main__":
    main()
