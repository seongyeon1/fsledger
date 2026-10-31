"""폴더 정리 계획 — 정리 루트 최상위의 낱개 파일만 카테고리 폴더로 옮긴다."""

import os
import stat
import time
from typing import List, Set

from fsledger.config import Config, OrganizeRoot, category_of
from fsledger.ops import Move


def plan_root(root: OrganizeRoot, min_age_days: int, skip: Set[str]) -> List[Move]:
    """루트 하나의 이동 계획. 폴더·숨김 파일·최근 파일·삭제 후보는 건드리지 않는다."""
    if not root.path.is_dir() or (root.path / ".git").exists():
        return []
    now = time.time()
    moves: List[Move] = []
    with os.scandir(root.path) as it:
        for e in it:
            if e.name.startswith(".") or e.path in skip:
                continue
            try:
                st = e.stat(follow_symlinks=False)
            except OSError:
                continue
            if not stat.S_ISREG(st.st_mode):
                continue
            if now - st.st_mtime < min_age_days * 86_400:
                continue  # 아직 작업 중일 수 있다
            cat = category_of(e.name)
            dst_dir = root.path / cat
            if root.group_by_month:
                dst_dir = dst_dir / time.strftime("%Y-%m", time.localtime(st.st_mtime))
            moves.append(Move("organize", e.path, str(dst_dir / e.name), f"{cat} 폴더로 정리"))
    moves.sort(key=lambda m: m.src)
    return moves


def plan(cfg: Config, skip: Set[str], only_auto: bool = False) -> List[Move]:
    """설정된 모든 정리 루트의 계획을 합친다."""
    moves: List[Move] = []
    for root in cfg.organize_roots:
        if only_auto and not root.auto:
            continue
        moves.extend(plan_root(root, cfg.organize_min_age_days, skip))
    return moves
