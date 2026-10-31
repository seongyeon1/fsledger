"""스캔·중복·정리·저널 단위 테스트."""

import os
import sqlite3
import time
from pathlib import Path
from typing import Dict, List

import pytest

from fsledger import dupes, junk, ops, organize
from fsledger.config import Config, OrganizeRoot
from fsledger.db import connect
from fsledger.scan import ScanAborted, run_scan

OLD = time.time() - 10 * 86_400


def _write(p: Path, data: bytes, mtime: float = OLD) -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    os.utime(p, (mtime, mtime))
    return p


@pytest.fixture
def env(tmp_path: Path) -> tuple[Config, sqlite3.Connection, Path]:
    root = tmp_path / "home"
    downloads = root / "Downloads"
    downloads.mkdir(parents=True)
    cfg = Config(
        data_dir=tmp_path / "data",
        roots=[root],
        dupe_min_size=1,
        organize_roots=[OrganizeRoot(downloads, group_by_month=True, auto=True)],
    )
    return cfg, connect(cfg.db_path), root


def _changes(conn: sqlite3.Connection, snap: int) -> Dict[str, List[str]]:
    out: Dict[str, List[str]] = {}
    for r in conn.execute("SELECT kind, path FROM changes WHERE snapshot_id=?", (snap,)):
        out.setdefault(r["kind"], []).append(Path(r["path"]).name)
    return out


class TestScan:
    """스캔 변경 감지."""

    def test_detects_added_modified_deleted_moved(self, env: tuple) -> None:
        """두 번째 스캔이 네 종류 변경을 모두 구분해야 한다."""
        # Given
        cfg, conn, root = env
        _write(root / "a.txt", b"aaa")
        _write(root / "b.txt", b"bbb")
        _write(root / "c.txt", b"ccc")
        run_scan(conn, cfg)

        # When
        _write(root / "a.txt", b"aaaa", mtime=OLD + 5)
        (root / "b.txt").unlink()
        (root / "sub").mkdir()
        (root / "c.txt").rename(root / "sub" / "c.txt")
        _write(root / "d.txt", b"ddd")
        res = run_scan(conn, cfg)

        # Then
        ch = _changes(conn, res.snapshot_id)
        assert ch == {
            "modified": ["a.txt"],
            "deleted": ["b.txt"],
            "moved": ["c.txt"],
            "added": ["d.txt"],
        }
        assert (res.added, res.modified, res.deleted, res.moved) == (1, 1, 1, 1)

    def test_unchanged_files_produce_no_new_versions(self, env: tuple) -> None:
        """stat 이 같으면 새 버전을 만들지 않아야 한다 (stat 캐시)."""
        # Given
        cfg, conn, root = env
        _write(root / "a.txt", b"aaa")
        run_scan(conn, cfg)

        # When
        res = run_scan(conn, cfg)

        # Then
        assert (res.added, res.modified, res.deleted, res.moved) == (0, 0, 0, 0)
        assert conn.execute("SELECT COUNT(*) FROM file_versions").fetchone()[0] == 1

    def test_history_keeps_closed_versions(self, env: tuple) -> None:
        """수정 시 옛 버전은 valid_to 로 닫히고 남아 있어야 한다 (SCD2)."""
        # Given
        cfg, conn, root = env
        f = _write(root / "a.txt", b"v1")
        s1 = run_scan(conn, cfg).snapshot_id
        _write(f, b"v2-longer", mtime=OLD + 5)

        # When
        s2 = run_scan(conn, cfg).snapshot_id

        # Then
        rows = conn.execute(
            "SELECT valid_from, valid_to, size FROM file_versions ORDER BY id"
        ).fetchall()
        assert [tuple(r) for r in rows] == [(s1, s2, 2), (s2, None, 9)]

    def test_racy_file_rechecked_even_when_stat_matches(self, env: tuple) -> None:
        """스캔 직전 수정된 파일은 stat 이 같아도 내용을 다시 비교해야 한다 (racy git)."""
        # Given: 방금 수정된 파일 → racy 로 기록
        cfg, conn, root = env
        f = root / "hot.txt"
        f.write_bytes(b"AAAA")
        st = f.stat()
        run_scan(conn, cfg)
        assert conn.execute("SELECT racy FROM file_versions").fetchone()[0] == 1

        # When: 같은 크기·같은 mtime 으로 내용만 바뀜
        f.write_bytes(b"BBBB")
        os.utime(f, ns=(st.st_atime_ns, st.st_mtime_ns))
        res = run_scan(conn, cfg)

        # Then
        assert res.modified == 1

    def test_git_repo_files_marked_and_junk_dirs_measured(self, env: tuple) -> None:
        """저장소 안 파일은 in_repo, node_modules 는 들어가지 않고 크기만 기록해야 한다."""
        # Given
        cfg, conn, root = env
        (root / "proj" / ".git").mkdir(parents=True)
        _write(root / "proj" / "main.py", b"print(1)")
        _write(root / "proj" / "node_modules" / "x" / "index.js", b"x" * 100)

        # When
        run_scan(conn, cfg)

        # Then
        paths = {r[0]: r[1] for r in conn.execute("SELECT path, in_repo FROM file_versions")}
        assert paths == {str(root / "proj" / "main.py"): 1}
        jd = conn.execute("SELECT name, bytes, files FROM junk_dirs").fetchone()
        assert tuple(jd) == ("node_modules", 100, 1)

    def test_unreadable_subdir_is_not_recorded_as_deleted(self, env: tuple) -> None:
        """권한이 막힌 폴더 아래 파일은 삭제가 아니라 그대로 유지돼야 한다."""
        # Given
        cfg, conn, root = env
        _write(root / "locked" / "a.txt", b"aaa")
        _write(root / "open.txt", b"bbb")
        run_scan(conn, cfg)
        (root / "locked").chmod(0)

        # When
        try:
            res = run_scan(conn, cfg)
        finally:
            (root / "locked").chmod(0o755)

        # Then
        assert (res.deleted, res.files) == (0, 2)

    def test_unreadable_root_aborts_without_snapshot(self, env: tuple) -> None:
        """루트를 못 읽으면 스냅샷을 남기지 않고 중단해야 한다 (TCC 차단 대비)."""
        # Given
        cfg, conn, root = env
        _write(root / "a.txt", b"aaa")
        run_scan(conn, cfg)
        root.chmod(0)

        # When / Then
        try:
            with pytest.raises(ScanAborted):
                run_scan(conn, cfg)
        finally:
            root.chmod(0o755)
        assert conn.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0] == 1


