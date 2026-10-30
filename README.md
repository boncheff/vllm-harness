# vllm-harness

A small harness that runs a vLLM bench, saves the metrics from that run, and shows them on a page. I made it out of my own frustration. Getting a full `/metrics` scrape off the pod by hand, and then trusting the number the bench printed, was a pain.

The bench runs on the pod against localhost. The page is on your machine. Counters are after minus before. Gauges are sampled while the bench is running, and the page shows the highest value.

`vllm serve` has to already be running.

## Run

```bash
export OPENAI_API_KEY=...
python3 pull_run.py --ssh USER@ssh.runpod.io --dollars-per-hour PRICE
```

`USER` is the name on the connect panel's `ssh.runpod.io` line. `PRICE` is the hourly price of that pod. The key defaults to `~/.ssh/id_runpod`.

That sends `capture.sh` over SSH. It runs a discard bench, then the timed bench, and writes `runs/<timestamp>/run.json`.

```bash
python3 room.py
```

Open `http://127.0.0.1:8765`. One block per run.

## Sample

`runs/2026-10-29-204312` is one L4 run: Qwen2.5-1.5B-Instruct, 128 prompts, 256 in, 128 out, concurrency 8. Prefix caching was on and both benches used the same prompts, so the timed bench is a cache hit.
