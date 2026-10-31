"""阶段 1 检索模块的离线单元测试；不访问网络，不写生产数据库。"""

from __future__ import annotations

from datetime import datetime, timezone

import feedparser
import pytest

from pinn_radar import config, ingest
from pinn_radar.models import PaperRecord


def _paper(arxiv_id: str, source_query: str) -> PaperRecord:
    return PaperRecord(
        arxiv_id=arxiv_id,
        version=1,
        title="Example title",
        abstract="Example abstract.",
        authors=["A. Author"],
        categories=["cs.LG"],
        primary_category="cs.LG",
        published_at="2026-10-01T00:00:00+00:00",
        updated_at="2026-10-01T00:00:00+00:00",
        comment="",
        journal_ref="",
        doi="",
        abs_url=f"https://arxiv.org/abs/{arxiv_id}",
        pdf_url=None,
        has_abstract=True,
        source_queries=[source_query],
        fetched_at="2026-10-31T00:00:00+00:00",
    )


def test_build_window_uses_requested_days() -> None:
    now = datetime(2026, 10, 31, 6, 0, tzinfo=timezone.utc)
    start, end = ingest.build_window(7, now=now)
    assert end == now
    assert (end - start).days == 7
    assert ingest.arxiv_date(start) == "202610240600"
    assert ingest.arxiv_date(end) == "202610310600"


def test_normalize_arxiv_id_strips_version_and_url() -> None:
    assert ingest.normalize_arxiv_id("http://arxiv.org/abs/2610.12345v2") == (
        "2610.12345",
        2,
    )
    assert ingest.normalize_arxiv_id("2610.12345") == ("2610.12345", 1)
    assert ingest.normalize_arxiv_id("hep-th/9901001v1") == ("hep-th/9901001", 1)


def test_dedupe_merges_source_queries() -> None:
    papers = [
        _paper("2610.00001", "dynamical systems"),
        _paper("2610.00001", "system identification"),
        _paper("2610.00002", "engineering applications"),
    ]
    unique = ingest.dedupe_papers(papers)
    assert len(unique) == 2
    first = next(p for p in unique if p.arxiv_id == "2610.00001")
    assert first.source_queries == ["dynamical systems", "system identification"]


def test_parse_entry_without_abstract_does_not_invent_one() -> None:
    xml = """<?xml version="1.0" encoding="UTF-8"?>
    <feed xmlns="http://www.w3.org/2005/Atom"
          xmlns:arxiv="http://arxiv.org/schemas/atom">
      <entry>
        <id>http://arxiv.org/abs/2610.99999v1</id>
        <title>No abstract example</title>
        <link href="https://arxiv.org/abs/2610.99999v1" rel="alternate"/>
        <published>2026-10-30T00:00:00Z</published>
        <updated>2026-10-30T00:00:00Z</updated>
        <author><name>Test Author</name></author>
        <category term="cs.LG"/>
      </entry>
    </feed>
    """
    entry = feedparser.parse(xml).entries[0]
    paper = ingest.parse_entry(entry, "test", "2026-10-31T00:00:00+00:00")
    assert paper.arxiv_id == "2610.99999"
    assert paper.has_abstract is False
    assert paper.abstract == ""
    assert paper.abs_url == "https://arxiv.org/abs/2610.99999"


def test_api_connection_failure_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "ARXIV_API_URL", "http://127.0.0.1:1/api/query")
    monkeypatch.setattr(config, "ARXIV_MAX_RETRIES", 1)
    client = ingest.ArxivClient(delay=0)
    with pytest.raises(ingest.ArxivError):
        client.fetch("all:test", 0, 1)
