from __future__ import annotations

import argparse
from pathlib import Path

import matplotlib.pyplot as plt
import pandas as pd


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Plot the Week 1 KV cache results.")
    parser.add_argument("--input", default="results/week01/raw/kv_cache.csv")
    parser.add_argument("--output-dir", default="results/week01/figures")
    return parser.parse_args()


def plot_metric(frame: pd.DataFrame, metric: str, ylabel: str, output: Path) -> None:
    summary = (
        frame.groupby(["prompt_tokens", "output_tokens", "use_cache"], as_index=False)[metric]
        .median()
        .sort_values(["prompt_tokens", "use_cache", "output_tokens"])
    )
    _, axis = plt.subplots(figsize=(9, 5))
    for (prompt_tokens, use_cache), group in summary.groupby(["prompt_tokens", "use_cache"]):
        cache_label = "cache on" if use_cache else "cache off"
        axis.plot(
            group["output_tokens"],
            group[metric],
            marker="o",
            label=f"prompt={prompt_tokens}, {cache_label}",
        )
    axis.set_xlabel("Output tokens")
    axis.set_ylabel(ylabel)
    axis.grid(alpha=0.3)
    axis.legend()
    plt.tight_layout()
    plt.savefig(output, dpi=160)
    plt.close()


def main() -> None:
    args = parse_args()
    frame = pd.read_csv(args.input)
    frame["use_cache"] = frame["use_cache"].astype(str).str.lower().eq("true")
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    plot_metric(
        frame,
        "total_generation_ms",
        "Median total generation time (ms)",
        output_dir / "generation-time.png",
    )
    plot_metric(
        frame,
        "output_tokens_per_second",
        "Median output tokens/s",
        output_dir / "output-throughput.png",
    )
    print(f"Wrote figures to {output_dir}")


if __name__ == "__main__":
    main()

