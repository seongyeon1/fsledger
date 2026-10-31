"""fsledger 명령줄 진입점."""

import argparse
import os
import plistlib
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from typing import List, Optional, Sequence

from fsledger import dupes, fulltext, hub, junk, ops, organize, report
from fsledger.config import Config, load_config
from fsledger.db import connect
from fsledger.scan import ScanAborted, print_progress, run_scan

LAUNCHD_LABEL = "com.fsledger.daily"


def _fmt_bytes(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if abs(n) < 1024:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def _home(p: str) -> str:
    h = str(Path.home())
    return "~" + p[len(h) :] if p.startswith(h) else p


def _analyze(
    conn: sqlite3.Connection, cfg: Config, quiet: bool = False
) -> tuple[List[dupes.DupeGroup], List[junk.Suggestion]]:
    progress = None if quiet else dupes.print_hash_progress
    n_partial, n_full = dupes.hash_candidates(conn, cfg.dupe_min_size, progress)
    if not quiet and (n_partial or n_full):
        print(f"\r  해시 계산: 부분 {n_partial:,}개, 전체 {n_full:,}개" + " " * 20, file=sys.stderr)
    groups = dupes.find_groups(conn, cfg.dupe_min_size)
    return groups, junk.collect(conn, groups)


def cmd_scan(conn: sqlite3.Connection, cfg: Config, args: argparse.Namespace) -> int:
    print("스캔 중:", ", ".join(_home(str(r)) for r in cfg.roots), file=sys.stderr)
    res = run_scan(conn, cfg, None if args.quiet else print_progress)
    print(
        f"\r스냅샷 #{res.snapshot_id}: {res.files:,}개 파일 · {_fmt_bytes(res.bytes)} · "
        f"{res.seconds:.1f}초\n  추가 {res.added:,} · 수정 {res.modified:,} · "
        f"삭제 {res.deleted:,} · 이동 {res.moved:,}"
    )
    if res.errors:
        print(f"  접근 못 한 경로 {len(res.errors)}개 (예: {res.errors[0]})", file=sys.stderr)
    if not args.no_report:
        return cmd_report(conn, cfg, args)
    return 0


def cmd_report(conn: sqlite3.Connection, cfg: Config, args: argparse.Namespace) -> int:
    groups, suggestions = _analyze(conn, cfg, quiet=getattr(args, "quiet", False))
    skip = {s.path for s in suggestions if s.level == junk.SAFE}
    plan = organize.plan(cfg, skip)
    path = report.write_dashboard(cfg, report.build_data(conn, cfg, groups, suggestions, plan))
    hub_path = hub.write_hub(cfg, hub.build_data(conn, cfg))
    if cfg.fulltext:
        n_new, n_gone = fulltext.update_index(conn, cfg)
        if not getattr(args, "quiet", False) and (n_new or n_gone):
            print(f"본문 색인: 새로 읽음 {n_new:,}개 · 뺌 {n_gone:,}개", file=sys.stderr)
    wasted = sum(g.wasted for g in groups)
    print(
        f"대시보드: {path}\n문서·데이터 허브: {hub_path}\n"
        f"  중복 {len(groups):,}묶음 ({_fmt_bytes(wasted)}) · "
        f"정리 후보 {len(suggestions):,}개 · 폴더 정리 대기 {len(plan):,}개"
    )
    if getattr(args, "open", False):
        subprocess.run(["open", str(path)], check=False)
    return 0


def cmd_hub(conn: sqlite3.Connection, cfg: Config, args: argparse.Namespace) -> int:
    data = hub.build_data(conn, cfg)
    path = hub.write_hub(cfg, data)
    print(f"문서·데이터 허브: {path}\n  {len(data['rows']):,}개 · {len(data['projects']):,}곳")
    if args.open:
        subprocess.run(["open", str(path)], check=False)
    return 0


def cmd_serve(conn: sqlite3.Connection, cfg: Config, args: argparse.Namespace) -> int:
    from fsledger import server

    def ready(app: server.HubApp) -> None:
        print(
            f"허브 서버: {app.url()}\n  이 주소에만 토큰이 들어 있습니다. 끝내려면 Ctrl+C",
            flush=True,
        )
        if not args.no_open:
            subprocess.run(["open", app.url()], check=False)

    server.serve(cfg, args.port, ready)
    return 0


def cmd_status(conn: sqlite3.Connection, cfg: Config, args: argparse.Namespace) -> int:
    rows = conn.execute(
        "SELECT * FROM snapshots ORDER BY id DESC LIMIT ?", (args.limit,)
    ).fetchall()
    if not rows:
        print("아직 스캔 기록이 없습니다. `fsledger scan` 을 먼저 실행하세요.")
        return 0
    for r in rows:
        when = time.strftime("%m-%d %H:%M", time.localtime(r["started_at"]))
        print(
            f"#{r['id']:<4} {when}  {r['files']:>9,}개 {_fmt_bytes(r['bytes']):>9}  "
            f"+{r['added']:,} ~{r['modified']:,} -{r['deleted']:,} >{r['moved']:,}"
        )
    return 0


def cmd_changes(conn: sqlite3.Connection, cfg: Config, args: argparse.Namespace) -> int:
    snap = args.snapshot or conn.execute("SELECT MAX(id) FROM snapshots").fetchone()[0]
    sql = "SELECT kind, path, old_path, size_delta FROM changes WHERE snapshot_id=?"
    params: List[object] = [snap]
    if args.kind:
        sql += " AND kind=?"
        params.append(args.kind)
    sql += " ORDER BY id LIMIT ?"
    params.append(args.limit)
    mark = {"added": "+", "modified": "~", "deleted": "-", "moved": ">"}
    for r in conn.execute(sql, params):
        extra = f"  ← {_home(r['old_path'])}" if r["old_path"] else ""
        print(f"{mark[r['kind']]} {_home(r['path'])}{extra}")
    return 0


def cmd_history(conn: sqlite3.Connection, cfg: Config, args: argparse.Namespace) -> int:
    path = str(Path(args.path).expanduser().resolve())
    rows = conn.execute(
        "SELECT v.*, s.started_at FROM file_versions v JOIN snapshots s ON s.id = v.valid_from"
        " WHERE v.path=? ORDER BY v.valid_from",
        (path,),
    ).fetchall()
    moves = conn.execute(
        "SELECT snapshot_id, old_path, path FROM changes WHERE kind='moved'"
        " AND (path=? OR old_path=?)",
        (path, path),
    ).fetchall()
    if not rows and not moves:
        print("기록 없음")
        return 1
    for r in rows:
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(r["started_at"]))
        to = f"~#{r['valid_to']}" if r["valid_to"] else "~현재"
        h = (r["content_hash"] or "")[:12] or "-"
        print(f"#{r['valid_from']}{to:<8} {when}  {_fmt_bytes(r['size']):>9}  {h}")
    for m in moves:
        print(f"#{m['snapshot_id']} 이동  {_home(m['old_path'])} → {_home(m['path'])}")
    return 0


