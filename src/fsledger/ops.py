"""되돌릴 수 있는 파일 이동 — 실행 전에 저널에 pending 으로 먼저 기록한다 (WAL 원칙)."""

import os
import re
import shutil
import sqlite3
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from fsledger.config import Config


@dataclass
class Move:
    """실행할 이동 하나."""

    kind: str  # 'quarantine' | 'organize'
    src: str
    dst: str
    reason: str


@dataclass
class BatchResult:
    """배치 실행 결과."""

    batch_id: str
    done: int = 0
    failed: int = 0
    skipped: int = 0


def new_batch_id(kind: str) -> str:
    """시각이 드러나는 배치 id."""
    return f"{time.strftime('%Y%m%d-%H%M%S')}-{kind}-{uuid.uuid4().hex[:6]}"


def quarantine_target(cfg: Config, batch_id: str, src: str) -> str:
    """격리 폴더 안의 대상 경로. 원래 절대경로 구조를 그대로 보존한다."""
    return str(cfg.quarantine_dir / batch_id / src.lstrip("/"))


def unique_dst(dst: str) -> str:
    """대상이 이미 있으면 '이름 (2).확장자' 식으로 비켜 간다."""
    if not os.path.lexists(dst):
        return dst
    p = Path(dst)
    stem, suffix = p.stem, p.suffix
    n = 2
    while True:
        cand = p.with_name(f"{stem} ({n}){suffix}")
        if not os.path.lexists(cand):
            return str(cand)
        n += 1


def _move(src: str, dst: str) -> None:
    Path(dst).parent.mkdir(parents=True, exist_ok=True)
    try:
        os.rename(src, dst)  # 같은 볼륨이면 원자적, inode 유지
    except OSError:
        shutil.move(src, dst)


def apply(conn: sqlite3.Connection, moves: Sequence[Move], batch_id: str) -> BatchResult:
    """이동을 하나씩 실행한다. 각 항목은 pending 기록 → 이동 → done 순서다."""
    res = BatchResult(batch_id)
    for m in moves:
        if not os.path.lexists(m.src):
            res.skipped += 1
            continue
        dst = unique_dst(m.dst)
        cur = conn.execute(
            "INSERT INTO ops (batch_id, kind, src, dst, reason, status, created_at)"
            " VALUES (?,?,?,?,?, 'pending', ?)",
            (batch_id, m.kind, m.src, dst, m.reason, time.time()),
        )
        op_id = cur.lastrowid
        try:
            _move(m.src, dst)
        except OSError as exc:
            conn.execute("UPDATE ops SET status='failed', error=? WHERE id=?", (str(exc), op_id))
            res.failed += 1
            continue
        conn.execute("UPDATE ops SET status='done', done_at=? WHERE id=?", (time.time(), op_id))
        res.done += 1
    return res


def _prune_empty_parents(path: str, stop: Path) -> None:
    """stop 아래에서 비어 버린 상위 폴더를 지운다. 비어 있지 않으면 OSError 로 멈춘다."""
    p = Path(path).parent
    while stop in p.parents:
        try:
            p.rmdir()
        except OSError:
            return
        p = p.parent


