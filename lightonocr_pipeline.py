#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
LightOnOCR batch pipeline

Scans an input directory for PDF files, renders each page via pypdfium2,
requests Markdown OCR results from a vLLM OpenAI-compatible endpoint serving
LightOnOCR, and emits a Parquet file with one row per source document.

The output Parquet schema:
  - source_path (str): path relative to the input root
  - source_md5 (str or null)
  - source_size_bytes (int or null)
  - source_modified_at (str or null, ISO-8601 UTC)
  - source_changed_at (str or null, ISO-8601 UTC)
  - source_accessed_at (str or null, ISO-8601 UTC)
  - page_count (int)
  - markdown (str): concatenated Markdown for the whole document
  - page_markdown (list[str]): Markdown per page (index aligned to PDF pages)
  - model_name (str)
  - generated_at (str, ISO-8601 UTC timestamp)
  - error (str or null)

Optionally dumps individual Markdown files to a workspace directory for
inspection. The script expects the model server to run locally (see
`dockerfile.lightonocr` + `lightonocr_entrypoint.sh`).
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import io
import json
import logging
import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, List

import pyarrow as pa
import pyarrow.parquet as pq
import requests
from pypdfium2 import PdfDocument

try:
    from PIL import Image  # noqa: F401  # Imported for side effects (via PdfDocument.render_topil)
except ImportError as exc:  # pragma: no cover - enforced in container build
    raise RuntimeError("Pillow is required. Ensure the container installs pillow before running.") from exc


LOGGER = logging.getLogger("lightonocr.pipeline")


@dataclass
class OCRConfig:
    model_name: str
    api_base: str
    api_url: str
    api_key: str
    system_prompt: str
    user_prompt_template: str
    max_output_tokens: int
    temperature: float
    top_p: float
    request_timeout: float
    retry_attempts: int
    retry_interval: float
    page_scale: float
    sleep_between_pages: float


def wait_for_server(config: OCRConfig, timeout: float = 600.0, poll_interval: float = 2.0) -> None:
    """Poll the vLLM /health endpoint until it responds OK or timeout expires."""
    deadline = time.monotonic() + timeout
    health_url = f"{config.api_base.rstrip('/')}/health"
    LOGGER.info("Waiting for LightOnOCR server at %s ...", health_url)
    while True:
        try:
            resp = requests.get(health_url, timeout=5)
            if resp.status_code == 200:
                LOGGER.info("vLLM server is ready.")
                return
            LOGGER.debug("Healthcheck status %s", resp.status_code)
        except requests.RequestException as exc:
            LOGGER.debug("Healthcheck failed: %s", exc)
        if time.monotonic() >= deadline:
            raise TimeoutError(f"Timed out waiting for LightOnOCR server at {health_url}")
        time.sleep(poll_interval)


def list_pdf_files(root: Path) -> List[Path]:
    files = sorted(p for p in root.rglob("*.pdf") if p.is_file())
    LOGGER.info("Discovered %d PDF files under %s", len(files), root)
    return files


def compute_md5(path: Path, chunk_size: int = 1 << 20) -> str | None:
    """
    Compute the hexadecimal MD5 digest for the given file.
    Returns None if the file cannot be read.
    """
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


def ts_to_iso(timestamp: float | None) -> str | None:
    if timestamp is None:
        return None
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat()


def collect_file_metadata(path: Path) -> dict:
    """
    Gather filesystem metadata for the source file.
    """
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


def render_page_to_base64(page, scale: float) -> str:
    """
    Render a PdfDocument page to PNG (base64 encoded).
    """
    pil_image = None
    if hasattr(page, "render_topil"):
        pil_image = page.render_topil(scale=scale)
    if pil_image is None:
        try:
            bitmap = page.render(scale=scale, rev_byteorder=True)
        except TypeError:
            bitmap = page.render(scale=scale)
        try:
            if hasattr(bitmap, "to_pil"):
                pil_image = bitmap.to_pil()
            elif hasattr(bitmap, "to_png"):
                png_bytes = bitmap.to_png()
                return base64.b64encode(png_bytes).decode("utf-8")
            else:  # pragma: no cover - unexpected API variant
                raise TypeError(f"Unsupported PdfBitmap conversion methods: {dir(bitmap)}")
        finally:
            if hasattr(bitmap, "close"):
                bitmap.close()

    if isinstance(pil_image, tuple):
        pil_image = pil_image[0]
    if pil_image is None or not hasattr(pil_image, "save"):
        raise TypeError(f"Unexpected render output type: {type(pil_image)!r}")

    with io.BytesIO() as buf:
        pil_image.save(buf, format="PNG")
        return base64.b64encode(buf.getvalue()).decode("utf-8")


