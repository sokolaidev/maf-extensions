"""Evidence publication keeps its identity across unstable release-list pagination."""

from __future__ import annotations

import copy
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from container_release import REPOSITORY  # noqa: E402
from container_release_assets import Evidence  # noqa: E402
from container_release_history import GitHub, encode  # noqa: E402

pytestmark = pytest.mark.workflow

SOURCE = "a" * 40
TAG = "image-bicep-docker-sbx-v0.2.0"
RELEASE = {
    "id": 123,
    "tag_name": TAG,
    "target_commitish": SOURCE,
    "draft": False,
    "immutable": True,
}


class Client(GitHub):
    def __init__(self, responses):
        self.responses = iter(responses)
        self.reads = []
        self.publications = []
        self.source = SOURCE

    def request(self, endpoint, **kwargs):
        assert kwargs.get("method", "GET") == "GET"
        self.reads.append(endpoint)
        response = next(self.responses)
        if isinstance(response, Exception):
            raise response
        return encode(response)

    def publish(self, release_id):
        self.publications.append(release_id)

    def tag_commit(self, tag):
        assert tag == TAG
        return self.source


@pytest.fixture
def delays(monkeypatch):
    values = []
    monkeypatch.setattr("container_release_assets.time.sleep", values.append)
    return values


@pytest.mark.parametrize("draft", [True, False])
def test_same_id_on_two_pages_restarts_complete_listing(delays, draft):
    first_page = [{"id": n, "tag_name": f"unrelated-{n}"} for n in range(1, 100)]
    first_page.append(RELEASE | {"draft": draft, "immutable": not draft})
    client = Client([first_page, [RELEASE], [RELEASE]])
    assert Evidence(client).find(TAG) == RELEASE
    assert client.reads == [
        f"repos/{REPOSITORY}/releases?per_page=100&page={page}" for page in (1, 2, 1)
    ]
    assert delays == [1]
    assert client.publications == []


def test_persistent_same_id_duplicates_fail_after_bounded_reads(delays):
    client = Client([[RELEASE, RELEASE]] * 3)
    with pytest.raises(ValueError, match=f"{TAG}: repeated release ID 123 after 3 complete reads"):
        Evidence(client).find(TAG)
    assert len(client.reads) == 3
    assert delays == [1, 2]
    assert client.publications == []


@pytest.mark.parametrize("prior_duplicates", [False, True])
def test_different_ids_never_become_a_retryable_listing(delays, prior_duplicates):
    conflicting = RELEASE | {"id": 456}
    responses = (
        [[RELEASE, RELEASE], [conflicting]] if prior_duplicates else [[RELEASE, conflicting]]
    )
    client = Client(responses)
    with pytest.raises(ValueError, match=r"Conflicting evidence releases.*IDs \[123, 456\]"):
        Evidence(client).ensure(TAG, SOURCE)
    assert delays == ([1] if prior_duplicates else [])
    assert len(client.reads) == len(responses)
    assert client.publications == []


def test_disappearance_after_duplicate_cannot_create_replacement(delays):
    client = Client([[RELEASE, RELEASE], []])
    with pytest.raises(ValueError, match=f"Evidence release disappeared during retrieval: {TAG}"):
        Evidence(client).ensure(TAG, SOURCE)
    assert len(client.reads) == 2
    assert delays == [1]
    assert client.publications == []


def test_missing_release_returns_none_without_retry(delays):
    client = Client([[]])
    assert Evidence(client).find(TAG) is None
    assert len(client.reads) == 1
    assert delays == []


def test_listing_failure_is_not_retried_as_a_duplicate(delays):
    client = Client([OSError("unavailable")])
    with pytest.raises(OSError, match="unavailable"):
        Evidence(client).find(TAG)
    assert len(client.reads) == 1
    assert delays == []


@pytest.mark.parametrize("release_id", [None, 0, True, "123"])
def test_invalid_release_identity_does_not_trigger_retry(delays, release_id):
    invalid = RELEASE | {"id": release_id}
    client = Client([[invalid, invalid]])
    with pytest.raises(ValueError):
        Evidence(client).find(TAG)
    assert len(client.reads) == 1
    assert delays == []


@pytest.mark.parametrize("draft", [False, True])
def test_publication_confirms_the_known_id_without_listing_releases(draft):
    client = Client([RELEASE])
    selected = RELEASE | {"draft": draft, "immutable": not draft}
    assert Evidence(client).publish(selected, SOURCE) == RELEASE
    assert client.publications == ([123] if draft else [])
    assert client.reads == [f"repos/{REPOSITORY}/releases/123"]


@pytest.mark.parametrize(
    "change",
    [
        {"id": 456},
        {"id": True},
        {"tag_name": "image-shell-v0.2.0"},
        {"target_commitish": "b" * 40},
        {"immutable": False},
        {"immutable": 1},
        {"draft": True},
        {"draft": 0},
    ],
)
def test_publication_rejects_changed_identity_or_unconfirmed_immutability(change):
    client = Client([RELEASE | change])
    with pytest.raises(ValueError):
        Evidence(client).publish(RELEASE, SOURCE)
    assert client.publications == []


@pytest.mark.parametrize("response", [None, [], {}])
def test_publication_requires_a_complete_release_response(response):
    with pytest.raises(ValueError):
        Evidence(Client([response])).publish(RELEASE, SOURCE)


def test_publication_still_resolves_the_real_tag_commit():
    client = Client([RELEASE])
    client.source = "b" * 40
    with pytest.raises(ValueError, match="tag identifies another source"):
        Evidence(client).publish(RELEASE, SOURCE)


def test_publication_read_failure_does_not_repeat_the_write():
    client = Client([OSError("confirmation unavailable"), RELEASE])
    selected = copy.deepcopy(RELEASE) | {"draft": True, "immutable": False}
    with pytest.raises(OSError, match="confirmation unavailable"):
        Evidence(client).publish(selected, SOURCE)
    assert client.publications == [123]
    assert len(client.reads) == 1
