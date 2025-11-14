#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ci_paddleocrvl_models.py

Warm PaddleOCR-VL (PaddleX) model caches locally and optionally PUT them to a
Snowflake stage for SPCS consumption.
"""

from __future__ import annotations

import argparse
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

try:
    from paddleocr import PaddleOCRVL
except (ImportError, AttributeError) as exc:  # pragma: no cover - import-time guidance
    raise RuntimeError(
        "PaddleOCR-VL 依存が見つかりません。例: "
        "`pip install --index-url https://www.paddlepaddle.org.cn/packages/stable/cu126/ paddlepaddle-gpu==3.2.1` "
        "および `pip install \"paddleocr[doc]==3.3.2\" pillow paddlex` を実行してください。"
    ) from exc

def run(cmd):
    printable = " ".join(shlex.quote(str(c)) for c in cmd)
    print(f"+ {printable}")
    subprocess.run(cmd, check=True)


def ensure(cmd_name: str):
    from shutil import which

    if not which(cmd_name):
        print(f"[ERROR] `{cmd_name}` が見つかりません。インストールしてください。", file=sys.stderr)
        sys.exit(1)


def build_stage_target(stage: str, dest_prefix: str | None, rel_parent: Path) -> str:
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


def upload_directory(root: Path, stage: str, dest_prefix: str | None, connection: str | None, overwrite: bool):
    ensure("snow")
    files = sorted(p for p in root.rglob("*") if p.is_file())
    if not files:
        print(f"[WARN] アップロード対象ファイルが見つかりません: {root}", file=sys.stderr)
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


def _has_official_models(path: Path) -> bool:
    official_dir = path / "official_models"
    if not official_dir.is_dir():
        return False
    try:
        next(official_dir.rglob("*"))
        return True
    except StopIteration:
        return False


def detect_paddlex_home(preferred: Path) -> Path:
    candidates: list[Path] = []
    candidates.append(preferred)
    env_candidate = Path(os.environ.get("PADDLEX_HOME", preferred))
    candidates.append(env_candidate)
    candidates.append(Path.home() / ".paddlex")

    seen: set[Path] = set()
    for candidate in candidates:
        candidate = candidate.resolve()
        if candidate in seen:
            continue
        seen.add(candidate)
        if _has_official_models(candidate):
            return candidate
    return preferred


def create_sample_image(width: int, height: int, text: str) -> Path:
    from PIL import Image, ImageDraw, ImageFont

    fd, tmp_path = tempfile.mkstemp(suffix=".png")
    os.close(fd)
    path = Path(tmp_path)

    img = Image.new("RGB", (width, height), color="white")
    draw = ImageDraw.Draw(img)
    font = ImageFont.load_default()
    draw.text((width * 0.05, height * 0.4), text, fill=(0, 0, 0), font=font)
    img.save(path)
    return path


def warm_paddleocr_models(
    requested_paddlex_home: Path,
    sample_text: str,
    width: int,
    height: int,
    use_doc_orientation: bool,
    use_doc_unwarping: bool,
    disable_layout: bool,
    vl_rec_server_url: str | None,
    device: str,
) -> None:
    os.environ.setdefault("FLAGS_allocator_strategy", "auto_growth")
    os.environ.setdefault("FLAGS_fraction_of_gpu_memory_to_use", "0.92")
    if device == "cpu":
        os.environ.setdefault("CUDA_VISIBLE_DEVICES", "")

    try:
        from PIL import Image  # noqa: F401 - ensure pillow is present
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("Pillow が見つかりません。`pip install pillow` を実行してください。") from exc

    requested_paddlex_home.mkdir(parents=True, exist_ok=True)
    os.environ["PADDLEX_HOME"] = str(requested_paddlex_home)
    os.environ["PADDLE_PDX_CACHE_HOME"] = str(requested_paddlex_home)

    sample_path = create_sample_image(width, height, sample_text)
    try:
        init_kwargs = {
            "use_doc_orientation_classify": use_doc_orientation,
            "use_doc_unwarping": use_doc_unwarping,
            "use_layout_detection": not disable_layout,
        }
        if vl_rec_server_url:
            init_kwargs["vl_rec_server_url"] = vl_rec_server_url
        print("[INFO] Initializing PaddleOCR-VL pipeline...")
        ocr = PaddleOCRVL(**init_kwargs)
        print("[INFO] Triggering warm-up inference to download official models...")
        ocr.predict(str(sample_path))
    finally:
        sample_path.unlink(missing_ok=True)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Warm PaddleOCR-VL (.paddlex) models and upload to a Snowflake stage."
    )
    parser.add_argument("--stage", help="Snowflake stage (e.g., @DOC_MODEL_STAGE/paddleocrvl). Required unless --no-upload.")
    parser.add_argument("--dest-prefix", default=".paddlex", help="Destination subdirectory under the stage.")
    parser.add_argument("--connection", help="snow CLI connection name")
    parser.add_argument("--local-dir", help="Directory to place or reuse the downloaded models (parent of .paddlex).")
    parser.add_argument("--skip-download", action="store_true", help="Reuse existing contents under --local-dir/.paddlex.")
    parser.add_argument("--no-upload", action="store_true", help="Skip uploading to Snowflake (download only).")
    parser.add_argument("--overwrite", action="store_true", help="Pass OVERWRITE=TRUE to PUT commands.")
    parser.add_argument("--keep-download", action="store_true", help="Keep temporary download directories.")
    parser.add_argument("--sample-text", default="PaddleOCR-VL warmup", help="Text to draw on the synthetic image.")
    parser.add_argument("--image-width", type=int, default=1280, help="Width of the synthetic warm-up image.")
    parser.add_argument("--image-height", type=int, default=720, help="Height of the synthetic warm-up image.")
    parser.add_argument("--use-doc-orientation-classify", action="store_true")
    parser.add_argument("--use-doc-unwarping", action="store_true")
    parser.add_argument("--disable-layout-detection", action="store_true")
    parser.add_argument("--vl-rec-server-url", help="Optional external PaddleOCR-VL server URL.")
    parser.add_argument(
        "--device",
        choices=("auto", "cpu"),
        default="auto",
        help="Force CPU for warm-up when GPU is unavailable.",
    )
    parser.add_argument("--paddlex-subdir", default=".paddlex", help="Subdirectory name that stores PaddleX cache.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    if args.skip_download and not args.local_dir:
        print("[ERROR] --skip-download requires --local-dir", file=sys.stderr)
        return 2
    if not args.no_upload and not args.stage:
        print("[ERROR] --stage を指定してください (--no-upload の場合を除く)", file=sys.stderr)
        return 2

    cleanup_targets: list[Path] = []

    try:
        if args.local_dir:
            download_root = Path(args.local_dir).resolve()
            if download_root.exists() and not download_root.is_dir():
                print(f"[ERROR] --local-dir がディレクトリではありません: {download_root}", file=sys.stderr)
                return 2
            download_root.mkdir(parents=True, exist_ok=True)
        else:
            download_root = Path(tempfile.mkdtemp(prefix="paddleocrvl_models_"))
            cleanup_targets.append(download_root)

        requested_paddlex_root = download_root / args.paddlex_subdir

        if not args.skip_download:
            warm_paddleocr_models(
                requested_paddlex_home=requested_paddlex_root,
                sample_text=args.sample_text,
                width=args.image_width,
                height=args.image_height,
                use_doc_orientation=args.use_doc_orientation_classify,
                use_doc_unwarping=args.use_doc_unwarping,
                disable_layout=args.disable_layout_detection,
                vl_rec_server_url=args.vl_rec_server_url,
                device=args.device,
            )
            print(f"[INFO] Models downloaded under {requested_paddlex_root}")
        else:
            print(f"[INFO] --skip-download 指定により {requested_paddlex_root} を再利用します。")

        actual_paddlex_root = detect_paddlex_home(requested_paddlex_root)
        if not actual_paddlex_root.exists():
            print(f"[ERROR] PaddleX ディレクトリが見つかりません: {actual_paddlex_root}", file=sys.stderr)
            return 3
        if actual_paddlex_root != requested_paddlex_root:
            print(
                f"[INFO] Upload source will use {actual_paddlex_root} "
                f"(fallback from {requested_paddlex_root})"
            )

        if not args.no_upload:
            upload_directory(
                actual_paddlex_root,
                args.stage,
                args.dest_prefix,
                args.connection,
                args.overwrite,
            )
        else:
            print("[INFO] Download-only mode; skipping Snowflake upload.")
    finally:
        if cleanup_targets:
            if args.keep_download:
                for retained in cleanup_targets:
                    print(f"[INFO] Download retained at {retained}")
            else:
                for target in cleanup_targets:
                    shutil.rmtree(target, ignore_errors=True)

    return 0


if __name__ == "__main__":
    sys.exit(main())