def cmd_asof(conn: sqlite3.Connection, cfg: Config, args: argparse.Namespace) -> int:
    like = str(Path(args.prefix).expanduser().resolve()) + "%"
    rows = conn.execute(
        "SELECT path, size FROM file_versions WHERE valid_from <= ?"
        " AND (valid_to IS NULL OR valid_to > ?) AND path LIKE ? ORDER BY path LIMIT ?",
        (args.snapshot, args.snapshot, like, args.limit),
    )
    for r in rows:
        print(f"{_fmt_bytes(r['size']):>9}  {_home(r['path'])}")
    return 0


def cmd_dupes(conn: sqlite3.Connection, cfg: Config, args: argparse.Namespace) -> int:
    groups, _ = _analyze(conn, cfg)
    for g in groups[: args.limit]:
        print(f"\n{_fmt_bytes(g.size)} × {len(g.paths)}  (낭비 {_fmt_bytes(g.wasted)})")
        for i, p in enumerate(g.paths):
            print(f"  {'원본' if i == 0 else '후보'}  {_home(p)}")
    print(f"\n총 {len(groups):,}묶음, 낭비 {_fmt_bytes(sum(g.wasted for g in groups))}")
    return 0


def cmd_clean(conn: sqlite3.Connection, cfg: Config, args: argparse.Namespace) -> int:
    _, suggestions = _analyze(conn, cfg)
    levels = {junk.SAFE} if args.level == "safe" else {junk.SAFE, junk.REVIEW}
    chosen = [
        s for s in suggestions if s.level in levels and (not args.rule or s.rule in args.rule)
    ]
    total = sum(s.bytes for s in chosen)
    for s in chosen[: args.limit]:
        print(f"[{s.level:<6}] {_fmt_bytes(s.bytes):>9}  {_home(s.path)}  — {s.reason}")
    if len(chosen) > args.limit:
        print(f"… 외 {len(chosen) - args.limit:,}개")
    print(f"\n{len(chosen):,}개 · {_fmt_bytes(total)}")
    if not args.apply:
        print("미리보기입니다. 실제로 옮기려면 --apply 를 붙이세요 (격리 폴더로 이동, undo 가능).")
        return 0
    batch = ops.new_batch_id("clean")
    moves = [
        ops.Move("quarantine", s.path, ops.quarantine_target(cfg, batch, s.path), s.rule)
        for s in chosen
    ]
    res = ops.apply(conn, moves, batch)
    print(
        f"격리 완료: {res.done:,}개 · 실패 {res.failed} · 건너뜀 {res.skipped}\n"
        f"배치 {batch}  →  되돌리기: fsledger undo {batch}"
    )
    return 0 if res.failed == 0 else 1


