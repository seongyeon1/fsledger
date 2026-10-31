"""BLAKE2b 기반 부분·전체 해시."""

import hashlib
from pathlib import Path

PARTIAL_BYTES = 64 * 1024
_CHUNK = 1024 * 1024


def partial_hash(path: Path) -> str:
    """앞 64 KiB 의 해시. 이 크기 이하 파일이면 전체 해시와 같다."""
    h = hashlib.blake2b(digest_size=32)
    with path.open("rb") as f:
        h.update(f.read(PARTIAL_BYTES))
    return h.hexdigest()


def full_hash(path: Path) -> str:
    """파일 전체의 해시."""
    h = hashlib.blake2b(digest_size=32)
    with path.open("rb") as f:
        while chunk := f.read(_CHUNK):
            h.update(chunk)
    return h.hexdigest()