class TestDupes:
    """중복 탐지."""

    def test_groups_same_content_and_skips_hardlinks(self, env: tuple) -> None:
        """같은 내용은 묶고, 하드링크와 크기만 같은 파일은 제외해야 한다."""
        # Given
        cfg, conn, root = env
        _write(root / "docs" / "report.pdf", b"X" * 5000)
        _write(root / "Downloads" / "report (1).pdf", b"X" * 5000)
        _write(root / "other.bin", b"Y" * 5000)
        os.link(root / "docs" / "report.pdf", root / "hard.pdf")
        run_scan(conn, cfg)

        # When
        dupes.hash_candidates(conn, 1)
        groups = dupes.find_groups(conn, 1)

        # Then
        assert len(groups) == 1
        names = [Path(p).name for p in groups[0].paths]
        assert names[-1] == "report (1).pdf"  # Downloads 쪽 복사본이 후보
        assert len(names) == 2

    def test_large_files_differing_after_partial_are_not_dupes(self, env: tuple) -> None:
        """앞 64KiB 가 같아도 뒤가 다르면 중복이 아니어야 한다."""
        # Given
        cfg, conn, root = env
        head = b"H" * (70 * 1024)
        _write(root / "a.bin", head + b"1")
        _write(root / "b.bin", head + b"2")
        run_scan(conn, cfg)

        # When
        dupes.hash_candidates(conn, 1)

        # Then
        assert dupes.find_groups(conn, 1) == []


class TestJunk:
    """불필요 파일 규칙."""

    def test_rules_flag_litter_partial_and_extracted_archive(self, env: tuple) -> None:
        """OS 찌꺼기·미완료 다운로드·이미 푼 압축을 각각 잡아야 한다."""
        # Given
        cfg, conn, root = env
        dl = root / "Downloads"
        _write(dl / ".DS_Store", b"x")
        _write(dl / "movie.mp4.crdownload", b"x" * 10)
        _write(dl / "과제 1.zip", b"zip")
        (dl / "과제").mkdir()
        _write(root / "pkg" / "__init__.py", b"")
        run_scan(conn, cfg)

        # When
        rules = {Path(s.path).name: s.rule for s in junk.collect(conn, [])}

        # Then
        assert rules == {
            ".DS_Store": "os-litter",
            "movie.mp4.crdownload": "partial-download",
            "과제 1.zip": "extracted-archive",
        }


