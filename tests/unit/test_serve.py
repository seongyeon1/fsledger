"""로컬 허브 서버와 미리보기 테스트."""

import http.client
import json
import os
import sqlite3
import threading
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