def last_batch(conn: sqlite3.Connection) -> Optional[str]:
    """되돌릴 수 있는 가장 최근 배치."""
    row = conn.execute(
        "SELECT batch_id FROM ops WHERE status='done' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    return row[0] if row else None


def undo(conn: sqlite3.Connection, cfg: Config, batch_id: str) -> BatchResult:
    """배치를 역순으로 되돌린다. 원래 자리에 다른 파일이 생겼으면 건너뛴다."""
    res = BatchResult(batch_id)
    rows = conn.execute(
        "SELECT id, kind, src, dst FROM ops WHERE batch_id=? AND status='done' ORDER BY id DESC",
        (batch_id,),
    ).fetchall()
    for r in rows:
        if not os.path.lexists(r["dst"]) or os.path.lexists(r["src"]):
            res.skipped += 1
            continue
        try:
            _move(r["dst"], r["src"])
        except OSError as exc:
            conn.execute("UPDATE ops SET error=? WHERE id=?", (f"undo: {exc}", r["id"]))
            res.failed += 1
            continue
        conn.execute("UPDATE ops SET status='undone', done_at=? WHERE id=?", (time.time(), r["id"]))
        # organize 는 루트 최상위 파일만 옮기므로 src 의 부모가 곧 정리 루트다.
        stop = cfg.quarantine_dir if r["kind"] == "quarantine" else Path(r["src"]).parent
        _prune_empty_parents(r["dst"], stop)
        res.done += 1
    return res


def recover(conn: sqlite3.Connection) -> List[str]:
    """중단돼서 pending 으로 남은 작업을 실제 파일 위치를 보고 확정한다."""
    notes: List[str] = []
    for r in conn.execute("SELECT id, src, dst FROM ops WHERE status='pending'").fetchall():
        if os.path.lexists(r["dst"]) and not os.path.lexists(r["src"]):
            conn.execute(
                "UPDATE ops SET status='done', done_at=? WHERE id=?", (time.time(), r["id"])
            )
            notes.append(f"done   {r['src']}")
        else:
            conn.execute(
                "UPDATE ops SET status='failed', error='interrupted before move' WHERE id=?",
                (r["id"],),
            )
            notes.append(f"failed {r['src']}")
    return notes


_BATCH_ID = re.compile(r"\d{8}-\d{6}-[a-z]+-[0-9a-f]{6}")


def purge_batch(conn: sqlite3.Connection, cfg: Config, batch_id: str) -> bool:
    """격리 배치 하나를 영구 삭제한다. 되돌릴 수 없다. 격리 중인 배치가 아니면 False."""
    if not _BATCH_ID.fullmatch(batch_id):
        return False
    row = conn.execute(
        "SELECT 1 FROM ops WHERE batch_id=? AND kind='quarantine' AND status='done' LIMIT 1",
        (batch_id,),
    ).fetchone()
    if row is None:
        return False
    target = cfg.quarantine_dir / batch_id
    if target.is_dir() and not target.is_symlink():
        shutil.rmtree(target)
    conn.execute(
        "UPDATE ops SET status='purged', done_at=? WHERE batch_id=? AND status='done'",
        (time.time(), batch_id),
    )
    return True


def purge(conn: sqlite3.Connection, cfg: Config, older_than_days: int) -> List[str]:
    """격리한 지 N일 지난 배치를 영구 삭제한다. 되돌릴 수 없다."""
    cutoff = time.time() - older_than_days * 86_400
    rows = conn.execute(
        "SELECT batch_id FROM ops WHERE kind='quarantine' AND status='done'"
        " GROUP BY batch_id HAVING MAX(created_at) < ?",
        (cutoff,),
    ).fetchall()
    return [b for (b,) in rows if purge_batch(conn, cfg, b)]


def _tree_size(path: Path) -> int:
    total = 0
    for dirpath, _, files in os.walk(path):
        for f in files:
            try:
                total += os.lstat(os.path.join(dirpath, f)).st_size
            except OSError:
                continue
    return total


def quarantine_batches(
    conn: sqlite3.Connection, cfg: Config, sizes: Optional[Dict[str, int]] = None
) -> List[Dict[str, Any]]:
    """격리 중인 배치 목록. 최근 것부터, 디스크에서 잰 크기와 함께.

    배치는 만든 뒤로 바뀌지 않으므로 sizes 에 잰 크기를 담아 두고 다시 쓴다.
    """
    sizes = {} if sizes is None else sizes
    rows = conn.execute(
        "SELECT batch_id, MAX(created_at) AS at, COUNT(*) AS n,"
        " GROUP_CONCAT(DISTINCT reason) AS reasons, MIN(src) AS sample"
        " FROM ops WHERE kind='quarantine' AND status='done'"
        " GROUP BY batch_id ORDER BY at DESC"
    ).fetchall()
    for r in rows:
        if r["batch_id"] not in sizes:
            sizes[r["batch_id"]] = _tree_size(cfg.quarantine_dir / r["batch_id"])
    return [
        {
            "batch": r["batch_id"],
            "at": r["at"],
            "count": r["n"],
            "reasons": sorted(set((r["reasons"] or "").split(","))),
            "sample": r["sample"],
            "bytes": sizes[r["batch_id"]],
        }
        for r in rows
    ]


def batch_items(conn: sqlite3.Connection, batch_id: str, limit: int = 300) -> List[str]:
    """격리 배치에 든 원래 경로들 (앞에서 limit 개)."""
    rows = conn.execute(
        "SELECT src FROM ops WHERE batch_id=? AND kind='quarantine' AND status='done'"
        " ORDER BY src LIMIT ?",
        (batch_id, limit),
    ).fetchall()
    return [r[0] for r in rows]
