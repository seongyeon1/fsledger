"""파일시스템 스캔 — stat 캐시로 변경을 감지하고 스냅샷 하나를 트랜잭션 하나로 기록한다."""

import json
import os
import sqlite3
import stat
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, Iterator, List, Optional, Set, Tuple

from fsledger.config import Config
from fsledger.hashing import partial_hash

# 스캔 시작과 이 간격 안에 수정된 파일은 같은 stat 으로 내용이 또 바뀔 수 있다 (racy git).
RACY_WINDOW_NS = 2_000_000_000


@dataclass
class Seen:
    """이번 스캔에서 본 파일 하나."""

    path: str
    size: int
    mtime_ns: int
    ino: int
    dev: int
    in_repo: bool


@dataclass
class Prev:
    """DB 에 기록된 현재 버전 하나."""

    id: int
    path: str
    size: int
    mtime_ns: int
    ino: int
    dev: int
    partial_hash: Optional[str]
    content_hash: Optional[str]
    racy: bool


@dataclass
class JunkDir:
    """들어가지 않고 크기만 잰 디렉터리."""

    path: str
    name: str
    dir_mtime_ns: int
    parent_mtime_ns: int


class ScanAborted(RuntimeError):
    """루트 폴더를 읽지 못해 스캔을 기록하면 안 되는 상황."""


@dataclass
class ScanResult:
    """스캔 결과 요약."""

    snapshot_id: int
    files: int = 0
    bytes: int = 0
    added: int = 0
    modified: int = 0
    deleted: int = 0
    moved: int = 0
    seconds: float = 0.0
    errors: List[str] = field(default_factory=list)


def _dir_size(path: str) -> Tuple[int, int]:
    total = files = 0
    stack = [path]
    while stack:
        d = stack.pop()
        try:
            with os.scandir(d) as it:
                for e in it:
                    try:
                        if e.is_dir(follow_symlinks=False):
                            stack.append(e.path)
                        elif e.is_file(follow_symlinks=False):
                            total += e.stat(follow_symlinks=False).st_size
                            files += 1
                    except OSError:
                        continue
        except OSError:
            continue
    return total, files


def walk(
    cfg: Config,
    errors: List[str],
    repos: Set[str],
    junk: List[JunkDir],
    unreadable: List[str],
) -> Iterator[Seen]:
    """설정된 루트를 순회하며 일반 파일을 낸다. 심볼릭 링크는 따라가지 않는다.

    읽지 못한 경로는 unreadable 에 모은다. 그 아래 기존 기록은 삭제로 보면 안 된다.
    """
    ignore = set(cfg.ignore_dirs)
    junk_names = set(cfg.junk_dirs)
    data_dir = str(cfg.data_dir)
    for root in cfg.roots:
        if not root.is_dir():
            errors.append(f"{root}: 루트가 없음")
            unreadable.append(str(root))
            continue
        try:
            os.scandir(root).close()
        except OSError as exc:
            # macOS 개인정보 보호(TCC)가 Desktop·Documents 접근을 막으면 여기서 걸린다.
            # 계속 진행하면 그 아래 전체가 삭제로 기록되므로 스냅샷을 남기지 않는다.
            raise ScanAborted(
                f"{root} 를 읽을 수 없습니다 ({exc.strerror}). "
                "시스템 설정 > 개인정보 보호 및 보안 > 전체 디스크 접근 권한에서 "
                "터미널(또는 fsledger)을 허용하세요."
            ) from exc
        stack: List[Tuple[str, bool, int]] = [(str(root), False, 0)]
        while stack:
            d, in_repo, d_mtime = stack.pop()
            try:
                entries = list(os.scandir(d))
            except OSError as exc:
                errors.append(f"{d}: {exc.strerror}")
                unreadable.append(d)
                continue
            if any(e.name == ".git" for e in entries):
                repos.add(d)
                in_repo = True
            for e in entries:
                try:
                    st = e.stat(follow_symlinks=False)
                except OSError as exc:
                    errors.append(f"{e.path}: {exc.strerror}")
                    unreadable.append(e.path)
                    continue
                if stat.S_ISDIR(st.st_mode):
                    if e.name in ignore or e.path == data_dir:
                        continue
                    if e.name in junk_names:
                        junk.append(JunkDir(e.path, e.name, st.st_mtime_ns, d_mtime))
                        continue
                    stack.append((e.path, in_repo, st.st_mtime_ns))
                elif stat.S_ISREG(st.st_mode):
                    yield Seen(e.path, st.st_size, st.st_mtime_ns, st.st_ino, st.st_dev, in_repo)


