#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
PaddleOCR Layout batch pipeline

Walk the given input directory for PDFs or images, run PaddleOCR PP-Structure
layout detection, optionally dump per-document JSON files, and persist a Parquet
summary (one row per source).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, List, Sequence, Tuple

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

try:
    from PIL import Image
except ImportError as exc:  # pragma: no cover - enforced during container build
    raise RuntimeError("Pillow が必要です。コンテナに pillow をインストールしてください。") from exc

try:
    from pypdfium2 import PdfDocument
except ImportError as exc:  # pragma: no cover
    raise RuntimeError("pypdfium2 が必要です。dockerfile.paddleocrlayout に追加してください。") from exc

from paddleocr_layout_common import instantiate_layout_engine, normalize_layout_blocks


LOGGER = logging.getLogger("paddleocr.layout.pipeline")
SUPPORTED_SUFFIXES = {".pdf", ".png", ".jpg", ".jpeg", ".bmp", ".tif", ".tiff"}


@dataclass
class LayoutConfig:
    pipeline_name: str
    model_name: str
    structure_version: str | None
    layout_model_name: str | None
    layout_algorithm: str | None
    enable_table: bool
    enable_ocr: bool
    enable_kie: bool
    lang: str | None
    page_scale: float
    emit_layout_json: bool
    json_subdir: str


def configure_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(level=level, format="%(asctime)s %(levelname)s %(message)s", stream=sys.stdout)


def ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def list_input_files(root: Path) -> List[Path]:
    files = sorted(
        p
        for p in root.rglob("*")
        if p.is_file() and p.suffix.lower() in SUPPORTED_SUFFIXES
    )
    LOGGER.info("Discovered %d candidate files under %s", len(files), root)
    return files


def ts_to_iso(ts: float | None) -> str | None:
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def compute_md5(path: Path, chunk_size: int = 1 << 20) -> str | None:
    digest = hashlib.md5()
    try:
        with path.open("rb") as handle:
            while True:
                chunk = handle.read(chunk_size)
                if not chunk:
                    break
                digest.update(chunk)
    except OSError as exc:
        LOGGER.warning("Unable to hash %s: %s", path, exc)
        return None
    return digest.hexdigest()


def collect_file_metadata(path: Path) -> dict:
    metadata: dict[str, object | None] = {
        "source_md5": None,
        "source_size_bytes": None,
        "source_modified_at": None,
        "source_changed_at": None,
        "source_accessed_at": None,
    }

    try:
        stat = path.stat()
    except OSError as exc:
        LOGGER.warning("Unable to stat %s: %s", path, exc)
        return metadata

    metadata["source_size_bytes"] = stat.st_size
    metadata["source_modified_at"] = ts_to_iso(getattr(stat, "st_mtime", None))
    metadata["source_changed_at"] = ts_to_iso(getattr(stat, "st_ctime", None))
    metadata["source_accessed_at"] = ts_to_iso(getattr(stat, "st_atime", None))
    metadata["source_md5"] = compute_md5(path)
    return metadata


def pil_to_bgr(image: Image.Image) -> np.ndarray:
    rgb = image.convert("RGB")
    arr = np.asarray(rgb, dtype=np.uint8)
    return arr[:, :, ::-1].copy()


def iter_pdf_images(path: Path, scale: float) -> Iterable[Tuple[int, np.ndarray, Tuple[int, int]]]:
    with PdfDocument(path) as pdf:
        for page_index, page in enumerate(pdf):
            pil_image = page.render_topil(scale=scale)
            if isinstance(pil_image, tuple):
                pil_image = pil_image[0]
            arr = pil_to_bgr(pil_image)
            yield page_index, arr, pil_image.size
            if hasattr(page, "close"):
                page.close()


def iter_image_file(path: Path) -> Iterable[Tuple[int, np.ndarray, Tuple[int, int]]]:
    with Image.open(path) as img:
        arr = pil_to_bgr(img)
        yield 0, arr, img.size


def iter_document_images(path: Path, scale: float) -> Iterable[Tuple[int, np.ndarray, Tuple[int, int]]]:
    if path.suffix.lower() == ".pdf":
        yield from iter_pdf_images(path, scale)
    else:
        yield from iter_image_file(path)


def write_json(path: Path, payload: dict) -> None:
    ensure_dir(path.parent)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def write_parquet(target: Path, rows: Sequence[dict]) -> None:
    data = list(rows)
    if not data:
        LOGGER.info("No rows to persist. Skipping Parquet write.")
        return
    table = pa.Table.from_pylist(data)
    pq.write_table(table, target, compression="zstd")
    LOGGER.info("Wrote Parquet dataset: %s (%d rows)", target, table.num_rows)


