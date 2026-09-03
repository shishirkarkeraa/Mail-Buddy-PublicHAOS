from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

from googleapiclient.errors import HttpError
from httplib2 import Response, ServerNotFoundError

from mail_buddy.contracts import CATEGORY_LABELS, LEGACY_CATEGORY_LABELS
from mail_buddy.gmail import (
    RECEIVED_QUERY,
    GmailClient,
    HistoryExpiredError,
)


class Request:
    def __init__(
        self,
        response: dict[str, Any] | None = None,
        error: BaseException | None = None,
    ) -> None:
        self.response = response or {}
        self.error = error

    def execute(self) -> dict[str, Any]:
        if self.error is not None:
            raise self.error
        return self.response


def http_error(status: int, reason: str = "backendError") -> HttpError:
    response = Response({"status": str(status)})
    payload = f'{{"error":{{"errors":[{{"reason":"{reason}"}}]}}}}'.encode()
    return HttpError(response, payload)


def service_resources() -> tuple[MagicMock, MagicMock, MagicMock, MagicMock]:
    service = MagicMock()
    users = service.users.return_value
    messages = users.messages.return_value
    history = users.history.return_value
    labels = users.labels.return_value
    return service, messages, history, labels


def test_received_page_uses_exclusion_query_and_pagination() -> None:
    service, messages, _, _ = service_resources()
    messages.list.return_value = Request(
        {
            "messages": [{"id": "m1", "threadId": "t1"}],
            "nextPageToken": "page-2",
        }
    )
    client = GmailClient(object(), service=service)

    result, token = client.list_received_page(
        page_token="page-1",
        max_results=999,
    )

    assert result == [{"id": "m1", "threadId": "t1"}]
    assert token == "page-2"
    messages.list.assert_called_once_with(
        userId="me",
        q=RECEIVED_QUERY,
        includeSpamTrash=False,
        maxResults=500,
        pageToken="page-1",
    )


def test_history_paginates_deduplicates_and_filters_excluded_labels() -> None:
    service, _, history, _ = service_resources()

    def list_history(**kwargs: Any) -> Request:
        if "pageToken" not in kwargs:
            return Request(
                {
                    "historyId": "12",
                    "nextPageToken": "next",
                    "history": [
                        {
                            "messagesAdded": [
                                {
                                    "message": {
                                        "id": "m1",
                                        "threadId": "t1",
                                        "labelIds": ["INBOX"],
                                    }
                                },
                                {
                                    "message": {
                                        "id": "sent",
                                        "threadId": "ts",
                                        "labelIds": ["SENT"],
                                    }
                                },
                            ]
                        }
                    ],
                }
            )
        return Request(
            {
                "historyId": "13",
                "history": [
                    {
                        "messagesAdded": [
                            {"message": {"id": "m1", "threadId": "t1"}},
                            {"message": {"id": "m2", "threadId": "t2"}},
                        ]
                    }
                ],
            }
        )

    history.list.side_effect = list_history
    client = GmailClient(object(), service=service)

    result, newest = client.list_history_added("10")

    assert [item["id"] for item in result] == ["m1", "m2"]
    assert newest == "13"
    assert history.list.call_count == 2


def test_history_404_is_classified_as_expired() -> None:
    service, _, history, _ = service_resources()
    history.list.return_value = Request(error=http_error(404, "notFound"))
    client = GmailClient(object(), service=service, max_attempts=1)

    try:
        client.list_history_added("old")
    except HistoryExpiredError as error:
        assert error.status == 404
    else:
        raise AssertionError("Expected HistoryExpiredError")


def test_transient_modify_retries_with_deterministic_message_labels() -> None:
    service, messages, _, _ = service_resources()
    messages.modify.side_effect = [
        Request(error=http_error(503)),
        Request({"id": "m1", "labelIds": ["label-a"]}),
    ]
    sleeps: list[float] = []
    client = GmailClient(
        object(),
        service=service,
        sleeper=sleeps.append,
        random_source=lambda: 0.0,
    )

    result = client.modify_message_labels(
        "m1",
        add_label_ids=["label-a", "label-a"],
        remove_label_ids=["INBOX", "label-a", "INBOX"],
    )

    assert result["id"] == "m1"
    assert messages.modify.call_count == 2
    assert messages.modify.call_args.kwargs["body"] == {
        "addLabelIds": ["label-a"],
        "removeLabelIds": ["INBOX"],
    }
    assert sleeps == [0.5]


def test_trash_and_untrash_use_the_gmail_message_endpoints() -> None:
    service, messages, _, _ = service_resources()
    messages.trash.return_value = Request({"id": "m1", "labelIds": ["TRASH"]})
    messages.untrash.return_value = Request({"id": "m1", "labelIds": ["INBOX"]})
    client = GmailClient(object(), service=service)

    assert client.trash_message("m1")["labelIds"] == ["TRASH"]
    assert client.untrash_message("m1")["labelIds"] == ["INBOX"]
    messages.trash.assert_called_once_with(userId="me", id="m1")
    messages.untrash.assert_called_once_with(userId="me", id="m1")


