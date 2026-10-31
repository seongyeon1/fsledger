"""중복 탐지 — 크기 → 부분 해시 → 전체 해시 순으로 거른다 (fdupes·rmlint 방식)."""

import sqlite3
import sys
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List, Optional, Set, Tuple

from fsledger.hashing import PARTIAL_BYTES, full_hash, partial_hash


@dataclass
class DupeGroup:
    """같은 내용을 가진 경로 묶음. paths[0] 이 보존 대상이다."""

    content_hash: str
    size: int
    paths: List[str]

    @property
    def wasted(self) -> int:
        """보존 대상을 뺀 나머지가 차지하는 바이트."""
        return self.size * (len(self.paths) - 1)


@dataclass
class _Row:
    id: int
    path: str
    size: int
    ino: int
    dev: int
    partial_hash: Optional[str]
    content_hash: Optional[str]


def _hash_or_none(fn: Callable[[Path], str], path: str) -> Optional[str]:
    try:
        return fn(Path(path))
    except OSError:
        return None


def hash_candidates(
    conn: sqlite3.Connection,
    min_size: int,
    progress: Optional[Callable[[str, int, int], None]] = None,
) -> Tuple[int, int]:
    """크기가 겹치는 파일만 해시를 채운다. (부분 해시 수, 전체 해시 수)를 돌려준다."""
    rows = [
        _Row(*r)
        for r in conn.execute(
            "SELECT id, path, size, ino, dev, partial_hash, content_hash FROM file_versions"
            " WHERE valid_to IS NULL AND size >= ? AND size IN ("
            "  SELECT size FROM file_versions WHERE valid_to IS NULL AND size >= ?"
            "  GROUP BY size HAVING COUNT(DISTINCT dev || ':' || ino) > 1)",
            (min_size, min_size),
        )
    ]
    by_size: Dict[int, List[_Row]] = defaultdict(list)
    for r in rows:
        by_size[r.size].append(r)

    # 1단계: 부분 해시. 건마다 autocommit 하지 않도록 500건씩 묶어 커밋한다.
    conn.execute("BEGIN")
    todo = [r for r in rows if r.partial_hash is None and r.content_hash is None]
    n_partial = 0
    for i, r in enumerate(todo, 1):
        r.partial_hash = _hash_or_none(partial_hash, r.path)
        if r.size <= PARTIAL_BYTES:
            r.content_hash = r.partial_hash
        conn.execute(
            "UPDATE file_versions SET partial_hash=?, content_hash=? WHERE id=?",
            (r.partial_hash, r.content_hash, r.id),
        )
        n_partial += 1
        if i % 500 == 0:
            conn.execute("COMMIT")
            conn.execute("BEGIN")
            if progress:
                progress("partial", i, len(todo))

    # 2단계: 크기와 부분 해시가 모두 같은 그룹만 전체 해시
    need_full: List[_Row] = []
    for group in by_size.values():
        by_partial: Dict[str, List[_Row]] = defaultdict(list)
        for r in group:
            if r.partial_hash is not None:
                by_partial[r.partial_hash].append(r)
        for same in by_partial.values():
            if len({(r.dev, r.ino) for r in same}) > 1:
                need_full.extend(r for r in same if r.content_hash is None)

    n_full = 0
    total_full = len(need_full)
    for i, r in enumerate(need_full, 1):
        r.content_hash = _hash_or_none(full_hash, r.path)
        conn.execute("UPDATE file_versions SET content_hash=? WHERE id=?", (r.content_hash, r.id))
        n_full += 1
        if i % 100 == 0:
            conn.execute("COMMIT")
            conn.execute("BEGIN")
            if progress:
                progress("full", i, total_full)
    conn.execute("COMMIT")
    return n_partial, n_full


def _keep_rank(path: str) -> Tuple[int, int, int, str]:
    """보존 우선순위: Downloads 밖 > 복사본 이름이 아닌 쪽 > 경로가 짧은 쪽."""
    p = Path(path)
    in_downloads = int("/Downloads/" in path)
    looks_copy = int(any(tag in p.stem for tag in (" (1)", " (2)", " 1", " copy", " 복사본")))
    return (in_downloads, looks_copy, len(path), path)


def find_groups(conn: sqlite3.Connection, min_size: int) -> List[DupeGroup]:
    """현재 상태에서 같은 내용을 가진 파일 묶음을 낭비 바이트 순으로 돌려준다."""
    rows = conn.execute(
        "SELECT content_hash, size, path, ino, dev FROM file_versions"
        " WHERE valid_to IS NULL AND content_hash IS NOT NULL AND size >= ? AND content_hash IN ("
        "  SELECT content_hash FROM file_versions WHERE valid_to IS NULL"
        "  AND content_hash IS NOT NULL GROUP BY content_hash HAVING COUNT(*) > 1)",
        (min_size,),
    )
    groups: Dict[str, DupeGroup] = {}
    inodes: Dict[str, Set[Tuple[int, int]]] = defaultdict(set)
    for r in rows:
        key = (r["dev"], r["ino"])
        if key in inodes[r["content_hash"]]:
            continue  # 하드링크는 같은 파일이다
        inodes[r["content_hash"]].add(key)
        g = groups.setdefault(r["content_hash"], DupeGroup(r["content_hash"], r["size"], []))
        g.paths.append(r["path"])
    result = [g for g in groups.values() if len(g.paths) > 1]
    for g in result:
        g.paths.sort(key=_keep_rank)
    result.sort(key=lambda g: g.wasted, reverse=True)
    return result


def print_hash_progress(stage: str, i: int, total: int) -> None:
    """해시 진행 상황을 stderr 에 갱신한다."""
    print(f"\r  hashing ({stage}) {i:,}/{total:,}", end="", file=sys.stderr, flush=True)
