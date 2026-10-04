"""Start one named variant in the foreground and retain its resolved configuration."""

import argparse
import json
import time
import uuid
from pathlib import Path

from src.common import write_json
from src.study_contract import load_study, serve_command, validate_week05, variant_config
from src.study_runner import managed_server


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="configs/week06.yaml")
    parser.add_argument("--variant", required=True)
    parser.add_argument("--run", action="store_true", help="otherwise print the command only")
    args = parser.parse_args()
    config = load_study(args.config)
    base = validate_week05(load_study(config["week05_config"]))
    variant = config["variants"][args.variant]
    argv = serve_command(base, variant=variant, tokenizer=config["tokenizer"])
    print(json.dumps(argv))
    if args.run:
        root = Path(config["output_root"]) / "manual" / str(uuid.uuid4())
        root.mkdir(parents=True)
        write_json(root / "resolved-config.json", {"variant": variant, "argv": argv})
        try:
            with managed_server(variant_config(base, variant, config["tokenizer"]), argv, root):
                while True:
                    time.sleep(1)
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
