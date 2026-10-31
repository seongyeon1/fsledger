"""문서·데이터 허브와 iCloud 자리표시 파일 보호 테스트."""

import os
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict

import pytest

from fsledger import dupes, hashing, hub
from fsledger.config import Config
from fsledger.db import connect
from fsledger.scan import run_scan


@pytest.fixture
def env(tmp_path: Path) -> tuple[Config, sqlite3.Connection, Path]:
    root = tmp_path / "home"
    root.mkdir()
    cfg = Config(data_dir=tmp_path / "data", roots=[root], dupe_min_size=1, organize_roots=[])
    return cfg, connect(cfg.db_path), root


def _write(p: Path, data: bytes = b"x") -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    return p


def _by_name(data: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    out = {}
    for r in data["rows"]:
        p = data["projects"][r[3]]
        out[r[1]] = {"kind": data["kinds"][r[2]], "project": p["name"], "flags": r[6]}
    return out


class TestHub:
    """허브 색인."""

    def test_kind_filters(self) -> None:
        """설정·빌드 산출물·venv 는 빼고 문서·데이터만 고른다."""
        assert hub.kind_of("/h/a/report.PDF", False) == "문서"
        assert hub.kind_of("/h/a/notes.txt", False) == "노트"
        assert hub.kind_of("/h/repo/requirements.txt", True) is None
        assert hub.kind_of("/h/repo/package.json", True) is None
        assert hub.kind_of("/h/repo/data/rows.jsonl", True) == "데이터"
        assert hub.kind_of("/h/repo/dist/guide.md", True) is None
        assert hub.kind_of("/h/p/venv311/share/doc.md", False) is None
        assert hub.kind_of("/h/a/photo.png", False) is None

    def test_groups_by_repo_then_repo_parent_then_top_folder(self, env: tuple) -> None:
        """저장소 → 저장소를 담은 프로젝트 폴더 → 루트 바로 아래 폴더 순으로 묶는다."""
        # Given
        cfg, conn, root = env
        (root / "work/proj/app/.git").mkdir(parents=True)
        _write(root / "work/proj/app/README.md")
        _write(root / "work/proj/data/raw.csv")
        _write(root / "work/proj/.claude/plan.md")
        _write(root / "misc/old/memo.docx")
        _write(root / "loose.pdf")
        run_scan(conn, cfg)

        # When
        rows = _by_name(hub.build_data(conn, cfg))

        # Then
        assert rows["README.md"]["project"] == "app"
        assert rows["raw.csv"]["project"] == "proj"
        assert rows["memo.docx"]["project"] == "home/misc"
        assert rows["loose.pdf"]["project"] == "home (낱개 파일)"
        assert rows["plan.md"]["flags"] & 2  # 숨김 폴더 안
        assert not rows["raw.csv"]["flags"] & 2

    def test_write_hub_embeds_data(self, env: tuple) -> None:
        """템플릿 자리표시가 데이터로 바뀐 단일 파일을 쓴다."""
        cfg, conn, root = env
        _write(root / "a.md")
        run_scan(conn, cfg)
        path = hub.write_hub(cfg, hub.build_data(conn, cfg))
        html = path.read_text(encoding="utf-8")
        for marker in ("/*__DATA__*/null", "/*__THEME__*/", "/*__ICONS__*/null"):
            assert marker not in html
        assert '"a.md"' in html
        assert "--ease: cubic-bezier" in html  # 공통 테마가 들어갔다


class TestDataless:
    """iCloud 에만 있는 파일은 읽지 않는다 (읽으면 다운로드가 시작된다)."""

    def test_hash_refuses_dataless_and_dupes_skip_it(
        self, env: tuple, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Given: 내용이 같은 두 파일 중 하나가 dataless
        cfg, conn, root = env
        a = _write(root / "a.bin", b"same")
        b = _write(root / "b.bin", b"same")
        run_scan(conn, cfg)
        real_stat = os.stat

        def fake_stat(p: Any, *args: Any, **kw: Any) -> Any:
            st = real_stat(p, *args, **kw)
            if str(p) == str(b):
                return SimpleNamespace(st_flags=hashing.SF_DATALESS)
            return st

        monkeypatch.setattr(hashing.os, "stat", fake_stat)

        # When / Then
        with pytest.raises(OSError):
            hashing.partial_hash(b)
        assert hashing.partial_hash(a)
        dupes.hash_candidates(conn, 1)
        assert dupes.find_groups(conn, 1) == []
