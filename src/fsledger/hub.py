"""문서·데이터 허브 — 원장의 현재 상태에서 문서·데이터 파일만 골라 한 페이지로 색인한다.

파일은 옮기지 않는다. 경로·크기·수정 시각만 모아 검색할 수 있게 만든다.
"""

import os
import re
import sqlite3
import time
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from fsledger import page
from fsledger.config import Config
from fsledger.hashing import is_dataless

KINDS: Dict[str, Tuple[str, ...]] = {
    "문서": (".pdf", ".doc", ".docx", ".hwp", ".hwpx", ".rtf", ".pages", ".odt"),
    "발표": (".ppt", ".pptx", ".key", ".odp"),
    "스프레드시트": (".xls", ".xlsx", ".numbers", ".ods"),
    "노트": (".md", ".txt"),
    "데이터": (
        ".csv",
        ".tsv",
        ".parquet",
        ".feather",
        ".jsonl",
        ".ndjson",
        ".json",
        ".sqlite",
        ".db",
        ".duckdb",
        ".h5",
        ".hdf5",
        ".npy",
        ".npz",
    ),
    "노트북": (".ipynb",),
    "다이어그램": (".drawio", ".excalidraw"),
}
_KIND_OF = {ext: kind for kind, exts in KINDS.items() for ext in exts}

# 저장소 안에서는 설정 파일·로그로 넘쳐나는 확장자라 저장소 밖에 있을 때만 넣는다.
OUTSIDE_REPO_ONLY = {".txt", ".json", ".db"}

# 빌드 산출물·의존성·작업 트리 사본 안의 문서는 내 자료가 아니다.
SKIP_PARTS = {
    "site-packages",
    "dist",
    "build",
    "vendor",
    "coverage",
    ".tox",
    "target",
    ".worktrees",
}
_VENV = re.compile(r"^\.?venv")

ICLOUD_SUFFIX = "Library/Mobile Documents/com~apple~CloudDocs"


def kind_of(path: str, in_repo: bool) -> Optional[str]:
    """허브에 넣을 종류. 넣지 않을 파일이면 None."""
    ext = os.path.splitext(path)[1].lower()
    kind = _KIND_OF.get(ext)
    if kind is None or (in_repo and ext in OUTSIDE_REPO_ONLY):
        return None
    parts = path.split("/")
    if not SKIP_PARTS.isdisjoint(parts) or any(_VENV.match(x) for x in parts):
        return None
    return kind


def in_hidden_dir(path: str, root: str) -> bool:
    """루트 아래 경로에 숨김 폴더(.claude, .agents 등 도구 상태)가 끼어 있는지."""
    return any(x.startswith(".") for x in path[len(root) + 1 :].split("/")[:-1])


def root_label(root: str) -> str:
    """루트를 사람이 읽는 이름으로."""
    return "iCloud Drive" if root.endswith(ICLOUD_SUFFIX) else Path(root).name


def _nearest(path: str, marks: Set[str], stop: str) -> Optional[str]:
    p = os.path.dirname(path)
    while len(p) > len(stop):
        if p in marks:
            return p
        p = os.path.dirname(p)
    return None


def project_of(
    path: str, root: str, repos: Set[str], repo_parents: Set[str]
) -> Tuple[str, str, bool]:
    """(프로젝트 이름, 프로젝트 경로, 저장소 여부).

    가장 가까운 git 저장소 → 저장소를 바로 아래에 둔 폴더(여러 저장소 + data/ 를 묶은 프로젝트)
    → 루트 바로 아래 폴더 순으로 고른다.
    """
    repo = _nearest(path, repos, root)
    if repo is not None:
        return os.path.basename(repo), repo, True
    parent = _nearest(path, repo_parents, root)
    if parent is not None:
        return os.path.basename(parent), parent, False
    rest = path[len(root) + 1 :]
    if "/" not in rest:
        return f"{root_label(root)} (낱개 파일)", root, False
    head = rest.split("/", 1)[0]
    return f"{root_label(root)}/{head}", f"{root}/{head}", False


def build_data(conn: sqlite3.Connection, cfg: Config) -> Dict[str, Any]:
    """허브 페이지가 쓰는 JSON 을 만든다.

    행은 [폴더 번호, 이름, 종류, 프로젝트, 크기, 수정 시각(초), 플래그] 배열이다.
    플래그 bit0 = iCloud 에만 있음, bit1 = 숨김 폴더 안.
    """
    roots = sorted((str(r) for r in cfg.roots), key=len, reverse=True)
    repos = {r[0] for r in conn.execute("SELECT path FROM repos")}
    repo_parents = {os.path.dirname(r) for r in repos} - set(roots)
    kinds = list(KINDS)
    projects: Dict[str, int] = {}
    dirs: Dict[str, int] = {}
    project_rows: List[Dict[str, Any]] = []
    rows: List[List[Any]] = []
    for path, size, mtime_ns, in_repo in conn.execute(
        "SELECT path, size, mtime_ns, in_repo FROM file_versions WHERE valid_to IS NULL"
    ):
        root = next((r for r in roots if path.startswith(r + "/")), None)
        kind = kind_of(path, bool(in_repo))
        if kind is None or root is None:
            continue
        pname, ppath, is_repo = project_of(path, root, repos, repo_parents)
        pid = projects.get(ppath)
        if pid is None:
            pid = projects[ppath] = len(project_rows)
            project_rows.append({"name": pname, "path": ppath, "repo": is_repo})
        try:
            cloud = is_dataless(Path(path))
        except OSError:
            continue  # 마지막 스캔 뒤로 사라진 파일
        flags = int(cloud) | int(in_hidden_dir(path, root)) << 1
        parent, name = os.path.split(path)
        did = dirs.setdefault(parent, len(dirs))
        rows.append([did, name, kinds.index(kind), pid, size, mtime_ns // 1_000_000_000, flags])

    counts = Counter(r[3] for r in rows)
    for i, p in enumerate(project_rows):
        p["n"] = counts[i]
    return {
        "generated_at": time.time(),
        "home": str(Path.home()),
        "roots": [{"path": r, "label": root_label(r)} for r in sorted(roots)],
        "kinds": kinds,
        "projects": project_rows,
        "dirs": list(dirs),
        "rows": rows,
    }


def write_hub(cfg: Config, data: Dict[str, Any]) -> Path:
    """템플릿에 데이터를 넣어 허브 파일을 쓴다."""
    return page.render("hub.html", data, cfg.hub_path)
