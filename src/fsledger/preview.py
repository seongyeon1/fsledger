"""파일 미리보기 — 종류별로 화면에 보여 줄 만큼만 읽어 JSON 으로 돌려준다.

표준 라이브러리만 쓴다. Office 문서는 zip 안의 XML 을 직접 읽고, macOS 에서는 textutil 로
워드 계열을 HTML 로 바꾼다. iCloud 에만 있는 파일은 읽지 않는다 (읽으면 다운로드가 시작된다).
"""

import contextlib
import csv
import io
import json
import re
import shutil
import subprocess
import zipfile
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple
from xml.etree import ElementTree as ET

from fsledger.hashing import is_dataless

MAX_TEXT = 512 * 1024
MAX_ROWS = 300
MAX_COLS = 60
MAX_ZIP_MEMBER = 64 * 1024 * 1024
MAX_NOTEBOOK = 20 * 1024 * 1024

MAX_DIAGRAM = 8 * 1024 * 1024  # excalidraw 는 이미지를 품고 있어 크다

TEXT_EXTS = {".txt", ".json", ".jsonl", ".ndjson", ".drawio"}
TEXTUTIL_EXTS = {".doc", ".docx", ".rtf", ".odt"}
MERMAID_EXTS = {".mmd", ".mermaid"}
HWP_EXTS = {".hwp", ".hwpx"}
INLINE_EXTS = {".pdf": "application/pdf"}
# /api/raw 로 원본 바이트를 내보내는 형식. 한글은 화면에서 rhwp 가 직접 그린다.
RAW_EXTS = {**INLINE_EXTS, ".hwp": "application/x-hwp", ".hwpx": "application/hwp+zip"}

_NS_SHEET = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
_NS_REL = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
_NS_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_NS_A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"


class PreviewError(Exception):
    """미리보기를 만들 수 없는 파일."""


