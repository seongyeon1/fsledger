"""설정 로드 — 기본값 위에 ~/.fsledger/config.toml 을 덮어쓴다."""

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

HOME = Path.home()
DEFAULT_DATA_DIR = HOME / ".fsledger"

# 들어가서 추적하지 않고, 크기만 집계해 정리 후보로 보고하는 디렉터리 (재생성 가능).
DEFAULT_JUNK_DIRS: List[str] = [
    "node_modules",
    ".venv",
    "venv",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".next",
    ".turbo",
    ".parcel-cache",
]

# 추적하지도, 보고하지도 않는 디렉터리.
DEFAULT_IGNORE_DIRS: List[str] = [".git", ".Trash", "Library", ".fsledger"]

CATEGORIES: Dict[str, List[str]] = {
    "문서": [
        ".pdf",
        ".doc",
        ".docx",
        ".hwp",
        ".hwpx",
        ".txt",
        ".md",
        ".rtf",
        ".pages",
        ".xls",
        ".xlsx",
        ".csv",
        ".numbers",
        ".ppt",
        ".pptx",
        ".key",
    ],
    "이미지": [".png", ".jpg", ".jpeg", ".gif", ".heic", ".webp", ".svg", ".bmp", ".tiff", ".psd"],
    "동영상": [".mp4", ".mov", ".avi", ".mkv", ".webm", ".m4v"],
    "오디오": [".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg"],
    "압축": [".zip", ".tar", ".gz", ".tgz", ".7z", ".rar", ".bz2", ".xz"],
    "설치파일": [".dmg", ".pkg", ".iso", ".app", ".exe", ".msi"],
    "코드·데이터": [
        ".py",
        ".js",
        ".ts",
        ".json",
        ".yaml",
        ".yml",
        ".ipynb",
        ".sql",
        ".html",
        ".sh",
        ".toml",
        ".xml",
        ".drawio",
    ],
}


@dataclass
class OrganizeRoot:
    """정리 대상 폴더 하나의 규칙."""

    path: Path
    group_by_month: bool = False
    auto: bool = False


@dataclass
class Config:
    """fsledger 실행 설정."""

    data_dir: Path = DEFAULT_DATA_DIR
    roots: List[Path] = field(
        default_factory=lambda: [HOME / "Desktop", HOME / "Documents", HOME / "Downloads"]
    )
    junk_dirs: List[str] = field(default_factory=lambda: list(DEFAULT_JUNK_DIRS))
    ignore_dirs: List[str] = field(default_factory=lambda: list(DEFAULT_IGNORE_DIRS))
    dupe_min_size: int = 1024
    organize_min_age_days: int = 2
    # 한글 편집기는 외부(rhwp-studio) 화면을 불러오므로 직접 켜야 한다.
    hwp_editor: bool = False
    # serve 가 켜져 있는 동안 몇 분마다 다시 스캔할지. 0 이면 끈다 (동기화 버튼은 그대로 된다).
    rescan_minutes: int = 15
    # 매일 자동 실행 때 격리한 지 며칠 지난 배치를 영구 삭제할지. 0 이면 끈다 (기본: 끔).
    purge_after_days: int = 0
    # 허브 본문 검색용 색인(search.db). 끄면 이름·경로로만 찾는다.
    fulltext: bool = True
    # 매일 자동 실행 때 다른 폴더에 같은 내용이 있는 다운로드 사본을 격리할지.
    clean_download_dupes: bool = False
    organize_roots: List[OrganizeRoot] = field(
        default_factory=lambda: [
            OrganizeRoot(HOME / "Downloads", group_by_month=True, auto=True),
            OrganizeRoot(HOME / "Desktop", group_by_month=False, auto=False),
        ]
    )

    @property
    def db_path(self) -> Path:
        """SQLite DB 경로."""
        return self.data_dir / "ledger.db"

    @property
    def search_db_path(self) -> Path:
        """본문 검색 색인. 지워도 다음 스캔 때 다시 만든다."""
        return self.data_dir / "search.db"

    @property
    def quarantine_dir(self) -> Path:
        """삭제 제안을 실행할 때 파일을 옮겨 두는 격리 폴더."""
        return self.data_dir / "quarantine"

    @property
    def dashboard_path(self) -> Path:
        """대시보드 HTML 경로."""
        return self.data_dir / "dashboard.html"

    @property
    def hub_path(self) -> Path:
        """문서·데이터 허브 HTML 경로."""
        return self.data_dir / "hub.html"


def _expand(p: str) -> Path:
    return Path(os.path.expanduser(p)).resolve()


def load_config(path: Optional[Path] = None) -> Config:
    """설정 파일이 있으면 읽어 기본값을 덮어쓴다."""
    cfg = Config()
    env_dir = os.environ.get("FSLEDGER_HOME")
    if env_dir:
        cfg.data_dir = _expand(env_dir)
    path = path or cfg.data_dir / "config.toml"
    if not path.exists():
        return cfg
    raw: Dict[str, Any] = tomllib.loads(path.read_text(encoding="utf-8"))
    if "roots" in raw:
        cfg.roots = [_expand(p) for p in raw["roots"]]
    if "junk_dirs" in raw:
        cfg.junk_dirs = list(raw["junk_dirs"])
    if "ignore_dirs" in raw:
        cfg.ignore_dirs = list(raw["ignore_dirs"])
    cfg.dupe_min_size = int(raw.get("dupe_min_size", cfg.dupe_min_size))
    cfg.organize_min_age_days = int(raw.get("organize_min_age_days", cfg.organize_min_age_days))
    cfg.hwp_editor = bool(raw.get("hwp_editor", cfg.hwp_editor))
    cfg.rescan_minutes = max(int(raw.get("rescan_minutes", cfg.rescan_minutes)), 0)
    cfg.purge_after_days = max(int(raw.get("purge_after_days", cfg.purge_after_days)), 0)
    cfg.fulltext = bool(raw.get("fulltext", cfg.fulltext))
    cfg.clean_download_dupes = bool(raw.get("clean_download_dupes", cfg.clean_download_dupes))
    if "organize" in raw:
        cfg.organize_roots = [
            OrganizeRoot(
                _expand(o["path"]),
                group_by_month=bool(o.get("group_by_month", False)),
                auto=bool(o.get("auto", False)),
            )
            for o in raw["organize"]
        ]
    return cfg


def category_of(name: str) -> str:
    """확장자로 카테고리 이름을 고른다."""
    suffix = Path(name).suffix.lower()
    for cat, exts in CATEGORIES.items():
        if suffix in exts:
            return cat
    return "기타"
