import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading
from types import SimpleNamespace

import pytest

from scripts.run_platform_study import injected_fault
from src.platform_contract import load_study
from src.platform_workload import make_jobs, matrix, run_requests


class Tokenizer:
    all_special_ids = [0]

    def get_vocab(self):
        return {str(i): i for i in range(4096)}


def config():
    return load_study("configs/week18-routing.yaml")


def test_paired_workloads_and_prefix_identity():
    cfg, _, _, _ = config()
    shared = make_jobs(cfg, Tokenizer(), "shared_prefix", 1, 0)
    low = make_jobs(cfg, Tokenizer(), "low_sharing", 1, 0)
    assert [j["offset"] for j in shared] == [j["offset"] for j in low]
    assert shared[0]["prompt_ids"][:512] == shared[4]["prompt_ids"][:512]
    assert low[0]["prompt_ids"][0] != low[4]["prompt_ids"][0]
    assert shared[0]["prompt_ids"][512:] != shared[4]["prompt_ids"][512:]
    hot = make_jobs(cfg, Tokenizer(), "hot_prefix", 1, 0)
    assert sum(j["prefix_family"] == 0 for j in hot) > 80
    cells = matrix(cfg)
    assert len(cells) == 3 * 2 * 4 * 2 * 2
    assert {c["case"] for c in cells} == {"load_aware", "precise_prefix"}
    assert cells[0]["case"] != next(c for c in cells if c["repeat"] == 1)["case"]


def test_no_sensitive_content_is_persisted(tmp_path, monkeypatch):
    cfg, base, _, _ = config()
    cfg = {**cfg, "requests": 1}
    jobs = make_jobs(cfg, Tokenizer(), "short", 1, 0)

    def fake_request(*args, **kwargs):
        return dict(
            request_id=jobs[0]["request_id"],
            workload="short",
            status="success",
            output_tokens=32,
            ttft_ms=1,
            tpot_ms=1,
            scheduled_at=1,
            completed_at=2,
            prompt_ids=[1, 2],
            output_excerpt="DO NOT SAVE",
            prompt_sha256="secret-key",
        )

    monkeypatch.setattr("src.platform_workload.request", fake_request)
    rows, _ = run_requests(cfg, base, jobs, tmp_path / "clients.jsonl")
    data = (tmp_path / "clients.jsonl").read_text()
    assert (
        "DO NOT SAVE" not in data
        and "prompt_sha256" not in data
        and "prompt_ids" not in data
    )
    assert rows[0]["prefix_family"] == 0


def test_fault_restores_owned_field_on_client_error(tmp_path, monkeypatch):
    cfg, _, lab, lock = config()
    calls = []
    deployment = dict(
        kind="Deployment",
        apiVersion="apps/v1",
        metadata=dict(
            name="epp",
            namespace=lab["namespace"],
            labels={"app.kubernetes.io/part-of": "serving-study"},
        ),
        spec=dict(replicas=1),
    )
    kube = SimpleNamespace(
        lab=lab,
        get=lambda *args: deployment,
        call=lambda *args, **kwargs: calls.append(args),
    )
    monkeypatch.setattr("scripts.run_platform_study.snapshot", lambda *args: None)
    with pytest.raises(RuntimeError, match="client failed"):
        with injected_fault(kube, cfg, lock, "epp_unavailable", tmp_path):
            raise RuntimeError("client failed")
    assert '"replicas": 0' in calls[0][-1]
    assert '"replicas": 1' in calls[-1][-1]
    assert (tmp_path / "fault-restored.json").is_file()


def test_templates_fail_before_cluster_access(tmp_path):
    from src.platform_contract import validate_versions

    cfg, base, lab, lock = config()
    with pytest.raises(ValueError, match="freeze"):
        validate_versions(cfg, base, lab, lock)


def test_live_metrics_count_arrivals_and_keep_missing_latency_unknown(tmp_path):
    from scripts.serve_platform_metrics import exposition

    _, base, _, _ = config()
    (tmp_path / "run.json").write_text(json.dumps(dict(baseline=base)))
    (tmp_path / "clients-arrivals.jsonl").write_text('{"request_id":"r1"}\n')
    (tmp_path / "clients.jsonl").write_text('{"partial":')
    output = exposition(tmp_path)
    assert "serving_study_offered_requests_total" in output
    assert " 1\n" in output
    assert "serving_study_ttft_p99_ms" not in output


def test_real_local_sse_smoke_and_arrival_journal(tmp_path):
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            assert self.headers["X-Request-ID"] == payload["request_id"]
            events = [
                dict(choices=[dict(index=0, text="synthetic", finish_reason=None)]),
                dict(
                    choices=[dict(index=0, text="", finish_reason="length")],
                    usage=dict(
                        prompt_tokens=len(payload["prompt"]),
                        completion_tokens=payload["max_tokens"],
                    ),
                ),
            ]
            data = (
                "".join("data: " + json.dumps(e) + "\n\n" for e in events)
                + "data: [DONE]\n\n"
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        cfg, base, _, _ = config()
        cfg = {
            **cfg,
            "requests": 1,
            "endpoint": f"http://127.0.0.1:{server.server_port}/v1/completions",
        }
        rows, summary = run_requests(
            cfg,
            base,
            make_jobs(cfg, Tokenizer(), "short", 1, 0),
            tmp_path / "clients.jsonl",
        )
        assert summary["successes"] == 1
        assert rows[0]["usage"]["completion_tokens"] == 32
        assert "synthetic" not in (tmp_path / "clients.jsonl").read_text()
        assert len((tmp_path / "clients-arrivals.jsonl").read_text().splitlines()) == 1
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
