"""SQLite 存取层。

阶段 1 只创建论文、检索运行和 meta 三张表；摘要与代码线索表在后续阶段加入。
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from pinn_radar import config
from pinn_radar.models import PaperRecord, SummaryResult, UpsertStats

SCHEMA_VERSION = "1"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(db_path: Path | None = None) -> sqlite3.Connection:
    path = Path(db_path or config.DB_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


@contextmanager
def _connection(db_path: Path | None = None):
    conn = connect(db_path)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db(db_path: Path | None = None) -> None:
    with _connection(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS papers (
                arxiv_id TEXT PRIMARY KEY,
                version INTEGER NOT NULL,
                title TEXT NOT NULL,
                abstract TEXT NOT NULL DEFAULT '',
                authors_json TEXT NOT NULL,
                categories_json TEXT NOT NULL,
                primary_category TEXT NOT NULL DEFAULT '',
                published_at TEXT NOT NULL,
                updated_at TEXT NOT NULL DEFAULT '',
                comment TEXT NOT NULL DEFAULT '',
                journal_ref TEXT NOT NULL DEFAULT '',
                doi TEXT NOT NULL DEFAULT '',
                abs_url TEXT NOT NULL,
                pdf_url TEXT,
                link_status TEXT NOT NULL DEFAULT 'unverified',
                link_checked_at TEXT,
                has_abstract INTEGER NOT NULL DEFAULT 1,
                source_query TEXT NOT NULL DEFAULT '',
                fetched_at TEXT NOT NULL,
                first_seen_at TEXT NOT NULL,
                is_fixture INTEGER NOT NULL DEFAULT 0
            );

            CREATE INDEX IF NOT EXISTS idx_papers_published_at
                ON papers(published_at DESC);
            CREATE INDEX IF NOT EXISTS idx_papers_primary_category
                ON papers(primary_category);
            CREATE INDEX IF NOT EXISTS idx_papers_link_status
                ON papers(link_status);

            CREATE TABLE IF NOT EXISTS runs (
                run_id INTEGER PRIMARY KEY AUTOINCREMENT,
                started_at TEXT NOT NULL,
                finished_at TEXT,
                days INTEGER NOT NULL,
                window_start_utc TEXT NOT NULL,
                window_end_utc TEXT NOT NULL,
                query_groups TEXT NOT NULL DEFAULT '',
                raw_count INTEGER NOT NULL DEFAULT 0,
                unique_count INTEGER NOT NULL DEFAULT 0,
                new_count INTEGER NOT NULL DEFAULT 0,
                updated_count INTEGER NOT NULL DEFAULT 0,
                unchanged_count INTEGER NOT NULL DEFAULT 0,
                link_ok INTEGER NOT NULL DEFAULT 0,
                link_unverified INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'running',
                error_message TEXT NOT NULL DEFAULT ''
            );

            CREATE TABLE IF NOT EXISTS meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS summaries (
                arxiv_id TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                summary_json TEXT,
                raw_response TEXT NOT NULL DEFAULT '',
                title_en TEXT NOT NULL DEFAULT '',
                abstract_en TEXT NOT NULL DEFAULT '',
                model TEXT NOT NULL DEFAULT '',
                prompt_version TEXT NOT NULL DEFAULT '',
                attempts INTEGER NOT NULL DEFAULT 0,
                error TEXT NOT NULL DEFAULT '',
                input_tokens INTEGER NOT NULL DEFAULT 0,
                output_tokens INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                edited_by TEXT NOT NULL DEFAULT 'machine',
                edited_at TEXT,
                code_status TEXT NOT NULL DEFAULT 'none',
                FOREIGN KEY (arxiv_id) REFERENCES papers(arxiv_id) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_summaries_status
                ON summaries(status);
            """
        )
        summary_columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(summaries)")
        }
        if "code_status" not in summary_columns:
            conn.execute(
                "ALTER TABLE summaries ADD COLUMN code_status TEXT NOT NULL DEFAULT 'none'"
            )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_summaries_code_status "
            "ON summaries(code_status)"
        )
        conn.execute(
            "INSERT OR IGNORE INTO meta(key, value, updated_at) VALUES (?, ?, ?)",
            ("schema_version", SCHEMA_VERSION, _utc_now()),
        )


