#!/bin/bash
set -euo pipefail

INPUT_ROOT="${PADDLEOCRVL_INPUT_DIR:-/in}"
OUTPUT_ROOT="${PADDLEOCRVL_OUTPUT_DIR:-/out}"
WORKSPACE_ROOT="${PADDLEOCRVL_WORKSPACE_DIR:-/workspace}"
PARQUET_NAME="${PADDLEOCRVL_PARQUET_NAME:-paddleocrvl_output.parquet}"
PIPELINE_NAME="${PADDLEOCRVL_PIPELINE_NAME:-PaddleOCR-VL}"
MODEL_NAME="${PADDLEOCRVL_MODEL_NAME:-PaddleOCR-VL-0.9B}"
MARKDOWN_SUBDIR="${PADDLEOCRVL_MARKDOWN_SUBDIR:-markdown}"

EMIT_MD="${PADDLEOCRVL_EMIT_MARKDOWN:-1}"
PRETTY_FLAG="${PADDLEOCRVL_PRETTY_MARKDOWN:-1}"
SHOW_FORMULA_FLAG="${PADDLEOCRVL_SHOW_FORMULA_NUMBER:-0}"
MIN_PIXELS="${PADDLEOCRVL_MIN_PIXELS:-}"
MAX_PIXELS="${PADDLEOCRVL_MAX_PIXELS:-}"
TEMPERATURE="${PADDLEOCRVL_TEMPERATURE:-}"
TOP_P="${PADDLEOCRVL_TOP_P:-}"
REPETITION="${PADDLEOCRVL_REPETITION_PENALTY:-}"

DOC_ORIENTATION="${PADDLEOCRVL_USE_DOC_ORIENTATION:-0}"
DOC_UNWARPING="${PADDLEOCRVL_USE_DOC_UNWARPING:-0}"
DISABLE_LAYOUT="${PADDLEOCRVL_DISABLE_LAYOUT_DETECTION:-0}"
LAYOUT_MODEL_DIR="${PADDLEOCRVL_LAYOUT_MODEL_DIR:-}"
VL_REC_SERVER_URL="${PADDLEOCRVL_VL_REC_SERVER_URL:-}"

VERBOSE="${PADDLEOCRVL_VERBOSE:-0}"

DEFAULT_PADDLEX_HOME="${PADDLEOCRVL_PADDLEX_HOME:-/models/.paddlex}"
export PADDLEX_HOME="${PADDLEX_HOME:-${DEFAULT_PADDLEX_HOME}}"
mkdir -p "${PADDLEX_HOME}"

export FLAGS_allocator_strategy="${FLAGS_allocator_strategy:-auto_growth}"
export FLAGS_fraction_of_gpu_memory_to_use="${FLAGS_fraction_of_gpu_memory_to_use:-0.92}"

mkdir -p "${OUTPUT_ROOT}" "${WORKSPACE_ROOT}"

check_models() {
  local required_dir="${PADDLEX_HOME}/official_models"
  if [ ! -d "${PADDLEX_HOME}" ]; then
    echo "[ERROR] PaddleX cache directory not found: ${PADDLEX_HOME}" >&2
    exit 42
  fi
  if [ ! -d "${required_dir}" ]; then
    echo "[ERROR] ${required_dir} is missing. Mount @DOC_MODEL_STAGE/paddleocrvl to /models." >&2
    exit 42
  fi
  if ! find "${required_dir}" -type f -print -quit | grep -q .; then
    echo "[ERROR] ${required_dir} is empty. Ensure PaddleOCR-VL official models are staged." >&2
    exit 42
  fi
  local pipelines_dir="${PADDLEX_HOME}/pipelines"
  if [ ! -d "${pipelines_dir}" ]; then
    echo "[ERROR] Missing ${pipelines_dir}. Upload the full .paddlex tree (pipelines + official_models)." >&2
    exit 42
  fi
  if ! find "${pipelines_dir}" -type f -print -quit | grep -q .; then
    echo "[ERROR] ${pipelines_dir} has no pipeline descriptors. Re-run ci_paddleocrvl_models.py --stage ... to regenerate." >&2
    exit 42
  fi
  local doc_layout_dir="${PADDLEX_HOME}/official_models/PP-DocLayoutV2"
  if [ -d "${doc_layout_dir}" ]; then
    if ! find "${doc_layout_dir}" -type f -name '*.pdiparams*' -print -quit | grep -q .; then
      echo "[ERROR] ${doc_layout_dir} exists but has no parameter files. Re-upload PaddleOCR-VL models." >&2
      exit 42
    fi
  else
    if [ "${PADDLEOCRVL_DISABLE_LAYOUT_DETECTION:-0}" != "1" ]; then
      cat >&2 <<EOF
[ERROR] Layout detector not found at ${doc_layout_dir}.
       Run ci_paddleocrvl_models.py without --disable-layout-detection to download PP-DocLayoutV2,
       or set PADDLEOCRVL_DISABLE_LAYOUT_DETECTION=1 in the spec to skip layout detection.
EOF
      exit 42
    fi
  fi
}

check_models

cmd=(
  /usr/bin/python3 /usr/local/bin/paddleocrvl_pipeline.py
  --input-dir "${INPUT_ROOT}"
  --output-dir "${OUTPUT_ROOT}"
  --workspace-dir "${WORKSPACE_ROOT}"
  --pipeline-name "${PIPELINE_NAME}"
  --model-name "${MODEL_NAME}"
  --parquet-name "${PARQUET_NAME}"
  --markdown-subdir "${MARKDOWN_SUBDIR}"
)

if [ "${EMIT_MD}" = "1" ]; then
  cmd+=("--emit-markdown")
fi
if [ "${PRETTY_FLAG}" = "0" ]; then
  cmd+=("--no-pretty-markdown")
fi
if [ "${SHOW_FORMULA_FLAG}" = "1" ]; then
  cmd+=("--show-formula-number")
fi
if [ -n "${MIN_PIXELS}" ]; then
  cmd+=("--min-pixels" "${MIN_PIXELS}")
fi
if [ -n "${MAX_PIXELS}" ]; then
  cmd+=("--max-pixels" "${MAX_PIXELS}")
fi
if [ -n "${TEMPERATURE}" ]; then
  cmd+=("--temperature" "${TEMPERATURE}")
fi
if [ -n "${TOP_P}" ]; then
  cmd+=("--top-p" "${TOP_P}")
fi
if [ -n "${REPETITION}" ]; then
  cmd+=("--repetition-penalty" "${REPETITION}")
fi
if [ "${DOC_ORIENTATION}" = "1" ]; then
  cmd+=("--use-doc-orientation-classify")
fi
if [ "${DOC_UNWARPING}" = "1" ]; then
  cmd+=("--use-doc-unwarping")
fi
if [ "${DISABLE_LAYOUT}" = "1" ]; then
  cmd+=("--disable-layout-detection")
fi
if [ -n "${LAYOUT_MODEL_DIR}" ]; then
  cmd+=("--layout-detection-model-dir" "${LAYOUT_MODEL_DIR}")
fi
if [ -n "${VL_REC_SERVER_URL}" ]; then
  cmd+=("--vl-rec-server-url" "${VL_REC_SERVER_URL}")
fi
if [ "${VERBOSE}" = "1" ]; then
  cmd+=("--verbose")
fi

echo "+ ${cmd[*]}"
exec "${cmd[@]}"
