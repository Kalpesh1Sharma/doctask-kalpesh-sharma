from hashlib import sha256
from pathlib import Path

import pytest

import services.local_artifact_storage as storage_module
from services.local_artifact_storage import (
    ArtifactIntegrityError,
    ArtifactPublicationError,
    LocalArtifactStorage,
)


def final_path(root: Path, content: bytes) -> Path:
    digest = sha256(content).hexdigest()
    return root / "final" / "sha256" / digest[:2] / digest


def test_publish_and_verify_round_trip_across_storage_instances(tmp_path: Path) -> None:
    content = b"final report bytes"
    storage = LocalArtifactStorage(tmp_path)

    published = storage.publish_bytes(content)
    verified = LocalArtifactStorage(tmp_path).verify(
        published.storage_uri,
        published.content_hash,
    )

    digest = sha256(content).hexdigest()
    assert published.storage_uri == f"artifact://sha256/{digest}"
    assert published.content_hash == f"sha256:{digest}"
    assert published.size_bytes == len(content)
    assert final_path(tmp_path, content).read_bytes() == content
    assert verified == published


def test_identical_publish_is_idempotent(tmp_path: Path) -> None:
    content = b"same artifact"
    storage = LocalArtifactStorage(tmp_path)

    first = storage.publish_bytes(content)
    second = storage.publish_bytes(content)

    assert second == first
    assert final_path(tmp_path, content).read_bytes() == content
    assert list(final_path(tmp_path, content).parent.glob("*.tmp")) == []


def test_existing_different_bytes_are_not_overwritten(tmp_path: Path) -> None:
    content = b"expected artifact"
    path = final_path(tmp_path, content)
    path.parent.mkdir(parents=True)
    path.write_bytes(b"corrupt bytes")

    with pytest.raises(ArtifactIntegrityError):
        LocalArtifactStorage(tmp_path).publish_bytes(content)

    assert path.read_bytes() == b"corrupt bytes"
    assert list(path.parent.glob("*.tmp")) == []


@pytest.mark.parametrize(
    "storage_uri,content_hash",
    [
        ("artifact://sha256/" + "0" * 64, "sha256:" + "1" * 64),
        ("artifact://sha256/" + "A" * 64, "sha256:" + "a" * 64),
        ("artifact://sha256/" + "0" * 64 + "?query=true", "sha256:" + "0" * 64),
        ("artifact://sha256/../../outside", "sha256:" + "0" * 64),
        ("file:///outside", "sha256:" + "0" * 64),
    ],
)
def test_verify_rejects_mismatched_or_unsafe_uri_and_hashes(
    tmp_path: Path,
    storage_uri: str,
    content_hash: str,
) -> None:
    outside = tmp_path.parent / "outside"
    outside.write_bytes(b"unchanged")

    with pytest.raises(ArtifactIntegrityError):
        LocalArtifactStorage(tmp_path).verify(storage_uri, content_hash)

    assert outside.read_bytes() == b"unchanged"


def test_verify_rejects_missing_and_altered_artifacts(tmp_path: Path) -> None:
    content = b"stored artifact"
    storage = LocalArtifactStorage(tmp_path)
    published = storage.publish_bytes(content)
    path = final_path(tmp_path, content)

    path.write_bytes(b"altered")
    with pytest.raises(ArtifactIntegrityError):
        storage.verify(published.storage_uri, published.content_hash)

    path.unlink()
    with pytest.raises(FileNotFoundError):
        storage.verify(published.storage_uri, published.content_hash)


def test_stale_temporary_file_is_not_a_final_artifact_and_does_not_block_retry(
    tmp_path: Path,
) -> None:
    content = b"new artifact"
    path = final_path(tmp_path, content)
    path.parent.mkdir(parents=True)
    stale_temporary = path.parent / ".stale.tmp"
    stale_temporary.write_bytes(b"partial")
    storage = LocalArtifactStorage(tmp_path)
    digest = sha256(content).hexdigest()

    with pytest.raises(FileNotFoundError):
        storage.verify(f"artifact://sha256/{digest}", f"sha256:{digest}")

    published = storage.publish_bytes(content)

    assert published.size_bytes == len(content)
    assert stale_temporary.read_bytes() == b"partial"
    assert path.read_bytes() == content


def test_hard_link_failure_cleans_current_temporary_file_without_creating_final(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    content = b"cannot publish"
    path = final_path(tmp_path, content)

    def unsupported_link(source: Path, target: Path) -> None:
        raise OSError("hard links unsupported")

    monkeypatch.setattr(storage_module.os, "link", unsupported_link)

    with pytest.raises(ArtifactPublicationError):
        LocalArtifactStorage(tmp_path).publish_bytes(content)

    assert not path.exists()
    assert list(path.parent.glob("*.tmp")) == []