def _source_query_text(queries: list[str]) -> str:
    return " | ".join(dict.fromkeys(q for q in queries if q))


def _paper_values(paper: PaperRecord) -> tuple:
    return (
        paper.arxiv_id,
        paper.version,
        paper.title,
        paper.abstract,
        json.dumps(paper.authors, ensure_ascii=False),
        json.dumps(paper.categories, ensure_ascii=False),
        paper.primary_category,
        paper.published_at,
        paper.updated_at,
        paper.comment,
        paper.journal_ref,
        paper.doi,
        paper.abs_url,
        paper.pdf_url,
        paper.link_status,
        paper.link_checked_at,
        1 if paper.has_abstract else 0,
        _source_query_text(paper.source_queries),
        paper.fetched_at,
        paper.fetched_at,
    )


def _content_key(row: sqlite3.Row | None, paper: PaperRecord) -> tuple | None:
    if row is None:
        return None
    return (
        row["version"],
        row["title"],
        row["abstract"],
        row["authors_json"],
        row["categories_json"],
        row["primary_category"],
        row["published_at"],
        row["updated_at"],
        row["comment"],
        row["journal_ref"],
        row["doi"],
        row["abs_url"],
        row["pdf_url"],
        row["link_status"],
        row["has_abstract"],
    )


def _paper_content_key(paper: PaperRecord) -> tuple:
    return (
        paper.version,
        paper.title,
        paper.abstract,
        json.dumps(paper.authors, ensure_ascii=False),
        json.dumps(paper.categories, ensure_ascii=False),
        paper.primary_category,
        paper.published_at,
        paper.updated_at,
        paper.comment,
        paper.journal_ref,
        paper.doi,
        paper.abs_url,
        paper.pdf_url,
        paper.link_status,
        1 if paper.has_abstract else 0,
    )


def upsert_papers(papers: list[PaperRecord], db_path: Path | None = None) -> UpsertStats:
    stats = UpsertStats()
    if not papers:
        return stats

    with _connection(db_path) as conn:
        ids = [p.arxiv_id for p in papers]
        placeholders = ",".join("?" for _ in ids)
        existing_rows = {
            row["arxiv_id"]: row
            for row in conn.execute(
                f"SELECT * FROM papers WHERE arxiv_id IN ({placeholders})", ids
            )
        }

        for paper in papers:
            row = existing_rows.get(paper.arxiv_id)
            if row is None:
                stats.new += 1
            elif _content_key(row, paper) == _paper_content_key(paper):
                stats.unchanged += 1
            else:
                stats.updated += 1

            merged_queries = paper.source_queries
            if row is not None and row["source_query"]:
                merged_queries = [
                    *row["source_query"].split(" | "),
                    *paper.source_queries,
                ]
            paper.source_queries = list(dict.fromkeys(merged_queries))

            conn.execute(
                """
                INSERT INTO papers (
                    arxiv_id, version, title, abstract, authors_json, categories_json,
                    primary_category, published_at, updated_at, comment, journal_ref,
                    doi, abs_url, pdf_url, link_status, link_checked_at, has_abstract,
                    source_query, fetched_at, first_seen_at, is_fixture
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
                ON CONFLICT(arxiv_id) DO UPDATE SET
                    version=excluded.version,
                    title=excluded.title,
                    abstract=excluded.abstract,
                    authors_json=excluded.authors_json,
                    categories_json=excluded.categories_json,
                    primary_category=excluded.primary_category,
                    published_at=excluded.published_at,
                    updated_at=excluded.updated_at,
                    comment=excluded.comment,
                    journal_ref=excluded.journal_ref,
                    doi=excluded.doi,
                    abs_url=excluded.abs_url,
                    pdf_url=excluded.pdf_url,
                    link_status=excluded.link_status,
                    link_checked_at=excluded.link_checked_at,
                    has_abstract=excluded.has_abstract,
                    source_query=excluded.source_query,
                    fetched_at=excluded.fetched_at
                """,
                _paper_values(paper),
            )
        conn.commit()
    return stats


