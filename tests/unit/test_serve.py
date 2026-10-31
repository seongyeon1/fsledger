"""로컬 허브 서버와 미리보기 테스트."""

import http.client
import json
import os
import sqlite3
import threading
import urllib.parse
import zipfile
from pathlib import Path
from typing import Any, Dict, Iterator, Optional, Tuple

import pytest

from fsledger import hashing, preview, server
from fsledger.config import Config
from fsledger.db import connect
from fsledger.scan import run_scan


@pytest.fixture
def env(tmp_path: Path) -> Tuple[Config, sqlite3.Connection, Path]:
    root = tmp_path / "home"
    root.mkdir()
    cfg = Config(data_dir=tmp_path / "data", roots=[root], organize_roots=[])
    return cfg, connect(cfg.db_path), root


def _write(p: Path, data: bytes) -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    return p


def _zip(p: Path, members: Dict[str, str]) -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(p, "w") as z:
        for name, text in members.items():
            z.writestr(name, text)
    return p


class TestResolveAllowed:
    """열 수 있는 파일은 원장에 있는 문서·데이터뿐이다."""

    def test_rules(self, env: tuple, tmp_path: Path) -> None:
        cfg, conn, root = env
        doc = _write(root / "notes/a.md", b"# hi")
        code = _write(root / "notes/run.py", b"print(1)")
        outside = _write(tmp_path / "secret.md", b"secret")
        link = _write(root / "notes/link.md", b"later a symlink")
        run_scan(conn, cfg)
        late = _write(root / "notes/late.md", b"not scanned yet")

        assert server.resolve_allowed(conn, cfg, str(doc)) == doc
        assert server.resolve_allowed(conn, cfg, str(code)) is None  # 허브 종류가 아님
        assert server.resolve_allowed(conn, cfg, str(late)) is None  # 원장에 없음
        assert server.resolve_allowed(conn, cfg, str(outside)) is None
        assert server.resolve_allowed(conn, cfg, f"{root}/notes/../notes/a.md") is None

        # 스캔 뒤 루트 밖을 가리키는 심볼릭 링크로 바꿔치기
        link.unlink()
        os.symlink(outside, link)
        assert server.resolve_allowed(conn, cfg, str(link)) is None


@pytest.fixture
def running(env: tuple) -> Iterator[Tuple[server.HubApp, Path]]:
    cfg, conn, root = env
    _write(root / "a.md", b"# title\n\nbody")
    _write(root / "b.pdf", b"%PDF-1.4 fake")
    run_scan(conn, cfg)
    httpd, app = server.make_server(cfg, 0)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    yield app, root
    httpd.shutdown()
    httpd.server_close()


def _req(
    app: server.HubApp,
    method: str,
    path: str,
    headers: Optional[Dict[str, str]] = None,
    body: Optional[bytes] = None,
    host: Optional[str] = None,
) -> Tuple[int, bytes]:
    c = http.client.HTTPConnection("127.0.0.1", app.port, timeout=10)
    h = {"Host": host or f"127.0.0.1:{app.port}", **(headers or {})}
    c.request(method, path, body=body, headers=h)
    r = c.getresponse()
    return r.status, r.read()


class TestServer:
    """토큰·Host 검사와 API."""

    def test_page_needs_token_and_local_host(self, running: tuple) -> None:
        app, _ = running
        assert _req(app, "GET", "/")[0] == 403
        assert _req(app, "GET", "/?t=wrong")[0] == 403
        status, body = _req(app, "GET", f"/?t={app.token}")
        assert status == 200 and b'"a.md"' in body
        assert _req(app, "GET", f"/?t={app.token}", host="evil.example:80")[0] == 403

    def test_preview_and_raw(self, running: tuple) -> None:
        app, root = running
        q = f"/api/preview?path={root}/a.md"
        assert _req(app, "GET", q)[0] == 403
        status, body = _req(app, "GET", q, {"X-Fsledger-Token": app.token})
        data: Dict[str, Any] = json.loads(body)
        assert status == 200 and data["type"] == "markdown" and "title" in data["text"]
        status, _ = _req(
            app, "GET", "/api/preview?path=/etc/hosts", {"X-Fsledger-Token": app.token}
        )
        assert status == 404
        status, body = _req(app, "GET", f"/api/raw?path={root}/b.pdf&t={app.token}")
        assert status == 200 and body.startswith(b"%PDF")
        assert _req(app, "GET", f"/api/raw?path={root}/a.md&t={app.token}")[0] == 404

    def test_open_requires_token(self, running: tuple) -> None:
        app, root = running
        body = json.dumps({"path": f"{root}/a.md"}).encode()
        assert _req(app, "POST", "/api/open", body=body)[0] == 403
        other = json.dumps({"path": "/etc/hosts"}).encode()
        assert _req(app, "POST", "/api/open", {"X-Fsledger-Token": app.token}, other)[0] == 404


