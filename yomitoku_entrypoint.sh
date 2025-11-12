#!/bin/bash
set -euo pipefail

INPUT_ROOT="${YOMITOKU_INPUT_DIR:-/in}"
OUTPUT_ROOT="${YOMITOKU_OUTPUT_DIR:-/out}"
WORKSPACE_ROOT="${YOMITOKU_WORKSPACE_DIR:-/workspace}"
MODEL_ROOT="${YOMITOKU_MODEL_DIR:-/models}"

TD_SUBDIR="${YOMITOKU_TEXT_DETECTOR_SUBDIR:-yomitoku-text-detector-dbnet-v2}"
TR_SUBDIR="${YOMITOKU_TEXT_RECOGNIZER_SUBDIR:-yomitoku-text-recognizer-parseq-middle-v2}"
LP_SUBDIR="${YOMITOKU_LAYOUT_PARSER_SUBDIR:-yomitoku-layout-parser-rtdtrv2-v2}"
TS_SUBDIR="${YOMITOKU_TABLE_RECOGNIZER_SUBDIR:-yomitoku-table-structure-recognizer-rtdtrv2-open-beta}"

FORMAT="${YOMITOKU_FORMAT:-md}"
REQUESTED_DEVICE="${YOMITOKU_DEVICE:-auto}"
VIS_FLAG="${YOMITOKU_VIS:-0}"
COMBINE_FLAG="${YOMITOKU_COMBINE:-1}"
FIGURE_FLAG="${YOMITOKU_FIGURE:-1}"
FIGURE_LETTER_FLAG="${YOMITOKU_FIGURE_LETTER:-0}"
IGNORE_LINE_BREAK_FLAG="${YOMITOKU_IGNORE_LINE_BREAK:-0}"
IGNORE_META_FLAG="${YOMITOKU_IGNORE_META:-0}"
READING_ORDER="${YOMITOKU_READING_ORDER:-auto}"
ENCODING="${YOMITOKU_ENCODING:-utf-8}"
DPI="${YOMITOKU_DPI:-200}"
FIGURE_WIDTH="${YOMITOKU_FIGURE_WIDTH:-200}"
FIGURE_DIR_RAW="${YOMITOKU_FIGURE_DIR:-/workspace/figures}"
PAGES="${YOMITOKU_PAGES:-}"
LITE_FLAG="${YOMITOKU_LITE:-0}"
FONT_PATH="${YOMITOKU_FONT_PATH:-}"
EXTRA_ARGS="${YOMITOKU_EXTRA_ARGS:-}"

