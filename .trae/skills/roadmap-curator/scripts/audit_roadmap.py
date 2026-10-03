from __future__ import annotations

import argparse
import re
import sys
from collections import Counter
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit


WEEK_PATTERN = re.compile(r"week-(\d{2})-(plan|references)\.md$")
LINK_PATTERN = re.compile(r"\[[^]]+\]\(([^)]+)\)")
URL_PATTERN = re.compile(r"https?://[^\s)>]+")
DAY_PATTERN = re.compile(r"^\| Day ([1-7]) \| ([0-9]+(?:\.[0-9]+)?) h \|", re.MULTILINE)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Audit roadmap structure and removals.")
    parser.add_argument("--repo", default=".")
    parser.add_argument("--expected-last-week", type=int)
    parser.add_argument("--forbid", action="append", default=[])
    parser.add_argument("--strict-week", action="append", default=[])
    return parser.parse_args()


def normalize_url(url: str) -> str:
    parts = urlsplit(url.rstrip(".,;"))
    return urlunsplit(
        (parts.scheme.lower(), parts.netloc.lower(), parts.path.rstrip("/"), parts.query, "")
    )


def main() -> None:
    args = parse_args()
    root = Path(args.repo).resolve()
    docs = root / "docs"
    errors: list[str] = []
    if not (root / "README.md").is_file():
        errors.append("missing roadmap entrypoint: README.md")
    if not docs.is_dir():
        errors.append("missing roadmap directory: docs/")
    plans: dict[int, Path] = {}
    references: dict[int, Path] = {}
    for path in docs.glob("week-*.md"):
        match = WEEK_PATTERN.match(path.name)
        if not match:
            continue
        week = int(match.group(1))
        (plans if match.group(2) == "plan" else references)[week] = path

    if not plans:
        errors.append("no weekly plan files found")
    if not references:
        errors.append("no weekly reference files found")
    if set(plans) != set(references):
        errors.append(
            f"plan/reference week mismatch: plans={sorted(plans)}, references={sorted(references)}"
        )
    if plans:
        expected_last = args.expected_last_week or max(plans)
        expected = set(range(1, expected_last + 1))
        if set(plans) != expected:
            errors.append(f"non-contiguous plan weeks: expected={sorted(expected)}, got={sorted(plans)}")
        if set(references) != expected:
            errors.append(
                f"non-contiguous reference weeks: expected={sorted(expected)}, got={sorted(references)}"
            )

    markdown_files = [root / "README.md", *sorted(docs.glob("*.md"))]
    for pattern in args.forbid:
        regex = re.compile(pattern, re.IGNORECASE)
        for path in markdown_files:
            if not path.is_file():
                continue
            for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if regex.search(line):
                    errors.append(
                        f"forbidden term {pattern!r}: {path.relative_to(root)}:{line_number}"
                    )

    for path in markdown_files:
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8")
        for target in LINK_PATTERN.findall(text):
            if target.startswith(("http://", "https://", "mailto:")):
                continue
            local = target.split("#", 1)[0]
            if local and not (path.parent / local).resolve().exists():
                errors.append(f"broken local link: {path.relative_to(root)} -> {target}")

    urls: list[tuple[str, str]] = []
    for path in references.values():
        for url in URL_PATTERN.findall(path.read_text(encoding="utf-8")):
            urls.append((normalize_url(url), str(path.relative_to(root))))
    counts = Counter(url for url, _ in urls)
    for url, count in sorted(counts.items()):
        if count > 1:
            locations = [path for candidate, path in urls if candidate == url]
            errors.append(f"duplicate external URL ({count}): {url} in {locations}")

    for value in args.strict_week:
        week = int(value)
        path = plans.get(week)
        if path is None:
            errors.append(f"strict week {week:02d} has no plan")
            continue
        entries = DAY_PATTERN.findall(path.read_text(encoding="utf-8"))
        days = {int(day) for day, _ in entries}
        hours = sum(float(number) for _, number in entries)
        if days != set(range(1, 8)) or abs(hours - 11.0) > 1e-9:
            errors.append(
                f"week {week:02d} schedule invalid: days={sorted(days)}, hours={hours:g}"
            )

    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        raise SystemExit(1)
    print(
        f"roadmap audit passed: {len(plans)} plan/reference pairs, "
        f"last week {max(plans) if plans else 0}"
    )


if __name__ == "__main__":
    main()
