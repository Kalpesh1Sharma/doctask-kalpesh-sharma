import errno
import os
import re
import tempfile
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from urllib.parse import urlsplit

_SHA256_HEX_PATTERN = re.compile(r"[0-9a-f]{64}")
_READ_CHUNK_SIZE = 1024 * 1024


class ArtifactIntegrityError(RuntimeError):
    """Raised when an artifact URI, hash, or stored file is inconsistent."""


class ArtifactPublicationError(RuntimeError):
    """Raised when immutable no-replacement publication cannot be completed."""


@dataclass(frozen=True)
class StoredArtifact:
    storage_uri: str
    content_hash: str
    size_bytes: int


class LocalArtifactStorage:
    def __init__(self, root: Path) -> None:
        self._root = Path(root).resolve()

    def publish_bytes(self, content: bytes) -> StoredArtifact:
        if not isinstance(content, bytes):
            raise TypeError("content must be bytes")

        digest = sha256(content).hexdigest()
        artifact = self._artifact_for_digest(digest, len(content))
        final_path = self._path_for_digest(digest)
        final_path.parent.mkdir(parents=True, exist_ok=True)

        temporary_path: Path | None = None
        try:
            file_descriptor, temporary_name = tempfile.mkstemp(
                prefix=f".{digest}.",
                suffix=".tmp",
                dir=final_path.parent,
            )
            temporary_path = Path(temporary_name)
            self._write_and_sync(file_descriptor, content)

            temporary_artifact = self._verify_path(temporary_path, digest)
            if temporary_artifact.size_bytes != artifact.size_bytes:
                raise ArtifactIntegrityError("temporary artifact size does not match supplied content")

            if final_path.exists():
                return self._reuse_existing(final_path, content, artifact)

            try:
                os.link(temporary_path, final_path)
            except FileExistsError:
                return self._reuse_existing(final_path, content, artifact)
            except OSError as error:
                raise ArtifactPublicationError(
                    "artifact filesystem does not support hard-link no-replacement publication"
                ) from error

            self._sync_directory(final_path.parent)
            return artifact
        finally:
            if temporary_path is not None:
                try:
                    temporary_path.unlink(missing_ok=True)
                except OSError:
                    pass

    def verify(self, storage_uri: str, content_hash: str) -> StoredArtifact:
        uri_digest = self._digest_from_uri(storage_uri)
        hash_digest = self._digest_from_content_hash(content_hash)
        if uri_digest != hash_digest:
            raise ArtifactIntegrityError("artifact URI and supplied content hash disagree")

        return self._verify_path(self._path_for_digest(uri_digest), uri_digest)

    def read_bytes(self, storage_uri: str, content_hash: str) -> bytes:
        """Read one immutable artifact while verifying its canonical digest."""

        uri_digest = self._digest_from_uri(storage_uri)
        hash_digest = self._digest_from_content_hash(content_hash)
        if uri_digest != hash_digest:
            raise ArtifactIntegrityError("artifact URI and supplied content hash disagree")

        path = self._path_for_digest(uri_digest)
        with path.open("rb") as stream:
            content = stream.read()
        if sha256(content).hexdigest() != uri_digest:
            raise ArtifactIntegrityError("stored artifact bytes do not match the expected content hash")
        return content

    def _artifact_for_digest(self, digest: str, size_bytes: int) -> StoredArtifact:
        return StoredArtifact(
            storage_uri=f"artifact://sha256/{digest}",
            content_hash=f"sha256:{digest}",
            size_bytes=size_bytes,
        )

    def _path_for_digest(self, digest: str) -> Path:
        return self._root / "final" / "sha256" / digest[:2] / digest

    def _reuse_existing(
        self,
        final_path: Path,
        content: bytes,
        artifact: StoredArtifact,
    ) -> StoredArtifact:
        existing = self._verify_path(final_path, artifact.content_hash.removeprefix("sha256:"))
        if existing.size_bytes != artifact.size_bytes or final_path.read_bytes() != content:
            raise ArtifactIntegrityError("existing immutable artifact differs from supplied content")

        return existing

    def _verify_path(self, path: Path, digest: str) -> StoredArtifact:
        with path.open("rb") as stream:
            hasher = sha256()
            size_bytes = 0
            while chunk := stream.read(_READ_CHUNK_SIZE):
                hasher.update(chunk)
                size_bytes += len(chunk)

        if hasher.hexdigest() != digest:
            raise ArtifactIntegrityError("stored artifact bytes do not match the expected content hash")

        return self._artifact_for_digest(digest, size_bytes)

    def _write_and_sync(self, file_descriptor: int, content: bytes) -> None:
        with os.fdopen(file_descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())

    def _sync_directory(self, directory: Path) -> None:
        if os.name == "nt":
            return

        try:
            directory_descriptor = os.open(directory, os.O_RDONLY)
        except OSError as error:
            if error.errno in {errno.EINVAL, errno.ENOTSUP}:
                return
            raise

        try:
            os.fsync(directory_descriptor)
        except OSError as error:
            if error.errno not in {errno.EINVAL, errno.ENOTSUP}:
                raise
        finally:
            os.close(directory_descriptor)

    def _digest_from_uri(self, storage_uri: str) -> str:
        parsed = urlsplit(storage_uri)
        digest = parsed.path.removeprefix("/")
        if (
            parsed.scheme != "artifact"
            or parsed.netloc != "sha256"
            or parsed.query
            or parsed.fragment
            or not _SHA256_HEX_PATTERN.fullmatch(digest)
        ):
            raise ArtifactIntegrityError("storage URI is not a canonical artifact URI")

        return digest

    def _digest_from_content_hash(self, content_hash: str) -> str:
        prefix = "sha256:"
        if not content_hash.startswith(prefix):
            raise ArtifactIntegrityError("content hash is not a canonical SHA-256 hash")

        digest = content_hash.removeprefix(prefix)
        if not _SHA256_HEX_PATTERN.fullmatch(digest):
            raise ArtifactIntegrityError("content hash is not a canonical SHA-256 hash")

        return digest
