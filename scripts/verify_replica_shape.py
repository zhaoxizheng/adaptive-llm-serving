"""Validate collected worker rank evidence against an actual Pod snapshot."""

import argparse
import json
from pathlib import Path

from src.cluster_contract import verify_rank_evidence
from src.common import write_json


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pod", required=True)
    parser.add_argument("--ranks", required=True)
    parser.add_argument("--gpus", type=int, required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    pod = json.loads(Path(args.pod).read_text())
    evidence = json.loads(Path(args.ranks).read_text())
    verify_rank_evidence(pod, evidence, args.gpus)
    write_json(args.output, dict(status="passed", pod_uid=pod["metadata"]["uid"],
                                 node=pod["spec"]["nodeName"], gpus=args.gpus, evidence=evidence))


if __name__ == "__main__":
    main()
