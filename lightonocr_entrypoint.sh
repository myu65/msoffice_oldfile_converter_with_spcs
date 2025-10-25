#!/bin/bash
set -euo pipefail

INPUT_ROOT="${OCR_INPUT_DIR:-/in}"
OUTPUT_ROOT="${OCR_OUTPUT_DIR:-/out}"
WORKSPACE_ROOT="${OCR_WORKSPACE_DIR:-/workspace}"
MODEL_ROOT="${OCR_MODEL_DIR:-/models}"
MODEL_SUBDIR="${OCR_MODEL_SUBDIR:-LightOnOCR-1B-1025}"


PORT="${OCR_SERVER_PORT:-8000}"
TENSOR_PARALLEL="${OCR_TENSOR_PARALLEL:-1}"
MAX_MODEL_LEN="${OCR_MAX_MODEL_LEN:-8192}"
MAX_BATCH_TOKENS_DEFAULT="${OCR_MAX_BATCH_TOKENS:-}"
if [ -n "${MAX_BATCH_TOKENS_DEFAULT}" ]; then
  MAX_BATCH_TOKENS="${MAX_BATCH_TOKENS_DEFAULT}"
else
  MAX_BATCH_TOKENS="${MAX_MODEL_LEN}"
fi
if (( MAX_BATCH_TOKENS < MAX_MODEL_LEN )); then
  echo "[WARN] OCR_MAX_BATCH_TOKENS (${MAX_BATCH_TOKENS}) < OCR_MAX_MODEL_LEN (${MAX_MODEL_LEN}); adjusting to ${MAX_MODEL_LEN}" >&2
  MAX_BATCH_TOKENS="${MAX_MODEL_LEN}"
fi
API_KEY="${OCR_API_KEY:-token}"
EXTRA_ARGS="${OCR_EXTRA_ARGS:-}"
REQUESTED_DEVICE="${OCR_DEVICE:-auto}"

PARQUET_NAME="${OCR_PARQUET_NAME:-lightonocr_output.parquet}"
MAX_OUTPUT_TOKENS="${OCR_MAX_OUTPUT_TOKENS:-3500}"
TEMPERATURE="${OCR_TEMPERATURE:-0.0}"
TOP_P="${OCR_TOP_P:-0.95}"
PAGE_SCALE="${OCR_PAGE_SCALE:-2.0}"
REQUEST_TIMEOUT="${OCR_REQUEST_TIMEOUT:-300}"
RETRY_ATTEMPTS="${OCR_RETRY_ATTEMPTS:-3}"
RETRY_INTERVAL="${OCR_RETRY_INTERVAL:-10}"
SLEEP_BETWEEN_PAGES="${OCR_SLEEP_BETWEEN_PAGES:-0}"
EMIT_MARKDOWN="${OCR_EMIT_MARKDOWN:-1}"
VERBOSE="${OCR_VERBOSE:-0}"

MODEL_PATH="${MODEL_ROOT%/}/${MODEL_SUBDIR}"
DEVICE_FLAG=()

detect_gpu() {
  if command -v nvidia-smi >/dev/null 2>&1; then
    if nvidia-smi -L >/dev/null 2>&1; then
      return 0
    fi
  fi
  return 1
}

if [ "${REQUESTED_DEVICE}" = "cpu" ]; then
  echo "[INFO] OCR_DEVICE=cpu specified. Forcing CPU execution." >&2
  export CUDA_VISIBLE_DEVICES=""
  DEVICE_FLAG=(--device cpu)
elif detect_gpu; then
  echo "[INFO] GPU detected. Using default device configuration." >&2
  DEVICE_FLAG=()
else
  echo "[WARN] GPU not detected; falling back to CPU mode. Set OCR_DEVICE=cpu explicitly to suppress this warning." >&2
  export CUDA_VISIBLE_DEVICES=""
  DEVICE_FLAG=(--device cpu)
fi

mkdir -p "${WORKSPACE_ROOT}" "${OUTPUT_ROOT}"

if [ ! -d "${MODEL_PATH}" ]; then
  echo "[ERROR] Model path not found: ${MODEL_PATH}" >&2
  exit 3
fi

vllm_cmd=(
  vllm serve "${MODEL_PATH}"
  --tokenizer "${MODEL_PATH}"
  --port "${PORT}"
  --tensor-parallel-size "${TENSOR_PARALLEL}"
  --max-model-len "${MAX_MODEL_LEN}"
  --max-num-batched-tokens "${MAX_BATCH_TOKENS}"
  --api-key "${API_KEY}"
  --trust-remote-code
  "${DEVICE_FLAG[@]}"
)

if [ -n "${EXTRA_ARGS}" ]; then
  # shellcheck disable=SC2206
  extra_tokens=(${EXTRA_ARGS})
  vllm_cmd+=("${extra_tokens[@]}")
fi

echo "+ ${vllm_cmd[*]}" >&2
"${vllm_cmd[@]}" &
SERVER_PID=$!

cleanup() {
  if kill -0 "${SERVER_PID}" >/dev/null 2>&1; then
    kill "${SERVER_PID}" || true
    wait "${SERVER_PID}" || true
  fi
}
trap cleanup EXIT INT TERM

pipeline_args=(
  /usr/bin/python3 /usr/local/bin/lightonocr_pipeline.py
  --input-dir "${INPUT_ROOT}"
  --output-dir "${OUTPUT_ROOT}"
  --workspace-dir "${WORKSPACE_ROOT}"
  --model-name "${MODEL_SUBDIR}"
  --api-base "http://127.0.0.1:${PORT}"
  --api-key "${API_KEY}"
  --parquet-name "${PARQUET_NAME}"
  --max-output-tokens "${MAX_OUTPUT_TOKENS}"
  --temperature "${TEMPERATURE}"
  --top-p "${TOP_P}"
  --request-timeout "${REQUEST_TIMEOUT}"
  --retry-attempts "${RETRY_ATTEMPTS}"
  --retry-interval "${RETRY_INTERVAL}"
  --page-scale "${PAGE_SCALE}"
  --sleep-between-pages "${SLEEP_BETWEEN_PAGES}"
)

if [ "${EMIT_MARKDOWN}" = "1" ]; then
  pipeline_args+=("--emit-markdown")
fi
if [ "${VERBOSE}" = "1" ]; then
  pipeline_args+=("--verbose")
fi

echo "+ ${pipeline_args[*]}" >&2
set +e
"${pipeline_args[@]}"
status=$?
set -e

cleanup

exit "${status}"
