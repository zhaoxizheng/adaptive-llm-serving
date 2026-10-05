"""Expose one local study's content-free client counters for the Week 20 dashboard."""

import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import re

from src.analyze_platform import grouped_slo


def completed_lines(path):
    if not path.exists():
        return []
    raw = path.read_text()
    # Ignore only the final partial write. Malformed complete records are errors.
    return [
        json.loads(line)
        for line in raw.splitlines(keepends=True)
        if line.endswith("\n") and line.strip()
    ]


def exposition(session):
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", session.name):
        raise ValueError("invalid run ID for metric label")
    state = json.loads((session / "run.json").read_text())
    arrivals = completed_lines(session / "clients-arrivals.jsonl")
    clients = completed_lines(session / "clients.jsonl")
    label = '{run_id="' + session.name + '"}'
    values = {
        "serving_study_offered_requests_total": len(arrivals),
        "serving_study_completed_requests_total": len(clients),
    }
    if clients:
        summary = grouped_slo(clients, state["baseline"]["slo"])["overall"]
        values["serving_study_slo_attainment"] = summary["slo_attainment"]
        if summary["ttft_p99_ms"] is not None:
            values["serving_study_ttft_p99_ms"] = summary["ttft_p99_ms"]
    return "".join(f"{key}{label} {value}\n" for key, value in values.items())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--session", required=True)
    parser.add_argument("--port", type=int, default=8099)
    args = parser.parse_args()
    session = Path(args.session).resolve()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path != "/metrics":
                self.send_error(404)
                return
            try:
                data = exposition(session).encode()
            except (ValueError, OSError, KeyError):
                self.send_error(503, "study evidence unavailable")
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; version=0.0.4")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"http://127.0.0.1:{args.port}/metrics", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
