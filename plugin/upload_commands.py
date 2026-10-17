"""Offline parsers and music-catalog resolution for upload commands.

This module deliberately does not perform I/O other than loading the local catalog
when requested by a caller.  It has no network or bot dependencies.
"""
from __future__ import annotations

import json
from pathlib import Path
import re
from typing import Any, Iterable

from item_upload import KINDS

MAX_INPUT = 512
MAX_CATALOG = 100_000
LEVELS = {
    "BASIC": 0, "绿": 0, "基础": 0, "0": 0,
    "ADVANCED": 1, "黄": 1, "进阶": 1, "1": 1,
    "EXPERT": 2, "红": 2, "专家": 2, "2": 2,
    "MASTER": 3, "紫": 3, "大师": 3, "3": 3,
    "REMASTER": 4, "RE:MASTER": 4, "白": 4, "白谱": 4, "4": 4,
}
LEVELS = {key.casefold(): value for key, value in LEVELS.items()}
_SCORE_RE = re.compile(r"^[0-9]+$")


def _text(value: Any, what: str = "参数") -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > MAX_INPUT:
        raise ValueError(f"{what}格式无效")
    return value.strip()


def parse_item_command(command: str) -> dict[str, int]:
    """Parse ``/上传物品 类型 id`` and return ``{'kind': ..., 'item_id': ...}``."""
    text = _text(command, "命令")
    parts = text.split()
    if len(parts) != 3 or parts[0] != "/上传物品":
        raise ValueError("上传物品格式应为：/上传物品 类型 id")
    kind_token, id_token = parts[1], parts[2]
    if kind_token in KINDS:
        kind = KINDS[kind_token]
    elif re.fullmatch(r"[0-9]+", kind_token):
        kind = int(kind_token)
        if kind not in KINDS.values():
            raise ValueError("道具类型无效")
    else:
        raise ValueError("道具类型无效")
    if not re.fullmatch(r"[0-9]+", id_token):
        raise ValueError("道具 id 无效")
    item_id = int(id_token)
    if not 1 <= item_id <= 2**31 - 1:
        raise ValueError("道具 id 无效")
    return {"kind": kind, "item_id": item_id}