def test_ensure_labels_reuses_existing_and_creates_missing() -> None:
    service, _, _, labels = service_resources()
    first_category, first_name = next(iter(CATEGORY_LABELS.items()))
    labels.list.return_value = Request({"labels": [{"id": "existing", "name": first_name}]})
    next_id = 0

    def create_label(**kwargs: Any) -> Request:
        nonlocal next_id
        next_id += 1
        return Request({"id": f"new-{next_id}", "name": kwargs["body"]["name"]})

    labels.create.side_effect = create_label
    client = GmailClient(object(), service=service)

    result = client.ensure_labels()

    assert result[first_category.value] == "existing"
    assert "needs_review" in result
    assert labels.create.call_count == len(CATEGORY_LABELS)


def test_ensure_labels_renames_legacy_labels_without_losing_ids() -> None:
    service, _, _, labels = service_resources()
    category = next(iter(CATEGORY_LABELS))
    legacy_name = LEGACY_CATEGORY_LABELS[category]
    labels.list.return_value = Request({"labels": [{"id": "legacy-id", "name": legacy_name}]})
    labels.patch.return_value = Request({"id": "legacy-id", "name": CATEGORY_LABELS[category]})
    labels.create.side_effect = lambda **kwargs: Request(
        {"id": f"new-{kwargs['body']['name']}", "name": kwargs["body"]["name"]}
    )

    result = GmailClient(object(), service=service).ensure_labels()

    assert result[category.value] == "legacy-id"
    labels.patch.assert_called_once_with(
        userId="me",
        id="legacy-id",
        body={
            "name": CATEGORY_LABELS[category],
            "labelListVisibility": "labelShow",
            "messageListVisibility": "show",
        },
    )


def test_ensure_labels_merges_duplicate_legacy_members_before_deleting_label() -> None:
    service, messages, _, labels = service_resources()
    category = next(iter(CATEGORY_LABELS))
    labels.list.return_value = Request(
        {
            "labels": [
                {"id": "direct-id", "name": CATEGORY_LABELS[category]},
                {"id": "legacy-id", "name": LEGACY_CATEGORY_LABELS[category]},
            ]
        }
    )
    messages.list.return_value = Request({"messages": [{"id": "old-message"}]})
    messages.modify.return_value = Request({"id": "old-message"})
    labels.delete.return_value = Request({})
    labels.create.side_effect = lambda **kwargs: Request(
        {"id": f"new-{kwargs['body']['name']}", "name": kwargs["body"]["name"]}
    )

    result = GmailClient(object(), service=service).ensure_labels()

    assert result[category.value] == "direct-id"
    messages.modify.assert_called_once_with(
        userId="me",
        id="old-message",
        body={"addLabelIds": ["direct-id"], "removeLabelIds": ["legacy-id"]},
    )
    labels.delete.assert_called_once_with(userId="me", id="legacy-id")


def test_sent_query_is_cached_per_normalized_sender() -> None:
    service, messages, _, _ = service_resources()
    messages.list.return_value = Request({"messages": [{"id": "sent"}]})
    client = GmailClient(object(), service=service)

    assert client.has_sent_to(" Friend@Example.com ") is True
    assert client.has_sent_to("friend@example.com") is True
    assert messages.list.call_count == 1


def test_httplib2_transport_error_is_retried() -> None:
    service, messages, _, _ = service_resources()
    messages.modify.side_effect = [
        Request(error=ServerNotFoundError("temporary DNS failure")),
        Request({"id": "m1", "labelIds": ["label-a"]}),
    ]
    sleeps: list[float] = []
    client = GmailClient(
        object(),
        service=service,
        sleeper=sleeps.append,
        random_source=lambda: 0.0,
    )

    result = client.modify_message_labels("m1", add_label_ids=["label-a"])

    assert result["id"] == "m1"
    assert messages.modify.call_count == 2
    assert sleeps == [0.5]


def test_negative_sent_cache_expires() -> None:
    service, messages, _, _ = service_resources()
    messages.list.side_effect = [
        Request({"messages": [], "resultSizeEstimate": 0}),
        Request({"messages": [{"id": "sent"}]}),
    ]
    clock = [100.0]
    client = GmailClient(
        object(),
        service=service,
        sent_cache_ttl_seconds=86_400,
        monotonic_clock=lambda: clock[0],
    )

    assert client.has_sent_to("friend@example.com") is False
    assert client.has_sent_to("friend@example.com") is False
    clock[0] += 86_401
    assert client.has_sent_to("friend@example.com") is True
    assert messages.list.call_count == 2
