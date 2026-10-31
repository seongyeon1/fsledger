"""BLAKE2b 기반 부분·전체 해시."""

import errno
import hashlib
import os
import stat
from pathlib import Path
from typing import BinaryIO

PARTIAL_BYTES = 64 * 1024
_CHUNK = 1024 * 1024

# iCloud 가 내려받지 않은(evicted) 파일. 읽는 순간 다운로드가 시작된다.
SF_DATALESS = getattr(stat, "SF_DATALESS", 0x40000000)


def is_dataless(path: Path) -> bool:
    """내용이 로컬에 없는 클라우드 자리표시 파일인지."""
    return bool(getattr(os.stat(path), "st_flags", 0) & SF_DATALESS)


def _open(path: Path) -> BinaryIO:
    if is_dataless(path):
        raise OSError(errno.EAGAIN, "not downloaded from iCloud", str(path))
    return path.open("rb")


def partial_hash(path: Path) -> str:
    """앞 64 KiB 의 해시. 이 크기 이하 파일이면 전체 해시와 같다."""
    h = hashlib.blake2b(digest_size=32)
    with _open(path) as f:
        h.update(f.read(PARTIAL_BYTES))
    return h.hexdigest()


def full_hash(path: Path) -> str:
    """파일 전체의 해시."""
    h = hashlib.blake2b(digest_size=32)
    with _open(path) as f:
        while chunk := f.read(_CHUNK):
            h.update(chunk)
    return h.hexdigest()