def _catalog_rows(catalog: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    if not isinstance(catalog, (list, tuple)) or len(catalog) > MAX_CATALOG:
        raise ValueError("曲目目录无效")
    rows = []
    for row in catalog:
        if isinstance(row, dict):
            rows.append(row)
    return rows


def _ref_matches(rows: list[dict[str, Any]], ref: str) -> list[dict[str, Any]]:
    # Numeric references are IDs; all other references are exact title matches,
    # case-insensitively.  Do not normalize whitespace or punctuation.
    if re.fullmatch(r"[0-9]+", ref):
        return [r for r in rows if str(r.get("id")) == ref]
    folded = ref.casefold()
    return [r for r in rows if isinstance(r.get("title"), str)
            and r["title"].casefold() == folded]


def resolve_music(reference: str, level: int, catalog: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """Resolve one title/id and chart level, rejecting SD/DX ambiguity."""
    ref = _text(reference, "曲目")
    if not isinstance(level, int) or isinstance(level, bool) or not 0 <= level <= 4:
        raise ValueError("等级无效")
    rows = _catalog_rows(catalog)
    matches = _ref_matches(rows, ref)
    if not matches:
        raise ValueError("找不到曲目")
    # A title/id can be represented by both SD and DX rows.  Never choose one.
    if len(matches) > 1:
        candidates = []
        for candidate in matches[:8]:
            ident = str(candidate.get("id", ""))
            kind = candidate.get("type")
            if re.fullmatch(r"[0-9]{1,10}", ident) and kind in ("SD", "DX"):
                candidates.append(ident + " " + kind)
        raise ValueError("曲目有多个候选，请使用 id：" + "；".join(candidates))
    row = matches[0]
    levels = row.get("level")
    charts = row.get("charts")
    if (not isinstance(levels, list) or level >= len(levels) or not levels[level]
            or (charts is not None and (not isinstance(charts, list)
                                      or level >= len(charts) or not isinstance(charts[level], dict)))):
        raise ValueError("该曲目没有此等级谱面")
    music_id = row.get("id")
    title = row.get("title")
    if not isinstance(music_id, (str, int)) or isinstance(music_id, bool) or not isinstance(title, str):
        raise ValueError("曲目目录记录无效")
    if not re.fullmatch(r"[0-9]+", str(music_id)) or not 1 <= int(music_id) <= 2**31 - 1:
        raise ValueError("曲目目录记录无效")
    return {"music_id": int(music_id), "level": level, "title": title}


def parse_score_command(command: str, catalog: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """Parse ``/上传成绩 名称/id 等级 成绩`` against *catalog*.

    ``rsplit(..., 2)`` intentionally permits spaces in titles.  Achievement is
    an integer in [0, 1010000], not a percentage.
    """
    text = _text(command, "命令")
    head = text.split(None, 1)
    if len(head) != 2 or head[0] != "/上传成绩":
        raise ValueError("上传成绩格式应为：/上传成绩 名称/id 等级 成绩")
    body = head[1]
    pieces = body.rsplit(None, 2)
    if len(pieces) != 3 or not all(pieces):
        raise ValueError("上传成绩格式应为：/上传成绩 名称/id 等级 成绩")
    reference, level_token, score_token = pieces
    level_key = level_token.casefold() if level_token.isascii() else level_token
    if level_key not in LEVELS:
        raise ValueError("等级无效")
    if not _SCORE_RE.fullmatch(score_token):
        raise ValueError("成绩必须是整数")
    achievement = int(score_token)
    if achievement > 1_010_000:
        raise ValueError("成绩超出范围")
    resolved = resolve_music(reference, LEVELS[level_key], catalog)
    return {"music_id": resolved["music_id"], "level": resolved["level"],
            "achievement": achievement, "title": resolved["title"]}


def load_catalog(path: str | Path | None = None) -> list[dict[str, Any]]:
    """Load the repository's local music_data.json; never fetch remotely."""
    if path is None:
        path = Path(__file__).with_name("data") / "music_data.json"
    try:
        with Path(path).open("r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError, TypeError):
        raise ValueError("曲目目录无效") from None
    if not isinstance(data, list) or len(data) > MAX_CATALOG:
        raise ValueError("曲目目录无效")
    return data


# Short aliases useful to command adapters.
parse_item = parse_item_command
parse_score = parse_score_command
resolve_catalog_music = resolve_music


def validate_operation(operation: Any) -> dict[str, Any]:
    """Validate untrusted operation JSON, rejecting extra fields and coercions.

    This validates structure only; score operations should originate from catalog
    resolution before reaching any upload adapter.
    """
    if type(operation) is not dict:
        raise ValueError("上传操作无效")
    action = operation.get("operation")
    if action == "item":
        if set(operation) != {"operation", "kind", "item_id"}:
            raise ValueError("道具操作字段无效")
        kind, item_id = operation["kind"], operation["item_id"]
        if (type(kind) is not int or kind not in KINDS.values()
                or type(item_id) is not int or not 1 <= item_id <= 2**31 - 1):
            raise ValueError("道具操作参数无效")
    elif action == "score":
        if set(operation) != {"operation", "music_id", "level", "achievement", "title"}:
            raise ValueError("成绩操作字段无效")
        for key, minimum, maximum in (("music_id", 1, 2**31 - 1),
                                      ("level", 0, 4), ("achievement", 0, 1_010_000)):
            if type(operation[key]) is not int or not minimum <= operation[key] <= maximum:
                raise ValueError("成绩操作参数无效")
        title = _text(operation["title"], "曲目名称")
        if title != operation["title"] or any(ord(c) < 32 or ord(c) == 127 for c in title):
            raise ValueError("曲目名称无效")
    else:
        raise ValueError("上传操作类型无效")
    return dict(operation)


def parse_command(text: str, catalog: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """Dispatch full command text and return a validated tagged operation."""
    command = _text(text, "命令")
    name = command.split(None, 1)[0]
    if name == "/上传物品":
        return validate_operation({"operation": "item", **parse_item_command(command)})
    if name == "/上传成绩":
        return validate_operation({"operation": "score", **parse_score_command(command, catalog)})
    raise ValueError("不支持的上传命令")
