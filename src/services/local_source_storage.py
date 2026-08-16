"""Content-addressed, local source-file storage.

Source files are immutable blobs.  Their presence is intentionally not a
database lifecycle signal: only a READY ``document_versions`` row makes a
source authoritative for the application.
"""

from __future__ import annotations

import errno
import os
import re
import tempfile
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import BinaryIO, cast
from urllib.parse import urlsplit

_HEX = re.compile(r"[0-9a-f]{64}")
_READ_SIZE = 1024 * 1024
MAX_SOURCE_BYTES = 100 * 1024 * 1024


class SourceStorageError(RuntimeError):
    """Base failure for local source publication and verification."""


class SourceTooLargeError(SourceStorageError):
    pass


class SourceIntegrityError(SourceStorageError):
    pass


class SourcePublicationError(SourceStorageError):
    pass


@dataclass(frozen=True)
class StoredSource:
    storage_uri: str
    content_hash: str
    size_bytes: int
    path: Path


SourceInput = bytes | bytearray | memoryview | Path | str | BinaryIO


class LocalSourceStorage:
    """Publish sources with no-replacement content-addressed semantics."""

    def __init__(self, root: Path, *, max_bytes: int = MAX_SOURCE_BYTES) -> None:
        if not isinstance(max_bytes, int) or isinstance(max_bytes, bool) or max_bytes <= 0:
            raise ValueError("max_bytes must be a positive integer")
        self._root = Path(root).resolve()
        self._max_bytes = max_bytes

    def publish(self, source: SourceInput) -> StoredSource:
        """Stream ``source`` into a synced temporary file and publish it atomically."""

        base = self._root / "sha256"
        base.mkdir(parents=True, exist_ok=True)
        temporary: Path | None = None
        try:
            descriptor, name = tempfile.mkstemp(prefix=".source-", suffix=".tmp", dir=base)
            temporary = Path(name)
            digest, size = self._copy_and_hash(descriptor, source)
            final_path = self._path_for_digest(digest)
            final_path.parent.mkdir(parents=True, exist_ok=True)
            expected = self._stored(digest, size)
            if final_path.exists():
                return self._reuse_existing(final_path, expected)
            try:
                os.link(temporary, final_path)
            except FileExistsError:
                return self._reuse_existing(final_path, expected)
            except OSError as error:
                raise SourcePublicationError(
                    "source filesystem does not support no-replacement hard-link publication",
                ) from error
            self._sync_directory(final_path.parent)
            return StoredSource(
                expected.storage_uri,
                expected.content_hash,
                expected.size_bytes,
                final_path,
            )
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass

    def verify(self, storage_uri: str, content_hash: str) -> StoredSource:
        digest = self._digest_from_uri(storage_uri)
        if digest != self._digest_from_content_hash(content_hash):
            raise SourceIntegrityError("source URI and content hash disagree")
        path = self._path_for_digest(digest)
        if not path.is_file():
            raise SourceIntegrityError("source artifact does not exist")
        hasher = sha256()
        size = 0
        with path.open("rb") as stream:
            while chunk := stream.read(_READ_SIZE):
                hasher.update(chunk)
                size += len(chunk)
        if hasher.hexdigest() != digest:
            raise SourceIntegrityError("stored source bytes do not match the expected content hash")
        stored = self._stored(digest, size)
        return StoredSource(stored.storage_uri, stored.content_hash, stored.size_bytes, path)

    def open_verified(self, storage_uri: str, content_hash: str) -> BinaryIO:
        # The caller owns the returned stream lifetime.
        return cast(BinaryIO, self.verify(storage_uri, content_hash).path.open("rb"))

    def _copy_and_hash(self, descriptor: int, source: SourceInput) -> tuple[str, int]:
        hasher = sha256()
        size = 0
        stream: BinaryIO | None = None
        close_stream = False
        try:
            if isinstance(source, (bytes, bytearray, memoryview)):
                from io import BytesIO

                stream = BytesIO(bytes(source))
                close_stream = True
            elif isinstance(source, (str, Path)):
                stream = Path(source).open("rb")  # noqa: SIM115 - closed in this method's finally block
                close_stream = True
            elif hasattr(source, "read"):
                stream = cast(BinaryIO, source)
            else:
                raise TypeError("source must be bytes, a path, or a binary file-like object")
            with os.fdopen(descriptor, "wb") as destination:
                while True:
                    chunk = stream.read(_READ_SIZE)
                    if not chunk:
                        break
                    if not isinstance(chunk, bytes):
                        raise TypeError("source stream must yield bytes")
                    size += len(chunk)
                    if size > self._max_bytes:
                        raise SourceTooLargeError(
                            f"source exceeds the {self._max_bytes}-byte ingestion limit",
                        )
                    hasher.update(chunk)
                    destination.write(chunk)
                destination.flush()
                os.fsync(destination.fileno())
            return hasher.hexdigest(), size
        finally:
            if close_stream and stream is not None:
                stream.close()

    def _stored(self, digest: str, size: int) -> StoredSource:
        return StoredSource(
            storage_uri=f"source://sha256/{digest}",
            content_hash=f"sha256:{digest}",
            size_bytes=size,
            path=self._path_for_digest(digest),
        )

    def _path_for_digest(self, digest: str) -> Path:
        if not _HEX.fullmatch(digest):
            raise SourceIntegrityError("source digest is not canonical SHA-256")
        return self._root / "sha256" / digest[:2] / digest

    def _reuse_existing(self, path: Path, expected: StoredSource) -> StoredSource:
        existing = self.verify(expected.storage_uri, expected.content_hash)
        if existing.size_bytes != expected.size_bytes:
            raise SourceIntegrityError("existing source has the expected hash but wrong size")
        return StoredSource(existing.storage_uri, existing.content_hash, existing.size_bytes, path)

    def _digest_from_uri(self, storage_uri: str) -> str:
        parsed = urlsplit(storage_uri)
        digest = parsed.path.removeprefix("/")
        if (
            parsed.scheme != "source"
            or parsed.netloc != "sha256"
            or parsed.query
            or parsed.fragment
            or parsed.username is not None
            or parsed.password is not None
            or not _HEX.fullmatch(digest)
        ):
            raise SourceIntegrityError("storage URI is not a canonical source URI")
        return digest

    @staticmethod
    def _digest_from_content_hash(content_hash: str) -> str:
        if not isinstance(content_hash, str) or not content_hash.startswith("sha256:"):
            raise SourceIntegrityError("content hash is not a canonical SHA-256 hash")
        digest = content_hash.removeprefix("sha256:")
        if not _HEX.fullmatch(digest):
            raise SourceIntegrityError("content hash is not a canonical SHA-256 hash")
        return digest

    @staticmethod
    def _sync_directory(directory: Path) -> None:
        if os.name == "nt":
            return
        try:
            descriptor = os.open(directory, os.O_RDONLY)
        except OSError as error:
            if error.errno in {errno.EINVAL, errno.ENOTSUP}:
                return
            raise
        try:
            os.fsync(descriptor)
        except OSError as error:
            if error.errno not in {errno.EINVAL, errno.ENOTSUP}:
                raise
        finally:
            os.close(descriptor)
