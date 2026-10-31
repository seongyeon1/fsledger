"""본문 검색 색인 테스트."""

import sqlite3
import zipfile
from pathlib import Path

import pytest

from fsledger import fulltext
from fsledger.config import Config
from fsledger.db import connect
from fsledger.scan import run_scan


@pytest.fixture
def env(tmp_path: Path) -> tuple[Config, sqlite3.Connection, Path]:
    root = tmp_path / "home"
    root.mkdir()
    cfg = Config(data_dir=tmp_path / "data", roots=[root], organize_roots=[])
    return cfg, connect(cfg.db_path), root


def _write(p: Path, data: bytes) -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    return p


def _zip(p: Path, members: dict) -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(p, "w") as z:
        for k, v in members.items():
            z.writestr(k, v)
    return p


class TestFulltext:
    """색인 갱신과 찾기."""

    def test_index_and_search(self, env: tuple) -> None:
        # Given: 마크다운, 워드, 한글(HWPX), 숨김 폴더 문서
        cfg, conn, root = env
        _write(root / "notes/meeting.md", "# 주간 회의록을 정리했다\n예산 검토".encode())
        _zip(
            root / "docs/plan.docx",
            {"word/document.xml": "<d><p><t>분기 사업계획서 초안</t></p></d>"},
        )
        _zip(root / "docs/form.hwpx", {"Contents/section0.xml": "<s><p>신청서 양식 안내</p></s>"})
        _write(root / ".claude/hidden.md", "회의록 숨김".encode())
        run_scan(conn, cfg)

        # When
        added, gone = fulltext.update_index(conn, cfg)

        # Then: 낱말 앞부분으로 찾고(회의 → 회의록을), 일치 부분을 표시한다
        assert (added, gone) == (3, 0)
        hits = dict(fulltext.search(cfg, "회의"))
        assert list(hits) == [str(root / "notes/meeting.md")]
        assert "\x02회의록을\x03" in hits[str(root / "notes/meeting.md")]
        assert [p for p, _ in fulltext.search(cfg, "사업계획서 초안")] == [
            str(root / "docs/plan.docx")
        ]
        assert [p for p, _ in fulltext.search(cfg, "신청서")] == [str(root / "docs/form.hwpx")]
        assert fulltext.search(cfg, '"; DROP TABLE ft; --') == []

    def test_incremental_update(self, env: tuple) -> None:
        cfg, conn, root = env
        note = _write(root / "a.md", "처음 내용".encode())
        _write(root / "b.md", "그대로".encode())
        run_scan(conn, cfg)
        fulltext.update_index(conn, cfg)

        # When: 하나는 고치고 하나는 지운다
        note.write_text("바뀐 내용", encoding="utf-8")
        (root / "b.md").unlink()
        run_scan(conn, cfg)

        # Then: 바뀐 것만 다시 읽고, 지운 것은 뺀다
        assert fulltext.update_index(conn, cfg) == (1, 1)
        assert fulltext.search(cfg, "처음") == []
        assert [p for p, _ in fulltext.search(cfg, "바뀐")] == [str(note)]
        assert fulltext.search(cfg, "그대로") == []
        assert fulltext.update_index(conn, cfg) == (0, 0)
        assert fulltext.indexed_count(cfg) == 1
