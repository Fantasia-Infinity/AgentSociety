from __future__ import annotations

from dataclasses import dataclass
import hashlib
from pathlib import Path
import tempfile
from typing import Protocol
from urllib.parse import urlparse


@dataclass(frozen=True, slots=True)
class StoredObject:
    uri: str
    sha256: str
    size_bytes: int


class ObjectStore(Protocol):
    def put(self, content: bytes, *, name: str, media_type: str) -> StoredObject: ...


class FileObjectStore:
    def __init__(self, root: Path) -> None:
        self._root = root.resolve()
        self._root.mkdir(parents=True, exist_ok=True, mode=0o700)

    def put(self, content: bytes, *, name: str, media_type: str) -> StoredObject:
        del name, media_type
        digest = hashlib.sha256(content).hexdigest()
        target = self._root / digest[:2] / digest[2:]
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not target.exists():
            with tempfile.NamedTemporaryFile(dir=target.parent, delete=False) as handle:
                temporary = Path(handle.name)
                handle.write(content)
                handle.flush()
            temporary.chmod(0o600)
            temporary.replace(target)
        return StoredObject(target.as_uri(), digest, len(content))


class S3ObjectStore:
    def __init__(self, bucket: str, prefix: str) -> None:
        try:
            import boto3
        except ImportError as exc:
            raise RuntimeError("S3 storage requires the 's3' optional dependency") from exc
        self._client = boto3.client("s3")
        self._bucket = bucket
        self._prefix = prefix.strip("/")

    def put(self, content: bytes, *, name: str, media_type: str) -> StoredObject:
        digest = hashlib.sha256(content).hexdigest()
        key = "/".join(part for part in (self._prefix, digest[:2], digest[2:]) if part)
        self._client.put_object(
            Bucket=self._bucket,
            Key=key,
            Body=content,
            ContentType=media_type,
            Metadata={"sha256": digest, "original-name": name[:500]},
        )
        return StoredObject(f"s3://{self._bucket}/{key}", digest, len(content))


def build_object_store(url: str | None) -> ObjectStore | None:
    if not url:
        return None
    parsed = urlparse(url)
    if parsed.scheme == "file":
        return FileObjectStore(Path(parsed.path))
    if parsed.scheme == "s3" and parsed.netloc:
        return S3ObjectStore(parsed.netloc, parsed.path)
    raise ValueError("AGENT_HUB_OBJECT_STORE_URL must use file:// or s3://")
