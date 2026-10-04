"""Deterministic bilingual/task sanity checks, not a model-quality benchmark."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

from src.common import read_json, write_json
from src.openai_stream import http_json
from src.study_contract import fingerprint


def check_output(case, response, expected_prompt_tokens, max_output_tokens):
    errors = []
    try:
        choices, usage = response["choices"], response["usage"]
        if len(choices) != 1 or choices[0]["finish_reason"] not in {"stop", "length"}:
            raise ValueError("invalid choices or finish reason")
        text = choices[0]["text"]
        if not isinstance(text, str) or not text.strip():
            raise ValueError("empty output")
        if (
            usage["prompt_tokens"] != expected_prompt_tokens
            or not 0 < usage["completion_tokens"] <= max_output_tokens
            or usage["total_tokens"] != usage["prompt_tokens"] + usage["completion_tokens"]
        ):
            raise ValueError("usage does not match token contract")
        if "\ufffd" in text or re.search(r"\b(?:nan|infinity)\b", text, re.I):
            errors.append("replacement character or nonfinite marker")
        words = text.split()
        if len(words) >= 20 and len(set(words)) <= 2:
            errors.append("degenerate repetition")
    except (KeyError, TypeError, ValueError, IndexError):
        return {"passed": False, "schema_valid": False, "errors": ["invalid response contract"]}
    check = case["check"]
    task_passed = True
    if check["type"] == "exact":
        task_passed = text.strip() == check["value"]
    elif check["type"] == "contains":
        task_passed = check["value"].casefold() in text.casefold()
    elif check["type"] == "json":
        try:
            task_passed = json.loads(text) == check["value"]
        except ValueError:
            task_passed = False
    else:
        raise ValueError("unknown output check")
    return {
        "passed": not errors and task_passed,
        "schema_valid": not errors,
        "task_passed": task_passed,
        "errors": errors,
        "output": text,
        "usage": usage,
    }


def validate_suite(
    url, model, tokenizer, cases, settings, output, *, max_model_len=4096, request=http_json
):
    if not 20 <= len(cases) <= 50 or len({c["id"] for c in cases}) != len(cases):
        raise ValueError("quality suite must contain 20-50 uniquely named prompts")
    rows = []
    for case in cases:
        prompt = case["prompt"]
        tokens = tokenizer.apply_chat_template(
            [{"role": "user", "content": prompt}], tokenize=True, add_generation_prompt=True
        )
        if case.get("near_context_limit"):
            target = max_model_len - settings["max_output_tokens"]
            padding = tokenizer.encode("Reference filler. ", add_special_tokens=False)
            if not padding:
                raise ValueError("empty boundary padding")
            tokens = (padding * (1 + target // len(padding)))[: target - len(tokens)] + tokens
        payload = {
            "model": model,
            "prompt": tokens,
            "temperature": 0,
            "seed": 42,
            "max_tokens": settings["max_output_tokens"],
            "stream": False,
        }
        try:
            response = request(url + "/v1/completions", payload, timeout=60)
            result = check_output(case, response, len(tokens), settings["max_output_tokens"])
        except Exception as error:
            result = {"passed": False, "schema_valid": False, "errors": [type(error).__name__]}
        rows.append({"id": case["id"], "prompt_sha256": fingerprint(tokens), **result})
        # These are committed synthetic prompts; the saved outputs enable manual review.
        write_json(output, {"status": "partial", "cases": rows})
    ratio = sum(r["passed"] for r in rows) / len(rows)
    suite = {
        "status": "completed",
        "suite_fingerprint": fingerprint(cases),
        "pass_fraction": ratio,
        "cases": rows,
        "passed": all(r["schema_valid"] for r in rows) and ratio >= settings["min_pass_fraction"],
        "manual_review": "pending",
    }
    write_json(output, suite)
    return suite


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--responses", required=True, help="JSON list: case/response/prompt_tokens")
    parser.add_argument("--output", required=True)
    parser.add_argument("--max-output-tokens", type=int, default=64)
    args = parser.parse_args()
    rows = [
        check_output(r["case"], r["response"], r["prompt_tokens"], args.max_output_tokens)
        for r in read_json(Path(args.responses))
    ]
    write_json(args.output, rows)
    if not rows or not all(r["passed"] for r in rows):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