def cmd_organize(conn: sqlite3.Connection, cfg: Config, args: argparse.Namespace) -> int:
    plan = organize.plan(cfg, set(), only_auto=args.auto_only)
    if args.root:
        root = str(Path(args.root).expanduser().resolve())
        plan = [m for m in plan if str(Path(m.src).parent) == root]
    for m in plan[: args.limit]:
        print(f"{_home(m.src)}\n   → {_home(m.dst)}")
    if len(plan) > args.limit:
        print(f"… 외 {len(plan) - args.limit:,}개")
    print(f"\n{len(plan):,}개 파일")
    if not args.apply or not plan:
        if plan:
            print("미리보기입니다. 실행하려면 --apply 를 붙이세요 (undo 가능).")
        return 0
    batch = ops.new_batch_id("organize")
    res = ops.apply(conn, plan, batch)
    print(
        f"정리 완료: {res.done:,}개 · 실패 {res.failed} · 건너뜀 {res.skipped}\n"
        f"배치 {batch}  →  되돌리기: fsledger undo {batch}"
    )
    return 0 if res.failed == 0 else 1


def cmd_undo(conn: sqlite3.Connection, cfg: Config, args: argparse.Namespace) -> int:
    batch = args.batch or ops.last_batch(conn)
    if not batch:
        print("되돌릴 배치가 없습니다.")
        return 0
    res = ops.undo(conn, cfg, batch)
    print(f"{batch}: 되돌림 {res.done:,} · 실패 {res.failed} · 건너뜀 {res.skipped}")
    return 0 if res.failed == 0 else 1


def cmd_recover(conn: sqlite3.Connection, cfg: Config, args: argparse.Namespace) -> int:
    notes = ops.recover(conn)
    print("\n".join(notes) if notes else "중단된 작업 없음")
    return 0


