#!/usr/bin/env python3
"""Diff one vLLM /metrics capture.

Counters come from after.txt minus before.txt.
Gauges come from the during-*.txt polls. The saved value is the peak.
"""

import argparse
import json
import re
import tarfile
from pathlib import Path

SAMPLE_RE = re.compile(
    r"^(?P<name>[a-zA-Z_:][a-zA-Z0-9_:]*)(?:\{(?P<labels>[^}]*)\})?\s+(?P<value>\S+)(?:\s+\d+)?$"
)
LABEL_RE = re.compile(r'(\w+)="((?:\\.|[^"\\])*)"')
TYPE_RE = re.compile(r"^# TYPE\s+(\S+)\s+(\S+)")

PEAK_GAUGES = {
    "vllm:num_requests_running",
    "vllm:num_requests_waiting",
    "vllm:num_requests_waiting_by_reason",
    "vllm:kv_cache_usage_perc",
    "vllm:engine_sleep_state",
}

EXTRA_TEXT = {"bench.txt", "discard.txt"}


def parse_labels(raw: str) -> tuple:
    if not raw:
        return ()
    return tuple(LABEL_RE.findall(raw))


def series_key(name: str, labels: tuple) -> str:
    if not labels:
        return name
    inner = ",".join(f'{k}="{v}"' for k, v in labels)
    return f"{name}{{{inner}}}"


def parse_page(text: str):
    types = {}
    samples = {}
    for line in text.splitlines():
        if not line or line.startswith("#"):
            matched = TYPE_RE.match(line)
            if matched:
                types[matched.group(1)] = matched.group(2)
            continue
        matched = SAMPLE_RE.match(line)
        if not matched:
            continue
        name = matched.group("name")
        if name.endswith("_created"):
            continue
        labels = parse_labels(matched.group("labels") or "")
        samples[series_key(name, labels)] = {
            "name": name,
            "labels": dict(labels),
            "value": float(matched.group("value")),
        }
    return types, samples


def is_capture_file(name: str) -> bool:
    return name in ("before.txt", "after.txt") or name.startswith("during-") or name in EXTRA_TEXT


def load_members(path: Path):
    files = {}
    if path.is_dir():
        for child in sorted(path.iterdir()):
            if is_capture_file(child.name):
                files[child.name] = child.read_text()
        return files
    with tarfile.open(path, "r:gz") as bundle:
        for member in bundle.getmembers():
            name = Path(member.name).name
            if is_capture_file(name):
                files[name] = bundle.extractfile(member).read().decode()
    return files


def parse_bench(text: str):
    duration = re.search(r"Benchmark duration \(s\):\s+([0-9.]+)", text)
    throughput = re.search(r"Output token throughput \(tok/s\):\s+([0-9.]+)", text)
    parsed = {}
    if duration:
        parsed["benchmark_duration_s"] = float(duration.group(1))
    if throughput:
        parsed["output_tokens_per_second"] = float(throughput.group(1))
    return parsed


def during_index(name: str) -> int:
    return int(name.removesuffix(".txt").split("-", 1)[1])


def series_kind(name: str, types: dict) -> str:
    declared = types.get(name)
    if declared == "counter":
        return "counter"
    if declared == "gauge":
        return "gauge"
    if declared == "histogram":
        return "histogram"
    for suffix in ("_bucket", "_sum", "_count"):
        if name.endswith(suffix) and types.get(name[: -len(suffix)]) == "histogram":
            return "counter"
    return "gauge"


def sum_delta(counters: dict, name: str):
    found = False
    total = 0.0
    for row in counters.values():
        if row["name"] == name:
            found = True
            total += row["delta"]
    return total if found else None


