"""본문 검색 — 허브 문서의 글자를 SQLite FTS5 색인(search.db)에 넣고 찾는다.

색인은 원장과 따로 둔다. 지워도 다음 스캔 때 다시 만들어진다. 바뀐 파일만 다시 읽는다.
PDF·한글(HWP)처럼 표준 라이브러리로 글자를 못 꺼내는 형식은 macOS Spotlight 결과로 보탠다.
"""

import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import zipfile
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Tuple
from xml.etree import ElementTree as ET

from fsledger import hub
from fsledger.config import Config
from fsledger.hashing import is_dataless

MAX_BODY = 64 * 1024
MAX_BODY_IN_REPO = 32 * 1024  # 저장소 안 문서는 코드 설명이 대부분이라 앞부분만
MAX_READ = 2 * 1024 * 1024
MAX_MEMBER = 32 * 1024 * 1024
CHUNK = 300  # 쓰기 잠금을 오래 잡지 않도록 이만큼씩 커밋한다

PLAIN_EXTS = {".md", ".txt", ".mmd", ".mermaid"}
TEXTUTIL_EXTS = {".doc", ".rtf", ".odt"}
# 직접 글자를 꺼내는 형식. 나머지 문서 형식은 Spotlight 에 맡긴다.
INDEXED_EXTS = PLAIN_EXTS | TEXTUTIL_EXTS | {".docx", ".pptx", ".xlsx", ".hwpx", ".ipynb"}

SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS ft USING fts5(
    path UNINDEXED, body, tokenize = 'unicode61 remove_diacritics 2'
);
CREATE TABLE IF NOT EXISTS ft_files (
    path     TEXT PRIMARY KEY,
    docid    INTEGER NOT NULL,
    size     INTEGER NOT NULL,
    mtime_ns INTEGER NOT NULL
);
"""

_WS = re.compile(r"\s+")
_TERM = re.compile(r"[\w]+", re.UNICODE)


def connect(cfg: Config) -> sqlite3.Connection:
    """search.db 를 연다."""
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(cfg.search_db_path, isolation_level=None)
    conn.execute("PRAGMA busy_timeout=60000")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    return conn


# ---- 글자 꺼내기 ------------------------------------------------------------
def _xml_text(data: bytes) -> str:
    root = ET.fromstring(data)
    return " ".join(t.strip() for t in root.itertext() if t.strip())


def _zip_text(path: Path, pick: Callable[[str], bool]) -> str:
    out: List[str] = []
    with zipfile.ZipFile(path) as z:
        names = sorted(n for n in z.namelist() if pick(n))
        for n in names:
            if z.getinfo(n).file_size > MAX_MEMBER:
                continue
            out.append(_xml_text(z.read(n)))
            if sum(map(len, out)) > MAX_BODY:
                break
    return "\n".join(out)


def _notebook_text(path: Path) -> str:
    if path.stat().st_size > MAX_MEMBER:
        return ""
    nb = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    cells = nb.get("cells", []) if isinstance(nb, dict) else []
    parts = []
    for c in cells:
        src = c.get("source", "") if isinstance(c, dict) else ""
        parts.append("".join(src) if isinstance(src, list) else str(src))
    return "\n".join(parts)


def _textutil(path: Path) -> str:
    exe = shutil.which("textutil")
    if exe is None:
        return ""
    try:
        out = subprocess.run(
            [exe, "-convert", "txt", "-stdout", "--", str(path)],
            capture_output=True,
            timeout=20,
            check=True,
        ).stdout
    except (subprocess.SubprocessError, OSError):
        return ""
    return out.decode("utf-8", errors="replace")


def extract_text(path: Path) -> str:
    """파일의 본문 글자. 못 읽으면 빈 문자열."""
    ext = path.suffix.lower()
    try:
        if ext in PLAIN_EXTS:
            with path.open("rb") as f:
                raw = f.read(MAX_READ)
            for enc in ("utf-8-sig", "cp949"):
                try:
                    return raw.decode(enc)
                except UnicodeDecodeError:
                    continue
            return raw.decode("utf-8", errors="replace")
        if ext == ".docx":
            return _zip_text(path, lambda n: n == "word/document.xml")
        if ext == ".pptx":
            return _zip_text(
                path, lambda n: re.fullmatch(r"ppt/slides/slide\d+\.xml", n) is not None
            )
        if ext == ".xlsx":
            return _zip_text(path, lambda n: n == "xl/sharedStrings.xml")
        if ext == ".hwpx":
            return _zip_text(
                path, lambda n: re.fullmatch(r"Contents/section\d+\.xml", n) is not None
            )
        if ext == ".ipynb":
            return _notebook_text(path)
        if ext in TEXTUTIL_EXTS:
            return _textutil(path)
    except (OSError, zipfile.BadZipFile, KeyError, ET.ParseError, ValueError):
        return ""
    return ""


# ---- 색인 갱신 --------------------------------------------------------------
def _candidates(conn: sqlite3.Connection, cfg: Config) -> Dict[str, Tuple[int, int, bool]]:
    """색인할 파일: 허브에 들어가고, 숨김 폴더 밖이며, 글자를 꺼낼 수 있는 형식."""
    roots = sorted((str(r) for r in cfg.roots), key=len, reverse=True)
    out: Dict[str, Tuple[int, int, bool]] = {}
    for path, size, mtime_ns, in_repo in conn.execute(
        "SELECT path, size, mtime_ns, in_repo FROM file_versions WHERE valid_to IS NULL"
    ):
        if os.path.splitext(path)[1].lower() not in INDEXED_EXTS:
            continue
        root = next((r for r in roots if path.startswith(r + "/")), None)
        if root is None or hub.kind_of(path, bool(in_repo)) is None:
            continue
        if hub.in_hidden_dir(path, root):
            continue
        out[path] = (size, mtime_ns, bool(in_repo))
    return out


def update_index(
    ledger: sqlite3.Connection, cfg: Config, progress: Optional[Callable[[int, int], None]] = None
) -> Tuple[int, int]:
    """원장의 현재 상태에 맞춰 색인을 고친다. (새로 읽은 수, 뺀 수)를 돌려준다."""
    want = _candidates(ledger, cfg)
    conn = connect(cfg)
    try:
        have = {
            r[0]: (r[1], r[2], r[3])
            for r in conn.execute("SELECT path, docid, size, mtime_ns FROM ft_files")
        }
        gone = [p for p in have if p not in want]
        todo = [
            p
            for p, (size, mtime, _) in want.items()
            if have.get(p, (0, -1, -1))[1:] != (size, mtime)
        ]
        _delete(conn, [have[p][0] for p in gone], gone)
        done = 0
        for i in range(0, len(todo), CHUNK):
            rows: List[Tuple[str, int, int, str]] = []
            for p in todo[i : i + CHUNK]:
                size, mtime, in_repo = want[p]
                path = Path(p)
                try:
                    if is_dataless(path):
                        continue  # 읽으면 iCloud 다운로드가 시작된다
                except OSError:
                    continue
                cap = MAX_BODY_IN_REPO if in_repo else MAX_BODY
                text = _WS.sub(" ", extract_text(path))[:cap].strip()
                rows.append((p, size, mtime, text))
            _write(conn, have, rows)
            done += len(rows)
            if progress:
                progress(min(i + CHUNK, len(todo)), len(todo))
        return done, len(gone)
    finally:
        conn.close()


def _delete(conn: sqlite3.Connection, docids: List[int], paths: List[str]) -> None:
    if not paths:
        return
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.executemany("DELETE FROM ft WHERE rowid = ?", [(d,) for d in docids])
        conn.executemany("DELETE FROM ft_files WHERE path = ?", [(p,) for p in paths])
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise


def _write(
    conn: sqlite3.Connection,
    have: Dict[str, Tuple[int, int, int]],
    rows: Iterable[Tuple[str, int, int, str]],
) -> None:
    conn.execute("BEGIN IMMEDIATE")
    try:
        for path, size, mtime, text in rows:
            old = have.get(path)
            if old is not None:
                conn.execute("DELETE FROM ft WHERE rowid = ?", (old[0],))
            cur = conn.execute("INSERT INTO ft (path, body) VALUES (?, ?)", (path, text))
            conn.execute(
                "INSERT OR REPLACE INTO ft_files (path, docid, size, mtime_ns) VALUES (?,?,?,?)",
                (path, cur.lastrowid, size, mtime),
            )
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise


# ---- 찾기 ------------------------------------------------------------------
def terms_of(q: str) -> List[str]:
    """검색어를 낱말로 나눈다. 따옴표·연산자는 버린다."""
    return _TERM.findall(q.lower())[:8]


def search(cfg: Config, q: str, limit: int = 300) -> List[Tuple[str, str]]:
    """(경로, 본문 조각) 목록. 낱말마다 앞부분 일치(회의 → 회의록을)로 모두 포함한 문서를 찾는다.

    조각의 일치 부분은 \\x02 … \\x03 로 감싼다.
    """
    terms = terms_of(q)
    if not terms or not cfg.search_db_path.exists():
        return []
    match = " AND ".join(f'"{t}"*' for t in terms)
    conn = connect(cfg)
    try:
        rows = conn.execute(
            "SELECT path, snippet(ft, 1, char(2), char(3), '…', 16) FROM ft"
            " WHERE ft MATCH ? ORDER BY rank LIMIT ?",
            (match, limit),
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    finally:
        conn.close()
    return [(r[0], r[1]) for r in rows]


def indexed_count(cfg: Config) -> int:
    """색인에 들어 있는 파일 수."""
    if not cfg.search_db_path.exists():
        return 0
    conn = connect(cfg)
    try:
        return int(conn.execute("SELECT COUNT(*) FROM ft_files").fetchone()[0])
    finally:
        conn.close()


def spotlight(cfg: Config, q: str, limit: int = 300) -> List[str]:
    """macOS Spotlight 로 본문을 찾는다. PDF·한글 등 직접 못 읽는 형식을 보탠다."""
    exe = shutil.which("mdfind") if sys.platform == "darwin" else None
    terms = terms_of(q)
    if exe is None or not terms:
        return []
    args = [exe]
    for root in cfg.roots:
        args += ["-onlyin", str(root)]
    try:
        res = subprocess.run([*args, " ".join(terms)], capture_output=True, timeout=5, check=False)
    except (subprocess.SubprocessError, OSError):
        return []
    out: List[str] = []
    for line in res.stdout.decode("utf-8", errors="replace").splitlines():
        # 코드 파일 등은 버리고, 직접 색인하지 못하는 허브 문서만 남긴다.
        ext = os.path.splitext(line)[1].lower()
        if ext not in INDEXED_EXTS and hub.kind_of(line, False) is not None:
            out.append(line)
            if len(out) >= limit:
                break
    return out