def cmd_purge(conn: sqlite3.Connection, cfg: Config, args: argparse.Namespace) -> int:
    if not args.yes:
        print("격리 폴더의 파일을 영구 삭제합니다. 되돌릴 수 없습니다. --yes 로 확인하세요.")
        return 1
    purged = ops.purge(conn, cfg, args.older_than)
    print(f"영구 삭제한 배치 {len(purged)}개" + "".join(f"\n  {b}" for b in purged))
    return 0


def cmd_auto(conn: sqlite3.Connection, cfg: Config, args: argparse.Namespace) -> int:
    """스케줄 실행용: 중단 작업 복구 → (설정 시) 오래된 격리분 삭제 → 스캔 → 정리 → 대시보드."""
    ops.recover(conn)
    if cfg.purge_after_days:
        purged = ops.purge(conn, cfg, cfg.purge_after_days)
        if purged:
            print(f"  격리 {cfg.purge_after_days}일 지난 배치 {len(purged)}개 영구 삭제")
    res = run_scan(conn, cfg)
    print(
        f"[{time.strftime('%F %T')}] 스냅샷 #{res.snapshot_id} "
        f"+{res.added} ~{res.modified} -{res.deleted} >{res.moved}"
    )
    plan = organize.plan(cfg, set(), only_auto=True)
    if plan:
        batch = ops.new_batch_id("organize")
        r = ops.apply(conn, plan, batch)
        print(f"  자동 정리 {r.done}개 (배치 {batch})")
        if r.done:
            run_scan(conn, cfg)  # 이동을 바로 기록에 반영
    args.quiet = True
    return cmd_report(conn, cfg, args)


def _plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LAUNCHD_LABEL}.plist"


