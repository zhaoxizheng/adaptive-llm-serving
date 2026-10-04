"""Run one controlled tuning phase at a time, with a measured Week 5 handoff."""

from __future__ import annotations

import argparse
import copy
import json
import uuid
from pathlib import Path

from src.common import read_json, write_json
from src.study_contract import (
    fingerprint,
    load_study,
    serve_command,
    validate_week05,
    variant_config,
)
from src.study_runner import load_tokenizer, managed_server, run_case
from src.validate_outputs import validate_suite


def phase_candidates(config, phase):
    tuning = config["tuning"]
    if phase == "representation":
        return [(name, {}) for name in config["variants"]]
    variant = tuning["selected_variant"]
    if variant not in config["variants"]:
        raise ValueError("choose tuning.selected_variant from the representation results")
    overrides = {}
    if phase in {"tokens", "memory", "confirm"}:
        seqs = tuning["selected_max_num_seqs"]
        if seqs not in tuning["max_num_seqs"]:
            raise ValueError("choose selected_max_num_seqs from the sequence sweep")
        overrides["max_num_seqs"] = seqs
    if phase in {"memory", "confirm"}:
        tokens = tuning["selected_max_num_batched_tokens"]
        if tokens not in tuning["max_num_batched_tokens"]:
            raise ValueError("choose selected_max_num_batched_tokens from the token sweep")
        overrides["max_num_batched_tokens"] = tokens
    keys = {
        "sequences": "max_num_seqs",
        "tokens": "max_num_batched_tokens",
        "memory": "gpu_memory_utilization",
    }
    if phase == "memory" and (
        not tuning["memory_pressure_evidence"]
        or not Path(tuning["memory_pressure_evidence"]).is_file()
    ):
        raise ValueError("memory sweep requires saved KV-pressure evidence")
    if phase == "confirm":
        if tuning.get("selected_gpu_memory_utilization") is not None:
            overrides["gpu_memory_utilization"] = tuning["selected_gpu_memory_utilization"]
        return [(variant, overrides), ("bf16", {})]
    key = keys[phase]
    return [(variant, {**overrides, key: value}) for value in tuning[key]]


def load_handoff(config):
    handoff = read_json(config["handoff"])
    study = load_study(config["week05_config"])
    if not handoff.get("ready") or fingerprint(study) != fingerprint(handoff["week05_config"]):
        raise ValueError("Week 5 handoff is incomplete or its workload/SLO contract changed")
    analysis_path = Path(config["handoff"]).with_name("analysis.json")
    if (
        not analysis_path.is_file()
        or fingerprint(read_json(analysis_path)) != handoff["analysis_fingerprint"]
    ):
        raise ValueError("Week 5 handoff is not bound to its saved analysis")
    base = validate_week05(study, execution=True)
    if serve_command(base) != handoff["baseline_server"]["argv"]:
        raise ValueError("Week 4 baseline server changed after the Week 5 experiment")
    return handoff, study, base


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="configs/week06.yaml")
    parser.add_argument(
        "--phase",
        choices=["representation", "sequences", "tokens", "memory", "confirm"],
        default="representation",
    )
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument("--plan", action="store_true")
    modes.add_argument("--run", action="store_true")
    args = parser.parse_args()
    config = load_study(args.config)
    candidates = phase_candidates(config, args.phase)
    study = load_study(config["week05_config"])
    base = validate_week05(study)
    if args.plan:
        print(
            json.dumps(
                {
                    "phase": args.phase,
                    "handoff": config["handoff"],
                    "candidates": [
                        {
                            "variant": name,
                            "overrides": overrides,
                            "argv": serve_command(
                                base,
                                variant=config["variants"][name],
                                tokenizer=config["tokenizer"],
                                overrides=overrides,
                            ),
                        }
                        for name, overrides in candidates
                    ],
                },
                indent=2,
            )
        )
        return
    handoff, study, base = load_handoff(config)
    tokenizer = load_tokenizer(base, config["tokenizer"])
    cases = read_json(config["quality"]["prompts"])
    failures = []
    for index, (name, overrides) in enumerate(candidates):
        root = Path(config["output_root"]) / "raw" / "sessions" / str(uuid.uuid4())
        root.mkdir(parents=True, exist_ok=False)
        variant = config["variants"][name]
        tags = {
            "phase": args.phase,
            "variant": name,
            "overrides": overrides,
            "role": "fallback" if args.phase == "confirm" and index == 1 else "candidate",
            "handoff_fingerprint": fingerprint(handoff),
        }
        write_json(
            root / "experiment.json",
            {"config": config, "tags": tags, "handoff": handoff, "status": "running"},
        )
        selected = variant_config(base, variant, config["tokenizer"])
        argv = serve_command(
            base, variant=variant, tokenizer=config["tokenizer"], overrides=overrides
        )
        try:
            with managed_server(selected, argv, root) as (url, server):
                if server["runtime"] != handoff["baseline_server"]["runtime"]:
                    raise ValueError("runtime/GPU changed from the Week 5 baseline")
                from transformers import AutoConfig

                model_config = AutoConfig.from_pretrained(
                    variant["model"], revision=variant["revision"]
                )
                baseline_model = AutoConfig.from_pretrained(
                    base["model"]["id"], revision=base["model"]["revision"]
                )
                for key in (
                    "model_type",
                    "hidden_size",
                    "num_hidden_layers",
                    "num_attention_heads",
                    "num_key_value_heads",
                    "vocab_size",
                ):
                    if getattr(model_config, key, None) != getattr(baseline_model, key, None):
                        raise ValueError(f"variant changed model architecture: {key}")
                write_json(root / "model-config.json", model_config.to_dict())
                quality = validate_suite(
                    url,
                    base["model"]["served_model_name"],
                    tokenizer,
                    cases,
                    config["quality"],
                    root / "quality.json",
                    max_model_len=base["server"]["max_model_len"],
                )
                if not quality["passed"]:
                    raise ValueError("quality sanity failed; performance phase is skipped")
                mixtures = list(study["mixtures"]) if args.phase == "representation" else ["mixed"]
                for mixture in mixtures:
                    for load_name, rate in handoff["load_points"][mixture].items():
                        for repeat in range(study["load"]["repeats"]):
                            run_case(
                                study,
                                selected,
                                url,
                                server,
                                root,
                                mixture,
                                rate,
                                repeat,
                                tokenizer,
                                tags={**tags, "load_point": load_name},
                            )
                if args.phase == "confirm":
                    soak = copy.deepcopy(study)
                    run_case(
                        soak,
                        selected,
                        url,
                        server,
                        root,
                        "mixed",
                        handoff["load_points"]["mixed"]["boundary"],
                        0,
                        tokenizer,
                        duration=config["confirmation"]["soak_seconds"],
                        tags={**tags, "load_point": "boundary", "soak": True},
                    )
            state = "completed"
        except (RuntimeError, ValueError, OSError, TimeoutError) as error:
            state = "failed"
            failures.append(
                {
                    "session": str(root),
                    "error_type": type(error).__name__,
                    "reason": str(error)[:300],
                }
            )
            write_json(root / "failure.json", failures[-1])
        write_json(
            root / "experiment.json",
            {"config": config, "tags": tags, "handoff": handoff, "status": state},
        )
    if failures:
        raise SystemExit(json.dumps(failures, indent=2))


if __name__ == "__main__":
    main()
