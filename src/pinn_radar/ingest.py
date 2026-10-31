"""arXiv 检索、去重、链接校验和入库。

运行示例：
    python -m pinn_radar.ingest --days 30
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from typing import Any

import feedparser
import requests

from pinn_radar import config, db
from pinn_radar.models import (
    FetchResult,
    GroupResult,
    PaperRecord,
    RunResult,
    UpsertStats,
)

USER_AGENT = "pinn-radar/0.1 (course project; arXiv metadata only)"
UTC_PLUS_8 = timezone(timedelta(hours=8))


class ArxivError(RuntimeError):
    """arXiv 请求或响应解析失败。"""


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def build_window(days: int, now: datetime | None = None) -> tuple[datetime, datetime]:
    """返回 UTC 的 [start, end]；窗口长度由用户指定。"""
    if days < 1 or days > 365:
        raise ValueError("days 必须在 1 到 365 之间")
    end = (now or utc_now()).astimezone(timezone.utc).replace(microsecond=0)
    start = end - timedelta(days=days)
    return start, end


def arxiv_date(value: datetime) -> str:
    """arXiv submittedDate 使用 YYYYMMDDHHMM（UTC）。"""
    return value.astimezone(timezone.utc).strftime("%Y%m%d%H%M")


def build_query(group_terms: str, start: datetime, end: datetime) -> str:
    return (
        f"{config.PINN_QUERY_TERMS} AND {group_terms} "
        f"AND submittedDate:[{arxiv_date(start)} TO {arxiv_date(end)}]"
    )


def normalize_arxiv_id(raw_id: str) -> tuple[str, int]:
    """把 abs URL 或带版本号的 ID 规范化为 (base_id, version)。"""
    text = raw_id.strip()
    if "/abs/" in text:
        text = text.split("/abs/", 1)[1]
    text = text.split("?", 1)[0].strip().strip("/")
    match = re.match(r"^(?P<base>.+?)(?P<version>v\d+)?$", text)
    if not match:
        raise ValueError(f"无法解析 arXiv ID: {raw_id!r}")
    base = match.group("base").rstrip("/")
    version_text = match.group("version")
    version = int(version_text[1:]) if version_text else 1
    if not base:
        raise ValueError(f"无法解析 arXiv ID: {raw_id!r}")
    return base, version


def canonical_abs_url(arxiv_id: str) -> str:
    return f"https://arxiv.org/abs/{arxiv_id}"


def _clean_text(value: Any) -> str:
    if not value:
        return ""
    return " ".join(str(value).split())


def _entry_time(entry: Any, key: str) -> str:
    parsed = entry.get(f"{key}_parsed")
    if parsed:
        return datetime(*parsed[:6], tzinfo=timezone.utc).isoformat(timespec="seconds")
    raw = entry.get(key)
    if not raw:
        return ""
    try:
        parsed_dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        return parsed_dt.astimezone(timezone.utc).isoformat(timespec="seconds")
    except ValueError:
        return ""


def _entry_link(entry: Any, *, pdf: bool = False) -> str:
    for link in entry.get("links", []):
        href = link.get("href", "")
        if not href:
            continue
        if pdf and link.get("type") == "application/pdf":
            return href
        if not pdf and link.get("rel") == "alternate":
            return href
    return ""


def parse_entry(entry: Any, source_query: str, fetched_at: str) -> PaperRecord:
    try:
        base_id, version = normalize_arxiv_id(entry.get("id", ""))
    except ValueError as exc:
        raise ArxivError(str(exc)) from exc

    title = _clean_text(entry.get("title"))
    abstract = _clean_text(entry.get("summary"))
    if not title:
        raise ArxivError(f"{base_id} 缺少标题")

    authors = [
        _clean_text(author.get("name"))
        for author in entry.get("authors", [])
        if _clean_text(author.get("name"))
    ]
    categories = [
        tag.get("term", "")
        for tag in entry.get("tags", [])
        if tag.get("term")
    ]
    primary = ""
    primary_category = entry.get("arxiv_primary_category")
    if isinstance(primary_category, dict):
        primary = _clean_text(primary_category.get("term"))

    published_at = _entry_time(entry, "published")
    if not published_at:
        raise ArxivError(f"{base_id} 缺少 published 时间")

    abs_url = canonical_abs_url(base_id)  # 统一为去掉版本号的可打开链接
    pdf_url = _entry_link(entry, pdf=True) or None

    return PaperRecord(
        arxiv_id=base_id,
        version=version,
        title=title,
        abstract=abstract,
        authors=authors,
        categories=categories,
        primary_category=primary,
        published_at=published_at,
        updated_at=_entry_time(entry, "updated"),
        comment=_clean_text(entry.get("arxiv_comment")),
        journal_ref=_clean_text(entry.get("arxiv_journal_ref")),
        doi=_clean_text(entry.get("arxiv_doi")),
        abs_url=abs_url,
        pdf_url=pdf_url,
        has_abstract=bool(abstract),
        source_queries=[source_query],
        fetched_at=fetched_at,
    )


def _feed_total(feed: Any) -> int | None:
    for key in ("opensearch_totalresults", "opensearch_total_results"):
        value = getattr(feed.feed, key, None)
        if value is None and hasattr(feed.feed, "get"):
            value = feed.feed.get(key)
        if value is not None:
            try:
                return int(value)
            except (TypeError, ValueError):
                return None
    return None


class ArxivClient:
    """串行访问 arXiv API，遵守 3 秒/请求的限流。"""

    def __init__(self, delay: float = config.ARXIV_REQUEST_DELAY_SECONDS) -> None:
        self.delay = delay
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": USER_AGENT})
        self._last_request_at = 0.0

    def _wait(self) -> None:
        elapsed = time.monotonic() - self._last_request_at
        if elapsed < self.delay:
            time.sleep(self.delay - elapsed)

    def fetch(self, query: str, start: int, max_results: int) -> Any:
        params = {
            "search_query": query,
            "start": start,
            "max_results": max_results,
            "sortBy": "submittedDate",
            "sortOrder": "descending",
        }
        last_error: Exception | None = None
        for attempt in range(config.ARXIV_MAX_RETRIES):
            self._wait()
            try:
                response = self.session.get(
                    config.ARXIV_API_URL,
                    params=params,
                    timeout=config.ARXIV_TIMEOUT_SECONDS,
                )
                self._last_request_at = time.monotonic()
                if response.status_code == 200:
                    feed = feedparser.parse(response.content)
                    if feed.bozo and not feed.entries:
                        raise ArxivError(
                            f"arXiv 返回无法解析的内容: {feed.bozo_exception}"
                        )
                    return feed
                if response.status_code in (429, 500, 502, 503, 504):
                    last_error = ArxivError(
                        f"arXiv 暂时不可用: HTTP {response.status_code}"
                    )
                    time.sleep(2 ** (attempt + 1))
                    continue
                raise ArxivError(
                    f"arXiv 请求失败: HTTP {response.status_code}"
                )
            except requests.RequestException as exc:
                last_error = exc
                if attempt == config.ARXIV_MAX_RETRIES - 1:
                    break
                time.sleep(2 ** (attempt + 1))
        raise ArxivError(f"arXiv 请求失败（已重试）: {last_error}")

    def fetch_group(
        self,
        name: str,
        group_terms: str,
        start: datetime,
        end: datetime,
        fetched_at: str,
    ) -> tuple[GroupResult, list[PaperRecord]]:
        query = build_query(group_terms, start, end)
        papers: list[PaperRecord] = []
        errors: list[str] = []
        total: int | None = None
        offset = 0
        pages = 0

        while True:
            feed = self.fetch(query, offset, config.ARXIV_PAGE_SIZE)
            pages += 1
            if total is None:
                total = _feed_total(feed)
            entries = feed.entries
            if not entries:
                break
            for entry in entries:
                try:
                    papers.append(parse_entry(entry, name, fetched_at))
                except ArxivError as exc:
                    errors.append(str(exc))
            offset += len(entries)
            if total is not None and offset >= total:
                break
            if len(entries) < config.ARXIV_PAGE_SIZE:
                break

        if total and total > 0 and not papers:
            detail = (
                f"；解析错误 {len(errors)} 条：" + "; ".join(errors[:3])
                if errors
                else ""
            )
            raise ArxivError(
                f"{name} 查询返回 totalResults={total}，但没有可解析的论文条目{detail}"
            )

        return GroupResult(name=name, total_results=total or len(papers), pages=pages, errors=errors), papers


def dedupe_papers(papers: list[PaperRecord]) -> list[PaperRecord]:
    """按 base ID 去重，并合并来源关键词组。"""
    by_id: dict[str, PaperRecord] = {}
    for paper in papers:
        existing = by_id.get(paper.arxiv_id)
        if existing is None:
            by_id[paper.arxiv_id] = paper
            continue
        for query in paper.source_queries:
            if query not in existing.source_queries:
                existing.source_queries.append(query)
    return list(by_id.values())


def _probe_link(url: str) -> bool:
    try:
        response = requests.head(
            url,
            headers={"User-Agent": USER_AGENT},
            timeout=config.LINK_CHECK_TIMEOUT_SECONDS,
            allow_redirects=True,
        )
        if response.status_code in (403, 405, 501):
            response = requests.get(
                url,
                headers={"User-Agent": USER_AGENT},
                timeout=config.LINK_CHECK_TIMEOUT_SECONDS,
                allow_redirects=True,
                stream=True,
            )
        return 200 <= response.status_code < 300
    except requests.RequestException:
        return False


def _check_link(paper: PaperRecord) -> None:
    paper.link_checked_at = utc_now().isoformat(timespec="seconds")
    ok = _probe_link(paper.abs_url)
    if not ok:
        time.sleep(0.5)
        ok = _probe_link(paper.abs_url)
    paper.link_status = "ok" if ok else "unverified"


def check_links(papers: list[PaperRecord]) -> None:
    if not papers:
        return
    with ThreadPoolExecutor(max_workers=config.LINK_CHECK_WORKERS) as executor:
        futures = [executor.submit(_check_link, paper) for paper in papers]
        for future in as_completed(futures):
            future.result()

    # 网络抖动会造成少量误判；对未通过项再做一次低频串行复核。
    unverified = [paper for paper in papers if paper.link_status != "ok"]
    for paper in unverified:
        time.sleep(2)
        _check_link(paper)


def fetch_papers(
    days: int,
    start: datetime | None = None,
    end: datetime | None = None,
) -> FetchResult:
    if start is None or end is None:
        start, end = build_window(days)
    fetched_at = utc_now().isoformat(timespec="seconds")
    client = ArxivClient()
    group_results: list[GroupResult] = []
    all_papers: list[PaperRecord] = []

    print(f"检索窗口（UTC）: {start.isoformat()} -> {end.isoformat()}")
    for name, terms in config.TOPIC_QUERY_GROUPS:
        print(f"查询组: {name} ...", flush=True)
        group_result, papers = client.fetch_group(name, terms, start, end, fetched_at)
        group_results.append(group_result)
        all_papers.extend(papers)
        print(
            f"  totalResults={group_result.total_results}, "
            f"pages={group_result.pages}, entries={len(papers)}, "
            f"errors={len(group_result.errors)}",
            flush=True,
        )

    unique = dedupe_papers(all_papers)
    print(f"原始条目: {len(all_papers)}，去重后: {len(unique)}", flush=True)
    return FetchResult(
        papers=unique,
        groups=group_results,
        window_start_utc=start.isoformat(timespec="seconds"),
        window_end_utc=end.isoformat(timespec="seconds"),
        requested_days=days,
        raw_count=len(all_papers),
    )


def _run_result(
    run_id: int,
    days: int,
    fetch_result: FetchResult,
    stats: UpsertStats,
    link_ok: int,
    link_unverified: int,
) -> RunResult:
    return RunResult(
        run_id=run_id,
        days=days,
        window_start_utc=fetch_result.window_start_utc,
        window_end_utc=fetch_result.window_end_utc,
        raw_count=fetch_result.raw_count,
        unique_count=len(fetch_result.papers),
        new_count=stats.new,
        updated_count=stats.updated,
        unchanged_count=stats.unchanged,
        link_ok=link_ok,
        link_unverified=link_unverified,
        status="success",
    )


def run_retrieval(days: int = config.DEFAULT_DAYS) -> RunResult:
    """完整执行一次 arXiv 检索、去重、链接检查和入库。"""
    db.init_db()
    start, end = build_window(days)
    group_names = [name for name, _ in config.TOPIC_QUERY_GROUPS]
    run_id = db.start_run(
        days,
        start.isoformat(timespec="seconds"),
        end.isoformat(timespec="seconds"),
        group_names,
    )
    fetch_result: FetchResult | None = None
    stats = UpsertStats()
    link_ok = 0
    link_unverified = 0
    try:
        fetch_result = fetch_papers(days, start, end)
        print("检查原文链接 ...", flush=True)
        check_links(fetch_result.papers)
        link_ok = sum(1 for p in fetch_result.papers if p.link_status == "ok")
        link_unverified = len(fetch_result.papers) - link_ok

        stats = db.upsert_papers(fetch_result.papers)
        result = _run_result(run_id, days, fetch_result, stats, link_ok, link_unverified)
        db.finish_run(
            run_id,
            raw_count=result.raw_count,
            unique_count=result.unique_count,
            new_count=result.new_count,
            updated_count=result.updated_count,
            unchanged_count=result.unchanged_count,
            link_ok=result.link_ok,
            link_unverified=result.link_unverified,
            status=result.status,
        )
        return result
    except Exception as exc:
        db.finish_run(
            run_id,
            raw_count=fetch_result.raw_count if fetch_result else 0,
            unique_count=len(fetch_result.papers) if fetch_result else 0,
            new_count=stats.new,
            updated_count=stats.updated,
            unchanged_count=stats.unchanged,
            link_ok=link_ok,
            link_unverified=link_unverified,
            status="failed",
            error_message=str(exc),
        )
        raise


def _local_time(iso_value: str) -> str:
    try:
        value = datetime.fromisoformat(iso_value)
        return value.astimezone(UTC_PLUS_8).strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return iso_value


def print_result(result: RunResult, preview: int = 10) -> None:
    print("\n" + "=" * 72)
    print("检索完成")
    print("=" * 72)
    print(f"时间窗口（UTC）: {result.window_start_utc} -> {result.window_end_utc}")
    print(
        f"时间窗口（UTC+8）: {_local_time(result.window_start_utc)} -> "
        f"{_local_time(result.window_end_utc)}"
    )
    print(
        f"原始条目: {result.raw_count}，去重后: {result.unique_count}，"
        f"新增: {result.new_count}，更新: {result.updated_count}，"
        f"未变: {result.unchanged_count}"
    )
    print(
        f"链接检查: ok={result.link_ok}，unverified={result.link_unverified}"
    )
    print(f"数据库总数: {db.count_papers()}")
    print(f"数据库路径: {config.DB_PATH}")

    if preview <= 0:
        print("\n预览已关闭（--preview 0）。")
        return
    rows = db.get_papers(limit=preview)
    if not rows:
        print("\n数据库中没有可预览的论文。")
        return
    print(f"\n最近入库预览（按 published_at 倒序，前 {len(rows)} 篇）:")
    for row in rows:
        authors = []
        try:
            authors = json.loads(row["authors_json"])
        except (TypeError, ValueError):
            pass
        author_text = ", ".join(authors[:3])
        if len(authors) > 3:
            author_text += " et al."
        print("-" * 72)
        print(f"{row['arxiv_id']}  v{row['version']}  [{row['link_status']}]")
        print(f"标题: {row['title']}")
        print(f"作者: {author_text or '未提供'}")
        print(f"提交时间（UTC+8）: {_local_time(row['published_at'])}")
        print(f"原文: {row['abs_url']}")
        if row["pdf_url"]:
            print(f"PDF: {row['pdf_url']}")
        if not row["has_abstract"]:
            print("摘要: 未提供（已保留记录，未编造内容）")


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
    parser = argparse.ArgumentParser(description="从 arXiv 检索 PINN 相关论文并写入 SQLite")
    parser.add_argument(
        "--days",
        type=int,
        default=config.DEFAULT_DAYS,
        help=f"检索最近多少天，默认 {config.DEFAULT_DAYS}（1-365）",
    )
    parser.add_argument(
        "--preview",
        type=int,
        default=10,
        help="预览最近入库的论文数量，默认 10",
    )
    args = parser.parse_args(argv)

    try:
        result = run_retrieval(days=args.days)
    except Exception as exc:
        print(f"检索失败: {exc}", file=sys.stderr)
        return 2

    print_result(result, preview=max(0, args.preview))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