mkdir -p "${OUTPUT_ROOT}" "${WORKSPACE_ROOT}"
if [[ "${FIGURE_DIR_RAW}" = /* ]]; then
  FIGURE_DIR_PATH="${FIGURE_DIR_RAW}"
else
  FIGURE_DIR_PATH="${OUTPUT_ROOT%/}/${FIGURE_DIR_RAW}"
fi
mkdir -p "${FIGURE_DIR_PATH}"

mapfile -t sources < <(
  find "${INPUT_ROOT}" -type f \( \
    -iname "*.pdf" -o \
    -iname "*.png" -o \
    -iname "*.jpg" -o \
    -iname "*.jpeg" -o \
    -iname "*.bmp" -o \
    -iname "*.tif" -o \
    -iname "*.tiff" \
  \) | sort
) || true

if [ "${#sources[@]}" -eq 0 ]; then
  echo "No supported input files found under ${INPUT_ROOT}. Nothing to do."
  exit 0
fi

detect_gpu() {
  if command -v nvidia-smi >/dev/null 2>&1; then
    if nvidia-smi -L >/dev/null 2>&1; then
      return 0
    fi
  fi
  return 1
}

resolve_device() {
  local desired="$1"
  if [ "${desired}" = "auto" ]; then
    if detect_gpu; then
      echo "cuda"
    else
      echo "cpu"
    fi
  else
    echo "${desired}"
  fi
}

DEVICE="$(resolve_device "${REQUESTED_DEVICE}")"
if [ "${REQUESTED_DEVICE}" = "auto" ] && [ "${DEVICE}" = "cpu" ]; then
  echo "[WARN] GPU not detected; falling back to CPU mode." >&2
elif [ "${REQUESTED_DEVICE}" = "cpu" ]; then
  echo "[INFO] OCR is pinned to CPU execution." >&2
fi

TEXT_DETECTOR_PATH="${MODEL_ROOT%/}/${TD_SUBDIR}"
TEXT_RECOGNIZER_PATH="${MODEL_ROOT%/}/${TR_SUBDIR}"
LAYOUT_PARSER_PATH="${MODEL_ROOT%/}/${LP_SUBDIR}"
TABLE_RECOGNIZER_PATH="${MODEL_ROOT%/}/${TS_SUBDIR}"

missing=0
for path in \
  "${TEXT_DETECTOR_PATH}" \
  "${TEXT_RECOGNIZER_PATH}" \
  "${LAYOUT_PARSER_PATH}" \
  "${TABLE_RECOGNIZER_PATH}"
do
  if [ ! -d "${path}" ]; then
    echo "[ERROR] Model directory not found: ${path}" >&2
    missing=1
  fi
done

if [ "${missing}" -ne 0 ]; then
  exit 3
fi

CONFIG_DIR="${WORKSPACE_ROOT%/}/cfg"
mkdir -p "${CONFIG_DIR}"

cat > "${CONFIG_DIR}/text_detector.yaml" <<EOF
hf_hub_repo: ${TEXT_DETECTOR_PATH}
EOF

cat > "${CONFIG_DIR}/text_recognizer.yaml" <<EOF
hf_hub_repo: ${TEXT_RECOGNIZER_PATH}
EOF

cat > "${CONFIG_DIR}/layout_parser.yaml" <<EOF
hf_hub_repo: ${LAYOUT_PARSER_PATH}
EOF

cat > "${CONFIG_DIR}/table_structure.yaml" <<EOF
hf_hub_repo: ${TABLE_RECOGNIZER_PATH}
EOF

cmd=(
  yomitoku "${INPUT_ROOT}"
  --format "${FORMAT}"
  --outdir "${OUTPUT_ROOT}"
  --device "${DEVICE}"
  --td_cfg "${CONFIG_DIR}/text_detector.yaml"
  --tr_cfg "${CONFIG_DIR}/text_recognizer.yaml"
  --lp_cfg "${CONFIG_DIR}/layout_parser.yaml"
  --tsr_cfg "${CONFIG_DIR}/table_structure.yaml"
  --encoding "${ENCODING}"
  --reading_order "${READING_ORDER}"
  --dpi "${DPI}"
  --figure_width "${FIGURE_WIDTH}"
  --figure_dir "${FIGURE_DIR_PATH}"
)

if [ "${VIS_FLAG}" = "1" ]; then
  cmd+=("--vis")
fi
if [ "${COMBINE_FLAG}" = "1" ]; then
  cmd+=("--combine")
fi
if [ "${FIGURE_FLAG}" = "1" ]; then
  cmd+=("--figure")
fi
if [ "${FIGURE_LETTER_FLAG}" = "1" ]; then
  cmd+=("--figure_letter")
fi
if [ "${IGNORE_LINE_BREAK_FLAG}" = "1" ]; then
  cmd+=("--ignore_line_break")
fi
if [ "${IGNORE_META_FLAG}" = "1" ]; then
  cmd+=("--ignore_meta")
fi
if [ -n "${PAGES}" ]; then
  cmd+=("--pages" "${PAGES}")
fi
if [ "${LITE_FLAG}" = "1" ]; then
  cmd+=("--lite")
fi
if [ -n "${FONT_PATH}" ]; then
  cmd+=("--font_path" "${FONT_PATH}")
fi

if [ -n "${EXTRA_ARGS}" ]; then
  # shellcheck disable=SC2206
  extra_tokens=(${EXTRA_ARGS})
  cmd+=("${extra_tokens[@]}")
fi

echo "+ ${cmd[*]}"
exec "${cmd[@]}"
