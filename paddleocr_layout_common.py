#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Shared helpers for PaddleOCR layout pipelines."""

from __future__ import annotations

import inspect
from typing import Any, Dict, Iterable, List, Tuple

try:
    import paddleocr as _paddleocr  # type: ignore
except (ImportError, AttributeError) as exc:  # pragma: no cover - guidance for env setup
    raise RuntimeError(
        "PaddleOCR が見つかりません。`pip install \"paddleocr[doc]==3.3.2\" paddlex` などでインストールしてください。"
    ) from exc

STRUCTURE_CLASS_CANDIDATES: Tuple[str, ...] = ("PPStructureV3", "PPStructureV2", "PPStructure")


def resolve_structure_class():
    """Return the most capable PPStructure* class exposed by paddleocr."""

    for name in STRUCTURE_CLASS_CANDIDATES:
        cls = getattr(_paddleocr, name, None)
        if cls is not None:
            return cls, name
    raise RuntimeError("paddleocr に PPStructure 系クラスが見つかりません。3.x 系をインストールしてください。")


def _filter_supported_kwargs(init_fn, kwargs: Dict[str, Any]) -> Dict[str, Any]:
    try:
        sig = inspect.signature(init_fn)
    except (TypeError, ValueError):  # pragma: no cover - native implementations
        return dict(kwargs)

    for param in sig.parameters.values():
        if param.kind == inspect.Parameter.VAR_KEYWORD:
            return dict(kwargs)

    allowed = {name for name in sig.parameters if name != "self"}
    return {key: value for key, value in kwargs.items() if key in allowed}


def instantiate_layout_engine(**overrides: Any):
    """Create a PPStructure* instance with safe defaults and filtered kwargs."""

    cls, cls_name = resolve_structure_class()
    base_kwargs: Dict[str, Any] = {
        "layout": True,
        "table": False,
        "ocr": False,
        "kie": False,
        "show_log": False,
    }

    for key, value in overrides.items():
        if value is not None:
            base_kwargs[key] = value

    filtered = _filter_supported_kwargs(cls.__init__, base_kwargs)
    instance = cls(**filtered)
    return instance, cls_name, filtered


def _to_polygon(coords: Any) -> List[List[float]] | None:
    if coords is None:
        return None

    points: List[List[float]] = []

    if isinstance(coords, dict):
        if "points" in coords:
            coords = coords["points"]
        elif "bbox" in coords:
            coords = coords["bbox"]

    if isinstance(coords, (list, tuple)):
        if coords and isinstance(coords[0], (list, tuple)):
            for pt in coords:
                if len(pt) >= 2:
                    points.append([float(pt[0]), float(pt[1])])
        else:
            flat = list(coords)
            if len(flat) % 2 == 0:
                for idx in range(0, len(flat), 2):
                    points.append([float(flat[idx]), float(flat[idx + 1])])
    return points or None


def _polygon_to_bbox(polygon: List[List[float]] | None) -> List[float] | None:
    if not polygon:
        return None
    xs = [pt[0] for pt in polygon]
    ys = [pt[1] for pt in polygon]
    return [min(xs), min(ys), max(xs), max(ys)]


def normalize_layout_blocks(raw_blocks: Iterable[dict] | None) -> list[dict]:
    normalized: list[dict] = []
    if not raw_blocks:
        return normalized

    for idx, block in enumerate(raw_blocks):
        if not isinstance(block, dict):
            continue
        polygon = _to_polygon(block.get("bbox") or block.get("poly") or block.get("points"))
        bbox = _polygon_to_bbox(polygon)
        label = block.get("type") or block.get("layout") or block.get("label")
        score = block.get("score") or block.get("confidence")
        res = block.get("res") if isinstance(block.get("res"), dict) else {}
        text = block.get("text") or res.get("text")
        html = res.get("html") or res.get("html_text")
        normalized.append(
            {
                "index": idx,
                "label": label,
                "bbox": bbox,
                "polygon": polygon,
                "score": float(score) if score is not None else None,
                "text": text,
                "html": html,
            }
        )
    return normalized