def _load_current(conn: sqlite3.Connection) -> Dict[str, Prev]:
    rows = conn.execute(
        "SELECT id, path, size, mtime_ns, ino, dev, partial_hash, content_hash, racy"
        " FROM file_versions WHERE valid_to IS NULL"
    )
    return {r["path"]: Prev(*r) for r in rows}


def _racy_partial(s: Seen, started_ns: int) -> Tuple[bool, Optional[str]]:
    """racy 파일은 지금 부분 해시를 떠 두고, 다음 스캔에서 stat 이 같아도 이걸로 비교한다."""
    if started_ns - s.mtime_ns >= RACY_WINDOW_NS:
        return False, None
    try:
        return True, partial_hash(Path(s.path))
    except OSError:
        return True, None


def run_scan(
    conn: sqlite3.Connection,
    cfg: Config,
    progress: Optional[Callable[[int], None]] = None,
) -> ScanResult:
    """스캔 1회를 실행해 스냅샷·버전·변경 이벤트를 하나의 트랜잭션으로 기록한다."""
    t0 = time.time()
    started_ns = time.time_ns()
    parent = conn.execute("SELECT MAX(id) FROM snapshots").fetchone()[0]
    prev = _load_current(conn)

    errors: List[str] = []
    repos: Set[str] = set()
    junk: List[JunkDir] = []
    unreadable: List[str] = []
    seen: Dict[str, Seen] = {}
    for i, s in enumerate(walk(cfg, errors, repos, junk, unreadable), 1):
        seen[s.path] = s
        if progress and i % 20000 == 0:
            progress(i)

    conn.execute("BEGIN IMMEDIATE")
    try:
        cur = conn.execute(
            "INSERT INTO snapshots (parent_id, started_at, roots) VALUES (?, ?, ?)",
            (parent, t0, json.dumps([str(r) for r in cfg.roots], ensure_ascii=False)),
        )
        snap = int(cur.lastrowid or 0)
        res = ScanResult(snapshot_id=snap, errors=errors)
        kept = _protect_unreadable(prev, seen, unreadable)
        _apply_diff(conn, snap, prev, seen, started_ns, res)
        _record_junk_dirs(conn, snap, junk)
        conn.execute("DELETE FROM repos")
        conn.executemany(
            "INSERT INTO repos (path, last_snapshot) VALUES (?, ?)", [(r, snap) for r in repos]
        )
        res.files = len(seen) + len(kept)
        res.bytes = sum(s.size for s in seen.values()) + sum(p.size for p in kept)
        res.seconds = time.time() - t0
        conn.execute(
            "UPDATE snapshots SET finished_at=?, files=?, bytes=?, added=?, modified=?,"
            " deleted=?, moved=? WHERE id=?",
            (
                time.time(),
                res.files,
                res.bytes,
                res.added,
                res.modified,
                res.deleted,
                res.moved,
                snap,
            ),
        )
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    return res


def _protect_unreadable(
    prev: Dict[str, Prev], seen: Dict[str, Seen], unreadable: List[str]
) -> List[Prev]:
    """읽지 못한 경로 아래의 기존 기록을 prev 에서 빼서, 삭제로 기록되지 않게 한다."""
    if not unreadable:
        return []
    prefixes = tuple(u + "/" for u in unreadable)
    exact = set(unreadable)
    kept = [
        p
        for path, p in prev.items()
        if path not in seen and (path in exact or path.startswith(prefixes))
    ]
    for p in kept:
        del prev[p.path]
    return kept