def build_payload(config: OCRConfig, image_b64: str, page_index: int, page_total: int) -> dict:
    user_prompt = config.user_prompt_template.format(
        page_number=page_index + 1,
        page_total=page_total,
    )
    return {
        "model": config.model_name,
        "messages": [
            {"role": "system", "content": config.system_prompt},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": user_prompt},
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/png;base64,{image_b64}"},
                    },
                ],
            },
        ],
        "max_output_tokens": config.max_output_tokens,
        "max_tokens": config.max_output_tokens,
        "temperature": config.temperature,
        "top_p": config.top_p,
    }


def extract_markdown_from_response(data: dict) -> str:
    """
    Normalize the OpenAI-compatible response payload into a Markdown string.
    """
    try:
        message = data["choices"][0]["message"]
    except (KeyError, IndexError) as exc:
        raise ValueError(f"Unexpected response schema: {json.dumps(data)[:400]}") from exc

    content = message.get("content")
    if isinstance(content, str):
        return content.strip()

    if isinstance(content, list):
        chunks: list[str] = []
        for block in content:
            if isinstance(block, dict) and block.get("type") in {"output_text", "text"}:
                text = block.get("text") or ""
                if text:
                    chunks.append(text)
        return "\n".join(chunks).strip()

    raise ValueError(f"Unsupported message content type: {type(content)}")


def call_lighton_api(
    session: requests.Session,
    config: OCRConfig,
    image_b64: str,
    page_index: int,
    page_total: int,
) -> str:
    payload = build_payload(config, image_b64, page_index, page_total)
    headers = {
        "Authorization": f"Bearer {config.api_key}",
        "Content-Type": "application/json",
    }

    for attempt in range(1, config.retry_attempts + 1):
        try:
            resp = session.post(
                config.api_url,
                json=payload,
                headers=headers,
                timeout=config.request_timeout,
            )
            if resp.status_code >= 500:
                raise requests.HTTPError(f"{resp.status_code} server error: {resp.text[:200]}")
            resp.raise_for_status()
            data = resp.json()
            markdown = extract_markdown_from_response(data)
            if markdown:
                return markdown
            LOGGER.warning("Empty OCR output (page %d). Retrying...", page_index + 1)
        except (requests.RequestException, ValueError) as exc:
            LOGGER.warning(
                "Attempt %d/%d failed for page %d: %s",
                attempt,
                config.retry_attempts,
                page_index + 1,
                exc,
            )
            if attempt == config.retry_attempts:
                raise
        time.sleep(config.retry_interval)

    raise RuntimeError("Unreachable: retries exhausted")


def ocr_pdf(
    session: requests.Session,
    config: OCRConfig,
    pdf_path: Path,
    scale: float,
    sleep_between_pages: float,
) -> list[str]:
    pdf = PdfDocument(str(pdf_path))
    try:
        page_total = len(pdf)
        results: list[str] = []
        for page_index in range(page_total):
            page = pdf[page_index]
            image_b64 = render_page_to_base64(page, scale)
            markdown = call_lighton_api(session, config, image_b64, page_index, page_total)
            results.append(markdown)
            LOGGER.info(
                "OCR success: %s page %d/%d",
                pdf_path.name,
                page_index + 1,
                page_total,
            )
            if sleep_between_pages > 0:
                time.sleep(sleep_between_pages)
        return results
    finally:
        pdf.close()


def ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def write_markdown_file(target: Path, content: str) -> None:
    ensure_dir(target.parent)
    target.write_text(content, encoding="utf-8")


def write_parquet(output_path: Path, rows: Iterable[dict]) -> None:
    data = list(rows)
    if not data:
        LOGGER.info("No OCR output rows to persist. Skipping Parquet write.")
        return
    table = pa.Table.from_pylist(data)
    pq.write_table(table, output_path, compression="zstd")
    LOGGER.info("Wrote Parquet: %s (%d rows)", output_path, table.num_rows)