def start_run(
    days: int,
    window_start_utc: str,
    window_end_utc: str,
    groups: list[str],
    db_path: Path | None = None,
) -> int:
    with _connection(db_path) as conn:
        cursor = conn.execute(
            """
            INSERT INTO runs (started_at, days, window_start_utc, window_end_utc, query_groups, status)
            VALUES (?, ?, ?, ?, ?, 'running')
            """,
            (_utc_now(), days, window_start_utc, window_end_utc, " | ".join(groups)),
        )
        conn.commit()
        return int(cursor.lastrowid)


def finish_run(
    run_id: int,
    *,
    raw_count: int,
    unique_count: int,
    new_count: int,
    updated_count: int,
    unchanged_count: int,
    link_ok: int,
    link_unverified: int,
    status: str,
    error_message: str = "",
    db_path: Path | None = None,
) -> None:
    with _connection(db_path) as conn:
        conn.execute(
            """
            UPDATE runs SET
                finished_at=?, raw_count=?, unique_count=?, new_count=?,
                updated_count=?, unchanged_count=?, link_ok=?, link_unverified=?,
                status=?, error_message=?
            WHERE run_id=?
            """,
            (
                _utc_now(),
                raw_count,
                unique_count,
                new_count,
                updated_count,
                unchanged_count,
                link_ok,
                link_unverified,
                status,
                error_message,
                run_id,
            ),
        )
        conn.commit()


def count_papers(db_path: Path | None = None) -> int:
    with _connection(db_path) as conn:
        return int(conn.execute("SELECT COUNT(*) FROM papers").fetchone()[0])


def get_papers(limit: int = 20, offset: int = 0, db_path: Path | None = None) -> list[sqlite3.Row]:
    with _connection(db_path) as conn:
        return list(
            conn.execute(
                """
                SELECT arxiv_id, version, title, abstract, authors_json, categories_json,
                       primary_category, published_at, updated_at, abs_url, pdf_url,
                       link_status, has_abstract, source_query, first_seen_at
                FROM papers
                WHERE is_fixture = 0
                ORDER BY published_at DESC
                LIMIT ? OFFSET ?
                """,
                (limit, offset),
            )
        )


def get_last_run(db_path: Path | None = None) -> sqlite3.Row | None:
    with _connection(db_path) as conn:
        return conn.execute(
            "SELECT * FROM runs ORDER BY run_id DESC LIMIT 1"
        ).fetchone()


def _row_to_paper(row: sqlite3.Row) -> PaperRecord:
    try:
        authors = json.loads(row["authors_json"])
    except (TypeError, ValueError):
        authors = []
    try:
        categories = json.loads(row["categories_json"])
    except (TypeError, ValueError):
        categories = []
    return PaperRecord(
        arxiv_id=row["arxiv_id"],
        version=row["version"],
        title=row["title"],
        abstract=row["abstract"],
        authors=authors,
        categories=categories,
        primary_category=row["primary_category"],
        published_at=row["published_at"],
        updated_at=row["updated_at"],
        comment=row["comment"],
        journal_ref=row["journal_ref"],
        doi=row["doi"],
        abs_url=row["abs_url"],
        pdf_url=row["pdf_url"],
        has_abstract=bool(row["has_abstract"]),
        source_queries=[
            value for value in (row["source_query"] or "").split(" | ") if value
        ],
        fetched_at=row["fetched_at"],
        link_status=row["link_status"],
        link_checked_at=row["link_checked_at"],
    )


