"""CPU-safe contracts, synthetic token traces and statistics for Weeks 13-16."""

from __future__ import annotations

import copy
import hashlib
import math
import random
import re

from src.common import load_yaml
from src.study_contract import VLLM_COMMIT, positive


def validate_baseline(base, *, frozen=False):
    engine = base["engine"]
    gpus = base["gpus_per_replica"]
    positive(gpus, "gpus_per_replica", integer=True)
    if engine["tensor_parallel_size"] != gpus or engine["pipeline_parallel_size"] != 1:
        raise ValueError("replica must be 1 Pod / 1 node / G GPUs / TP=G / PP=1")
    if base["vllm_commit"] != VLLM_COMMIT:
        raise ValueError("vLLM source revision drift")
    if not re.fullmatch(r"[0-9a-f]{40}", base["model"]["revision"]):
        raise ValueError("immutable model revision required")
    for key in ("ttft_ms", "tpot_ms"):
        positive(base["slo"][key], key)
    if not 0 <= base["slo"]["max_error_rate"] <= 1:
        raise ValueError("invalid error budget")
    if frozen and (not base["frozen"] or not base.get("selection_evidence")):
        raise ValueError("freeze the measured Week 14 selection before cluster experiments")
    return base


def load_experiment(filename):
    cfg = load_yaml(filename)
    base = validate_baseline(load_yaml(cfg["baseline"]))
    if cfg.get("schema_version") != 1 or cfg.get("week") not in (13, 14, 15, 16):
        raise ValueError("expected Week 13-16 schema_version=1")
    for key in ("requests", "repeats", "max_inflight", "warmup_requests", "prefix_families"):
        positive(cfg[key], key, integer=True)
    if cfg["requests"] > 10000 or cfg["max_inflight"] > 512 or cfg["repeats"] < 3:
        raise ValueError("require >=3 repeats, <=10000 requests and <=512 inflight")
    positive(cfg["timeout_seconds"], "timeout_seconds")
    for shape in cfg["shapes"].values():
        for key in ("prompt_tokens", "output_tokens"):
            positive(shape[key], key, integer=True)
        if sum(shape.values()) > base["engine"]["max_model_len"]:
            raise ValueError("workload exceeds model context")
    if not 0 < cfg["shared_tokens"] < cfg["shapes"]["shared"]["prompt_tokens"]:
        raise ValueError("shared prefix must leave a nonempty unique suffix")
    # YAML 1.1 parses bare 'off' as False; normalize at the input boundary.
    for case in cfg["cases"].values():
        if case.get("cache") is False:
            case["cache"] = "off"
        if case.get("cache") not in ("off", "cold", "warm", "low_overlap"):
            raise ValueError("unknown cache treatment")
    return cfg, base


def engine_command(base, overrides=None, *, host="127.0.0.1", port=8000):
    values = copy.deepcopy(base["engine"])
    overrides = overrides or {}
    if overrides.keys() - values.keys():
        raise ValueError("uncontrolled engine override")
    values.update(overrides)
    model = base["model"]
    argv = ["vllm", "serve", model["id"], "--revision", model["revision"],
            "--served-model-name", model["served_model_name"], "--dtype", model["dtype"],
            "--host", host, "--port", str(port)]
    for key, value in values.items():
        flag = "--" + key.replace("_", "-")
        if isinstance(value, bool):
            if value:
                argv.append(flag)
            elif key != "enforce_eager":
                argv.append("--no-" + flag[2:])
        elif isinstance(value, dict):
            import json
            argv += [flag, json.dumps(value, sort_keys=True)]
        else:
            positive(value, key)
            argv += [flag, str(value)]
    return argv


