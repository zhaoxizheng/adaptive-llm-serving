#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
CONFIG="configs/week04.yaml"
CASE_ID=""
PLAN_ONLY=false
SMOKE=false

usage() {
  echo "Usage: scripts/benchmark_vllm.sh [--config PATH] --case-id ID [--plan] [--smoke]"
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --config)
      [[ $# -ge 2 ]] || { echo "--config requires a path" >&2; exit 2; }
      CONFIG="$2"
      shift 2
      ;;
    --case-id)
      [[ $# -ge 2 ]] || { echo "--case-id requires an ID" >&2; exit 2; }
      CASE_ID="$2"
      shift 2
      ;;
    --plan|--dry-run)
      PLAN_ONLY=true
      shift
      ;;
    --smoke)
      SMOKE=true
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown argument: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

if [[ -z "${CASE_ID}" ]]; then
  echo "--case-id is required" >&2
  usage >&2
  exit 2
fi

if [[ "${CONFIG}" != /* ]]; then
  if [[ -f "${CONFIG}" ]]; then
    CONFIG="$(cd "$(dirname "${CONFIG}")" && pwd)/$(basename "${CONFIG}")"
  else
    CONFIG="${PROJECT_DIR}/${CONFIG}"
  fi
fi
if [[ ! -f "${CONFIG}" ]]; then
  echo "Config file does not exist: ${CONFIG}" >&2
  exit 1
fi

PYTHON="${VLLM_PYTHON:-${PROJECT_DIR}/.venv-vllm/bin/python}"
VLLM_BIN="${VLLM_BIN:-${PROJECT_DIR}/.venv-vllm/bin/vllm}"
if [[ ! -x "${PYTHON}" ]]; then
  if ${PLAN_ONLY}; then
    PYTHON="${CONTRACT_PYTHON:-python}"
  else
    echo "Dedicated vLLM Python is missing: ${PYTHON}" >&2
    echo "Run scripts/bootstrap_vllm_gcp.sh first." >&2
    exit 1
  fi
fi

cd "${PROJECT_DIR}"
PLAN_JSON="$("${PYTHON}" -m src.vllm_contract benchmark-plan \
  --config "${CONFIG}" --case-id "${CASE_ID}")"

CONTRACT_ARGV=()
while IFS= read -r -d '' value; do
  CONTRACT_ARGV+=("${value}")
done < <(
  PLAN_JSON="${PLAN_JSON}" "${PYTHON}" -c \
    'import json, os, sys; [sys.stdout.buffer.write(value.encode() + b"\0") for value in json.loads(os.environ["PLAN_JSON"])]'
)
if [[ ${#CONTRACT_ARGV[@]} -lt 3 ]]; then
  echo "Contract returned an invalid benchmark argv." >&2
  exit 1
fi
CONTRACT_ARGV[0]="${VLLM_BIN}"

SETTINGS_JSON="$("${PYTHON}" - "${CONFIG}" <<'PY'
import json, sys
from src.common import load_yaml
from src.vllm_contract import validate_config
config = validate_config(load_yaml(sys.argv[1]))
print(json.dumps([
    config["output"]["raw_dir"],
    config["output"]["bench_serve_help"],
    str(config["benchmark"]["warmup_requests"]),
    config["output"]["server_metrics_dir"],
    str(config["server"]["port"]),
    str(config["benchmark"]["timeout_seconds"]),
    str(min(30, config["server"]["shutdown_timeout_seconds"])),
]))
PY
)"
SETTINGS=()
while IFS= read -r -d '' value; do
  SETTINGS+=("${value}")
done < <(
  SETTINGS_JSON="${SETTINGS_JSON}" "${PYTHON}" -c \
    'import json, os, sys; [sys.stdout.buffer.write(value.encode() + b"\0") for value in json.loads(os.environ["SETTINGS_JSON"])]'
)
RAW_DIR="${SETTINGS[0]}"
HELP_PATH="${SETTINGS[1]}"
WARMUP_REQUESTS="${SETTINGS[2]}"
METRICS_DIR="${SETTINGS[3]}"
SERVER_PORT="${SETTINGS[4]}"
TIMEOUT_SECONDS="${SETTINGS[5]}"
TERM_GRACE_SECONDS="${SETTINGS[6]}"
if [[ "${RAW_DIR}" != /* ]]; then RAW_DIR="${PROJECT_DIR}/${RAW_DIR}"; fi
if [[ "${HELP_PATH}" != /* ]]; then HELP_PATH="${PROJECT_DIR}/${HELP_PATH}"; fi
if [[ "${METRICS_DIR}" != /* ]]; then METRICS_DIR="${PROJECT_DIR}/${METRICS_DIR}"; fi

MODE_DIR="${RAW_DIR}/benchmark"
RUN_LABEL="${CASE_ID}"
if ${SMOKE}; then
  MODE_DIR="${RAW_DIR}/benchmark-smoke"
  RUN_LABEL="${CASE_ID}-smoke"
  for ((index = 0; index < ${#CONTRACT_ARGV[@]}; index++)); do
    if [[ "${CONTRACT_ARGV[index]}" == "--num-prompts" ]]; then
      CONTRACT_ARGV[index + 1]="${WARMUP_REQUESTS}"
      break
    fi
  done
fi

FINAL_JSON="${MODE_DIR}/${RUN_LABEL}.json"
STDOUT_PATH="${MODE_DIR}/${RUN_LABEL}.stdout.txt"
HELP_SNAPSHOT_PATH="${MODE_DIR}/${RUN_LABEL}.help.txt"
ARGV_PATH="${MODE_DIR}/${RUN_LABEL}.argv.json"
MARKER_PATH="${MODE_DIR}/${RUN_LABEL}.incomplete.json"
COMPLETE_PATH="${MODE_DIR}/${RUN_LABEL}.complete.json"
WATCHDOG_PATH="${MODE_DIR}/${RUN_LABEL}.watchdog.json"
METRICS_BEFORE="${METRICS_DIR}/${RUN_LABEL}.before.prom"
METRICS_AFTER="${METRICS_DIR}/${RUN_LABEL}.prom"
LOCK_DIR="${MODE_DIR}/${RUN_LABEL}.lock"
TEMP_ROOT="${MODE_DIR}/.partial"
RESULT_FILENAME="${RUN_LABEL}.json"

CASE_METADATA_JSON="$("${PYTHON}" - "${CONFIG}" "${CASE_ID}" "${SMOKE}" "${PLAN_ONLY}" <<'PY'
import json, sys
from src.common import load_yaml
from src.vllm_contract import PINNED_VLLM_VERSION, get_case, validate_config
config = validate_config(load_yaml(sys.argv[1]))
case = get_case(config, sys.argv[2])
payload = case.to_dict()
payload.update({
    "case_id": sys.argv[2],
    "mode": "smoke" if sys.argv[3] == "true" else case.mode,
    "benchmark_mode": case.mode,
    "vllm_version": PINNED_VLLM_VERSION,
    "model_revision": config["model"]["revision"],
    "model": config["model"]["id"],
    "dtype": config["model"]["dtype"],
    "requested_prompt_tokens": case.prompt_tokens,
    "requested_output_tokens": case.output_tokens,
})
if sys.argv[4] == "true":
    payload.update({
        "run_id": "<run-uuid>",
        "git_commit": "<git-commit>",
        "source_tree_fingerprint": "<source-tree-fingerprint>",
        "config_fingerprint": "<config-fingerprint>",
        "runtime_fingerprint": "<runtime-fingerprint>",
        "model_identity_fingerprint": "<model-identity-fingerprint>",
        "server_instance_id": "<server-instance-uuid>",
        "server_attempt_id": "<server-attempt-uuid>",
    })
else:
    from pathlib import Path
    from src.common import read_json
    from src.week04_contract import (
        load_run_metadata, server_attempt_identity, validate_artifact_identity,
    )
    run = load_run_metadata(config)
    source = run["source"]
    server_path = Path(config["server"]["pid_metadata_path"])
    if not server_path.is_file():
        raise ValueError(f"server ownership metadata is missing: {server_path}")
    server = read_json(server_path)
    validate_artifact_identity(server, run, "server ownership metadata")
    if server.get("state") != "ready":
        raise ValueError("server ownership metadata is not ready")
    server_identity = server_attempt_identity(server, "server ownership metadata")
    payload.update({
        "run_id": run["run_id"],
        "git_commit": source["git_commit"],
        "source_tree_fingerprint": source["source_tree_fingerprint"],
        "config_fingerprint": run["config_fingerprint"],
        "runtime_fingerprint": run["runtime_fingerprint"],
        "model_identity_fingerprint": run["model_identity_fingerprint"],
        **server_identity,
    })
if sys.argv[3] == "true":
    payload["num_prompts"] = int(config["benchmark"]["warmup_requests"])
print(json.dumps(payload, sort_keys=True))
PY
)"

if ! ${PLAN_ONLY}; then
  METADATA_ARGS=()
  while IFS= read -r -d '' value; do
    METADATA_ARGS+=("${value}")
  done < <(
    CASE_METADATA_JSON="${CASE_METADATA_JSON}" "${PYTHON}" -c \
      'import json,os,sys; m=json.loads(os.environ["CASE_METADATA_JSON"]); keys=("run_id","git_commit","source_tree_fingerprint","config_fingerprint","runtime_fingerprint","model_identity_fingerprint","server_instance_id","server_attempt_id"); [sys.stdout.buffer.write((f"{key}={m[key]}").encode()+b"\0") for key in keys]'
  )
  # vLLM declares --metadata as one nargs list. Keep one occurrence so argparse
  # cannot replace the scientific metadata with a later repeated option.
  INSERT_AT=${#CONTRACT_ARGV[@]}
  for ((index = 0; index < ${#CONTRACT_ARGV[@]}; index++)); do
    if [[ "${CONTRACT_ARGV[index]}" == "--max-concurrency" ]]; then
      INSERT_AT=${index}
      break
    fi
  done
  PREFIX_ARGV=("${CONTRACT_ARGV[@]:0:${INSERT_AT}}")
  SUFFIX_ARGV=("${CONTRACT_ARGV[@]:${INSERT_AT}}")
  CONTRACT_ARGV=("${PREFIX_ARGV[@]}" "${METADATA_ARGS[@]}" "${SUFFIX_ARGV[@]}")
fi

if ${PLAN_ONLY}; then
  PLAN_JSON="${PLAN_JSON}" VLLM_BIN="${VLLM_BIN}" MODE_DIR="${MODE_DIR}" \
    RUN_LABEL="${RUN_LABEL}" HELP_PATH="${HELP_PATH}" SMOKE="${SMOKE}" \
    WARMUP_REQUESTS="${WARMUP_REQUESTS}" CASE_METADATA_JSON="${CASE_METADATA_JSON}" \
    "${PYTHON}" - <<'PY'
import json, os
argv = json.loads(os.environ["PLAN_JSON"])
argv[0] = os.environ["VLLM_BIN"]
if os.environ["SMOKE"] == "true":
    index = argv.index("--num-prompts")
    argv[index + 1] = os.environ["WARMUP_REQUESTS"]
argv += [
    "--result-dir", os.path.join(os.environ["MODE_DIR"], ".partial", "<run>"),
    "--result-filename", os.environ["RUN_LABEL"] + ".json",
]
print(json.dumps({
    "mode": "smoke" if os.environ["SMOKE"] == "true" else "benchmark",
    "case_id": os.environ["RUN_LABEL"],
    "metadata": json.loads(os.environ["CASE_METADATA_JSON"]),
    "argv": argv,
    "help_path": os.environ["HELP_PATH"],
    "note": "Plan mode does not require or execute vLLM; flags are preflighted at execution time.",
}, indent=2, sort_keys=True))
PY
  exit 0
fi

if [[ -e "${FINAL_JSON}" ]]; then
  if [[ -e "${COMPLETE_PATH}" && ! -e "${MARKER_PATH}" ]]; then
    if FINAL_JSON="${FINAL_JSON}" COMPLETE_PATH="${COMPLETE_PATH}" \
      CASE_METADATA_JSON="${CASE_METADATA_JSON}" "${PYTHON}" - <<'PY'
import hashlib, json, os, sys
from pathlib import Path
complete = json.loads(Path(os.environ["COMPLETE_PATH"]).read_text(encoding="utf-8"))
expected = json.loads(os.environ["CASE_METADATA_JSON"])
raw = Path(os.environ["FINAL_JSON"])
artifacts = complete.get("artifacts", {})
entry = artifacts.get("raw_json", {})
sidecars_valid = isinstance(artifacts, dict) and bool(artifacts)
for artifact in artifacts.values() if isinstance(artifacts, dict) else ():
    path = Path(str(artifact.get("path", ""))) if isinstance(artifact, dict) else Path("")
    if (
        not path.is_file()
        or artifact.get("size_bytes") != path.stat().st_size
        or artifact.get("sha256") != hashlib.sha256(path.read_bytes()).hexdigest()
    ):
        sidecars_valid = False
        break
valid = (
    complete.get("status") == "completed"
    and complete.get("metadata") == expected
    and sidecars_valid
    and entry.get("sha256") == hashlib.sha256(raw.read_bytes()).hexdigest()
    and entry.get("size_bytes") == raw.stat().st_size
)
raise SystemExit(0 if valid else 1)
PY
    then
      echo "Skipping validated completed benchmark case: ${RUN_LABEL}"
      exit 0
    fi
  fi
  echo "Refusing to overwrite unvalidated published benchmark evidence: ${FINAL_JSON}" >&2
  exit 1
fi
if [[ -e "${COMPLETE_PATH}" && ! -e "${FINAL_JSON}" ]]; then
  echo "Refusing to overwrite benchmark completion evidence: ${COMPLETE_PATH}" >&2
  exit 1
fi
if [[ -e "${MARKER_PATH}" ]]; then
  echo "An incomplete marker already exists for this run: ${MARKER_PATH}" >&2
  exit 1
fi
mkdir -p "${MODE_DIR}"
if ! mkdir "${LOCK_DIR}" 2>/dev/null; then
  echo "Another benchmark process holds the run lock: ${LOCK_DIR}" >&2
  exit 1
fi
release_lock() {
  rmdir "${LOCK_DIR}" 2>/dev/null || true
}
trap release_lock EXIT INT TERM HUP

if [[ ! -x "${VLLM_BIN}" ]]; then
  echo "Dedicated vLLM executable is missing: ${VLLM_BIN}" >&2
  exit 1
fi

mkdir -p "${MODE_DIR}" "${TEMP_ROOT}" "$(dirname "${HELP_PATH}")"
VLLM_VERSION_PATH="${RAW_DIR}/vllm-version.txt"
VLLM_HELP_PATH="${RAW_DIR}/vllm-help.txt"
ACTUAL_VLLM_VERSION="$("${PYTHON}" -c 'from importlib.metadata import version; print(version("vllm"))')"
printf '%s\n' "${ACTUAL_VLLM_VERSION}" > "${VLLM_VERSION_PATH}"
if [[ "${ACTUAL_VLLM_VERSION}" != "0.10.2" ]]; then
  echo "Expected vllm==0.10.2, got ${ACTUAL_VLLM_VERSION}." >&2
  exit 1
fi

"${VLLM_BIN}" --help > "${VLLM_HELP_PATH}" 2>&1
"${VLLM_BIN}" bench serve --help > "${HELP_PATH}" 2>&1
cp "${HELP_PATH}" "${HELP_SNAPSHOT_PATH}"

RUN_TEMP_DIR="$(mktemp -d "${TEMP_ROOT}/${RUN_LABEL}.XXXXXX")"
cleanup() {
  if [[ -d "${RUN_TEMP_DIR}" ]]; then
    FAILURE_DIR="${MODE_DIR}/${RUN_LABEL}.partial"
    if find "${RUN_TEMP_DIR}" -maxdepth 1 -type f -print -quit | grep -q .; then
      mkdir -p "${FAILURE_DIR}"
      find "${RUN_TEMP_DIR}" -maxdepth 1 -type f -exec mv -f {} "${FAILURE_DIR}/" \; 2>/dev/null || true
    fi
    rmdir "${RUN_TEMP_DIR}" 2>/dev/null || true
  fi
  release_lock
}
trap cleanup EXIT INT TERM HUP

FINAL_ARGV=("${CONTRACT_ARGV[@]}"
  --result-dir "${RUN_TEMP_DIR}"
  --result-filename "${RESULT_FILENAME}")

ARGV_JSON="$(printf '%s\0' "${FINAL_ARGV[@]}" | "${PYTHON}" -c \
  'import json, sys; print(json.dumps([part.decode() for part in sys.stdin.buffer.read().split(b"\0")[:-1]]))')"
ARGV_JSON="${ARGV_JSON}" HELP_PATH="${HELP_PATH}" "${PYTHON}" - <<'PY'
import json, os
from pathlib import Path
from src.vllm_contract import validate_help_support
argv = json.loads(os.environ["ARGV_JSON"])
help_text = Path(os.environ["HELP_PATH"]).read_text(encoding="utf-8")
validate_help_support(argv[3:], help_text)
PY

ARGV_JSON="${ARGV_JSON}" ARGV_PATH="${ARGV_PATH}" MARKER_PATH="${MARKER_PATH}" \
  CASE_ID="${CASE_ID}" SMOKE="${SMOKE}" CASE_METADATA_JSON="${CASE_METADATA_JSON}" \
  "${PYTHON}" - <<'PY'
import json, os, tempfile
from datetime import datetime, timezone
from pathlib import Path
def write(path, payload):
    target = Path(path); target.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=target.parent, prefix=f".{target.name}.", suffix=".tmp", text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n"); handle.flush(); os.fsync(handle.fileno())
        os.replace(name, target)
    except BaseException:
        try: os.unlink(name)
        except FileNotFoundError: pass
        raise
argv = json.loads(os.environ["ARGV_JSON"])
metadata = json.loads(os.environ["CASE_METADATA_JSON"])
write(os.environ["ARGV_PATH"], {"argv": argv, "metadata": metadata})
write(os.environ["MARKER_PATH"], {
    "status": "incomplete", "case_id": os.environ["CASE_ID"],
    "smoke": os.environ["SMOKE"] == "true", "argv": argv,
    "metadata": metadata,
    "started_at": datetime.now(timezone.utc).isoformat(),
})
PY

fetch_metrics() {
  local destination="$1"
  "${PYTHON}" - "${SERVER_PORT}" "${destination}" <<'PY'
import os, sys, tempfile, urllib.request
from pathlib import Path
port, raw_path = sys.argv[1:]
path = Path(raw_path); path.parent.mkdir(parents=True, exist_ok=True)
request = urllib.request.Request(
    f"http://127.0.0.1:{port}/metrics", headers={"Accept": "text/plain"}
)
with urllib.request.urlopen(request, timeout=5) as response:
    payload = response.read(32 << 20)
if not payload:
    raise SystemExit("empty /metrics response")
fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
try:
    with os.fdopen(fd, "wb") as handle:
        handle.write(payload); handle.flush(); os.fsync(handle.fileno())
    os.replace(temporary, path)
except BaseException:
    try: os.unlink(temporary)
    except FileNotFoundError: pass
    raise
PY
}

fetch_metrics "${METRICS_BEFORE}"

set +e
"${PYTHON}" -m src.process_watchdog \
  --timeout-seconds "${TIMEOUT_SECONDS}" \
  --term-grace-seconds "${TERM_GRACE_SECONDS}" \
  --stdout "${STDOUT_PATH}" \
  --result "${WATCHDOG_PATH}" \
  -- "${FINAL_ARGV[@]}"
EXIT_CODE=$?
set -e

CANDIDATE_JSON="${RUN_TEMP_DIR}/${RESULT_FILENAME}"
if [[ ${EXIT_CODE} -eq 0 ]]; then
  fetch_metrics "${METRICS_AFTER}"
  if [[ ! -s "${CANDIDATE_JSON}" ]]; then
    echo "vLLM exited 0 but did not create expected raw JSON: ${CANDIDATE_JSON}" >&2
    exit 1
  fi
  if ! "${PYTHON}" -m json.tool "${CANDIDATE_JSON}" >/dev/null; then
    echo "vLLM exited 0 but produced invalid raw JSON: ${CANDIDATE_JSON}" >&2
    exit 1
  fi
  mv -f "${CANDIDATE_JSON}" "${FINAL_JSON}"
  COMPLETE_PATH="${COMPLETE_PATH}" FINAL_JSON="${FINAL_JSON}" \
    ARGV_PATH="${ARGV_PATH}" STDOUT_PATH="${STDOUT_PATH}" \
    HELP_SNAPSHOT_PATH="${HELP_SNAPSHOT_PATH}" METRICS_BEFORE="${METRICS_BEFORE}" \
    METRICS_AFTER="${METRICS_AFTER}" WATCHDOG_PATH="${WATCHDOG_PATH}" \
    CASE_METADATA_JSON="${CASE_METADATA_JSON}" "${PYTHON}" - <<'PY'
import hashlib, json, os
from datetime import datetime, timezone
from pathlib import Path
from src.common import write_json
paths = {
    "raw_json": Path(os.environ["FINAL_JSON"]),
    "argv": Path(os.environ["ARGV_PATH"]),
    "stdout": Path(os.environ["STDOUT_PATH"]),
    "help": Path(os.environ["HELP_SNAPSHOT_PATH"]),
    "metrics_before": Path(os.environ["METRICS_BEFORE"]),
    "metrics_after": Path(os.environ["METRICS_AFTER"]),
    "watchdog": Path(os.environ["WATCHDOG_PATH"]),
}
missing = [str(path) for path in paths.values() if not path.is_file()]
if missing:
    raise SystemExit(f"cannot complete benchmark; missing sidecars: {missing}")
metadata = json.loads(os.environ["CASE_METADATA_JSON"])
write_json(os.environ["COMPLETE_PATH"], {
    "schema_version": 1,
    "status": "completed",
    "completed_at": datetime.now(timezone.utc).isoformat(),
    "case_id": metadata["case_id"],
    "metadata": metadata,
    "artifacts": {
        name: {
            "path": str(path),
            "size_bytes": path.stat().st_size,
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        }
        for name, path in paths.items()
    },
})
PY
  rm -f "${MARKER_PATH}"
  trap - EXIT INT TERM HUP
  cleanup
  echo "Published valid raw benchmark JSON: ${FINAL_JSON}"
  exit 0
fi

MARKER_PATH="${MARKER_PATH}" WATCHDOG_PATH="${WATCHDOG_PATH}" \
  "${PYTHON}" - <<'PY'
import json, os
from pathlib import Path
from src.common import read_json, utc_now, write_json
marker = read_json(os.environ["MARKER_PATH"])
watchdog_path = Path(os.environ["WATCHDOG_PATH"])
watchdog = read_json(watchdog_path) if watchdog_path.is_file() else None
marker.update({
    "status": "incomplete",
    "finished_at": utc_now(),
    "terminal_reason": (watchdog or {}).get("status", "runner_failure"),
    "watchdog": watchdog,
})
write_json(os.environ["MARKER_PATH"], marker)
PY
echo "vLLM benchmark exited ${EXIT_CODE}; incomplete marker and raw stdout were preserved." >&2
exit "${EXIT_CODE}"