def get_papers_for_summary(
    limit: int = 5,
    arxiv_ids: list[str] | None = None,
    db_path: Path | None = None,
) -> list[PaperRecord]:
    """选择还没有成功摘要的论文；失败摘要会在下次尝试时重新处理。"""
    with _connection(db_path) as conn:
        if arxiv_ids:
            placeholders = ",".join("?" for _ in arxiv_ids)
            rows = conn.execute(
                f"""
                SELECT p.* FROM papers p
                LEFT JOIN summaries s ON s.arxiv_id = p.arxiv_id
                WHERE p.is_fixture = 0
                  AND p.arxiv_id IN ({placeholders})
                  AND (s.arxiv_id IS NULL OR s.status != 'success')
                ORDER BY p.published_at DESC
                LIMIT ?
                """,
                [*arxiv_ids, limit],
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT p.* FROM papers p
                LEFT JOIN summaries s ON s.arxiv_id = p.arxiv_id
                WHERE p.is_fixture = 0
                  AND (s.arxiv_id IS NULL OR s.status != 'success')
                ORDER BY p.published_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
    return [_row_to_paper(row) for row in rows]


def save_summary(result: SummaryResult, db_path: Path | None = None) -> None:
    summary_json = (
        json.dumps(result.summary, ensure_ascii=False)
        if result.summary is not None
        else None
    )
    code_status = "none"
    if result.status == "success" and result.summary:
        code_status = (
            (result.summary.get("code") or {}).get("status") or "not_found"
        )
    with _connection(db_path) as conn:
        conn.execute(
            """
            INSERT INTO summaries (
                arxiv_id, status, summary_json, raw_response, title_en, abstract_en,
                model, prompt_version, attempts, error, input_tokens, output_tokens,
                created_at, edited_by, code_status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'machine', ?)
            ON CONFLICT(arxiv_id) DO UPDATE SET
                status=excluded.status,
                summary_json=excluded.summary_json,
                raw_response=excluded.raw_response,
                title_en=excluded.title_en,
                abstract_en=excluded.abstract_en,
                model=excluded.model,
                prompt_version=excluded.prompt_version,
                attempts=excluded.attempts,
                error=excluded.error,
                input_tokens=excluded.input_tokens,
                output_tokens=excluded.output_tokens,
                created_at=excluded.created_at,
                edited_by=excluded.edited_by,
                edited_at=NULL,
                code_status=excluded.code_status
            """,
            (
                result.arxiv_id,
                result.status,
                summary_json,
                result.raw_response,
                result.title_en,
                result.abstract_en,
                result.model,
                result.prompt_version,
                result.attempts,
                result.error,
                result.input_tokens,
                result.output_tokens,
                _utc_now(),
                code_status,
            ),
        )
        conn.commit()


def get_summary(arxiv_id: str, db_path: Path | None = None) -> sqlite3.Row | None:
    with _connection(db_path) as conn:
        return conn.execute(
            "SELECT * FROM summaries WHERE arxiv_id = ?", (arxiv_id,)
        ).fetchone()


def get_summary_rows(limit: int = 10, db_path: Path | None = None) -> list[sqlite3.Row]:
    with _connection(db_path) as conn:
        return list(
            conn.execute(
                """
                SELECT s.*, p.title, p.abstract, p.abs_url, p.published_at
                FROM summaries s
                JOIN papers p ON p.arxiv_id = s.arxiv_id
                WHERE p.is_fixture = 0
                ORDER BY p.published_at DESC
                LIMIT ?
                """,
                (limit,),
            )
        )


def count_summaries(status: str | None = None, db_path: Path | None = None) -> int:
    with _connection(db_path) as conn:
        if status:
            return int(
                conn.execute(
                    "SELECT COUNT(*) FROM summaries WHERE status = ?", (status,)
                ).fetchone()[0]
            )
        return int(conn.execute("SELECT COUNT(*) FROM summaries").fetchone()[0])