def analyze_document(path: Path, engine, config: LayoutConfig) -> Tuple[list[dict], int]:
    page_layouts: list[dict] = []
    block_total = 0
    for page_index, image_bgr, size in iter_document_images(path, config.page_scale):
        LOGGER.debug("Layout inference on %s page %d", path.name, page_index + 1)
        outputs = engine(image_bgr)
        normalized = normalize_layout_blocks(outputs)
        block_total += len(normalized)
        page_layouts.append(
            {
                "page_index": page_index,
                "width": size[0],
                "height": size[1],
                "blocks": normalized,
            }
        )
    return page_layouts, block_total


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Batch layout detection via PaddleOCR PP-Structure.")
    parser.add_argument("--input-dir", default="/in", help="Directory containing PDFs or images.")
    parser.add_argument("--output-dir", default="/out", help="Directory to store Parquet output.")
    parser.add_argument("--workspace-dir", default="/workspace", help="Workspace for layout JSON dumps.")
    parser.add_argument("--parquet-name", default="paddleocrlayout_output.parquet")
    parser.add_argument("--model-name", default="PP-DocLayout_plus-L", help="Metadata tag for Parquet.")
    parser.add_argument("--pipeline-name", default="PaddleOCR-Layout", help="Pipeline identifier for logs.")
    parser.add_argument("--structure-version", default="PP-StructureV3", help="structure_version init arg (if supported).")
    parser.add_argument("--layout-model-name", default="PP-DocLayout_plus-L", help="layout_model_name init arg (if supported).")
    parser.add_argument("--layout-algorithm", help="layout_algorithm init arg override.")
    parser.add_argument("--page-scale", type=float, default=1.5, help="PDF render scale passed to pypdfium2.")
    parser.add_argument("--emit-layout-json", action="store_true", help="Persist per-document JSON payloads.")
    parser.add_argument("--json-subdir", default="layout_json", help="Workspace subdir for layout JSON outputs.")
    parser.add_argument("--enable-table", action="store_true")
    parser.add_argument("--enable-ocr", action="store_true")
    parser.add_argument("--enable-kie", action="store_true")
    parser.add_argument("--lang", help="Pass lang= to PPStructure (e.g., en).")
    parser.add_argument("--verbose", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    configure_logging(args.verbose)

    input_root = Path(args.input_dir).resolve()
    output_root = Path(args.output_dir).resolve()
    workspace_root = Path(args.workspace_dir).resolve()
    json_root = workspace_root / args.json_subdir

    ensure_dir(output_root)
    ensure_dir(workspace_root)

    if not input_root.exists():
        LOGGER.error("Input directory does not exist: %s", input_root)
        return 2

    files = list_input_files(input_root)
    if not files:
        LOGGER.info("No supported files detected under %s", input_root)
        return 0

    config = LayoutConfig(
        pipeline_name=args.pipeline_name,
        model_name=args.model_name,
        structure_version=args.structure_version,
        layout_model_name=args.layout_model_name,
        layout_algorithm=args.layout_algorithm,
        enable_table=args.enable_table,
        enable_ocr=args.enable_ocr,
        enable_kie=args.enable_kie,
        lang=args.lang,
        page_scale=args.page_scale,
        emit_layout_json=args.emit_layout_json,
        json_subdir=args.json_subdir,
    )

    engine, engine_name, init_kwargs = instantiate_layout_engine(
        structure_version=config.structure_version,
        layout_model_name=config.layout_model_name,
        layout_algorithm=config.layout_algorithm,
        table=config.enable_table,
        ocr=config.enable_ocr,
        kie=config.enable_kie,
        lang=config.lang,
        show_log=args.verbose,
    )
    LOGGER.info("Initializing %s with kwargs %s", engine_name, init_kwargs)

    generated_at = datetime.now(timezone.utc).isoformat()
    rows: list[dict] = []

    for source_path in files:
        rel_path = source_path.relative_to(input_root)
        LOGGER.info("Processing %s", rel_path)
        metadata = collect_file_metadata(source_path)
        try:
            page_layouts, block_total = analyze_document(source_path, engine, config)
            if config.emit_layout_json and page_layouts:
                target_path = json_root / rel_path.with_suffix(".layout.json")
                payload = {
                    "source_path": str(rel_path),
                    "model_name": config.model_name,
                    "structure_version": config.structure_version,
                    "layout_model_name": config.layout_model_name,
                    "pages": page_layouts,
                }
                write_json(target_path, payload)

            record = {
                "source_path": str(rel_path),
                "page_count": len(page_layouts),
                "layout": page_layouts,
                "block_count": block_total,
                "model_name": config.model_name,
                "structure_version": config.structure_version,
                "layout_model_name": config.layout_model_name,
                "generated_at": generated_at,
                "pipeline_name": config.pipeline_name,
                "error": None,
            }
            record.update(metadata)
            rows.append(record)
        except Exception as exc:  # pragma: no cover - runtime safety
            LOGGER.error("Failed to detect layout for %s: %s", rel_path, exc)
            failure = {
                "source_path": str(rel_path),
                "page_count": 0,
                "layout": [],
                "block_count": 0,
                "model_name": config.model_name,
                "structure_version": config.structure_version,
                "layout_model_name": config.layout_model_name,
                "generated_at": generated_at,
                "pipeline_name": config.pipeline_name,
                "error": str(exc),
            }
            failure.update(metadata)
            rows.append(failure)

    parquet_path = output_root / args.parquet_name
    write_parquet(parquet_path, rows)
    return 0


if __name__ == "__main__":
    sys.exit(main())