class TestOrganizeAndJournal:
    """정리 계획과 되돌리기."""

    def test_organize_apply_then_undo_restores_everything(self, env: tuple) -> None:
        """정리 후 undo 하면 원위치되고 빈 카테고리 폴더도 사라져야 한다."""
        # Given
        cfg, conn, root = env
        dl = root / "Downloads"
        _write(dl / "a.pdf", b"pdf")
        _write(dl / "b.png", b"png")
        _write(dl / "fresh.pdf", b"new", mtime=time.time())  # 최근 파일은 제외
        (dl / "folder").mkdir()
        before = sorted(p.name for p in dl.iterdir())

        # When
        moves = organize.plan(cfg, set())
        res = ops.apply(conn, moves, "b1")

        # Then
        assert res.done == 2
        month = time.strftime("%Y-%m", time.localtime(OLD))
        assert (dl / "문서" / month / "a.pdf").exists()
        assert (dl / "이미지" / month / "b.png").exists()

        # When
        ops.undo(conn, cfg, "b1")

        # Then
        assert sorted(p.name for p in dl.iterdir()) == before

    def test_apply_avoids_overwriting_existing_destination(self, env: tuple) -> None:
        """대상에 같은 이름이 있으면 덮어쓰지 않고 비켜 가야 한다."""
        # Given
        cfg, conn, root = env
        dl = root / "Downloads"
        month = time.strftime("%Y-%m", time.localtime(OLD))
        _write(dl / "문서" / month / "a.pdf", b"existing")
        _write(dl / "a.pdf", b"incoming")

        # When
        ops.apply(conn, organize.plan(cfg, set()), "b2")

        # Then
        assert (dl / "문서" / month / "a.pdf").read_bytes() == b"existing"
        assert (dl / "문서" / month / "a (2).pdf").read_bytes() == b"incoming"

    def test_quarantine_then_scan_reports_move_not_delete(self, env: tuple) -> None:
        """격리는 이동이므로 파일이 사라지지 않고 undo 로 돌아와야 한다."""
        # Given
        cfg, conn, root = env
        f = _write(root / "Downloads" / ".DS_Store", b"x")
        run_scan(conn, cfg)
        dst = ops.quarantine_target(cfg, "q1", str(f))

        # When
        ops.apply(conn, [ops.Move("quarantine", str(f), dst, "os-litter")], "q1")

        # Then
        assert not f.exists() and Path(dst).exists()
        ops.undo(conn, cfg, "q1")
        assert f.exists()
        assert not (cfg.quarantine_dir / "q1").exists()

    def test_recover_settles_interrupted_ops(self, env: tuple) -> None:
        """pending 으로 남은 작업은 실제 파일 위치로 done/failed 를 정해야 한다."""
        # Given: 이동은 끝났지만 done 기록 전에 죽은 상황과, 이동 전에 죽은 상황
        cfg, conn, root = env
        moved = _write(root / "x" / "moved.txt", b"1")
        stuck = _write(root / "stuck.txt", b"2")
        for src, dst in [
            (str(root / "moved.txt"), str(moved)),
            (str(stuck), str(root / "y" / "stuck.txt")),
        ]:
            conn.execute(
                "INSERT INTO ops (batch_id, kind, src, dst, status, created_at)"
                " VALUES ('b', 'organize', ?, ?, 'pending', 0)",
                (src, dst),
            )

        # When
        ops.recover(conn)

        # Then
        status = [r[0] for r in conn.execute("SELECT status FROM ops ORDER BY id")]
        assert status == ["done", "failed"]


class TestPurge:
    """격리분 영구 삭제: 나이 기준, 자동 실행은 설정을 켰을 때만."""

    def _quarantine(self, conn: sqlite3.Connection, cfg: Config, src: Path, age_days: float) -> str:
        batch = ops.new_batch_id("clean")
        ops.apply(
            conn,
            [ops.Move("quarantine", str(src), ops.quarantine_target(cfg, batch, str(src)), "t")],
            batch,
        )
        conn.execute(
            "UPDATE ops SET created_at=? WHERE batch_id=?", (time.time() - age_days * 86_400, batch)
        )
        return batch

    def test_purge_by_age(self, env: tuple) -> None:
        # Given
        cfg, conn, root = env
        old = self._quarantine(conn, cfg, _write(root / "old.txt", b"o"), 40)
        new = self._quarantine(conn, cfg, _write(root / "new.txt", b"n"), 1)

        # When
        purged = ops.purge(conn, cfg, 30)

        # Then
        assert purged == [old]
        assert not (cfg.quarantine_dir / old).exists() and (cfg.quarantine_dir / new).exists()
        assert ops.undo(conn, cfg, old).done == 0  # 영구 삭제한 배치는 되돌릴 수 없다
        assert [b["batch"] for b in ops.quarantine_batches(conn, cfg)] == [new]

    def test_auto_purges_only_when_enabled(self, env: tuple) -> None:
        # Given
        from fsledger import cli

        cfg, conn, root = env
        old = self._quarantine(conn, cfg, _write(root / "old.txt", b"o"), 40)
        args = cli.build_parser().parse_args(["auto"])

        # When: 기본값(0)이면 지우지 않는다
        cli.cmd_auto(conn, cfg, args)
        assert (cfg.quarantine_dir / old).exists()

        # When: 켜면 오래된 배치를 지운다
        cfg.purge_after_days = 30
        cli.cmd_auto(conn, cfg, args)
        assert not (cfg.quarantine_dir / old).exists()
