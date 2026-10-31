"""단일 파일 HTML 렌더링 — 공통 테마·아이콘·데이터를 템플릿에 넣고 원자적으로 쓴다."""

import json
from pathlib import Path
from typing import Any, Dict

HERE = Path(__file__).parent


def build(template: str, data: Dict[str, Any]) -> str:
    """템플릿 자리표시를 채운 HTML. 데이터는 마지막에 넣어 다시 치환되지 않게 한다."""
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    html = (HERE / template).read_text(encoding="utf-8")
    html = html.replace("/*__THEME__*/", (HERE / "theme.css").read_text(encoding="utf-8"))
    icons = (HERE / "icons.json").read_text(encoding="utf-8").strip()
    html = html.replace("/*__ICONS__*/null", icons)
    return html.replace("/*__DATA__*/null", payload)


def render(template: str, data: Dict[str, Any], out: Path) -> Path:
    """build 결과를 out 에 원자적으로 쓴다 (git index.lock 방식)."""
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".tmp")
    tmp.write_text(build(template, data), encoding="utf-8")
    tmp.replace(out)
    return out