def configure_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(message)s",
        stream=sys.stdout,
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Batch OCR PDFs with LightOnOCR and export Parquet.")
    parser.add_argument("--input-dir", default="/in", help="Input root directory containing PDFs.")
    parser.add_argument("--output-dir", default="/out", help="Directory to place Parquet output.")
    parser.add_argument("--workspace-dir", default="/workspace", help="Workspace for optional Markdown dumps.")
    parser.add_argument("--model-name", default="LightOnOCR-1B-1025", help="Model identifier for metadata.")
    parser.add_argument("--api-base", default="http://127.0.0.1:8000", help="Base URL of the vLLM server.")
    parser.add_argument("--api-key", default="token", help="API key to send in Authorization header.")
    parser.add_argument("--max-output-tokens", type=int, default=3500)
    parser.add_argument("--temperature", type=float, default=0.0)
    parser.add_argument("--top-p", type=float, default=0.95)
    parser.add_argument("--request-timeout", type=float, default=300.0)
    parser.add_argument("--retry-attempts", type=int, default=3)
    parser.add_argument("--retry-interval", type=float, default=10.0)
    parser.add_argument("--page-scale", type=float, default=2.0, help="Rendering scale factor for PDF pages.")
    parser.add_argument("--sleep-between-pages", type=float, default=0.0, help="Throttle requests per page (seconds).")
    parser.add_argument("--parquet-name", default="lightonocr_output.parquet", help="Filename for the Parquet dataset.")
    parser.add_argument("--emit-markdown", action="store_true", help="Write combined Markdown files under workspace.")
    parser.add_argument("--system-prompt", default=(
        "You are an expert OCR engine. Convert each page image into clean GitHub-Flavored Markdown, preserving tables, "
        "lists, headings, and inline formatting. Do not add commentary outside the document content."
    ))
    parser.add_argument("--user-prompt-template", default=(
        "Transcribe this page ({page_number}/{page_total}) into GitHub-Flavored Markdown. "
        "Keep the original reading order and explain nothing."
    ))
    parser.add_argument("--verbose", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    configure_logging(args.verbose)

    input_root = Path(args.input_dir).resolve()
    output_root = Path(args.output_dir).resolve()
    workspace_root = Path(args.workspace_dir).resolve()
    markdown_root = workspace_root / "markdown"

    if not input_root.exists():
        LOGGER.error("Input directory does not exist: %s", input_root)
        return 2

    ensure_dir(output_root)
    ensure_dir(workspace_root)

    pdf_files = list_pdf_files(input_root)
    if not pdf_files:
        LOGGER.info("No PDFs detected. Nothing to process.")
        return 0

    config = OCRConfig(
        model_name=args.model_name,
        api_base=args.api_base,
        api_url=f"{args.api_base.rstrip('/')}/v1/chat/completions",
        api_key=args.api_key,
        system_prompt=args.system_prompt,
        user_prompt_template=args.user_prompt_template,
        max_output_tokens=args.max_output_tokens,
        temperature=args.temperature,
        top_p=args.top_p,
        request_timeout=args.request_timeout,
        retry_attempts=args.retry_attempts,
        retry_interval=args.retry_interval,
        page_scale=args.page_scale,
        sleep_between_pages=args.sleep_between_pages,
    )

    wait_for_server(config)

    records = []
    session = requests.Session()
    generated_at = datetime.now(timezone.utc).isoformat()

    for pdf_path in pdf_files:
        rel_path = pdf_path.relative_to(input_root)
        LOGGER.info("Processing %s", rel_path)
        file_meta = collect_file_metadata(pdf_path)
        try:
            page_markdowns = ocr_pdf(
                session,
                config,
                pdf_path,
                scale=args.page_scale,
                sleep_between_pages=args.sleep_between_pages,
            )
        except Exception as exc:
            LOGGER.error("Failed to OCR %s: %s", rel_path, exc)
            failure_record = {
                "source_path": str(rel_path),
                "page_count": 0,
                "markdown": "",
                "page_markdown": [],
                "model_name": config.model_name,
                "generated_at": generated_at,
                "error": str(exc),
            }
            failure_record.update(file_meta)
            records.append(failure_record)
            continue

        combined_markdown = "\n\n".join(
            f"<!-- Page {idx + 1} -->\n{page_md.strip()}"
            for idx, page_md in enumerate(page_markdowns)
            if page_md
        ).strip()

        if args.emit_markdown and combined_markdown:
            markdown_path = markdown_root / rel_path.with_suffix(".md")
            write_markdown_file(markdown_path, combined_markdown)

        success_record = {
            "source_path": str(rel_path),
            "page_count": len(page_markdowns),
            "markdown": combined_markdown,
            "page_markdown": page_markdowns,
            "model_name": config.model_name,
            "generated_at": generated_at,
            "error": None,
        }
        success_record.update(file_meta)
        records.append(success_record)

    parquet_path = output_root / args.parquet_name
    write_parquet(parquet_path, records)

    return 0


if __name__ == "__main__":
    sys.exit(main())