def trace_jobs(cfg, tokenizer, workload, rate, repeat, *, low_overlap=False, warmup=False):
    """Token IDs avoid text retokenization drift; independent RNGs preserve paired arrivals."""
    positive(rate, "offered_rps")
    vocab = sorted(set(tokenizer.get_vocab().values()) - set(tokenizer.all_special_ids))
    count = cfg["warmup_requests"] if warmup else cfg["requests"]
    families = cfg["prefix_families"]
    if len(vocab) < count + families + 4:
        raise ValueError("vocabulary too small for disjoint leading token identities")
    arrivals = random.Random(cfg["seed"] + repeat)
    jobs, offset = [], 0.0
    for index in range(count):
        kind = "short" if warmup else ("long" if index % 4 == 0 else "short") if workload in {"mixed", "burst"} else workload
        shape = cfg["shapes"][kind]
        family = index % families
        leading = vocab[-1] if warmup else vocab[families + index] if low_overlap or kind != "shared" else vocab[family]
        seed = int.from_bytes(hashlib.sha256(f"{cfg['seed']}:{family}".encode()).digest()[:8])
        shared_rng = random.Random(seed)
        suffix_rng = random.Random(cfg["seed"] + index + (100000 if warmup else 0))
        shared = cfg["shared_tokens"] if kind == "shared" and not low_overlap and not warmup else 1
        prefix = [leading] + [shared_rng.choice(vocab) for _ in range(shared - 1)]
        ids = prefix + [suffix_rng.choice(vocab) for _ in range(shape["prompt_tokens"] - shared)]
        jobs.append(dict(request_id=f"r{index:06d}", workload=kind, prompt_ids=ids,
                         output_tokens=shape["output_tokens"], offset=offset, prefix_family=family))
        multiplier = 4 if workload == "burst" and count // 3 <= index < 2 * count // 3 else 1
        offset += arrivals.expovariate(rate * multiplier)
    return jobs


def percentile(values, q):
    values = sorted(v for v in values if v is not None and math.isfinite(v))
    if not values:
        return None
    pos = (len(values) - 1) * q
    low, high = math.floor(pos), math.ceil(pos)
    return values[low] + (values[high] - values[low]) * (pos - low)


def summarize(records, slo, window_seconds):
    positive(window_seconds, "window_seconds")
    if not records:
        raise ValueError("empty client evidence")
    success = [r for r in records if r["status"] == "success"]
    good = [r for r in success if r["ttft_ms"] is not None and r["ttft_ms"] <= slo["ttft_ms"]
            and (r["output_tokens"] == 1 or (r["tpot_ms"] is not None and r["tpot_ms"] <= slo["tpot_ms"]))]
    return dict(requests=len(records), successes=len(success), errors=len(records) - len(success),
                error_rate=1 - len(success) / len(records), slo_attainment=len(good) / len(records),
                goodput_rps=len(good) / window_seconds,
                output_tokens_per_second=sum(r["output_tokens"] for r in success) / window_seconds,
                window_seconds=window_seconds, p99_exploratory=len(success) < 1000,
                **{f"{metric}_p{int(q * 100)}": percentile([r.get(metric) for r in success], q)
                   for metric in ("ttft_ms", "tpot_ms", "arrival_lag_ms") for q in (.5, .95, .99)})


def counter_delta(before, after, name):
    """Compare identical per-series identities before summing; never mask a reset."""
    pattern = re.compile(r"^(" + re.escape(name) + r"(?:\{.*\})?)\s+(\S+)(?:\s+\S+)?$")
    def parse(text):
        result = {}
        for line in text.splitlines():
            match = pattern.match(line)
            if match:
                value = float(match[2])
                if not math.isfinite(value) or value < 0 or match[1] in result:
                    raise ValueError("invalid counter series")
                result[match[1]] = value
        return result
    left, right = parse(before), parse(after)
    if not left or left.keys() != right.keys():
        return dict(status="unavailable", value=None)
    changes = [right[k] - left[k] for k in left]
    if any(v < 0 for v in changes):
        return dict(status="reset", value=None)
    return dict(status="observed", value=sum(changes))
