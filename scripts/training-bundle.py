#!/usr/bin/env python3
"""Prepare private MLX files and assemble checksum-bound candidate metadata."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any


def _load(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return value


def _private_write(path: Path, content: str) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(descriptor, content.encode())
    finally:
        os.close(descriptor)


def prepare(args: argparse.Namespace) -> int:
    bundle = _load(args.bundle)
    manifest = bundle.get("manifest")
    if not isinstance(manifest, dict) or manifest.get("schema") != 1:
        raise ValueError("unsupported training-bundle manifest")
    output = args.output.resolve()
    output.mkdir(mode=0o700)
    counts: dict[str, int] = {}
    for split in ("train", "valid", "test"):
        rows = bundle.get(split)
        if not isinstance(rows, list) or not rows:
            raise ValueError(f"training bundle has no {split} records")
        records: list[str] = []
        for row in rows:
            if not isinstance(row, dict) or not isinstance(row.get("messages"), list):
                raise ValueError(f"invalid {split} record")
            records.append(json.dumps({"messages": row["messages"]}, separators=(",", ":")))
        _private_write(output / f"{split}.jsonl", "\n".join(records) + "\n")
        counts[split] = len(records)
    iterations = max(1, counts["train"] * 5)
    config = "\n".join(
        (
            f'model: "{args.model}"',
            "train: true",
            "fine_tune_type: lora",
            f'data: "{output}"',
            "seed: 42",
            "num_layers: 8",
            "batch_size: 1",
            f"iters: {iterations}",
            "val_batches: -1",
            "grad_accumulation_steps: 8",
            f'adapter_path: "{args.adapter_path.resolve()}"',
            "max_seq_length: 1024",
            "grad_checkpoint: true",
            "mask_prompt: true",
            "lora_parameters:",
            '  keys: ["self_attn.q_proj", "self_attn.v_proj"]',
            "  rank: 8",
            "  scale: 20.0",
            "  dropout: 0.0",
            "",
        )
    )
    _private_write(output / "lora-config.yaml", config)
    _private_write(output / "manifest.json", json.dumps(manifest, sort_keys=True) + "\n")
    print(json.dumps({"iterations": iterations, "counts": counts}, sort_keys=True))
    return 0


def metadata(args: argparse.Namespace) -> int:
    manifest = _load(args.manifest)
    candidate = _load(args.candidate_metrics)
    production = _load(args.production_metrics)
    candidate["production"] = production
    candidate["gates"] = {
        "structured_output": bool(candidate.get("structured_output")),
        "prompt_injection": args.application_tests_passed,
        "primary_fallback": args.application_tests_passed,
        "both_hosts_down": args.application_tests_passed,
    }
    payload = {
        "name": args.name,
        "base_model": manifest["base_model"],
        "sha256": args.sha256,
        "dataset_version": manifest["dataset_version"],
        "example_count": manifest["example_count"],
        "metrics": candidate,
    }
    _private_write(args.output.resolve(), json.dumps(payload, sort_keys=True) + "\n")
    return 0


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser()
    commands = root.add_subparsers(dest="command", required=True)
    prep = commands.add_parser("prepare")
    prep.add_argument("--bundle", type=Path, required=True)
    prep.add_argument("--output", type=Path, required=True)
    prep.add_argument("--model", required=True)
    prep.add_argument("--adapter-path", type=Path, required=True)
    prep.set_defaults(handler=prepare)
    meta = commands.add_parser("metadata")
    meta.add_argument("--manifest", type=Path, required=True)
    meta.add_argument("--candidate-metrics", type=Path, required=True)
    meta.add_argument("--production-metrics", type=Path, required=True)
    meta.add_argument("--name", required=True)
    meta.add_argument("--sha256", required=True)
    meta.add_argument("--output", type=Path, required=True)
    meta.add_argument("--application-tests-passed", action="store_true")
    meta.set_defaults(handler=metadata)
    return root


def main() -> int:
    args = parser().parse_args()
    try:
        return int(args.handler(args))
    except (KeyError, OSError, ValueError, json.JSONDecodeError) as exc:
        raise SystemExit(f"training-bundle: {exc}") from exc


if __name__ == "__main__":
    raise SystemExit(main())
