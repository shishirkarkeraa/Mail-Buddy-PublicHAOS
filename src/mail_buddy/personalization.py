"""Small, local text model trained from explicit mailbox-owner feedback.

The learner is deliberately dependency-free so it can train on a Raspberry Pi.
It is an ensemble companion to Ollama, not a replacement for the main model.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from mail_buddy.contracts import TAXONOMY_VERSION, Category, EmailMetadata
from mail_buddy.security import redact_text

_TOKEN = re.compile(r"[a-z][a-z0-9_-]{2,}", re.IGNORECASE)
_MAX_TOKENS_PER_EXAMPLE = 1_500


def build_feature_text(metadata: EmailMetadata, content: str = "") -> str:
    """Create the bounded, redacted representation stored for local training."""

    return redact_text(
        "\n".join(
            (
                f"sender_domain {metadata.sender_domain}",
                f"subject {metadata.subject}",
                f"snippet {metadata.snippet}",
                f"content {content}",
            )
        )[:12_000]
    )


@dataclass(frozen=True)
class TrainingExample:
    message_id: str
    sender_key: str
    text: str
    category: Category


@dataclass(frozen=True)
class PersonalPrediction:
    category: Category
    confidence: float
    margin: float
    model_name: str


@dataclass(frozen=True)
class TrainingResult:
    artifact: dict[str, Any]
    accuracy: float
    macro_f1: float
    evaluated_count: int
    category_recall: dict[str, float]
    folds: dict[str, int]


def _tokens(text: str) -> list[str]:
    tokens = [match.group(0).lower() for match in _TOKEN.finditer(text)]
    return tokens[:_MAX_TOKENS_PER_EXAMPLE]


def train(examples: list[TrainingExample]) -> dict[str, Any]:
    class_documents: Counter[str] = Counter()
    class_totals: Counter[str] = Counter()
    class_tokens: dict[str, Counter[str]] = {}
    vocabulary: set[str] = set()
    for example in examples:
        category = example.category.value
        words = _tokens(example.text)
        if not words:
            continue
        class_documents[category] += 1
        counts = class_tokens.setdefault(category, Counter())
        counts.update(words)
        class_totals[category] += len(words)
        vocabulary.update(words)
    return {
        "schema": 1,
        "taxonomy_version": TAXONOMY_VERSION,
        "trained_at": datetime.now(UTC).isoformat(),
        "example_count": sum(class_documents.values()),
        "vocabulary_size": len(vocabulary),
        "class_documents": dict(class_documents),
        "class_totals": dict(class_totals),
        "class_tokens": {category: dict(counts) for category, counts in class_tokens.items()},
    }


def predict(artifact: dict[str, Any], text: str, model_name: str) -> PersonalPrediction | None:
    try:
        documents = {
            str(key): int(value) for key, value in dict(artifact["class_documents"]).items()
        }
        totals = {str(key): int(value) for key, value in dict(artifact["class_totals"]).items()}
        token_maps = {
            str(category): {str(key): int(value) for key, value in dict(counts).items()}
            for category, counts in dict(artifact["class_tokens"]).items()
        }
        vocabulary_size = max(1, int(artifact["vocabulary_size"]))
    except (KeyError, TypeError, ValueError):
        return None
    words = _tokens(text)
    total_documents = sum(documents.values())
    if not words or total_documents < 2 or len(documents) < 2:
        return None
    scores: dict[str, float] = {}
    for category, document_count in documents.items():
        denominator = totals.get(category, 0) + vocabulary_size
        counts = token_maps.get(category, {})
        score = math.log(document_count / total_documents)
        for word in words:
            score += math.log((counts.get(word, 0) + 1) / denominator)
        scores[category] = score
    ordered = sorted(scores.items(), key=lambda item: item[1], reverse=True)
    if not ordered:
        return None
    maximum = ordered[0][1]
    weights = [(category, math.exp(score - maximum)) for category, score in ordered]
    normalizer = sum(weight for _, weight in weights)
    probabilities = [(category, weight / normalizer) for category, weight in weights]
    confidence = probabilities[0][1]
    runner_up = probabilities[1][1] if len(probabilities) > 1 else 0.0
    try:
        category = Category(probabilities[0][0])
    except ValueError:
        return None
    return PersonalPrediction(
        category=category,
        confidence=confidence,
        margin=confidence - runner_up,
        model_name=model_name,
    )


def assign_stratified_folds(examples: list[TrainingExample]) -> dict[str, int]:
    """Assign sender groups to deterministic, category-balanced folds."""

    grouped: dict[str, list[TrainingExample]] = {}
    for example in examples:
        grouped.setdefault(example.sender_key or example.message_id, []).append(example)
    category_groups: dict[str, list[tuple[str, list[TrainingExample]]]] = {}
    for sender_key, items in grouped.items():
        primary = Counter(item.category.value for item in items).most_common(1)[0][0]
        category_groups.setdefault(primary, []).append((sender_key, items))
    assignments: dict[str, int] = {}
    for category, groups in sorted(category_groups.items()):
        ordered = sorted(
            groups,
            key=lambda item: hashlib.sha256(f"{category}:{item[0]}".encode()).hexdigest(),
        )
        for index, (_, items) in enumerate(ordered):
            fold = index % 5
            for item in items:
                assignments[item.message_id] = fold
    return assignments


def _metrics(
    expected: list[Category], predicted: list[Category]
) -> tuple[float, float, dict[str, float]]:
    if not expected:
        return 0.0, 0.0, {}
    labels = sorted({item.value for item in expected} | {item.value for item in predicted})
    correct = sum(left == right for left, right in zip(expected, predicted, strict=True))
    f1_scores: list[float] = []
    recall: dict[str, float] = {}
    for label in labels:
        true_positive = sum(
            left.value == label and right.value == label
            for left, right in zip(expected, predicted, strict=True)
        )
        false_positive = sum(
            left.value != label and right.value == label
            for left, right in zip(expected, predicted, strict=True)
        )
        false_negative = sum(
            left.value == label and right.value != label
            for left, right in zip(expected, predicted, strict=True)
        )
        precision = (
            true_positive / (true_positive + false_positive)
            if true_positive + false_positive
            else 0.0
        )
        category_recall = (
            true_positive / (true_positive + false_negative)
            if true_positive + false_negative
            else 0.0
        )
        f1_scores.append(
            2 * precision * category_recall / (precision + category_recall)
            if precision + category_recall
            else 0.0
        )
        recall[label] = category_recall
    return correct / len(expected), sum(f1_scores) / len(f1_scores), recall


def train_and_evaluate(examples: list[TrainingExample]) -> TrainingResult:
    """Train the deployable model and score sender-grouped stratified folds."""

    expected: list[Category] = []
    predicted: list[Category] = []
    folds = assign_stratified_folds(examples)
    if len(examples) >= 5:
        for fold in range(5):
            training = [item for item in examples if folds.get(item.message_id) != fold]
            testing = [item for item in examples if folds.get(item.message_id) == fold]
            if not training:
                continue
            candidate = train(training)
            for example in testing:
                result = predict(candidate, example.text, "cross-validation")
                if result is None:
                    continue
                expected.append(example.category)
                predicted.append(result.category)
    accuracy, macro_f1, recall = _metrics(expected, predicted)
    return TrainingResult(
        artifact=train(examples),
        accuracy=accuracy,
        macro_f1=macro_f1,
        evaluated_count=len(expected),
        category_recall=recall,
        folds=folds,
    )


def serialize_artifact(artifact: dict[str, Any]) -> str:
    return json.dumps(artifact, separators=(",", ":"), sort_keys=True)


def deserialize_artifact(value: str) -> dict[str, Any]:
    decoded = json.loads(value)
    if not isinstance(decoded, dict) or decoded.get("schema") != 1:
        raise ValueError("Unsupported personalized-model artifact")
    return decoded
