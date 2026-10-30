#!/usr/bin/env python3
"""Local control room. Reads harness/runs and serves a page on localhost."""

import json
import tarfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from parse_run import parse_bench

HERE = Path(__file__).resolve().parent
RUNS = HERE / "runs"
HOST = "127.0.0.1"
PORT = 8765


def read_text(bundle, name):
    for member in bundle.getmembers():
        if Path(member.name).name == name:
            return bundle.extractfile(member).read().decode("utf-8", "replace")
    return ""


def money(dollars_per_hour, tokens_per_second):
    if not dollars_per_hour or not tokens_per_second:
        return None
    return (dollars_per_hour / 3600.0) / tokens_per_second * 1_000_000


def bench_row(label, text, dollars_per_hour, hit_rate=None):
    parsed = parse_bench(text) if text else {}
    # parse_bench only pulls duration and output tok/s. Pull the percentiles too.
    import re

    def grab(pattern):
        found = re.search(pattern, text or "")
        return float(found.group(1)) if found else None

    tok_s = parsed.get("output_tokens_per_second")
    return {
        "label": label,
        "ttft_p50": grab(r"Median TTFT \(ms\):\s+([0-9.]+)"),
        "ttft_p99": grab(r"P99 TTFT \(ms\):\s+([0-9.]+)"),
        "itl_p50": grab(r"Median ITL \(ms\):\s+([0-9.]+)"),
        "itl_p99": grab(r"P99 ITL \(ms\):\s+([0-9.]+)"),
        "tok_s": tok_s,
        "duration_s": parsed.get("benchmark_duration_s"),
        "dollars_per_million": money(dollars_per_hour, tok_s),
        "hit_rate": hit_rate,
    }



def sum_label(document, name, label):
    total = 0.0
    found = False
    for row in document.get("counters", {}).values():
        if row["name"] == name:
            found = True
            if label is None or row["labels"].get(label[0]) == label[1]:
                total += row["delta"]
    return total if found else None


def detail_for(document):
    waiting = {}
    for row in document.get("gauges", {}).values():
        if row["name"] == "vllm:num_requests_waiting_by_reason":
            waiting[row["labels"].get("reason", "?")] = row.get("peak")
    return {
        "local_compute": sum_label(document, "vllm:prompt_tokens_by_source_total", ("source", "local_compute")),
        "local_cache_hit": sum_label(document, "vllm:prompt_tokens_by_source_total", ("source", "local_cache_hit")),
        "external_kv": sum_label(document, "vllm:prompt_tokens_by_source_total", ("source", "external_kv_transfer")),
        "cached_device": sum_label(document, "vllm:prompt_tokens_cached_by_source_total", ("source", "device")),
        "cached_host": sum_label(document, "vllm:prompt_tokens_cached_by_source_total", ("source", "host")),
        "preemptions": sum_label(document, "vllm:num_preemptions_total", None),
        "reject_reqs": sum_label(document, "vllm:admission_rejections_total", ("reason", "max_num_queued_reqs")),
        "reject_tokens": sum_label(document, "vllm:admission_rejections_total", ("reason", "max_num_queued_tokens")),
        "waiting_capacity": waiting.get("capacity"),
        "waiting_deferred": waiting.get("deferred"),
        "finished_length": (document.get("summary") or {}).get("finished_delta", {}).get("length"),
    }


def gauges_for(document):
    running = []
    kv = []
    for row in document.get("gauges", {}).values():
        samples = row.get("samples") or []
        if row["name"] == "vllm:num_requests_running" and samples:
            running = samples
        if row["name"] == "vllm:kv_cache_usage_perc" and samples:
            kv = samples
    return {"running": running, "kv": kv}


def pretty_when(name: str) -> str:
    import re
    months = "Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split()
    found = re.match(r"(\d{4})-(\d{2})-(\d{2})(?:-(\d{2})(\d{2})\d{2})?", name)
    if not found:
        return name
    year, month, day, hour, minute = found.groups()
    label = f"{int(day)} {months[int(month) - 1]} {year}"
    if hour:
        label += f", {hour}:{minute}"
    return label


def load_runs():
    runs = []
    if not RUNS.exists():
        return runs
    for folder in sorted(RUNS.iterdir()):
        summary_path = folder / "run.json"
        if not summary_path.exists():
            continue
        document = json.loads(summary_path.read_text())
        summary = document["summary"]
        price = summary.get("dollars_per_hour")
        hit = summary.get("prefix_cache_hit_rate")
        tarball = folder / "metrics-run.tgz"
        discard = bench = ""
        if tarball.exists():
            with tarfile.open(tarball, "r:gz") as bundle:
                discard = read_text(bundle, "discard.txt")
                bench = read_text(bundle, "bench.txt")
        rows = []
        if discard:
            rows.append(bench_row("First bench, full prefill", discard, price))
        if bench:
            label = "Second bench, prefix cache hit" if hit else "Second bench"
            rows.append(bench_row(label, bench, price, hit))
        if not rows:
            rows.append(
                {
                    "label": "Timed bench",
                    "ttft_p50": None,
                    "ttft_p99": None,
                    "itl_p50": None,
                    "itl_p99": None,
                    "tok_s": summary.get("output_tokens_per_second"),
                    "duration_s": summary.get("benchmark_duration_s"),
                    "dollars_per_million": summary.get("dollars_per_million_tokens"),
                    "hit_rate": hit,
                }
            )
        runs.append(
            {
                "id": folder.name,
                "when": pretty_when(folder.name),
                "price": price,
                "rows": rows,
                "gauges": gauges_for(document),
                "peak_running": summary.get("peak_num_requests_running"),
                "peak_waiting": summary.get("peak_num_requests_waiting"),
                "peak_kv": summary.get("peak_kv_cache_usage_perc"),
                "preemptions": summary.get("preemptions_delta"),
                "detail": detail_for(document),
            }
        )
    return runs