def _decode(raw: bytes) -> str:
    for enc in ("utf-8-sig", "cp949"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def _read_text(path: Path) -> Tuple[str, bool]:
    with path.open("rb") as f:
        raw = f.read(MAX_TEXT + 1)
    return _decode(raw[:MAX_TEXT]), len(raw) > MAX_TEXT


def _text(path: Path) -> Dict[str, Any]:
    text, truncated = _read_text(path)
    if path.suffix.lower() == ".json" and not truncated:
        with contextlib.suppress(ValueError):
            text = json.dumps(json.loads(text), ensure_ascii=False, indent=2)
    return {"type": "text", "text": text, "truncated": truncated}


def _csv(path: Path) -> Dict[str, Any]:
    text, truncated = _read_text(path)
    delim = "\t" if path.suffix.lower() == ".tsv" else ","
    rows: List[List[str]] = []
    for i, row in enumerate(csv.reader(io.StringIO(text), delimiter=delim)):
        if i > MAX_ROWS:
            truncated = True
            break
        rows.append(row[:MAX_COLS])
    return {"type": "table", "sheets": [{"name": path.name, "rows": rows}], "truncated": truncated}


def _member(z: zipfile.ZipFile, name: str) -> bytes:
    info = z.getinfo(name)
    if info.file_size > MAX_ZIP_MEMBER:
        raise PreviewError("문서 안의 데이터가 너무 커서 미리보기를 만들지 않았습니다")
    return z.read(name)


def _col_index(ref: str) -> int:
    n = 0
    for ch in ref:
        if not ch.isalpha():
            break
        n = n * 26 + ord(ch.upper()) - 64
    return max(n - 1, 0)


def _xlsx_rows(z: zipfile.ZipFile, member: str, shared: List[str]) -> Tuple[List[List[str]], bool]:
    rows: List[List[str]] = []
    info = z.getinfo(member)
    if info.file_size > MAX_ZIP_MEMBER:
        raise PreviewError("시트가 너무 커서 미리보기를 만들지 않았습니다")
    with z.open(member) as f:
        for _, el in ET.iterparse(f):
            if el.tag != f"{_NS_SHEET}row":
                continue
            row: List[str] = []
            for c in el.findall(f"{_NS_SHEET}c"):
                idx = _col_index(c.get("r", ""))
                if idx >= MAX_COLS:
                    continue
                t = c.get("t")
                v = c.find(f"{_NS_SHEET}v")
                if t == "s" and v is not None and v.text:
                    val = shared[int(v.text)] if int(v.text) < len(shared) else ""
                elif t == "inlineStr":
                    val = "".join(x.text or "" for x in c.iter(f"{_NS_SHEET}t"))
                elif t == "b" and v is not None:
                    val = "TRUE" if v.text == "1" else "FALSE"
                else:
                    val = v.text if v is not None and v.text else ""
                row.extend([""] * (idx - len(row)))
                row.append(val)
            rows.append(row)
            el.clear()
            if len(rows) >= MAX_ROWS:
                return rows, True
    return rows, False


def _xlsx(path: Path) -> Dict[str, Any]:
    with zipfile.ZipFile(path) as z:
        names = set(z.namelist())
        shared: List[str] = []
        if "xl/sharedStrings.xml" in names:
            root = ET.fromstring(_member(z, "xl/sharedStrings.xml"))
            for si in root.findall(f"{_NS_SHEET}si"):
                shared.append("".join(t.text or "" for t in si.iter(f"{_NS_SHEET}t")))
        wb = ET.fromstring(_member(z, "xl/workbook.xml"))
        rels = ET.fromstring(_member(z, "xl/_rels/workbook.xml.rels"))
        target = {r.get("Id"): r.get("Target", "") for r in rels}
        sheets: List[Dict[str, Any]] = []
        truncated = False
        for s in wb.iter(f"{_NS_SHEET}sheet"):
            tgt = target.get(s.get(f"{_NS_REL}id"), "")
            member = tgt.lstrip("/") if tgt.startswith("/") else "xl/" + tgt
            if member not in names:
                continue
            rows, cut = _xlsx_rows(z, member, shared)
            truncated = truncated or cut
            sheets.append({"name": s.get("name", ""), "rows": rows})
            if len(sheets) >= 8:
                break
    return {"type": "table", "sheets": sheets, "truncated": truncated}


def _paragraphs(xml: bytes, p_tag: str, t_tag: str) -> Iterator[str]:
    root = ET.fromstring(xml)
    for p in root.iter(p_tag):
        text = "".join(t.text or "" for t in p.iter(t_tag)).strip()
        if text:
            yield text


def _docx_text(path: Path) -> Dict[str, Any]:
    with zipfile.ZipFile(path) as z:
        paras = list(_paragraphs(_member(z, "word/document.xml"), f"{_NS_W}p", f"{_NS_W}t"))
    return {"type": "text", "text": "\n\n".join(paras), "truncated": False}


def _textutil(path: Path) -> Optional[Dict[str, Any]]:
    exe = shutil.which("textutil")
    if exe is None:
        return None
    try:
        out = subprocess.run(
            [exe, "-convert", "html", "-stdout", "--", str(path)],
            capture_output=True,
            timeout=20,
            check=True,
        ).stdout
    except (subprocess.SubprocessError, OSError):
        return None
    # 화면에서는 스크립트를 막은 sandbox iframe 으로만 그린다.
    return {"type": "html", "html": _decode(out[: MAX_TEXT * 4])}


def _pptx(path: Path) -> Dict[str, Any]:
    with zipfile.ZipFile(path) as z:
        slides = [n for n in z.namelist() if re.fullmatch(r"ppt/slides/slide\d+\.xml", n)]
        slides.sort(key=lambda n: int(re.findall(r"\d+", n)[-1]))
        out = [
            {"n": i, "lines": list(_paragraphs(_member(z, n), f"{_NS_A}p", f"{_NS_A}t"))}
            for i, n in enumerate(slides[:200], 1)
        ]
    return {"type": "slides", "slides": out}


def _source(v: Any) -> str:
    return "".join(v) if isinstance(v, list) else str(v or "")


def _notebook(path: Path) -> Dict[str, Any]:
    if path.stat().st_size > MAX_NOTEBOOK:
        raise PreviewError("노트북이 너무 커서 미리보기를 만들지 않았습니다")
    nb = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    cells: List[Dict[str, Any]] = []
    for c in nb.get("cells", [])[:400]:
        outputs: List[str] = []
        for o in c.get("outputs", []):
            if "text" in o:
                outputs.append(_source(o["text"]))
            elif "data" in o:
                data = o["data"]
                if "text/plain" in data:
                    outputs.append(_source(data["text/plain"]))
                elif any(k.startswith("image/") for k in data):
                    outputs.append("[이미지 출력]")
        cells.append(
            {
                "kind": c.get("cell_type", "code"),
                "source": _source(c.get("source"))[:20000],
                "outputs": [x[:4000] for x in outputs[:5]],
            }
        )
    return {"type": "notebook", "cells": cells}


def _excalidraw(path: Path) -> Dict[str, Any]:
    if path.stat().st_size > MAX_DIAGRAM:
        raise PreviewError("다이어그램이 너무 커서 미리보기를 만들지 않았습니다")
    text = path.read_text(encoding="utf-8", errors="replace")
    scene = json.loads(text)
    if not isinstance(scene, dict) or not isinstance(scene.get("elements"), list):
        raise PreviewError("Excalidraw 파일이 아닙니다")
    return {"type": "excalidraw", "text": text}


def preview(path: Path) -> Dict[str, Any]:
    """미리보기 데이터. 지원하지 않는 종류면 type=unsupported."""
    ext = path.suffix.lower()
    if is_dataless(path):
        return {"type": "cloud"}
    if path.stat().st_size == 0:
        return {"type": "error", "message": "빈 파일입니다"}
    try:
        if ext in INLINE_EXTS:
            return {"type": "inline", "mime": INLINE_EXTS[ext]}
        if ext == ".md":
            text, truncated = _read_text(path)
            return {"type": "markdown", "text": text, "truncated": truncated}
        if ext in TEXT_EXTS:
            return _text(path)
        if ext in MERMAID_EXTS:
            text, truncated = _read_text(path)
            return {"type": "mermaid", "text": text, "truncated": truncated}
        if ext == ".excalidraw":
            return _excalidraw(path)
        if ext in HWP_EXTS:
            return {"type": "hwp"}
        if ext in (".csv", ".tsv"):
            return _csv(path)
        if ext == ".xlsx":
            return _xlsx(path)
        if ext in TEXTUTIL_EXTS:
            html = _textutil(path)
            if html is not None:
                return html
            if ext == ".docx":
                return _docx_text(path)
        if ext == ".pptx":
            return _pptx(path)
        if ext == ".ipynb":
            return _notebook(path)
    except PreviewError as exc:
        return {"type": "error", "message": str(exc)}
    except (zipfile.BadZipFile, KeyError, ET.ParseError, ValueError):
        return {"type": "error", "message": "파일 형식을 읽지 못했습니다. 앱에서 열어 보세요"}
    return {"type": "unsupported"}
