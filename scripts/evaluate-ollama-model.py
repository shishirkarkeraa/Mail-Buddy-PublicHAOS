#!/usr/bin/env python3
"""Evaluate a versioned Ollama model against Mail-Buddy's held-out JSONL."""

from __future__ import annotations

import argparse
import json
import urllib.error
import urllib.request
from collections import Counter
from pathlib import Path
from typing import Any


def _post(url: str, payload: dict[str, Any], timeout: float) -> dict[str, Any]:
    request = urllib.request.Request(  # noqa: S310 - URL is operator-configured localhost
        f"{url.rstrip('/')}/api/chat",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
        value = json.loads(response.read())
    if not isinstance(value, dict):
        raise ValueError("Ollama returned a non-object envelope")
    return value


def _metrics(expected: list[str], predicted: list[str]) -> dict[str, Any]:
    labels = sorted(set(expected) | set(predicted))
    correct = sum(left == right for left, right in zip(expected, predicted, strict=True))
    f1: list[float] = []
    recall: dict[str, float] = {}
    for label in labels:
        tp = sum(a == label and b == label for a, b in zip(expected, predicted, strict=True))
        fp = sum(a != label and b == label for a, b in zip(expected, predicted, strict=True))
        fn = sum(a == label and b != label for a, b in zip(expected, predicted, strict=True))
        precision = tp / (tp + fp) if tp + fp else 0.0
        item_recall = tp / (tp + fn) if tp + fn else 0.0
        recall[label] = item_recall
        f1.append(
            2 * precision * item_recall / (precision + item_recall)
            if precision + item_recall
            else 0.0
        )
    return {
        "accuracy": correct / len(expected) if expected else 0.0,
        "macro_f1": sum(f1) / len(f1) if f1 else 0.0,
        "category_recall": recall,
        "evaluated_count": len(expected),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:11434")
    parser.add_argument("--model", required=True)
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--timeout", type=float, default=180.0)
    args = parser.parse_args()
    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    schema = manifest["structured_output_schema"]
    allowed = set(schema["$defs"]["Category"]["enum"])
    expected: list[str] = []
    predicted: list[str] = []
    structured = True
    for line in args.dataset.read_text(encoding="utf-8").splitlines():
        row = json.loads(line)
        messages = row["messages"]
        answer = json.loads(messages[-1]["content"])
        envelope = _post(
            args.url,
            {
                "model": args.model,
                "stream": False,
                "format": schema,
                "messages": messages[:-1],
                "options": {"temperature": 0, "num_ctx": 4096, "num_predict": 256},
            },
            args.timeout,
        )
        try:
            decoded = json.loads(envelope["message"]["content"])
            category = decoded["primary_category"]
            if category not in allowed:
                raise ValueError("category outside taxonomy")
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            structured = False
            category = "__invalid__"
        expected.append(answer["primary_category"])
        predicted.append(category)
    result = _metrics(expected, predicted)
    result["structured_output"] = structured
    result["prediction_counts"] = dict(Counter(predicted))
    args.output.write_text(json.dumps(result, sort_keys=True) + "\n", encoding="utf-8")
    args.output.chmod(0o600)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, json.JSONDecodeError, urllib.error.URLError) as exc:
        raise SystemExit(f"evaluate-ollama-model: {exc}") from exc
