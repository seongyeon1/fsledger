"""SQLite 스키마 — 스냅샷 체인, 시간 테이블, 변경 이벤트, 선기록 작업 저널."""

import sqlite3
from pathlib import Path

SCHEMA = """
-- git commit 에 해당: 스캔 1회 = 스냅샷 1개, parent_id 로 체인을 이룬다.
CREATE TABLE IF NOT EXISTS snapshots (
    id          INTEGER PRIMARY KEY,
    parent_id   INTEGER REFERENCES snapshots(id),
    started_at  REAL NOT NULL,
    finished_at REAL,
    roots       TEXT NOT NULL,
    files       INTEGER DEFAULT 0,
    bytes       INTEGER DEFAULT 0,
    added       INTEGER DEFAULT 0,
    modified    INTEGER DEFAULT 0,
    deleted     INTEGER DEFAULT 0,
    moved       INTEGER DEFAULT 0
);

-- SCD Type 2 시간 테이블: 현재 상태는 valid_to IS NULL.
-- (size, mtime_ns, ino, dev) 는 git index 의 stat 캐시 역할을 한다.
CREATE TABLE IF NOT EXISTS file_versions (
    id           INTEGER PRIMARY KEY,
    path         TEXT NOT NULL,
    valid_from   INTEGER NOT NULL REFERENCES snapshots(id),
    valid_to     INTEGER REFERENCES snapshots(id),
    size         INTEGER NOT NULL,
    mtime_ns     INTEGER NOT NULL,
    ino          INTEGER NOT NULL,
    dev          INTEGER NOT NULL,
    partial_hash TEXT,
    content_hash TEXT,
    racy         INTEGER NOT NULL DEFAULT 0,
    in_repo      INTEGER NOT NULL DEFAULT 0
);
CREATE UNIQUE INDEX IF NOT EXISTS fv_current ON file_versions(path) WHERE valid_to IS NULL;
CREATE INDEX IF NOT EXISTS fv_size ON file_versions(size) WHERE valid_to IS NULL;
CREATE INDEX IF NOT EXISTS fv_hash ON file_versions(content_hash) WHERE valid_to IS NULL;
CREATE INDEX IF NOT EXISTS fv_path_hist ON file_versions(path, valid_from);

-- CDC 이벤트 스트림.
CREATE TABLE IF NOT EXISTS changes (
    id          INTEGER PRIMARY KEY,
    snapshot_id INTEGER NOT NULL REFERENCES snapshots(id),
    kind        TEXT NOT NULL CHECK (kind IN ('added', 'modified', 'deleted', 'moved')),
    path        TEXT NOT NULL,
    old_path    TEXT,
    size_delta  INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ch_snapshot ON changes(snapshot_id);

-- 들어가지 않고 크기만 잰 디렉터리 (node_modules 등). 최신 상태만 유지.
CREATE TABLE IF NOT EXISTS junk_dirs (
    path          TEXT PRIMARY KEY,
    name          TEXT NOT NULL,
    bytes         INTEGER NOT NULL,
    files         INTEGER NOT NULL,
    dir_mtime_ns  INTEGER NOT NULL,
    parent_mtime_ns INTEGER NOT NULL,
    last_snapshot INTEGER NOT NULL
);

-- git 저장소 루트. 정리 대상에서 제외한다.
CREATE TABLE IF NOT EXISTS repos (
    path          TEXT PRIMARY KEY,
    last_snapshot INTEGER NOT NULL
);

-- WAL 원칙: 파일을 옮기기 전에 pending 으로 먼저 기록하고, 끝나면 done.
CREATE TABLE IF NOT EXISTS ops (
    id         INTEGER PRIMARY KEY,
    batch_id   TEXT NOT NULL,
    kind       TEXT NOT NULL CHECK (kind IN ('quarantine', 'organize')),
    src        TEXT NOT NULL,
    dst        TEXT NOT NULL,
    reason     TEXT,
    status     TEXT NOT NULL
        CHECK (status IN ('pending', 'done', 'failed', 'undone', 'purged')),
    created_at REAL NOT NULL,
    done_at    REAL,
    error      TEXT
);
CREATE INDEX IF NOT EXISTS ops_batch ON ops(batch_id);
"""


def connect(db_path: Path) -> sqlite3.Connection:
    """DB 를 WAL 모드로 열고 스키마를 보장한다."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA)
    return conn