PAGE = """<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>Harness</title>
<style>
  body { font: 14px/20px -apple-system, BlinkMacSystemFont, sans-serif; margin: 28px 32px 48px; color: #e6e6e6; background: #181818; }
  h1 { font-size: 22px; font-weight: 590; margin: 0 0 6px; letter-spacing: -0.02em; }
  h2 { font-size: 16px; font-weight: 590; margin: 0 0 12px; }
  p { margin: 0 0 20px; color: #9a9a9a; max-width: 760px; }
  section { margin: 0 0 36px; }
  table { border-collapse: collapse; width: 100%; background: #202020; border: 1px solid #2e2e2e; border-radius: 8px; overflow: hidden; margin: 0 0 12px; }
  caption { caption-side: bottom; text-align: left; color: #9a9a9a; font-size: 12px; padding: 8px 12px 0; }
  th { color: #9a9a9a; font-weight: 500; font-size: 12px; }
  th, td { text-align: right; padding: 8px 12px; border-bottom: 1px solid #2a2a2a; }
  th:first-child, td:first-child { text-align: left; }
  td.metric { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 12px; }
  .charts { display: flex; gap: 16px; flex-wrap: wrap; }
  figure { margin: 0; background: #202020; border: 1px solid #2e2e2e; border-radius: 8px; padding: 12px 16px 16px; }
  figcaption { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 12px; color: #e6e6e6; margin-bottom: 8px; }
  svg { display: block; }
</style>
</head>
<body>
<h1>Harness</h1>
<p>One block per run: bench table from vllm bench serve, series table from the /metrics diff.</p>
<div id="runs"></div>
<script>
const RUNS = __RUNS__;
function fmt(value, suffix) {
  if (value === null || value === undefined) return "—";
  const n = Math.round(value * 10) / 10;
  return suffix ? n + suffix : String(n);
}
function money(value) {
  if (value === null || value === undefined) return "—";
  return "$" + value.toFixed(2);
}
function pct(value) {
  if (value === null || value === undefined) return "—";
  return Math.round(value * 1000) / 10 + "%";
}
function num(value) {
  if (value === null || value === undefined) return "—";
  return String(value);
}
function line(samples, map) {
  const pts = samples.map((s, i) => [i, map(s.value)]);
  if (!pts.length) return "";
  const w = 420, h = 140, pad = 16;
  const max = Math.max(...pts.map(p => p[1]), 1);
  const d = pts.map((p, i) => {
    const x = pad + (i / Math.max(pts.length - 1, 1)) * (w - pad * 2);
    const y = h - pad - (p[1] / max) * (h - pad * 2);
    return (i ? "L" : "M") + x.toFixed(1) + " " + y.toFixed(1);
  }).join(" ");
  return `<svg width="${w}" height="${h}" viewBox="0 0 ${w} ${h}"><path d="${d}" fill="none" stroke="#7aa2f7" stroke-width="2"/></svg>`;
}
function series(name, value) {
  return `<tr><td class="metric">${name}</td><td>${num(value)}</td></tr>`;
}
document.getElementById("runs").innerHTML = RUNS.slice().reverse().map(run => {
  const d = run.detail || {};
  const running = run.gauges.running || [];
  const kv = run.gauges.kv || [];
  const benches = `<table>
    <tr><th>Bench</th><th>TTFT p50</th><th>TTFT p99</th><th>ITL p50</th><th>ITL p99</th><th>tok/s</th><th>$ / M</th><th>prefix hit rate</th></tr>
    ${run.rows.map(row => `<tr><td>${row.label}</td>
      <td>${fmt(row.ttft_p50, " ms")}</td><td>${fmt(row.ttft_p99, " ms")}</td>
      <td>${fmt(row.itl_p50, " ms")}</td><td>${fmt(row.itl_p99, " ms")}</td>
      <td>${fmt(row.tok_s)}</td><td>${money(row.dollars_per_million)}</td><td>${pct(row.hit_rate)}</td></tr>`).join("")}
    </table>`;
  const metrics = `<table>
    <tr><th>Series</th><th>This run</th></tr>
    ${series('vllm:prompt_tokens_by_source_total{source="local_compute"}', d.local_compute)}
    ${series('vllm:prompt_tokens_by_source_total{source="local_cache_hit"}', d.local_cache_hit)}
    ${series('vllm:prompt_tokens_cached_by_source_total{source="device"}', d.cached_device)}
    ${series("vllm:num_preemptions_total", d.preemptions)}
    ${series('vllm:num_requests_waiting_by_reason{reason="capacity"} peak', d.waiting_capacity)}
    ${series('vllm:num_requests_waiting_by_reason{reason="deferred"} peak', d.waiting_deferred)}
    ${series('vllm:admission_rejections_total{reason="max_num_queued_reqs"}', d.reject_reqs)}
    ${series('vllm:admission_rejections_total{reason="max_num_queued_tokens"}', d.reject_tokens)}
    </table>`;
  const charts = `<div class="charts">
    <figure><figcaption>vllm:num_requests_running</figcaption>${line(running, v => v)}</figure>
    <figure><figcaption>vllm:kv_cache_usage_perc</figcaption>${line(kv, v => v * 100)}</figure>
    </div>`;
  return `<section><h2>${run.when}</h2>${benches}${metrics}${charts}</section>`;
}).join("");
</script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path not in ("/", "/index.html"):
            self.send_error(404)
            return
        body = PAGE.replace("__RUNS__", json.dumps(load_runs())).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        return


def main():
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"http://{HOST}:{PORT}")
    server.serve_forever()


if __name__ == "__main__":
    main()