def _wait_scan(app: server.HubApp) -> Dict[str, Any]:
    for _ in range(200):
        status, body = _req(app, "GET", "/api/state", {"X-Fsledger-Token": app.token})
        st: Dict[str, Any] = json.loads(body)
        if status == 200 and not st["scanning"]:
            return st
        threading.Event().wait(0.05)
    raise AssertionError("스캔이 끝나지 않았습니다")


class TestSync:
    """동기화 버튼과 자동 스캔: 새 파일이 바로 허브에 들어온다."""

    def test_sync_picks_up_new_file(self, running: tuple) -> None:
        app, root = running
        assert _req(app, "GET", "/api/state")[0] == 403
        assert _req(app, "POST", "/api/sync")[0] == 403
        before = _wait_scan(app)["version"]
        _write(root / "new.md", b"# fresh")
        q = f"/api/preview?path={root}/new.md"
        assert _req(app, "GET", q, {"X-Fsledger-Token": app.token})[0] == 404

        assert _req(app, "POST", "/api/sync", {"X-Fsledger-Token": app.token})[0] == 200
        st = _wait_scan(app)
        assert st["version"] > before and st["error"] is None
        assert _req(app, "GET", q, {"X-Fsledger-Token": app.token})[0] == 200
        assert b'"new.md"' in _req(app, "GET", f"/?t={app.token}")[1]

    def test_unchanged_scan_keeps_version(self, running: tuple) -> None:
        app, _ = running
        before = _wait_scan(app)["version"]
        app.sync()
        assert _wait_scan(app)["version"] == before  # 변경 없는 스캔으로 화면을 새로 고치지 않는다

    def test_trash_syncs(self, running: tuple) -> None:
        app, root = running
        before = _wait_scan(app)["version"]
        body = json.dumps({"path": f"{root}/a.md"}).encode()
        assert _req(app, "POST", "/api/trash", {"X-Fsledger-Token": app.token}, body)[0] == 200
        assert _wait_scan(app)["version"] > before

    def test_concurrent_scans_serialize(self, env: tuple) -> None:
        cfg, conn, root = env
        for i in range(50):
            _write(root / f"d/{i}.md", b"x" * i)
        errors: list = []

        def scan() -> None:
            c = connect(cfg.db_path)
            try:
                run_scan(c, cfg)
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)
            finally:
                c.close()

        threads = [threading.Thread(target=scan) for _ in range(3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert not errors
        current = conn.execute("SELECT COUNT(*) FROM file_versions WHERE valid_to IS NULL")
        added = conn.execute("SELECT COUNT(*) FROM changes WHERE kind = 'added'")
        assert current.fetchone()[0] == 50 and added.fetchone()[0] == 50


class TestPreview:
    """종류별 미리보기."""

    def test_csv_and_text(self, tmp_path: Path) -> None:
        p = _write(tmp_path / "t.csv", "이름,값\n가,1\n".encode("cp949"))
        data = preview.preview(p)
        assert data["type"] == "table" and data["sheets"][0]["rows"][1] == ["가", "1"]
        j = _write(tmp_path / "d.json", b'{"a":1}')
        assert '"a": 1' in preview.preview(j)["text"]

    def test_xlsx(self, tmp_path: Path) -> None:
        ns = 'xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"'
        rel = 'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"'
        p = _zip(
            tmp_path / "s.xlsx",
            {
                "xl/workbook.xml": f"<workbook {ns} {rel}><sheets>"
                '<sheet name="요약" sheetId="1" r:id="rId1"/></sheets></workbook>',
                "xl/_rels/workbook.xml.rels": '<Relationships xmlns="http://schemas.openxmlformats.org'
                '/package/2006/relationships"><Relationship Id="rId1"'
                ' Target="worksheets/sheet1.xml" Type="x"/></Relationships>',
                "xl/sharedStrings.xml": f"<sst {ns}><si><t>매출</t></si></sst>",
                "xl/worksheets/sheet1.xml": f'<worksheet {ns}><sheetData><row r="1">'
                '<c r="A1" t="s"><v>0</v></c><c r="C1"><v>42</v></c></row></sheetData></worksheet>',
            },
        )
        data = preview.preview(p)
        assert data["sheets"][0]["name"] == "요약"
        assert data["sheets"][0]["rows"][0] == ["매출", "", "42"]

    def test_pptx_and_notebook(self, tmp_path: Path) -> None:
        a = 'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"'
        p = _zip(
            tmp_path / "d.pptx",
            {
                "ppt/slides/slide2.xml": f"<s {a}><a:p><a:t>둘째</a:t></a:p></s>",
                "ppt/slides/slide1.xml": f"<s {a}><a:p><a:t>첫째</a:t></a:p></s>",
            },
        )
        assert [s["lines"] for s in preview.preview(p)["slides"]] == [["첫째"], ["둘째"]]
        nb = {"cells": [{"cell_type": "code", "source": ["x = 1"], "outputs": [{"text": "1"}]}]}
        n = _write(tmp_path / "n.ipynb", json.dumps(nb).encode())
        cell = preview.preview(n)["cells"][0]
        assert cell["source"] == "x = 1" and cell["outputs"] == ["1"]

    def test_docx_and_broken_file(self, tmp_path: Path) -> None:
        w = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
        p = _zip(
            tmp_path / "r.docx",
            {
                "word/document.xml": f"<w:document {w}><w:body><w:p><w:r><w:t>보고서</w:t>"
                "</w:r></w:p></w:body></w:document>"
            },
        )
        data = preview.preview(p)
        # macOS 는 textutil(HTML), 그 밖은 XML 에서 뽑은 텍스트
        assert data["type"] in ("html", "text", "error")
        if data["type"] == "text":
            assert "보고서" in data["text"]
        bad = _write(tmp_path / "x.xlsx", b"not a zip")
        assert preview.preview(bad)["type"] == "error"

    def test_dataless_is_not_read(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        p = _write(tmp_path / "c.md", b"# cloud")
        monkeypatch.setattr(preview, "is_dataless", lambda _: True)
        assert preview.preview(p) == {"type": "cloud"}
        assert hashing.SF_DATALESS  # 실제 판정 함수는 hashing 테스트가 다룬다


class TestTrash:
    """허브에서 삭제는 격리 폴더로 옮기기이고, 같은 서버에서 만든 배치만 되돌린다."""

    def test_trash_then_undo(self, running: tuple) -> None:
        # Given
        app, root = running
        target = root / "a.md"
        tok = {"X-Fsledger-Token": app.token}
        body = json.dumps({"path": str(target)}).encode()

        # When: 토큰 없이 → 거부, 토큰과 함께 → 격리
        assert _req(app, "POST", "/api/trash", body=body)[0] == 403
        status, raw = _req(app, "POST", "/api/trash", tok, body)
        batch = json.loads(raw)["batch"]

        # Then
        assert status == 200 and not target.exists()
        moved = list((app.cfg.quarantine_dir / batch).rglob("a.md"))
        assert len(moved) == 1
        # 다시 지우려 하면 파일이 없으니 거부된다
        assert _req(app, "POST", "/api/trash", tok, body)[0] == 404
        # 모르는 배치는 되돌릴 수 없다
        bogus = json.dumps({"batch": "20260101-000000-clean-abcdef"}).encode()
        assert _req(app, "POST", "/api/undo", tok, bogus)[0] == 404
        undo = json.dumps({"batch": batch}).encode()
        assert _req(app, "POST", "/api/undo", tok, undo)[0] == 200
        assert target.read_bytes().startswith(b"# title")
        assert _req(app, "POST", "/api/undo", tok, undo)[0] == 404  # 한 번만


class TestQuarantineBin:
    """격리함: 배치 목록, 복원, 영구 삭제."""

    def _trash(self, app: server.HubApp, path: Path) -> str:
        tok = {"X-Fsledger-Token": app.token}
        body = json.dumps({"path": str(path)}).encode()
        status, raw = _req(app, "POST", "/api/trash", tok, body)
        assert status == 200
        return str(json.loads(raw)["batch"])

    def test_list_restore_purge(self, running: tuple) -> None:
        # Given: 파일 두 개를 따로 지움
        app, root = running
        tok = {"X-Fsledger-Token": app.token}
        a, b = self._trash(app, root / "a.md"), self._trash(app, root / "b.pdf")

        # When / Then: 목록에 크기와 원래 경로가 나온다
        assert _req(app, "GET", "/api/quarantine")[0] == 403
        batches = json.loads(_req(app, "GET", "/api/quarantine", tok)[1])["batches"]
        by_id = {x["batch"]: x for x in batches}
        assert set(by_id) == {a, b} and by_id[a]["count"] == 1
        assert by_id[a]["bytes"] == len(b"# title\n\nbody") and by_id[a]["sample"] == str(
            root / "a.md"
        )
        items = json.loads(_req(app, "GET", f"/api/quarantine/items?batch={a}", tok)[1])["items"]
        assert items == [str(root / "a.md")]

        # 복원은 이 서버가 만든 배치가 아니어도 된다
        app.batches.clear()
        status, raw = _req(app, "POST", "/api/restore", tok, json.dumps({"batch": a}).encode())
        assert status == 200 and json.loads(raw)["done"] == 1 and (root / "a.md").exists()
        assert _req(app, "POST", "/api/restore", tok, json.dumps({"batch": a}).encode())[0] == 404

        # 영구 삭제: 디스크에서 사라지고 다시 복원할 수 없다
        purge = json.dumps({"batch": b}).encode()
        assert _req(app, "POST", "/api/purge", body=purge)[0] == 403
        assert _req(app, "POST", "/api/purge", tok, purge)[0] == 200
        assert not (app.cfg.quarantine_dir / b).exists() and not (root / "b.pdf").exists()
        assert _req(app, "POST", "/api/restore", tok, json.dumps({"batch": b}).encode())[0] == 404
        assert json.loads(_req(app, "GET", "/api/quarantine", tok)[1])["batches"] == []
        _wait_scan(app)

    def test_purge_rejects_bad_batch(self, running: tuple) -> None:
        app, _ = running
        tok = {"X-Fsledger-Token": app.token}
        for bad in ("../../etc", "", "20260101-000000-clean-abcdef"):
            body = json.dumps({"batch": bad}).encode()
            assert _req(app, "POST", "/api/purge", tok, body)[0] == 404

    def test_purge_all(self, running: tuple) -> None:
        app, root = running
        tok = {"X-Fsledger-Token": app.token}
        self._trash(app, root / "a.md")
        self._trash(app, root / "b.pdf")
        status, raw = _req(app, "POST", "/api/purge", tok, json.dumps({"all": True}).encode())
        assert status == 200 and json.loads(raw)["purged"] == 2
        assert not any(app.cfg.quarantine_dir.iterdir())
        _wait_scan(app)


class TestTrashFolder:
    """폴더 삭제: 루트·데이터 폴더·모르는 폴더·심볼릭 링크는 거부, 폴더째 격리 후 되돌린다."""

    def test_rules(self, env: tuple) -> None:
        # Given
        cfg, conn, root = env
        _write(root / "proj" / "sub" / "a.md", b"# a")
        (root / "empty").mkdir()
        (root / "link").symlink_to(root / "proj")
        run_scan(conn, cfg)

        # When / Then
        assert server.resolve_folder(conn, cfg, str(root / "proj")) == root / "proj"
        assert server.resolve_folder(conn, cfg, str(root)) is None  # 루트 자체
        assert server.resolve_folder(conn, cfg, str(root / "empty")) is None  # 원장에 없음
        assert server.resolve_folder(conn, cfg, str(root / "link")) is None  # 링크
        assert server.resolve_folder(conn, cfg, str(root / "proj" / ".." / "proj")) is None
        assert server.resolve_folder(conn, cfg, "proj") is None  # 상대 경로
        assert server.resolve_folder(conn, cfg, str(root.parent)) is None  # 루트 밖

    def test_trash_then_undo(self, running: tuple) -> None:
        # Given
        app, root = running
        _write(root / "proj" / "x.md", b"# x")
        (root / "proj" / ".git").mkdir()
        conn = app.conn()
        run_scan(conn, app.cfg)
        conn.close()
        tok = {"X-Fsledger-Token": app.token}
        q = "/api/folder?path=" + urllib.parse.quote(str(root / "proj"))

        # When
        status, raw = _req(app, "GET", q, tok)
        info = json.loads(raw)
        body = json.dumps({"path": str(root / "proj")}).encode()
        assert _req(app, "POST", "/api/trash-folder", body=body)[0] == 403
        status2, raw2 = _req(app, "POST", "/api/trash-folder", tok, body)

        # Then
        assert status == 200 and info["files"] >= 1
        assert info["repos"] == [str(root / "proj")]
        assert status2 == 200 and not (root / "proj").exists()
        root_body = json.dumps({"path": str(root)}).encode()
        assert _req(app, "POST", "/api/trash-folder", tok, root_body)[0] == 404
        undo = json.dumps({"batch": json.loads(raw2)["batch"]}).encode()
        assert _req(app, "POST", "/api/undo", tok, undo)[0] == 200
        assert (root / "proj" / "x.md").read_bytes() == b"# x"


class TestDiagramsAndHwp:
    """다이어그램·한글 미리보기 형식과 한글 저장(원본 백업·되돌리기)."""

    def test_preview_types(self, tmp_path: Path) -> None:
        # Given
        mmd = _write(tmp_path / "a.mmd", b"graph LR\n A-->B\n")
        exc = _write(tmp_path / "b.excalidraw", json.dumps({"elements": []}).encode())
        bad = _write(tmp_path / "c.excalidraw", b'{"x": 1}')
        hwp = _write(tmp_path / "d.hwp", server._MAGIC[".hwp"] + b"\0" * 8)

        # When / Then
        assert preview.preview(mmd) == {
            "type": "mermaid",
            "text": "graph LR\n A-->B\n",
            "truncated": False,
        }
        assert preview.preview(exc)["type"] == "excalidraw"
        assert preview.preview(bad)["type"] == "error"
        assert preview.preview(hwp) == {"type": "hwp"}

    def test_save_and_undo(self, env: tuple) -> None:
        # Given: 편집기를 켠 서버
        cfg, conn, root = env
        original = server._MAGIC[".hwp"] + b"original"
        target = _write(root / "doc.hwp", original)
        run_scan(conn, cfg)
        cfg.hwp_editor = True
        httpd, app = server.make_server(cfg, 0)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        tok = {"X-Fsledger-Token": app.token}
        where = {"X-Fsledger-Path": urllib.parse.quote(str(target))}
        try:
            # When: 미리보기 → 원본 내려받기 → 저장
            status, raw = _req(
                app, "GET", "/api/preview?path=" + urllib.parse.quote(str(target)), tok
            )
            assert json.loads(raw) == {"type": "hwp", "editable": True}
            q = f"/api/raw?path={urllib.parse.quote(str(target))}&t={app.token}"
            assert _req(app, "GET", q)[1] == original
            edited = server._MAGIC[".hwp"] + b"edited"
            assert _req(app, "POST", "/api/save", {**tok, **where}, b"PK\x03\x04zip")[0] == 400
            status, raw = _req(app, "POST", "/api/save", {**tok, **where}, edited)

            # Then: 새 내용이 제자리에, 원본은 격리 폴더에
            assert status == 200 and target.read_bytes() == edited
            batch = json.loads(raw)["batch"]
            assert list((cfg.quarantine_dir / batch).rglob("doc.hwp"))[0].read_bytes() == original
            undo = json.dumps({"batch": batch}).encode()
            assert _req(app, "POST", "/api/undo-save", tok, undo)[0] == 200
            assert target.read_bytes() == original
            assert _req(app, "POST", "/api/undo-save", tok, undo)[0] == 404

            # 편집기를 끄면 저장 자체가 거부된다
            cfg.hwp_editor = False
            assert _req(app, "POST", "/api/save", {**tok, **where}, edited)[0] == 403
        finally:
            httpd.shutdown()
            httpd.server_close()
