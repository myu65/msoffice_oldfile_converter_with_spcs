#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
PaddleOCR-VL batch pipeline

Walks the given input directory for PDFs or images, runs PaddleOCR-VL to
extract Markdown, emits optional Markdown files per document, and writes a
Parquet summary (one row per source).
"""

from __future__ import annotations

import argparse
import hashlib
import logging
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, List

import pyarrow as pa
import pyarrow.parquet as pq

try:
    from paddleocr import PaddleOCRVL
except (ImportError, AttributeError) as exc:
    raise RuntimeError(
        "PaddleOCRVL is unavailable. Install paddleocr[doc]==3.3.2 and paddlex[ocr]==3.3.9."
    ) from exc

try:
    from PIL import Image  # noqa: F401  # imported to ensure pillow is available
except ImportError as exc:  # pragma: no cover
    raise RuntimeError("Pillow is required for PaddleOCR-VL pipeline output.") from exc


LOGGER = logging.getLogger("paddleocrvl.pipeline")
SUPPORTED_SUFFIXES = {".pdf", ".png", ".jpg", ".jpeg", ".bmp", ".tif", ".tiff"}


@dataclass
class PaddleConfig:
    """Runtime configuration for PaddleOCR-VL."""

    use_doc_orientation_classify: bool
    use_doc_unwarping: bool
    use_layout_detection: bool
    layout_detection_model_dir: str | None
    vl_rec_server_url: str | None
    pipeline_name: str
    model_name: str
    pretty_markdown: bool
    show_formula_number: bool
    min_pixels: int | None
    max_pixels: int | None
    temperature: float | None
    top_p: float | None
    repetition_penalty: float | None


def configure_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(message)s",
        stream=sys.stdout,
    )


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


def extract_markdown_texts(results: List[object]) -> list[str]:
    page_texts: list[str] = []
    for res in results:
        markdown = getattr(res, "markdown", None)
        if isinstance(markdown, dict):
            texts = markdown.get("markdown_texts") or []
            for text in texts:
                normalized = (text or "").strip()
                if normalized:
                    page_texts.append(normalized)
    return page_texts


def save_markdown_images(results: List[object], base_dir: Path) -> None:
    """
    Persist Markdown-referenced images to disk.
    Keys from PaddleOCR-VL already include relative paths.
    """
    written: set[Path] = set()
    base_resolved = base_dir.resolve()
    for res in results:
        markdown = getattr(res, "markdown", None)
        if not isinstance(markdown, dict):
            continue
        images = markdown.get("markdown_images") or {}
        for rel_path, image in images.items():
            if not rel_path or image is None:
                continue
            rel_norm = str(rel_path).strip().replace("\\", "/")
            rel_path_obj = Path(rel_norm)
            if rel_path_obj.is_absolute():
                rel_path_obj = Path(rel_path_obj.name)
            target = (base_dir / rel_path_obj).resolve()
            if not str(target).startswith(str(base_resolved)):
                LOGGER.warning("Skipping unsafe image path %s", rel_path)
                continue
            ensure_dir(target.parent)
            pil_image = image[0] if isinstance(image, tuple) else image
            if hasattr(pil_image, "save"):
                if target not in written:
                    pil_image.save(target)
                    written.add(target)


def combine_markdown(page_markdowns: list[str]) -> str:
    if not page_markdowns:
        return ""
    combined: list[str] = []
    for idx, text in enumerate(page_markdowns, start=1):
        combined.append(f"<!-- Page {idx} -->\n{text}")
    return "\n\n".join(combined).strip()


def write_markdown_file(path: Path, content: str) -> None:
    ensure_dir(path.parent)
    path.write_text(content, encoding="utf-8")


def write_parquet(target: Path, rows: Iterable[dict]) -> None:
    data = list(rows)
    if not data:
        LOGGER.info("No rows to persist. Skipping Parquet write.")
        return
    table = pa.Table.from_pylist(data)
    pq.write_table(table, target, compression="zstd")
    LOGGER.info("Wrote Parquet dataset: %s (%d rows)", target, table.num_rows)


def init_pipeline(config: PaddleConfig) -> PaddleOCRVL:
    init_kwargs = {
        "use_doc_orientation_classify": config.use_doc_orientation_classify,
        "use_doc_unwarping": config.use_doc_unwarping,
        "use_layout_detection": config.use_layout_detection,
    }
    if config.layout_detection_model_dir:
        init_kwargs["layout_detection_model_dir"] = config.layout_detection_model_dir
    if config.vl_rec_server_url:
        init_kwargs["vl_rec_server_url"] = config.vl_rec_server_url
    LOGGER.info("Initializing PaddleOCR-VL pipeline %s", config.pipeline_name)
    return PaddleOCRVL(**init_kwargs)


def predict_document(
    pipeline: PaddleOCRVL,
    path: Path,
    config: PaddleConfig,
) -> List[object]:
    kwargs = {
        "pretty": config.pretty_markdown,
        "show_formula_number": config.show_formula_number,
    }
    if config.min_pixels is not None:
        kwargs["min_pixels"] = config.min_pixels
    if config.max_pixels is not None:
        kwargs["max_pixels"] = config.max_pixels
    if config.temperature is not None:
        kwargs["temperature"] = config.temperature
    if config.top_p is not None:
        kwargs["top_p"] = config.top_p
    if config.repetition_penalty is not None:
        kwargs["repetition_penalty"] = config.repetition_penalty
    LOGGER.debug("Running PaddleOCR-VL on %s", path)
    outputs = pipeline.predict(str(path), **kwargs)
    return list(outputs)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Batch Markdown export via PaddleOCR-VL.")
    parser.add_argument("--input-dir", default="/in", help="Directory containing PDFs or images.")
    parser.add_argument("--output-dir", default="/out", help="Directory to store Parquet output.")
    parser.add_argument("--workspace-dir", default="/workspace", help="Workspace for Markdown dumps.")
    parser.add_argument("--parquet-name", default="paddleocrvl_output.parquet")
    parser.add_argument("--model-name", default="PaddleOCR-VL-0.9B", help="Metadata tag for Parquet.")
    parser.add_argument("--pipeline-name", default="PaddleOCR-VL", help="Pipeline identifier for logs.")
    parser.add_argument("--emit-markdown", action="store_true", help="Persist combined Markdown files.")
    parser.add_argument("--markdown-subdir", default="markdown", help="Workspace subdir for Markdown.")
    parser.add_argument("--use-doc-orientation-classify", action="store_true")
    parser.add_argument("--use-doc-unwarping", action="store_true")
    parser.add_argument("--disable-layout-detection", action="store_true", help="Skip layout detection.")
    parser.add_argument("--layout-detection-model-dir")
    parser.add_argument("--vl-rec-server-url", help="Optional remote VLM endpoint.")
    parser.add_argument("--pretty-markdown", action="store_true", default=True)
    parser.add_argument("--no-pretty-markdown", action="store_false", dest="pretty_markdown")
    parser.add_argument("--show-formula-number", action="store_true")
    parser.add_argument("--min-pixels", type=int)
    parser.add_argument("--max-pixels", type=int)
    parser.add_argument("--temperature", type=float)
    parser.add_argument("--top-p", type=float)
    parser.add_argument("--repetition-penalty", type=float)
    parser.add_argument("--verbose", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    configure_logging(args.verbose)

    input_root = Path(args.input_dir).resolve()
    output_root = Path(args.output_dir).resolve()
    workspace_root = Path(args.workspace_dir).resolve()
    markdown_root = workspace_root / args.markdown_subdir

    ensure_dir(output_root)
    ensure_dir(workspace_root)

    if not input_root.exists():
        LOGGER.error("Input directory does not exist: %s", input_root)
        return 2

    files = list_input_files(input_root)
    if not files:
        LOGGER.info("No supported files detected under %s", input_root)
        return 0

    config = PaddleConfig(
        use_doc_orientation_classify=args.use_doc_orientation_classify,
        use_doc_unwarping=args.use_doc_unwarping,
        use_layout_detection=not args.disable_layout_detection,
        layout_detection_model_dir=args.layout_detection_model_dir,
        vl_rec_server_url=args.vl_rec_server_url,
        pipeline_name=args.pipeline_name,
        model_name=args.model_name,
        pretty_markdown=args.pretty_markdown,
        show_formula_number=args.show_formula_number,
        min_pixels=args.min_pixels,
        max_pixels=args.max_pixels,
        temperature=args.temperature,
        top_p=args.top_p,
        repetition_penalty=args.repetition_penalty,
    )

    pipeline = init_pipeline(config)
    generated_at = datetime.now(timezone.utc).isoformat()
    rows: list[dict] = []

    for source_path in files:
        rel_path = source_path.relative_to(input_root)
        LOGGER.info("Processing %s", rel_path)
        metadata = collect_file_metadata(source_path)
        try:
            results = predict_document(pipeline, source_path, config)
            page_markdowns = extract_markdown_texts(results)
            combined_markdown = combine_markdown(page_markdowns)

            if args.emit_markdown and combined_markdown:
                target_path = markdown_root / rel_path.with_suffix(".md")
                save_markdown_images(results, target_path.parent)
                write_markdown_file(target_path, combined_markdown)

            record = {
                "source_path": str(rel_path),
                "page_count": len(page_markdowns),
                "markdown": combined_markdown,
                "page_markdown": page_markdowns,
                "model_name": config.model_name,
                "generated_at": generated_at,
                "error": None,
            }
            record.update(metadata)
            rows.append(record)
        except Exception as exc:  # pragma: no cover
            LOGGER.error("Failed to OCR %s: %s", rel_path, exc)
            failure = {
                "source_path": str(rel_path),
                "page_count": 0,
                "markdown": "",
                "page_markdown": [],
                "model_name": config.model_name,
                "generated_at": generated_at,
                "error": str(exc),
            }
            failure.update(metadata)
            rows.append(failure)

    parquet_path = output_root / args.parquet_name
    write_parquet(parquet_path, rows)
    return 0


if __name__ == "__main__":
    sys.exit(main())