def cmd_schedule(conn: sqlite3.Connection, cfg: Config, args: argparse.Namespace) -> int:
    plist = _plist_path()
    domain = f"gui/{os.getuid()}"
    if args.action == "remove":
        subprocess.run(["launchctl", "bootout", f"{domain}/{LAUNCHD_LABEL}"], check=False)
        plist.unlink(missing_ok=True)
        print("스케줄 제거됨")
        return 0
    exe = shutil.which("fsledger")
    if not exe:
        print("PATH 에서 fsledger 를 찾지 못했습니다. `uv tool install .` 후 다시 실행하세요.")
        return 1
    log = cfg.data_dir / "auto.log"
    spec = {
        "Label": LAUNCHD_LABEL,
        "ProgramArguments": [exe, "auto"],
        "StartCalendarInterval": {"Hour": args.hour, "Minute": 0},
        "StandardOutPath": str(log),
        "StandardErrorPath": str(log),
        "EnvironmentVariables": {"FSLEDGER_HOME": str(cfg.data_dir)},
        "LowPriorityIO": True,
        "Nice": 10,
    }
    if args.action == "show":
        print(plistlib.dumps(spec).decode())
        return 0
    plist.parent.mkdir(parents=True, exist_ok=True)
    plist.write_bytes(plistlib.dumps(spec))
    subprocess.run(
        ["launchctl", "bootout", f"{domain}/{LAUNCHD_LABEL}"], check=False, capture_output=True
    )
    subprocess.run(["launchctl", "bootstrap", domain, str(plist)], check=True)
    print(f"매일 {args.hour}:00 에 `fsledger auto` 실행 — {plist}\n로그: {log}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    """서브커맨드 파서."""
    p = argparse.ArgumentParser(prog="fsledger", description="로컬 파일 변경 추적·정리")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("scan", help="스캔하고 스냅샷 기록 + 대시보드 갱신")
    s.add_argument("--no-report", action="store_true")
    s.add_argument("--quiet", action="store_true")
    s.add_argument("--open", action="store_true", help="끝나면 대시보드 열기")
    s.set_defaults(fn=cmd_scan)

    s = sub.add_parser("report", help="대시보드만 다시 만들기")
    s.add_argument("--open", action="store_true")
    s.set_defaults(fn=cmd_report)

    s = sub.add_parser("hub", help="문서·데이터 허브만 다시 만들기 (파일은 옮기지 않음)")
    s.add_argument("--open", action="store_true")
    s.set_defaults(fn=cmd_hub)

    s = sub.add_parser("serve", help="허브를 로컬 서버로 열기 (미리보기·앱에서 열기)")
    s.add_argument("--port", type=int, default=8765)
    s.add_argument("--no-open", action="store_true", help="브라우저를 열지 않기")
    s.set_defaults(fn=cmd_serve)

    s = sub.add_parser("status", help="스냅샷 목록")
    s.add_argument("--limit", type=int, default=15)
    s.set_defaults(fn=cmd_status)

    s = sub.add_parser("changes", help="스냅샷 하나의 변경 이벤트")
    s.add_argument("--snapshot", type=int)
    s.add_argument("--kind", choices=["added", "modified", "deleted", "moved"])
    s.add_argument("--limit", type=int, default=200)
    s.set_defaults(fn=cmd_changes)

    s = sub.add_parser("history", help="파일 하나의 버전 이력")
    s.add_argument("path")
    s.set_defaults(fn=cmd_history)

    s = sub.add_parser("asof", help="특정 스냅샷 시점의 파일 목록")
    s.add_argument("snapshot", type=int)
    s.add_argument("prefix", help="경로 접두사 (예: ~/Downloads)")
    s.add_argument("--limit", type=int, default=200)
    s.set_defaults(fn=cmd_asof)

    s = sub.add_parser("dupes", help="중복 파일 묶음")
    s.add_argument("--limit", type=int, default=30)
    s.set_defaults(fn=cmd_dupes)

    s = sub.add_parser("clean", help="불필요 파일 제안 / 격리")
    s.add_argument("--apply", action="store_true")
    s.add_argument("--level", choices=["safe", "review"], default="safe")
    s.add_argument("--rule", action="append", help="특정 규칙만 (여러 번 지정 가능)")
    s.add_argument("--limit", type=int, default=50)
    s.set_defaults(fn=cmd_clean)

    s = sub.add_parser("organize", help="최상위 낱개 파일을 카테고리 폴더로 정리")
    s.add_argument("--apply", action="store_true")
    s.add_argument("--root", help="이 정리 루트만")
    s.add_argument("--auto-only", action="store_true", help="auto=true 루트만")
    s.add_argument("--limit", type=int, default=40)
    s.set_defaults(fn=cmd_organize)

    s = sub.add_parser("undo", help="배치 되돌리기 (기본: 마지막 배치)")
    s.add_argument("batch", nargs="?")
    s.set_defaults(fn=cmd_undo)

    s = sub.add_parser("recover", help="중단된 작업 정리")
    s.set_defaults(fn=cmd_recover)

    s = sub.add_parser("purge", help="오래된 격리분 영구 삭제")
    s.add_argument("--older-than", type=int, default=30, help="일 단위")
    s.add_argument("--yes", action="store_true")
    s.set_defaults(fn=cmd_purge)

    s = sub.add_parser("auto", help="스케줄 실행용 일괄 작업")
    s.set_defaults(fn=cmd_auto)

    s = sub.add_parser("schedule", help="launchd 일일 스케줄 관리")
    s.add_argument("action", choices=["install", "remove", "show"])
    s.add_argument("--hour", type=int, default=9)
    s.set_defaults(fn=cmd_schedule)
    return p


def main(argv: Optional[Sequence[str]] = None) -> int:
    """CLI 실행."""
    args = build_parser().parse_args(argv)
    cfg = load_config()
    conn = connect(cfg.db_path)
    try:
        code: int = args.fn(conn, cfg, args)
        return code
    except ScanAborted as exc:
        print(f"스캔 중단: {exc}", file=sys.stderr)
        return 2
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
