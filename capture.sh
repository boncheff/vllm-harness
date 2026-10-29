#!/bin/bash
# One clean /metrics capture on the pod. vllm serve is already running.
# This script does not stop the server.
set -euo pipefail

cd /vllm-workspace

MODEL="${MODEL:-Qwen/Qwen2.5-1.5B-Instruct}"
NUM_PROMPTS="${NUM_PROMPTS:-128}"
INPUT_LEN="${INPUT_LEN:-256}"
OUTPUT_LEN="${OUTPUT_LEN:-128}"
MAX_CONCURRENCY="${MAX_CONCURRENCY:-8}"
DISCARD="${DISCARD:-1}"

if [[ -z "${OPENAI_API_KEY:-}" ]]; then
  echo "OPENAI_API_KEY is not set" >&2
  exit 1
fi

if ! curl -sf localhost:8000/v1/models >/dev/null; then
  echo "server is not answering on localhost:8000" >&2
  exit 1
fi

run_bench() {
  vllm bench serve \
    --backend openai-chat \
    --base-url http://127.0.0.1:8000 \
    --endpoint /v1/chat/completions \
    --model "$MODEL" \
    --dataset-name random \
    --random-input-len "$INPUT_LEN" \
    --random-output-len "$OUTPUT_LEN" \
    --num-prompts "$NUM_PROMPTS" \
    --max-concurrency "$MAX_CONCURRENCY"
}

if [[ "$DISCARD" == "1" ]]; then
  echo "discard bench, then the timed run"
  run_bench > discard.txt
fi

rm -f before.txt after.txt bench.txt during.txt during-*.txt /tmp/m.tgz

poll_pid=
stop_poll() {
  if [[ -n "${poll_pid}" ]]; then
    kill "$poll_pid" 2>/dev/null || true
    wait "$poll_pid" 2>/dev/null || true
    poll_pid=
  fi
}
trap stop_poll EXIT

(
  i=1
  while true; do
    curl -s localhost:8000/metrics > "during-$i.txt" || true
    i=$((i + 1))
    sleep 1
  done
) &
poll_pid=$!

curl -s localhost:8000/metrics > before.txt
run_bench | tee bench.txt
curl -s localhost:8000/metrics > after.txt
stop_poll
trap - EXIT

status=ok
if ! grep -H 'num_requests_running{' during-*.txt | grep -q -v ' 0.0'; then
  status=no-inflight
fi

tar_files=(before.txt after.txt bench.txt during-*.txt)
if [[ -f discard.txt ]]; then
  tar_files+=(discard.txt)
fi
tar czf /tmp/m.tgz "${tar_files[@]}"

echo "CAPTURE_STATUS=${status}"
ls -lh /tmp/m.tgz
echo "BEGIN_HARNESS_TGZ"
base64 -w 76 /tmp/m.tgz
echo "END_HARNESS_TGZ"