def _open_version(
    conn: sqlite3.Connection,
    snap: int,
    s: Seen,
    started_ns: int,
    carry: Optional[Prev] = None,
) -> None:
    racy, ph = _racy_partial(s, started_ns)
    content: Optional[str] = None
    if carry is not None and carry.size == s.size and carry.mtime_ns == s.mtime_ns:
        ph = ph or carry.partial_hash
        content = carry.content_hash
    conn.execute(
        "INSERT INTO file_versions (path, valid_from, size, mtime_ns, ino, dev,"
        " partial_hash, content_hash, racy, in_repo) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (s.path, snap, s.size, s.mtime_ns, s.ino, s.dev, ph, content, int(racy), int(s.in_repo)),
    )


def _is_unchanged(p: Prev, s: Seen) -> bool:
    if (p.size, p.mtime_ns, p.ino, p.dev) != (s.size, s.mtime_ns, s.ino, s.dev):
        return False
    if not p.racy or p.partial_hash is None:
        return True
    try:
        return partial_hash(Path(s.path)) == p.partial_hash
    except OSError:
        return True


def _apply_diff(
    conn: sqlite3.Connection,
    snap: int,
    prev: Dict[str, Prev],
    seen: Dict[str, Seen],
    started_ns: int,
    res: ScanResult,
) -> None:
    close = "UPDATE file_versions SET valid_to=? WHERE id=?"
    change = (
        "INSERT INTO changes (snapshot_id, kind, path, old_path, size_delta) VALUES (?,?,?,?,?)"
    )

    gone = {path: p for path, p in prev.items() if path not in seen}
    new_paths = [path for path in seen if path not in prev]

    for path, s in seen.items():
        p = prev.get(path)
        if p is None:
            continue
        if _is_unchanged(p, s):
            if p.racy:
                conn.execute("UPDATE file_versions SET racy=0 WHERE id=?", (p.id,))
            continue
        conn.execute(close, (snap, p.id))
        _open_version(conn, snap, s, started_ns)
        conn.execute(change, (snap, "modified", path, None, s.size - p.size))
        res.modified += 1

    # rename 추론: 같은 볼륨 안의 mv 는 inode 가 유지된다.
    by_inode = {(p.dev, p.ino): p for p in gone.values()}
    for path in new_paths:
        s = seen[path]
        p = by_inode.pop((s.dev, s.ino), None)
        if p is not None and p.size == s.size and p.path in gone:
            del gone[p.path]
            conn.execute(close, (snap, p.id))
            _open_version(conn, snap, s, started_ns, carry=p)
            conn.execute(change, (snap, "moved", path, p.path, 0))
            res.moved += 1
            continue
        _open_version(conn, snap, s, started_ns)
        conn.execute(change, (snap, "added", path, None, s.size))
        res.added += 1

    for path, p in gone.items():
        conn.execute(close, (snap, p.id))
        conn.execute(change, (snap, "deleted", path, None, -p.size))
        res.deleted += 1


def _record_junk_dirs(conn: sqlite3.Connection, snap: int, junk: List[JunkDir]) -> None:
    cached = {
        r["path"]: r for r in conn.execute("SELECT path, bytes, files, dir_mtime_ns FROM junk_dirs")
    }
    conn.execute("DELETE FROM junk_dirs")
    for j in junk:
        c = cached.get(j.path)
        # 디렉터리 mtime 이 그대로면 직속 항목이 안 바뀐 것이므로 지난 크기를 재사용한다.
        if c is not None and c["dir_mtime_ns"] == j.dir_mtime_ns:
            size, files = c["bytes"], c["files"]
        else:
            size, files = _dir_size(j.path)
        conn.execute(
            "INSERT INTO junk_dirs (path, name, bytes, files, dir_mtime_ns, parent_mtime_ns,"
            " last_snapshot) VALUES (?,?,?,?,?,?,?)",
            (j.path, j.name, size, files, j.dir_mtime_ns, j.parent_mtime_ns, snap),
        )


def print_progress(n: int) -> None:
    """진행 상황을 stderr 에 한 줄로 갱신한다."""
    print(f"\r  {n:,} files…", end="", file=sys.stderr, flush=True)
