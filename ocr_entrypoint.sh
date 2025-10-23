#!/bin/bash
set -euo pipefail

INPUT_ROOT="${OCR_INPUT_DIR:-/in}"
WORKSPACE_ROOT="${OCR_WORKSPACE_DIR:-/workspace}"
MODEL_ROOT="${OCR_MODEL_DIR:-/models}"
MODEL_SUBDIR="${OCR_MODEL_SUBDIR:-olmOCR-2-7B-1025-FP8}"
WORKERS="${OCR_WORKERS:-4}"
EXTRA_ARGS="${OCR_EXTRA_ARGS:-}"

LIST_FILE="${WORKSPACE_ROOT}/input_files.txt"
MODEL_PATH="${MODEL_ROOT%/}/${MODEL_SUBDIR}"

mkdir -p "${WORKSPACE_ROOT}"

mapfile -t sources < <(find "${INPUT_ROOT}" -type f \( -iname "*.pdf" -o -iname "*.png" -o -iname "*.jpg" -o -iname "*.jpeg" \) | sort) || true

if [ "${#sources[@]}" -eq 0 ]; then
  echo "No PDF or image files found under ${INPUT_ROOT}. Nothing to do."
  exit 0
fi

printf '%s\n' "${sources[@]}" > "${LIST_FILE}"

cmd=(
  python3 -m olmocr.pipeline "${WORKSPACE_ROOT}"
  --markdown
  --pdfs "${LIST_FILE}"
  --model "${MODEL_PATH}"
  --workers "${WORKERS}"
)

if [ -n "${EXTRA_ARGS}" ]; then
  # shellcheck disable=SC2206
  extra_tokens=(${EXTRA_ARGS})
  cmd+=("${extra_tokens[@]}")
fi

set -x
exec "${cmd[@]}"
