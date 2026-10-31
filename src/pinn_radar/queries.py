"""页面用的数据查询与视图模型。

app.py 只负责布局和交互；这里集中处理日期窗口、搜索、代码状态筛选和摘要 JSON 解析。
"""

from __future__ import annotations

import json
from contextlib import closing
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from pinn_radar import db

UTC_PLUS_8 = timezone(timedelta(hours=8))


@dataclass(frozen=True)
class PaperView:
    arxiv_id: str
    title: str
    abstract: str
    authors: list[str]
    published_at: str
    published_local: str
    primary_category: str
    abs_url: str
    pdf_url: str | None
    summary_status: str
    summary_error: str
    one_liner: str
    methods: list[str] = field(default_factory=list)
    datasets: list[str] = field(default_factory=list)
    code_status: str = "none"
    code_note: str = ""
    code_urls: list[str] = field(default_factory=list)


def window_start(days: int) -> str:
    if days < 1 or days > 365:
        raise ValueError("days 必须在 1 到 365 之间")
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(
        timespec="seconds"
    )


def _local_time(iso_value: str) -> str:
    try:
        value = datetime.fromisoformat(iso_value)
        return value.astimezone(UTC_PLUS_8).strftime("%Y-%m-%d %H:%M")
    except (TypeError, ValueError):
        return iso_value or ""


def _item_label(item: Any) -> str:
    if not isinstance(item, dict):
        return str(item)
    name = str(item.get("name_zh") or item.get("term_en") or "").strip()
    term = str(item.get("term_en") or "").strip()
    if term and term.lower() not in name.lower():
        return f"{name} ({term})" if name else term
    return name or term


def _parse_summary(row: Any) -> dict:
    if not row["summary_json"] or row["summary_status"] != "success":
        return {}
    try:
        data = json.loads(row["summary_json"])
    except (TypeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _row_to_view(row: Any) -> PaperView:
    summary = _parse_summary(row)
    code = summary.get("code") if isinstance(summary.get("code"), dict) else {}
    code_status = str(row["summary_code_status"] or "none")
    if row["summary_status"] != "success":
        code_status = "none"
    authors: list[str] = []
    try:
        parsed_authors = json.loads(row["authors_json"])
        if isinstance(parsed_authors, list):
            authors = [str(value) for value in parsed_authors]
    except (TypeError, ValueError):
        pass
    return PaperView(
        arxiv_id=row["arxiv_id"],
        title=row["title"],
        abstract=row["abstract"],
        authors=authors,
        published_at=row["published_at"],
        published_local=_local_time(row["published_at"]),
        primary_category=row["primary_category"] or "",
        abs_url=row["abs_url"],
        pdf_url=row["pdf_url"],
        summary_status=row["summary_status"] or "none",
        summary_error=row["summary_error"] or "",
        one_liner=str(summary.get("core_contribution_zh") or ""),
        methods=[
            label
            for item in (summary.get("methods") or [])
            if (label := _item_label(item))
        ],
        datasets=[
            label
            for item in (summary.get("datasets") or [])
            if (label := _item_label(item))
        ],
        code_status=code_status,
        code_note=str(code.get("note_zh") or ""),
        code_urls=[
            str(url) for url in (code.get("urls") or []) if str(url).strip()
        ],
    )


def get_stats(days: int) -> dict[str, int]:
    start = window_start(days)
    with closing(db.connect()) as conn:
        conn.row_factory = None
        total = conn.execute(
            "SELECT COUNT(*) FROM papers WHERE is_fixture = 0"
        ).fetchone()[0]
        window_count = conn.execute(
            "SELECT COUNT(*) FROM papers WHERE is_fixture = 0 AND published_at >= ?",
            (start,),
        ).fetchone()[0]
        code_evidence = conn.execute(
            """
            SELECT COUNT(*) FROM papers p
            JOIN summaries s ON s.arxiv_id = p.arxiv_id
            WHERE p.is_fixture = 0
              AND p.published_at >= ?
              AND s.status = 'success'
              AND s.code_status IN ('confirmed', 'claimed')
            """,
            (start,),
        ).fetchone()[0]
        summarized = conn.execute(
            """
            SELECT COUNT(*) FROM papers p
            JOIN summaries s ON s.arxiv_id = p.arxiv_id
            WHERE p.is_fixture = 0
              AND p.published_at >= ?
              AND s.status = 'success'
            """,
            (start,),
        ).fetchone()[0]
    return {
        "total": int(total),
        "window_count": int(window_count),
        "code_evidence": int(code_evidence),
        "summarized": int(summarized),
    }


def search_papers(
    days: int,
    keyword: str = "",
    code_status: str = "",
    limit: int = 300,
) -> list[PaperView]:
    start = window_start(days)
    conditions = ["p.is_fixture = 0", "p.published_at >= ?"]
    params: list[Any] = [start]

    keyword = keyword.strip()
    if keyword:
        conditions.append(
            "("
            "instr(lower(p.title), lower(?)) > 0 OR "
            "instr(lower(p.abstract), lower(?)) > 0 OR "
            "instr(lower(p.authors_json), lower(?)) > 0 OR "
            "instr(lower(COALESCE(s.summary_json, '')), lower(?)) > 0"
            ")"
        )
        params.extend([keyword, keyword, keyword, keyword])

    if code_status == "none":
        conditions.append("(s.arxiv_id IS NULL OR s.status != 'success')")
    elif code_status in {"confirmed", "claimed", "not_found"}:
        conditions.append("s.status = 'success' AND s.code_status = ?")
        params.append(code_status)

    where_clause = " AND ".join(conditions)
    with closing(db.connect()) as conn:
        rows = conn.execute(
            f"""
            SELECT p.*, s.status AS summary_status, s.summary_json,
                   s.error AS summary_error, s.code_status AS summary_code_status
            FROM papers p
            LEFT JOIN summaries s ON s.arxiv_id = p.arxiv_id
            WHERE {where_clause}
            ORDER BY p.published_at DESC
            LIMIT ?
            """,
            [*params, limit],
        ).fetchall()
    return [_row_to_view(row) for row in rows]


def get_pending_ids(days: int, limit: int = 5) -> list[str]:
    start = window_start(days)
    with closing(db.connect()) as conn:
        rows = conn.execute(
            """
            SELECT p.arxiv_id FROM papers p
            LEFT JOIN summaries s ON s.arxiv_id = p.arxiv_id
            WHERE p.is_fixture = 0
              AND p.published_at >= ?
              AND (s.arxiv_id IS NULL OR s.status != 'success')
            ORDER BY p.published_at DESC
            LIMIT ?
            """,
            (start, limit),
        ).fetchall()
    return [str(row[0]) for row in rows]


def get_last_run() -> dict[str, Any] | None:
    row = db.get_last_run()
    if row is None:
        return None
    return dict(row)
