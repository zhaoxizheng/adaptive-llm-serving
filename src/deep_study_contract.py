"""CPU-safe experiment contracts for Weeks 9-12."""

from __future__ import annotations

import copy
import hashlib
import random

from src.common import load_yaml
from src.study_contract import positive
from src.vllm_contract import validate_config


def load_config(path):
    config = load_yaml(path)
    if config.get("schema_version") != 1 or config.get("week") not in (9, 10, 11, 12):
        raise ValueError("expected Week 9-12 schema_version=1")
    source = load_yaml(config["week07_config"])
    base = validate_config(load_yaml(source["baseline_config"]))
    for key in ("timeout_seconds", "event_limit"):
        positive(config[key], key, integer=True)
    if not isinstance(config["seed"], int) or isinstance(config["seed"], bool):
        raise ValueError("seed must be an integer")
    if config["week"] >= 11:
        positive(config["warmup_requests"], "warmup_requests", integer=True)
    workloads = load_yaml(config["workload_config"])["workloads"] if config["week"] != 9 else {}
    scenarios = {}
    for name, entry in config["scenarios"].items():
        workload = copy.deepcopy(workloads[entry["workload"]] if workloads else entry)
        workload["overrides"] = {
            **config.get("server_overrides", {}),
            **workload.get("overrides", {}),
            **entry.get("overrides", {}),
        }
        if "capture" in entry:
            workload["capture"] = entry["capture"]
        if config["week"] >= 11:
            window = workload["capture"]
            for key in ("wait", "warmup", "active"):
                value = window[key]
                if (
                    isinstance(value, bool)
                    or not isinstance(value, int)
                    or value < (key == "active")
                ):
                    raise ValueError("invalid wait/warmup/active schedule")
            if sum(window.values()) > 512:
                raise ValueError("capture exceeds 512 engine steps")
        ids, arrivals = set(), []
        for request in workload["requests"]:
            if request["request_id"] in ids:
                raise ValueError("duplicate request ID")
            ids.add(request["request_id"])
            for key in ("prompt_tokens", "output_tokens"):
                positive(request[key], key, integer=True)
            if (
                request["prompt_tokens"] + request["output_tokens"]
                > base["server"]["max_model_len"]
            ):
                raise ValueError("request exceeds fixed model context")
            shared = request.get("shared_tokens", 0)
            if not isinstance(shared, int) or not 0 <= shared <= request["prompt_tokens"]:
                raise ValueError("invalid shared prefix boundary")
            offset = request.get("arrival_ms", 0)
            if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
                raise ValueError("arrival_ms must be a nonnegative integer")
            arrivals.append(offset)
        if not ids or arrivals != sorted(arrivals):
            raise ValueError("requests must be nonempty and sorted by arrival")
        scenarios[name] = workload
    if not scenarios:
        raise ValueError("no study scenarios")
    return config, source, base, scenarios


def override_command(argv, overrides):
    argv = list(argv)
    booleans = {"enable_prefix_caching", "enable_chunked_prefill", "enforce_eager"}
    integers = {"block_size", "num_gpu_blocks_override", "max_num_batched_tokens"}
    if overrides.keys() - booleans - integers:
        raise ValueError("uncontrolled server override")
    for key, value in overrides.items():
        flag = "--" + key.replace("_", "-")
        if key in booleans:
            if not isinstance(value, bool):
                raise ValueError("boolean override expected")
            argv = [a for a in argv if a not in {flag, "--no-" + flag[2:]}]
            if value:
                argv.append(flag)
            elif key != "enforce_eager":
                argv.append("--no-" + flag[2:])
        else:
            positive(value, key, integer=True)
            if flag in argv:
                argv[argv.index(flag) + 1] = str(value)
            else:
                argv += [flag, str(value)]
    return argv


def make_jobs(scenario, tokenizer, seed):
    # Synthetic valid IDs give exact lengths and reproducible shared-prefix boundaries.
    vocabulary = sorted(set(tokenizer.get_vocab().values()) - set(tokenizer.all_special_ids))
    if len(vocabulary) < 32:
        raise ValueError("tokenizer vocabulary is too small")

    def tokens(family, count):
        derived = int.from_bytes(hashlib.sha256(f"{seed}:{family}".encode()).digest()[:8])
        rng = random.Random(derived)
        return [rng.choice(vocabulary) for _ in range(count)]

    jobs = []
    for index, row in enumerate(scenario["requests"]):
        shared = row.get("shared_tokens", 0)
        prefix = tokens("prefix:" + row.get("prefix_family", "default"), shared)
        suffix = tokens("suffix:" + row["request_id"], row["prompt_tokens"] - shared)
        # Force the first suffix token to differ for the exact/partial A/B pair.
        if suffix:
            suffix[0] = vocabulary[index % len(vocabulary)]
        jobs.append(
            dict(
                request_id=row["request_id"],
                workload=row["request_id"],
                prompt_ids=prefix + suffix,
                output_tokens=row["output_tokens"],
                offset=row.get("arrival_ms", 0) / 1000,
                abort=row.get("abort", False),
            )
        )
    return jobs
