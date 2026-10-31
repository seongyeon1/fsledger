"""로컬 허브 서버 — 127.0.0.1 에서만 열고, 실행마다 새 토큰을 요구한다.

file:// 페이지는 다른 파일을 읽을 수 없어서, 미리보기·앱에서 열기는 이 서버를 거친다.
열 수 있는 파일은 원장의 현재 상태에 있고 허브 종류에 들어가는 문서·데이터뿐이다.
"""

import hmac
import json
import os
import secrets
import sqlite3
import subprocess
import sys
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Set, Tuple
from urllib.parse import parse_qs, unquote, urlparse

from fsledger import dupes, fulltext, hub, ops, page, preview
from fsledger.config import Config
from fsledger.db import connect
from fsledger.scan import run_scan

_CHUNK = 1024 * 1024
MAX_SAVE = 200 * 1024 * 1024
# 저장할 바이트가 정말 그 형식인지 첫 바이트로 확인한다 (HWP 는 OLE 복합 파일, HWPX 는 zip).
_MAGIC = {".hwp": b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1", ".hwpx": b"PK\x03\x04"}


def resolve_allowed(conn: sqlite3.Connection, cfg: Config, raw: str) -> Optional[Path]:
    """요청한 경로가 열어도 되는 파일이면 Path, 아니면 None.

    경로는 원장에 적힌 그대로여야 하고(../ 우회 차단), 실제 위치도 추적 루트 안이어야 한다
    (스캔 뒤 심볼릭 링크로 바꿔치기 차단).
    """
    row = conn.execute(
        "SELECT in_repo FROM file_versions WHERE path=? AND valid_to IS NULL", (raw,)
    ).fetchone()
    if row is None or hub.kind_of(raw, bool(row[0])) is None:
        return None
    real = os.path.realpath(raw)
    roots = [os.path.realpath(r) + os.sep for r in cfg.roots]
    if not any(real.startswith(r) for r in roots) or not os.path.isfile(real):
        return None
    return Path(raw)


def resolve_folder(conn: sqlite3.Connection, cfg: Config, raw: str) -> Optional[Path]:
    """통째로 지워도 되는 폴더면 Path, 아니면 None.

    추적 루트 안쪽이어야 하고 루트 자체는 안 된다. 경로에 심볼릭 링크가 끼면 안 되고,
    원장에 그 아래 파일 기록이 있어야 한다 (모르는 폴더는 건드리지 않는다).
    """
    if not raw or not os.path.isabs(raw) or os.path.normpath(raw) != raw:
        return None
    if os.path.islink(raw) or not os.path.isdir(raw) or os.path.realpath(raw) != raw:
        return None
    roots = [str(r) for r in cfg.roots]
    if raw in roots or not any(raw.startswith(r + os.sep) for r in roots):
        return None
    data = str(cfg.data_dir)
    if raw == data or raw.startswith(data + os.sep):
        return None
    # '/' 다음 문자가 '0' 이라 [raw/, raw0) 범위가 곧 raw 아래 전체다.
    hit = conn.execute(
        "SELECT 1 FROM file_versions WHERE valid_to IS NULL AND path > ? AND path < ? LIMIT 1",
        (raw + "/", raw + "0"),
    ).fetchone()
    return Path(raw) if hit else None


def folder_info(conn: sqlite3.Connection, raw: str) -> Dict[str, Any]:
    """확인 창에 보여 줄 폴더 요약: 원장 기준 전체 파일 수·크기, 걸린 git 저장소."""
    lo, hi = raw + "/", raw + "0"
    files, size = conn.execute(
        "SELECT COUNT(*), COALESCE(SUM(size), 0) FROM file_versions"
        " WHERE valid_to IS NULL AND path > ? AND path < ?",
        (lo, hi),
    ).fetchone()
    junk = conn.execute(
        "SELECT COALESCE(SUM(bytes), 0) FROM junk_dirs WHERE path > ? AND path < ?", (lo, hi)
    ).fetchone()[0]
    repos = [
        r[0]
        for r in conn.execute("SELECT path FROM repos")
        if r[0] == raw or r[0].startswith(lo) or raw.startswith(r[0] + "/")
    ]
    return {"files": files, "bytes": size + junk, "repos": sorted(repos)[:20]}


def _write_atomic(path: Path, data: bytes) -> None:
    """같은 폴더의 임시 파일에 쓴 뒤 바꿔치기해서, 중간에 끊겨도 반쪽 파일이 남지 않게 한다."""
    tmp = path.with_name(f".{path.name}.fsledger-tmp")
    with tmp.open("xb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


class HubApp:
    """서버 상태: 설정, 토큰, 마지막으로 만든 페이지, 백그라운드 스캔."""

    def __init__(self, cfg: Config, port: int) -> None:
        self.cfg = cfg
        self.port = port
        self.token = secrets.token_urlsafe(24)
        self._lock = threading.Lock()
        self._html: Optional[str] = None
        self._version: Optional[str] = None
        # 스캔 뒤 중복 해시가 새로 채워지면 올린다. 파일 변경이 없어도 허브를 다시 그려야 한다.
        self.derived = 0
        self.indexing = False
        self.batches: Set[str] = set()  # 이 서버에서 만든 삭제 배치만 되돌릴 수 있다
        self.saves: Dict[str, str] = {}  # 저장 배치 → 저장한 파일 경로
        self.bin_sizes: Dict[str, int] = {}  # 격리 배치 → 디스크 크기 (격리함 목록용)
        self._scan_lock = threading.Lock()
        self._scan_again = False  # 스캔 중에 들어온 요청은 끝난 뒤 한 번 더 돈다
        self.scanning = False
        self.scan_error: Optional[str] = None

    def conn(self) -> sqlite3.Connection:
        """요청마다 새 연결 (sqlite 연결은 스레드끼리 나눠 쓰지 않는다)."""
        return connect(self.cfg.db_path)

    def version(self, conn: sqlite3.Connection) -> str:
        """허브 내용의 버전: 내용이 바뀐 스냅샷 + 이 서버에서 새로 채운 중복 해시."""
        return f"{hub.content_version(conn)}.{self.derived}"

    def page_html(self) -> str:
        """버전이 바뀌었을 때만 허브 페이지를 다시 만든다."""
        conn = self.conn()
        try:
            version = self.version(conn)
            with self._lock:
                if self._html is None or version != self._version:
                    data = hub.build_data(conn, self.cfg)
                    data["version"] = version
                    self._html = page.build("hub.html", data)
                    self._version = version
                return self._html
        finally:
            conn.close()

    def state(self) -> Dict[str, Any]:
        """허브가 새 내용을 가져올지 판단할 상태."""
        conn = self.conn()
        try:
            last = conn.execute("SELECT MAX(finished_at) FROM snapshots").fetchone()[0]
            version = self.version(conn)
        finally:
            conn.close()
        return {
            "version": version,
            "scanned_at": last,
            "scanning": self.scanning,
            "indexing": self.indexing,
            "error": self.scan_error,
        }

    def sync(self) -> None:
        """백그라운드에서 다시 스캔한다. 이미 도는 중이면 끝난 뒤 한 번 더 돈다."""
        with self._scan_lock:
            if self.scanning:
                self._scan_again = True
                return
            self.scanning = True
        threading.Thread(target=self._scan_loop, name="fsledger-scan", daemon=True).start()

    def _scan_loop(self) -> None:
        while True:
            try:
                conn = self.conn()
                try:
                    run_scan(conn, self.cfg)
                    if any(dupes.hash_candidates(conn, self.cfg.dupe_min_size)):
                        self.derived += 1
                    if self.cfg.fulltext:
                        self.indexing = True
                        try:
                            fulltext.update_index(conn, self.cfg)
                        finally:
                            self.indexing = False
                finally:
                    conn.close()
                self.scan_error = None
            except Exception as exc:  # noqa: BLE001 — 스캔이 실패해도 서버는 계속 돈다
                self.scan_error = str(exc) or type(exc).__name__
                print(f"스캔 실패: {self.scan_error}", file=sys.stderr, flush=True)
            with self._scan_lock:
                if not self._scan_again:
                    self.scanning = False
                    return
                self._scan_again = False

    def start_auto_sync(self, stop: threading.Event) -> None:
        """rescan_minutes 마다 다시 스캔한다. 마지막 스캔이 그보다 오래됐으면 바로 한 번 돈다."""
        interval = self.cfg.rescan_minutes * 60
        if interval <= 0:
            return

        def loop() -> None:
            last = self.state()["scanned_at"] or 0
            if time.time() - last >= interval:
                self.sync()
            while not stop.wait(interval):
                self.sync()

        threading.Thread(target=loop, name="fsledger-auto-sync", daemon=True).start()

    def url(self) -> str:
        """브라우저로 열 주소."""
        return f"http://127.0.0.1:{self.port}/?t={self.token}"


def make_handler(app: HubApp) -> type:
    """앱에 묶인 요청 처리 클래스를 만든다."""

    class Handler(BaseHTTPRequestHandler):
        server_version = "fsledger"

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            return  # 경로가 로그에 남지 않게 한다

        # --- 공통 검사 -----------------------------------------------------
        def _host_ok(self) -> bool:
            # DNS rebinding 차단: 다른 이름으로 들어온 요청은 받지 않는다.
            host = self.headers.get("Host", "")
            return host in (f"127.0.0.1:{app.port}", f"localhost:{app.port}")

        def _token_ok(self, given: Optional[str]) -> bool:
            return given is not None and hmac.compare_digest(given, app.token)

        def _query(self) -> Tuple[str, Dict[str, str]]:
            u = urlparse(self.path)
            return u.path, {k: v[0] for k, v in parse_qs(u.query).items()}

        def _send(self, status: int, body: bytes, ctype: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, status: int, data: Dict[str, Any]) -> None:
            self._send(status, json.dumps(data, ensure_ascii=False).encode(), "application/json")

        def _deny(self, status: int = HTTPStatus.FORBIDDEN) -> None:
            self._json(status, {"error": "허용되지 않은 요청입니다"})

        def _file(self, q: Dict[str, str]) -> Optional[Path]:
            conn = app.conn()
            try:
                return resolve_allowed(conn, app.cfg, q.get("path", ""))
            finally:
                conn.close()

        # --- 라우팅 ---------------------------------------------------------
        def do_GET(self) -> None:  # noqa: N802
            if not self._host_ok():
                return self._deny()
            route, q = self._query()
            if route == "/":
                if not self._token_ok(q.get("t")):
                    return self._send(
                        HTTPStatus.FORBIDDEN,
                        "fsledger serve 가 출력한 주소로 열어 주세요.".encode(),
                        "text/plain; charset=utf-8",
                    )
                html = app.page_html().encode()
                return self._send(HTTPStatus.OK, html, "text/html; charset=utf-8")
            if route == "/api/state":
                if not self._token_ok(self.headers.get("X-Fsledger-Token")):
                    return self._deny()
                return self._json(HTTPStatus.OK, app.state())
            if route in ("/api/quarantine", "/api/quarantine/items"):
                if not self._token_ok(self.headers.get("X-Fsledger-Token")):
                    return self._deny()
                conn = app.conn()
                try:
                    if route == "/api/quarantine":
                        batches = ops.quarantine_batches(conn, app.cfg, app.bin_sizes)
                        return self._json(HTTPStatus.OK, {"batches": batches})
                    items = ops.batch_items(conn, q.get("batch", ""))
                    return self._json(HTTPStatus.OK, {"items": items})
                finally:
                    conn.close()
            if route == "/api/search":
                if not self._token_ok(self.headers.get("X-Fsledger-Token")):
                    return self._deny()
                text = q.get("q", "")[:200]
                hits = fulltext.search(app.cfg, text) if app.cfg.fulltext else []
                return self._json(
                    HTTPStatus.OK,
                    {
                        "hits": hits,
                        "spotlight": fulltext.spotlight(app.cfg, text),
                        "indexing": app.indexing,
                    },
                )
            if route == "/api/preview":
                if not self._token_ok(self.headers.get("X-Fsledger-Token")):
                    return self._deny()
                path = self._file(q)
                if path is None:
                    return self._deny(HTTPStatus.NOT_FOUND)
                data = preview.preview(path)
                if data.get("type") == "hwp":
                    data["editable"] = app.cfg.hwp_editor
                return self._json(HTTPStatus.OK, data)
            if route == "/api/folder":
                if not self._token_ok(self.headers.get("X-Fsledger-Token")):
                    return self._deny()
                conn = app.conn()
                try:
                    folder = resolve_folder(conn, app.cfg, q.get("path", ""))
                    if folder is None:
                        return self._deny(HTTPStatus.NOT_FOUND)
                    return self._json(HTTPStatus.OK, folder_info(conn, str(folder)))
                finally:
                    conn.close()
            if route == "/api/raw":
                # iframe 은 헤더를 못 붙이므로 토큰을 쿼리로 받는다. PDF·한글만 내보낸다.
                if not self._token_ok(q.get("t")):
                    return self._deny()
                path = self._file(q)
                mime = preview.RAW_EXTS.get(path.suffix.lower(), "") if path else ""
                if path is None or not mime:
                    return self._deny(HTTPStatus.NOT_FOUND)
                return self._stream(path, mime)
            return self._deny(HTTPStatus.NOT_FOUND)

        def do_POST(self) -> None:  # noqa: N802
            if not self._host_ok() or not self._token_ok(self.headers.get("X-Fsledger-Token")):
                return self._deny()
            route, _ = self._query()
            if route == "/api/save":
                return self._save()
            length = min(int(self.headers.get("Content-Length", "0") or 0), 64 * 1024)
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
            except ValueError:
                return self._deny(HTTPStatus.BAD_REQUEST)
            if not isinstance(body, dict):
                return self._deny(HTTPStatus.BAD_REQUEST)
            if route == "/api/sync":
                app.sync()
                return self._json(HTTPStatus.OK, {"ok": True})
            if route == "/api/open":
                return self._open(body)
            if route == "/api/trash":
                return self._trash(body)
            if route == "/api/trash-folder":
                return self._trash_folder(body)
            if route == "/api/trash-many":
                return self._trash_many(body)
            if route == "/api/undo":
                return self._undo(body)
            if route == "/api/undo-save":
                return self._undo_save(body)
            if route == "/api/restore":
                return self._restore(body)
            if route == "/api/purge":
                return self._purge(body)
            return self._deny(HTTPStatus.NOT_FOUND)

        def _open(self, body: Dict[str, Any]) -> None:
            path = self._file({"path": str(body.get("path", ""))})
            if path is None:
                return self._deny(HTTPStatus.NOT_FOUND)
            if sys.platform != "darwin":
                return self._json(HTTPStatus.NOT_IMPLEMENTED, {"error": "macOS 에서만 됩니다"})
            args = ["open", "-R", str(path)] if body.get("reveal") else ["open", str(path)]
            subprocess.run(args, check=False, timeout=10)
            return self._json(HTTPStatus.OK, {"ok": True})

        def _trash(self, body: Dict[str, Any]) -> None:
            path = self._file({"path": str(body.get("path", ""))})
            if path is None:
                return self._deny(HTTPStatus.NOT_FOUND)
            return self._quarantine(str(path), "허브에서 삭제")

        def _trash_many(self, body: Dict[str, Any]) -> None:
            # 중복 사본 정리: 여러 파일을 한 배치로 격리해 한 번에 되돌릴 수 있게 한다.
            raw = body.get("paths")
            if not isinstance(raw, list) or not 0 < len(raw) <= 500:
                return self._deny(HTTPStatus.BAD_REQUEST)
            paths = [self._file({"path": str(p)}) for p in raw]
            if any(p is None for p in paths):
                return self._deny(HTTPStatus.NOT_FOUND)
            batch = ops.new_batch_id("clean")
            moves = [
                ops.Move(
                    "quarantine",
                    str(p),
                    ops.quarantine_target(app.cfg, batch, str(p)),
                    "허브에서 중복 정리",
                )
                for p in paths
                if p is not None
            ]
            conn = app.conn()
            try:
                res = ops.apply(conn, moves, batch)
            finally:
                conn.close()
            if res.done:
                app.batches.add(batch)
                app.sync()
            if res.done != len(moves):
                return self._json(
                    HTTPStatus.CONFLICT,
                    {"error": f"{len(moves) - res.done}개를 옮기지 못했습니다", "batch": batch},
                )
            return self._json(HTTPStatus.OK, {"ok": True, "batch": batch})

        def _trash_folder(self, body: Dict[str, Any]) -> None:
            conn = app.conn()
            try:
                folder = resolve_folder(conn, app.cfg, str(body.get("path", "")))
            finally:
                conn.close()
            if folder is None:
                return self._deny(HTTPStatus.NOT_FOUND)
            return self._quarantine(str(folder), "허브에서 폴더 삭제")

        def _quarantine(self, src: str, reason: str) -> None:
            # 바로 지우지 않고 격리 폴더로 옮긴다. 작업 기록에 남아 undo 로 되돌린다.
            batch = ops.new_batch_id("clean")
            move = ops.Move("quarantine", src, ops.quarantine_target(app.cfg, batch, src), reason)
            conn = app.conn()
            try:
                res = ops.apply(conn, [move], batch)
            finally:
                conn.close()
            if res.done != 1:
                return self._json(
                    HTTPStatus.CONFLICT,
                    {"error": "옮기지 못했습니다. 파일이 열려 있는지 확인하세요"},
                )
            app.batches.add(batch)
            app.sync()
            return self._json(HTTPStatus.OK, {"ok": True, "batch": batch})

        def _undo(self, body: Dict[str, Any]) -> None:
            batch = str(body.get("batch", ""))
            if batch not in app.batches:
                return self._deny(HTTPStatus.NOT_FOUND)
            conn = app.conn()
            try:
                res = ops.undo(conn, app.cfg, batch)
            finally:
                conn.close()
            if not res.done or res.failed or res.skipped:
                return self._json(
                    HTTPStatus.CONFLICT,
                    {"error": "되돌리지 못했습니다. 원래 자리에 같은 이름의 파일이 있습니다"},
                )
            app.batches.discard(batch)
            app.sync()
            return self._json(HTTPStatus.OK, {"ok": True})

        def _restore(self, body: Dict[str, Any]) -> None:
            # 격리함에서 복원: 이 서버가 만든 배치가 아니어도 격리 중이면 되돌린다.
            batch = str(body.get("batch", ""))
            conn = app.conn()
            try:
                if not ops.batch_items(conn, batch, limit=1):
                    return self._deny(HTTPStatus.NOT_FOUND)
                res = ops.undo(conn, app.cfg, batch)
            finally:
                conn.close()
            app.batches.discard(batch)
            app.bin_sizes.pop(batch, None)  # 일부만 복원됐을 수 있다
            app.sync()
            return self._json(
                HTTPStatus.OK, {"done": res.done, "skipped": res.skipped + res.failed}
            )

        def _purge(self, body: Dict[str, Any]) -> None:
            # 영구 삭제. 되돌릴 수 없으므로 화면에서 확인을 받은 뒤에만 부른다.
            conn = app.conn()
            try:
                if body.get("all") is True:
                    purged = ops.purge(conn, app.cfg, 0)
                elif ops.purge_batch(conn, app.cfg, str(body.get("batch", ""))):
                    purged = [str(body["batch"])]
                else:
                    return self._deny(HTTPStatus.NOT_FOUND)
            finally:
                conn.close()
            for b in purged:
                app.batches.discard(b)
                app.saves.pop(b, None)
                app.bin_sizes.pop(b, None)
            return self._json(HTTPStatus.OK, {"purged": len(purged)})

        def _save(self) -> None:
            # 한글 편집기에서 저장. 원본은 격리 폴더로 옮겨 두고 새 내용을 그 자리에 쓴다.
            if not app.cfg.hwp_editor:
                return self._deny()
            raw_path = unquote(self.headers.get("X-Fsledger-Path", ""))
            path = self._file({"path": raw_path})
            if path is None or path.suffix.lower() not in _MAGIC:
                return self._deny(HTTPStatus.NOT_FOUND)
            as_hwp = self.headers.get("X-Fsledger-Save-As") == "hwp"
            fmt = ".hwp" if as_hwp else path.suffix.lower()
            length = int(self.headers.get("Content-Length", "0") or 0)
            if not 0 < length <= MAX_SAVE:
                return self._deny(HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
            data = self.rfile.read(length)
            if len(data) != length or not data.startswith(_MAGIC[fmt]):
                return self._deny(HTTPStatus.BAD_REQUEST)
            if as_hwp:
                # HWPX 원본은 건드리지 않고 옆에 .hwp 로 새로 만든다.
                target = Path(ops.unique_dst(str(path.with_suffix(".hwp"))))
                _write_atomic(target, data)
                app.sync()
                return self._json(HTTPStatus.OK, {"ok": True, "path": str(target)})
            batch = ops.new_batch_id("clean")
            src = str(path)
            move = ops.Move(
                "quarantine", src, ops.quarantine_target(app.cfg, batch, src), "편집 전 원본"
            )
            conn = app.conn()
            try:
                if ops.apply(conn, [move], batch).done != 1:
                    return self._json(HTTPStatus.CONFLICT, {"error": "원본을 백업하지 못했습니다"})
                try:
                    _write_atomic(path, data)
                except OSError:
                    ops.undo(conn, app.cfg, batch)
                    raise
            finally:
                conn.close()
            app.saves[batch] = src
            app.sync()
            return self._json(HTTPStatus.OK, {"ok": True, "batch": batch})

        def _undo_save(self, body: Dict[str, Any]) -> None:
            # 편집본도 지우지 않고 격리한 다음 원본을 제자리로 돌린다.
            batch = str(body.get("batch", ""))
            src = app.saves.get(batch)
            if src is None:
                return self._deny(HTTPStatus.NOT_FOUND)
            aside = ops.new_batch_id("clean")
            conn = app.conn()
            try:
                if os.path.lexists(src):
                    move = ops.Move(
                        "quarantine",
                        src,
                        ops.quarantine_target(app.cfg, aside, src),
                        "되돌리기 전 편집본",
                    )
                    if ops.apply(conn, [move], aside).done != 1:
                        return self._json(
                            HTTPStatus.CONFLICT, {"error": "편집본을 옮기지 못했습니다"}
                        )
                res = ops.undo(conn, app.cfg, batch)
            finally:
                conn.close()
            if res.done != 1:
                return self._json(HTTPStatus.CONFLICT, {"error": "원본을 되돌리지 못했습니다"})
            del app.saves[batch]
            app.sync()
            return self._json(HTTPStatus.OK, {"ok": True})

        def _stream(self, path: Path, mime: str) -> None:
            size = path.stat().st_size
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(size))
            self.send_header("Content-Disposition", "inline")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            with path.open("rb") as f:
                while chunk := f.read(_CHUNK):
                    self.wfile.write(chunk)

    return Handler


def make_server(cfg: Config, port: int) -> Tuple[ThreadingHTTPServer, HubApp]:
    """127.0.0.1 에 묶은 서버. 포트가 쓰이고 있으면 빈 포트를 고른다."""
    try:
        httpd = ThreadingHTTPServer(("127.0.0.1", port), BaseHTTPRequestHandler)
    except OSError:
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), BaseHTTPRequestHandler)
    app = HubApp(cfg, httpd.server_address[1])
    httpd.RequestHandlerClass = make_handler(app)
    httpd.daemon_threads = True
    return httpd, app


def serve(cfg: Config, port: int, on_ready: Optional[Callable[[HubApp], None]] = None) -> None:
    """서버를 띄우고 Ctrl+C 까지 기다린다."""
    httpd, app = make_server(cfg, port)
    stop = threading.Event()
    app.start_auto_sync(stop)
    if on_ready is not None:
        on_ready(app)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        httpd.server_close()
