"""대시보드 HTML 생성 — DB 를 읽어 데이터를 박아 넣은 단일 파일을 쓴다."""

import sqlite3
import time
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List

from fsledger import page
from fsledger.config import Config
from fsledger.dupes import DupeGroup
from fsledger.hub import root_label
from fsledger.junk import SAFE, Suggestion
from fsledger.ops import Move

PER_RULE_LIMIT = 300


def _folder_sizes(conn: sqlite3.Connection, cfg: Config) -> List[Dict[str, Any]]:
    sizes: Dict[str, int] = defaultdict(int)
    counts: Dict[str, int] = defaultdict(int)
    roots = [str(r) for r in cfg.roots]
    for path, size in conn.execute("SELECT path, size FROM file_versions WHERE valid_to IS NULL"):
        for root in roots:
            if path.startswith(root + "/"):
                rest = path[len(root) + 1 :]
                head = rest.split("/", 1)[0] if "/" in rest else "(낱개 파일)"
                key = f"{root_label(root)}/{head}"
                sizes[key] += size
                counts[key] += 1
                break
    for path, size in conn.execute("SELECT path, bytes FROM junk_dirs"):
        for root in roots:
            if path.startswith(root + "/"):
                head = path[len(root) + 1 :].split("/", 1)[0]
                sizes[f"{root_label(root)}/{head}"] += size
                break
    top = sorted(sizes.items(), key=lambda kv: kv[1], reverse=True)[:20]
    return [{"name": k, "bytes": v, "files": counts[k]} for k, v in top]


def build_data(
    conn: sqlite3.Connection,
    cfg: Config,
    groups: List[DupeGroup],
    suggestions: List[Suggestion],
    plan: List[Move],
) -> Dict[str, Any]:
    """대시보드가 쓰는 JSON 하나를 만든다."""
    snaps = [
        dict(r)
        for r in conn.execute(
            "SELECT id, started_at, finished_at, files, bytes, added, modified, deleted, moved"
            " FROM snapshots ORDER BY id DESC LIMIT 60"
        )
    ][::-1]
    last = snaps[-1]["id"] if snaps else None
    changes = [
        dict(r)
        for r in conn.execute(
            "SELECT c.snapshot_id, c.kind, c.path, c.old_path, c.size_delta, s.started_at"
            " FROM changes c JOIN snapshots s ON s.id = c.snapshot_id"
            " WHERE c.snapshot_id > 1 ORDER BY c.id DESC LIMIT 500"
        )
    ]
    batches = [
        dict(r)
        for r in conn.execute(
            "SELECT batch_id, kind, status, COUNT(*) AS n, MIN(created_at) AS at"
            " FROM ops GROUP BY batch_id, kind, status ORDER BY at DESC LIMIT 40"
        )
    ]
    rule_totals: Dict[str, Dict[str, Any]] = {}
    # 규칙마다 상위 N개씩 담는다. 전체 상위 N개로 자르면 개수 많은 safe 규칙이 자리를 다 차지한다.
    per_rule: Dict[str, List[Suggestion]] = defaultdict(list)
    for s in suggestions:
        if len(per_rule[s.rule]) < PER_RULE_LIMIT:
            per_rule[s.rule].append(s)
        t = rule_totals.setdefault(s.rule, {"rule": s.rule, "level": s.level, "n": 0, "bytes": 0})
        t["n"] += 1
        t["bytes"] += s.bytes
    return {
        "generated_at": time.time(),
        "db": str(cfg.db_path),
        "roots": [root_label(str(r)) for r in cfg.roots],
        "last_snapshot": last,
        "snapshots": snaps,
        "changes": changes,
        "repos": conn.execute("SELECT COUNT(*) FROM repos").fetchone()[0],
        "folders": _folder_sizes(conn, cfg),
        "dupes": {
            "groups": len(groups),
            "wasted": sum(g.wasted for g in groups),
            "wasted_outside_repos": sum(s.bytes for s in suggestions if s.rule == "duplicate"),
            "top": [
                {"hash": g.content_hash[:12], "size": g.size, "paths": g.paths}
                for g in groups[:150]
            ],
        },
        "suggestions": {
            "safe_bytes": sum(s.bytes for s in suggestions if s.level == SAFE),
            "review_bytes": sum(s.bytes for s in suggestions if s.level != SAFE),
            "rules": sorted(rule_totals.values(), key=lambda t: -t["bytes"]),
            "items": [s.__dict__ for items in per_rule.values() for s in items],
            "total": len(suggestions),
        },
        "plan": [m.__dict__ for m in plan],
        "batches": batches,
    }


def write_dashboard(cfg: Config, data: Dict[str, Any]) -> Path:
    """템플릿에 데이터를 넣어 대시보드 파일을 쓴다."""
    return page.render("dashboard.html", data, cfg.dashboard_path)
