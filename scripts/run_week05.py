"""Plan or execute the Week 5 fixed-server load sweep."""

import argparse
import json
import uuid
from pathlib import Path

from src.common import write_json
from src.study_contract import load_study, serve_command, validate_week05
from src.study_runner import load_tokenizer, managed_server, run_case


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="configs/week05.yaml")
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument("--plan", action="store_true")
    modes.add_argument("--run", action="store_true")
    args = parser.parse_args()
    config = load_study(args.config)
    base = validate_week05(config, execution=args.run)
    argv = serve_command(base)
    if args.plan:
        print(
            json.dumps(
                {
                    "server_argv": argv,
                    "mixtures": config["mixtures"],
                    "load": config["load"],
                    "calibration": config["calibration"],
                },
                indent=2,
            )
        )
        return
    root = Path(config["output_root"]) / "raw" / "sessions" / str(uuid.uuid4())
    root.mkdir(parents=True, exist_ok=False)
    write_json(root / "config.json", config)
    tokenizer = load_tokenizer(base)
    with managed_server(base, argv, root) as (url, server):
        for mixture in config["mixtures"]:
            for multiplier in config["load"]["multipliers"]:
                for repeat in range(config["load"]["repeats"]):
                    run_case(
                        config,
                        base,
                        url,
                        server,
                        root,
                        mixture,
                        config["calibration"]["baseline_rps"] * multiplier,
                        repeat,
                        tokenizer,
                    )
    print(f"Evidence saved in {root}; sync and stop the GPU VM before analysis.")


if __name__ == "__main__":
    main()
