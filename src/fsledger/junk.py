"""불필요 파일 탐지 — 규칙마다 이유와 확신도(safe / review)를 붙여 삭제 후보를 만든다."""

import re
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Set

from fsledger.dupes import DupeGroup

DAY_NS = 86_400 * 1_000_000_000

SAFE = "safe"  # 지워도 OS·앱이 다시 만들거나 내용이 없는 것
REVIEW = "review"  # 사람이 한 번 보고 판단해야 하는 것

OS_LITTER = {".DS_Store", "Thumbs.db", "desktop.ini", "Icon\r"}
PARTIAL_DOWNLOADS = {".crdownload", ".part", ".download", ".partial", ".tmp", ".opdownload"}
INSTALLERS = {".dmg", ".pkg", ".iso", ".msi", ".exe"}
EMPTY_OK = {"__init__.py", ".gitkeep", ".keep", "py.typed", ".nojekyll", ".gitignore"}
CACHE_DIRS = {
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".parcel-cache",
    ".turbo",
    ".next",
}
# "보고서 (1).pdf", "보고서 1.zip" 처럼 다운로드가 붙이는 복사 접미사.
_COPY_SUFFIX = re.compile(r"(\s\(\d+\)|\s\d+)$")


@dataclass
class Suggestion:
    """삭제 후보 하나."""

    path: str
    rule: str
    reason: str
    bytes: int
    level: str
    is_dir: bool = False


def _file_rules(conn: sqlite3.Connection, now_ns: int) -> Iterable[Suggestion]:
    rows = conn.execute(
        "SELECT path, size, mtime_ns, in_repo FROM file_versions WHERE valid_to IS NULL"
    )
    for r in rows:
        path, size, age_days = r["path"], r["size"], (now_ns - r["mtime_ns"]) / DAY_NS
        p = Path(path)
        name, suffix = p.name, p.suffix.lower()
        in_downloads = "/Downloads/" in path

        if name in OS_LITTER:
            yield Suggestion(
                path, "os-litter", "Finder·Windows가 다시 만드는 메타데이터 파일", size, SAFE
            )
        elif name.startswith("._") and size <= 4096:
            yield Suggestion(
                path, "appledouble", "외장 디스크·zip에서 생긴 AppleDouble 조각", size, SAFE
            )
        elif name.startswith("~$") and age_days > 1:
            yield Suggestion(path, "office-lock", "Office가 남긴 잠금 파일", size, SAFE)
        elif suffix in PARTIAL_DOWNLOADS and age_days > 1:
            yield Suggestion(path, "partial-download", "하루 넘게 멈춘 미완료 다운로드", size, SAFE)
        elif size == 0 and not r["in_repo"] and name not in EMPTY_OK:
            yield Suggestion(path, "empty-file", "내용이 없는 0바이트 파일", 0, REVIEW)
        elif in_downloads and suffix in INSTALLERS and age_days > 14:
            yield Suggestion(
                path,
                "old-installer",
                f"{int(age_days)}일 지난 설치 파일 — 이미 설치했다면 필요 없음",
                size,
                REVIEW,
            )
        elif in_downloads and size >= 100 * 1024 * 1024 and age_days > 180:
            yield Suggestion(
                path,
                "stale-large-download",
                f"{int(age_days)}일 동안 안 건드린 대용량 다운로드",
                size,
                REVIEW,
            )


def _extracted_archives(conn: sqlite3.Connection) -> Iterable[Suggestion]:
    """옆에 같은 이름의 폴더가 있는 압축 파일 = 이미 풀어 둔 것."""
    rows = conn.execute(
        "SELECT path, size FROM file_versions WHERE valid_to IS NULL AND in_repo = 0"
        " AND (path LIKE '%.zip' OR path LIKE '%.tar.gz' OR path LIKE '%.tgz')"
    )
    for r in rows:
        p = Path(r["path"])
        stem = p.name.removesuffix(".tar.gz").removesuffix(".tgz").removesuffix(".zip")
        for candidate in {stem, _COPY_SUFFIX.sub("", stem)}:
            sibling = p.with_name(candidate)
            if candidate and sibling.is_dir():
                yield Suggestion(
                    str(p),
                    "extracted-archive",
                    f"같은 이름의 폴더({sibling.name}/)로 이미 풀려 있음",
                    r["size"],
                    REVIEW,
                )
                break


def _junk_dir_rules(conn: sqlite3.Connection, now_ns: int) -> Iterable[Suggestion]:
    for r in conn.execute("SELECT path, name, bytes, parent_mtime_ns FROM junk_dirs"):
        idle_days = (now_ns - r["parent_mtime_ns"]) / DAY_NS
        if r["name"] in CACHE_DIRS:
            yield Suggestion(
                r["path"],
                "cache-dir",
                "도구가 다시 만드는 캐시 디렉터리",
                r["bytes"],
                SAFE,
                is_dir=True,
            )
        elif idle_days > 60:
            yield Suggestion(
                r["path"],
                "stale-deps",
                f"{int(idle_days)}일 동안 안 쓴 프로젝트의 의존성 — 재설치 가능",
                r["bytes"],
                REVIEW,
                is_dir=True,
            )


def _dupe_rules(groups: List[DupeGroup], repo_files: Set[str]) -> Iterable[Suggestion]:
    for g in groups:
        keep = g.paths[0]
        for path in g.paths[1:]:
            if path in repo_files:
                continue  # 저장소 안 파일은 git 이 관리한다
            yield Suggestion(path, "duplicate", f"중복 — 원본 유지: {keep}", g.size, REVIEW)


def collect(conn: sqlite3.Connection, groups: List[DupeGroup]) -> List[Suggestion]:
    """모든 규칙을 돌려 경로별로 하나씩만 남긴 후보 목록을 만든다."""
    now_ns = time.time_ns()
    repo_files = {
        r[0]
        for r in conn.execute(
            "SELECT path FROM file_versions WHERE valid_to IS NULL AND in_repo = 1"
        )
    }
    by_path: Dict[str, Suggestion] = {}
    sources: List[Iterable[Suggestion]] = [
        _file_rules(conn, now_ns),
        _extracted_archives(conn),
        _junk_dir_rules(conn, now_ns),
        _dupe_rules(groups, repo_files),
    ]
    for source in sources:
        for s in source:
            by_path.setdefault(s.path, s)
    return sorted(by_path.values(), key=lambda s: (s.level != SAFE, -s.bytes))