def peak_of(gauges: dict, name: str):
    best = None
    best_file = None
    for row in gauges.values():
        if row["name"] != name or row["peak"] is None:
            continue
        if best is None or row["peak"] > best:
            best = row["peak"]
            best_file = row["peak_file"]
    return best, best_file


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("src")
    parser.add_argument("-o", "--out", required=True)
    parser.add_argument(
        "--dollars-per-hour",
        type=float,
        default=None,
        help="Card price. Used only to add dollars per million output tokens.",
    )
    args = parser.parse_args()

    files = load_members(Path(args.src))
    if "before.txt" not in files or "after.txt" not in files:
        raise SystemExit("before.txt and after.txt are required")

    types, before = parse_page(files["before.txt"])
    _, after = parse_page(files["after.txt"])
    during_names = sorted(
        (name for name in files if name.startswith("during-")),
        key=during_index,
    )
    during_pages = [(name, parse_page(files[name])[1]) for name in during_names]

    keys = set(before) | set(after)
    for _, page in during_pages:
        keys |= set(page)

    counters = {}
    gauges = {}
    for key in sorted(keys):
        sample = before.get(key) or after.get(key)
        if sample is None:
            for _, page in during_pages:
                if key in page:
                    sample = page[key]
                    break
        if sample is None or not sample["name"].startswith("vllm:"):
            continue
        kind = series_kind(sample["name"], types)
        if kind == "counter":
            before_value = before.get(key, {}).get("value", 0.0)
            after_value = after.get(key, {}).get("value", 0.0)
            counters[key] = {
                "name": sample["name"],
                "labels": sample["labels"],
                "before": before_value,
                "after": after_value,
                "delta": after_value - before_value,
            }
            continue
        if kind != "gauge":
            continue
        peak = None
        peak_file = None
        samples = []
        keep_samples = sample["name"] in PEAK_GAUGES
        for fname, page in during_pages:
            if key not in page:
                continue
            value = page[key]["value"]
            if keep_samples:
                samples.append({"file": fname, "value": value})
            if peak is None or value > peak:
                peak = value
                peak_file = fname
        gauges[key] = {
            "name": sample["name"],
            "labels": sample["labels"],
            "before": before.get(key, {}).get("value"),
            "after": after.get(key, {}).get("value"),
            "peak": peak,
            "peak_file": peak_file,
            "samples": samples,
        }

    finished = {}
    for row in counters.values():
        if row["name"] == "vllm:request_success_total":
            finished[row["labels"].get("finished_reason", "?")] = row["delta"]

    hits = sum_delta(counters, "vllm:prefix_cache_hits_total")
    queries = sum_delta(counters, "vllm:prefix_cache_queries_total")
    running, running_file = peak_of(gauges, "vllm:num_requests_running")
    waiting, _waiting_file = peak_of(gauges, "vllm:num_requests_waiting")
    kv, kv_file = peak_of(gauges, "vllm:kv_cache_usage_perc")

    summary = {
        "generation_tokens_delta": sum_delta(counters, "vllm:generation_tokens_total"),
        "prompt_tokens_delta": sum_delta(counters, "vllm:prompt_tokens_total"),
        "prefix_cache_hits_delta": hits,
        "prefix_cache_queries_delta": queries,
        "prefix_cache_hit_rate": (hits / queries) if queries else None,
        "preemptions_delta": sum_delta(counters, "vllm:num_preemptions_total"),
        "finished_delta": finished,
        "peak_num_requests_running": running,
        "peak_num_requests_running_file": running_file,
        "peak_num_requests_waiting": waiting,
        "peak_kv_cache_usage_perc": kv,
        "peak_kv_cache_usage_perc_file": kv_file,
        "during_polls": len(during_names),
    }
    if queries and hits:
        summary["prefix_cache_note"] = (
            f"{hits:.0f} of {queries:.0f} prompt tokens were prefix cache hits"
        )
    bench = parse_bench(files["bench.txt"]) if "bench.txt" in files else {}
    summary.update(bench)
    tokens_per_second = bench.get("output_tokens_per_second")
    if args.dollars_per_hour is not None and tokens_per_second:
        summary["dollars_per_hour"] = args.dollars_per_hour
        summary["dollars_per_million_tokens"] = (
            (args.dollars_per_hour / 3600.0) / tokens_per_second * 1_000_000
        )

    document = {
        "source": str(Path(args.src)),
        "summary": summary,
        "counters": counters,
        "gauges": gauges,
    }
    out = Path(args.out)
    out.write_text(json.dumps(document, indent=2) + "\n")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
