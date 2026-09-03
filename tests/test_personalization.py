from __future__ import annotations

from mail_buddy.contracts import Category, EmailMetadata
from mail_buddy.personalization import (
    TrainingExample,
    build_feature_text,
    deserialize_artifact,
    predict,
    serialize_artifact,
    train_and_evaluate,
)


def _examples() -> list[TrainingExample]:
    result: list[TrainingExample] = []
    for index in range(15):
        result.append(
            TrainingExample(
                message_id=f"job-{index}",
                sender_key=f"job-sender-{index}",
                text="recruiter interview candidate hiring role opportunity",
                category=Category.JOB_RELATED,
            )
        )
        result.append(
            TrainingExample(
                message_id=f"shop-{index}",
                sender_key=f"shop-sender-{index}",
                text="retail shop sale coupon discount collection",
                category=Category.SHOPPING_PROMOTION,
            )
        )
    return result


def test_personalized_model_trains_evaluates_and_round_trips() -> None:
    result = train_and_evaluate(_examples())
    artifact = deserialize_artifact(serialize_artifact(result.artifact))
    prediction = predict(
        artifact,
        "candidate recruiter scheduled an interview for this role",
        "personal-v1",
    )

    assert result.evaluated_count == 30
    assert result.accuracy == 1.0
    assert prediction is not None
    assert prediction.category == Category.JOB_RELATED
    assert prediction.confidence > 0.9


def test_training_features_are_bounded_and_redacted() -> None:
    metadata = EmailMetadata(
        message_id="message",
        thread_id="thread",
        sender_domain="example.com",
        subject="Code 123456 for person@example.com",
        snippet="Visit https://example.com/reset?token=secret",
    )

    text = build_feature_text(metadata, "x" * 20_000)

    assert len(text) <= 12_000
    assert "123456" not in text
    assert "person@example.com" not in text
    assert "token=secret" not in text
