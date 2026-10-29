#!/usr/bin/env python3
"""Run capture.sh on the pod and write run.json on the Mac.

The bench runs on the pod against localhost. This process only starts that
script over SSH and brings the tarball back.
"""

import argparse
import base64
import os
import pty
import re
import select
import subprocess
import sys
import time
from pathlib import Path

BEGIN = "BEGIN_HARNESS_TGZ"
END = "END_HARNESS_TGZ"
ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b\][^\x07]*\x07")


def strip_ansi(text: str) -> str:
    return ANSI_RE.sub("", text).replace("\r", "")


def extract_tarball(raw: str) -> tuple[str, bytes]:
    clean = strip_ansi(raw)
    status_match = re.search(r"CAPTURE_STATUS=(\S+)", clean)
    status = status_match.group(1) if status_match else "missing"
    start = clean.find(BEGIN)
    stop = clean.find(END)
    if start < 0 or stop < 0 or stop < start:
        raise RuntimeError("the pod did not return a tarball")
    body = clean[start + len(BEGIN) : stop]
    payload = "".join(
        line.strip()
        for line in body.splitlines()
        if re.fullmatch(r"[A-Za-z0-9+/=]+", line.strip())
    )
    return status, base64.b64decode(payload)


def run_session(command: list[str], remote_lines: str, timeout_s: int) -> str:
    master, slave = pty.openpty()
    proc = subprocess.Popen(
        command,
        stdin=slave,
        stdout=slave,
        stderr=slave,
        close_fds=True,
    )
    os.close(slave)
    chunks = []
    sent = False
    deadline = time.monotonic() + timeout_s
    try:
        while time.monotonic() < deadline:
            if proc.poll() is not None and not select.select([master], [], [], 0)[0]:
                break
            readable, _, _ = select.select([master], [], [], 0.5)
            if master in readable:
                try:
                    data = os.read(master, 65536)
                except OSError:
                    break
                if not data:
                    break
                chunks.append(data)
                if not sent and "workspace#" in strip_ansi(b"".join(chunks).decode("utf-8", "replace")):
                    os.write(master, remote_lines.encode())
                    sent = True
            if sent and END.encode() in b"".join(chunks):
                # Let the remote shell exit after the marker.
                time.sleep(0.5)
                break
        if not sent:
            raise RuntimeError("SSH never reached a pod prompt")
        if END.encode() not in b"".join(chunks):
            raise RuntimeError("timed out waiting for the tarball")
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
        os.close(master)
    return b"".join(chunks).decode("utf-8", "replace")


def remote_script(capture: str, api_key: str, dollars_note: str) -> str:
    encoded = base64.b64encode(capture.encode()).decode()
    wrapped = "\n".join(encoded[i : i + 76] for i in range(0, len(encoded), 76))
    return (
        "base64 -d > /tmp/capture.sh << 'END_SCRIPT'\n"
        f"{wrapped}\n"
        "END_SCRIPT\n"
        "chmod +x /tmp/capture.sh\n"
        f"export OPENAI_API_KEY='{api_key}'\n"
        "bash /tmp/capture.sh\n"
        "exit\n"
    )


def main():
    here = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser()
    parser.add_argument("--ssh", required=True, help="user@ssh.runpod.io from the connect panel")
    parser.add_argument("--key", default=str(Path.home() / ".ssh" / "id_runpod"))
    parser.add_argument("--dollars-per-hour", type=float, default=None)
    parser.add_argument("--timeout", type=int, default=1200)
    args = parser.parse_args()

    api_key = os.environ.get("OPENAI_API_KEY", "")
    if not api_key:
        raise SystemExit("set OPENAI_API_KEY in this shell first")
    capture = (here / "capture.sh").read_text()
    stamp = time.strftime("%Y-%m-%d-%H%M%S")
    out_dir = here / "runs" / stamp
    out_dir.mkdir(parents=True)

    command = [
        "ssh",
        "-tt",
        "-i",
        args.key,
        "-o",
        "BatchMode=yes",
        "-o",
        "StrictHostKeyChecking=accept-new",
        args.ssh,
    ]
    raw = run_session(command, remote_script(capture, api_key, ""), args.timeout)
    log = raw.replace(api_key, "***")
    (out_dir / "session.log").write_text(log)
    status, blob = extract_tarball(raw)
    tarball = out_dir / "metrics-run.tgz"
    tarball.write_bytes(blob)
    summary_path = out_dir / "run.json"
    cmd = [
        sys.executable,
        str(here / "parse_run.py"),
        str(tarball),
        "-o",
        str(summary_path),
    ]
    if args.dollars_per_hour is not None:
        cmd.extend(["--dollars-per-hour", str(args.dollars_per_hour)])
    subprocess.check_call(cmd)
    print(f"wrote {summary_path}")
    if status != "ok":
        raise SystemExit(f"capture status {status}: the run does not count")


if __name__ == "__main__":
    main()
