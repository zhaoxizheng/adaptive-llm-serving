from __future__ import annotations

import argparse
from copy import deepcopy
from pathlib import Path
from typing import Any, Mapping

from src.common import load_yaml, write_json
from src.week03_contract import expand_matrix


def effective_config(
    config: Mapping[str, Any],
    *,
    profile: str,
    backend: str,
    output_root: str | None = None,
    run_log: str | None = None,
    calibrate: bool = False,
) -> dict[str, Any]:
    """Resolve every writable artifact before logging or HF preflight begins."""
    expand_matrix(config, profile)
    if backend not in {"fake", "hf"}:
        raise ValueError(f"unsupported backend: {backend}")
    result = deepcopy(dict(config))
    output = result["output"]
    canonical_root = Path(output["root"])
    if output_root:
        root = Path(output_root)
    elif backend == "fake":
        root = Path(output["simulation_root"])
    elif profile == "smoke":
        root = Path(output["smoke_root"])
    elif profile != "primary":
        root = canonical_root.with_name(f"{canonical_root.name}-{profile}")
    else:
        root = canonical_root
    nonofficial = backend != "hf" or profile != "primary"
    if nonofficial and root.resolve().is_relative_to(canonical_root.resolve()):
        raise ValueError("nonofficial output root must be outside the official root")
    relocated = root.resolve() != canonical_root.resolve()
    if relocated:
        artifacts = {
            "environment_json": "environment.json",
            "dependency_freeze": "dependency-freeze.txt",
            "model_snapshot": "model-snapshot.json",
            "run_log": "logs/week03.log",
            "raw_events_csv": "raw/events.csv",
            "raw_batches_csv": "raw/batches.csv",
            "run_metadata": "raw/run_metadata.json",
            "summary_csv": "summary.csv",
            "analysis_json": "analysis.json",
            "figures_dir": "figures",
            "run_status": "run-status.json",
            "verification_receipt": "verification-receipt.json",
            "report_markdown": "report.md",
        }
        output.update({name: str(root / suffix) for name, suffix in artifacts.items()})
        # Existing calibration is read-only input unless generation was requested.
        if calibrate:
            result["calibration"]["artifact"] = str(root / "calibration.json")
    output["root"] = str(root)
    if run_log:
        if relocated and not Path(run_log).resolve().is_relative_to(root.resolve()):
            raise ValueError("isolated run log must be inside the effective output root")
        output["run_log"] = run_log
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="Prepare isolated Week 3 output paths.")
    parser.add_argument("--config", required=True)
    parser.add_argument("--profile", required=True)
    parser.add_argument("--backend", required=True)
    parser.add_argument("--output-root")
    parser.add_argument("--run-log")
    parser.add_argument("--calibrate", action="store_true")
    args = parser.parse_args()
    config = effective_config(
        load_yaml(args.config),
        profile=args.profile,
        backend=args.backend,
        output_root=args.output_root,
        run_log=args.run_log,
        calibrate=args.calibrate,
    )
    destination = Path(config["output"]["root"]) / "run-config.json"
    write_json(destination, config)
    print(destination)


if __name__ == "__main__":
    main()
