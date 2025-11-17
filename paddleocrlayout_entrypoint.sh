#!/bin/bash
set -euo pipefail

INPUT_ROOT="${PADDLEOCRLAYOUT_INPUT_DIR:-/in}"
OUTPUT_ROOT="${PADDLEOCRLAYOUT_OUTPUT_DIR:-/out}"
WORKSPACE_ROOT="${PADDLEOCRLAYOUT_WORKSPACE_DIR:-/workspace}"
PARQUET_NAME="${PADDLEOCRLAYOUT_PARQUET_NAME:-paddleocrlayout_output.parquet}"
PIPELINE_NAME="${PADDLEOCRLAYOUT_PIPELINE_NAME:-PaddleOCR-Layout}"
MODEL_NAME="${PADDLEOCRLAYOUT_MODEL_NAME:-PP-DocLayout_plus-L}"
JSON_SUBDIR="${PADDLEOCRLAYOUT_JSON_SUBDIR:-layout_json}"

STRUCTURE_VERSION="${PADDLEOCRLAYOUT_STRUCTURE_VERSION:-PP-StructureV3}"
LAYOUT_MODEL_NAME="${PADDLEOCRLAYOUT_LAYOUT_MODEL_NAME:-PP-DocLayout_plus-L}"
LAYOUT_ALGORITHM="${PADDLEOCRLAYOUT_LAYOUT_ALGORITHM:-}"
PAGE_SCALE="${PADDLEOCRLAYOUT_PAGE_SCALE:-1.5}"

ENABLE_TABLE="${PADDLEOCRLAYOUT_ENABLE_TABLE:-0}"
ENABLE_OCR="${PADDLEOCRLAYOUT_ENABLE_OCR:-0}"
ENABLE_KIE="${PADDLEOCRLAYOUT_ENABLE_KIE:-0}"
LANG_HINT="${PADDLEOCRLAYOUT_LANG:-}"

EMIT_JSON="${PADDLEOCRLAYOUT_EMIT_JSON:-1}"
VERBOSE="${PADDLEOCRLAYOUT_VERBOSE:-0}"

DEFAULT_PADDLEX_HOME="${PADDLEOCRLAYOUT_PADDLEX_HOME:-/models/.paddlex}"
MODEL_SUBDIR="${PADDLEOCRLAYOUT_MODEL_SUBDIR:-PP-DocLayout_plus-L}"

export PADDLEX_HOME="${PADDLEX_HOME:-${DEFAULT_PADDLEX_HOME}}"
export PADDLE_PDX_CACHE_HOME="${PADDLE_PDX_CACHE_HOME:-${PADDLEX_HOME}}"
mkdir -p "${PADDLEX_HOME}" "${OUTPUT_ROOT}" "${WORKSPACE_ROOT}"

export FLAGS_allocator_strategy="${FLAGS_allocator_strategy:-auto_growth}"
export FLAGS_fraction_of_gpu_memory_to_use="${FLAGS_fraction_of_gpu_memory_to_use:-0.92}"

check_layout_models() {
  local required_dir="${PADDLEX_HOME}/official_models/${MODEL_SUBDIR}"
  if [ ! -d "${PADDLEX_HOME}" ]; then
    echo "[ERROR] PaddleX cache directory not found: ${PADDLEX_HOME}" >&2
    exit 42
  fi
  if [ ! -d "${PADDLEX_HOME}/official_models" ]; then
    echo "[ERROR] official_models directory missing under ${PADDLEX_HOME}" >&2
    exit 42
  fi
  if [ ! -d "${required_dir}" ]; then
    cat >&2 <<EOF
[ERROR] Layout weights not found at ${required_dir}.
       Run ci_paddleocrlayout_models.py to download ${MODEL_NAME} models into the stage
       and mount it to /models when running SPCS jobs.
EOF
    exit 42
  fi
  if ! find "${required_dir}" -type f \( -name '*.pdiparams*' -o -name '*.safetensors' \) -print -quit | grep -q .; then
    echo "[ERROR] ${required_dir} exists but has no parameter files. Re-upload layout models." >&2
    exit 42
  fi
}

check_layout_models

cmd=(
  /usr/bin/python3 /usr/local/bin/paddleocrlayout_pipeline.py
  --input-dir "${INPUT_ROOT}"
  --output-dir "${OUTPUT_ROOT}"
  --workspace-dir "${WORKSPACE_ROOT}"
  --parquet-name "${PARQUET_NAME}"
  --pipeline-name "${PIPELINE_NAME}"
  --model-name "${MODEL_NAME}"
  --json-subdir "${JSON_SUBDIR}"
  --structure-version "${STRUCTURE_VERSION}"
  --layout-model-name "${LAYOUT_MODEL_NAME}"
  --page-scale "${PAGE_SCALE}"
)

if [ -n "${LAYOUT_ALGORITHM}" ]; then
  cmd+=("--layout-algorithm" "${LAYOUT_ALGORITHM}")
fi
if [ "${ENABLE_TABLE}" = "1" ]; then
  cmd+=("--enable-table")
fi
if [ "${ENABLE_OCR}" = "1" ]; then
  cmd+=("--enable-ocr")
fi
if [ "${ENABLE_KIE}" = "1" ]; then
  cmd+=("--enable-kie")
fi
if [ -n "${LANG_HINT}" ]; then
  cmd+=("--lang" "${LANG_HINT}")
fi
if [ "${EMIT_JSON}" = "1" ]; then
  cmd+=("--emit-layout-json")
fi
if [ "${VERBOSE}" = "1" ]; then
  cmd+=("--verbose")
fi

echo "+ ${cmd[*]}"
exec "${cmd[@]}"

