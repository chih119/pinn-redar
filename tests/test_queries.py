"""页面查询测试：使用独立临时数据库，不碰生产 data/papers.db。"""

from __future__ import annotations

from pathlib import Path

import pytest

from pinn_radar import config, db, queries
from pinn_radar.models import PaperRecord, SummaryResult

TEST_DB = Path("tests/_tmp_queries.sqlite3")


@pytest.fixture()
def temp_db(monkeypatch: pytest.MonkeyPatch):
    for suffix in ("", "-wal", "-shm"):
        Path(str(TEST_DB) + suffix).unlink(missing_ok=True)
    monkeypatch.setattr(config, "DB_PATH", TEST_DB)
    db.init_db()
    try:
        yield TEST_DB
    finally:
        for suffix in ("", "-wal", "-shm"):
            Path(str(TEST_DB) + suffix).unlink(missing_ok=True)


def _add_paper_with_summary() -> None:
    paper = PaperRecord(
        arxiv_id="2610.00099",
        version=1,
        title="Physics-Informed Neural Network for Test Parameter Identification",
        abstract=(
            "We propose a physics-informed neural network for parameter identification."
        ),
        authors=["Test Author"],
        categories=["cs.LG"],
        primary_category="cs.LG",
        published_at="2026-10-30T00:00:00+00:00",
        updated_at="2026-10-30T00:00:00+00:00",
        comment="Code is available at https://github.com/example/repo",
        journal_ref="",
        doi="",
        abs_url="https://arxiv.org/abs/2610.00099",
        pdf_url=None,
        has_abstract=True,
        source_queries=["test"],
        fetched_at="2026-10-31T00:00:00+00:00",
    )
    db.upsert_papers([paper])
    result = SummaryResult(
        arxiv_id=paper.arxiv_id,
        status="success",
        summary={
            "core_contribution_zh": "测试用的中文核心贡献。",
            "core_contribution_evidence_en": (
                "We propose a physics-informed neural network for parameter identification."
            ),
            "methods": [
                {
                    "name_zh": "物理信息神经网络",
                    "term_en": "physics-informed neural network",
                    "evidence_en": (
                        "We propose a physics-informed neural network for parameter identification."
                    ),
                }
            ],
            "datasets": [],
            "code": {
                "status": "confirmed",
                "urls": ["https://github.com/example/repo"],
                "evidence_en": "Code is available at https://github.com/example/repo",
                "note_zh": "测试代码状态。",
            },
        },
        raw_response="{}",
        model="test",
        prompt_version="test",
        attempts=1,
        error="",
        title_en=paper.title,
        abstract_en=paper.abstract,
    )
    db.save_summary(result)


def test_stats_and_keyword_search(temp_db) -> None:
    _add_paper_with_summary()
    stats = queries.get_stats(30)
    assert stats["total"] == 1
    assert stats["window_count"] == 1
    assert stats["summarized"] == 1
    assert stats["code_evidence"] == 1

    assert len(queries.search_papers(30)) == 1
    assert len(queries.search_papers(30, "parameter identification")) == 1
    assert len(queries.search_papers(30, "中文核心贡献")) == 1
    assert len(queries.search_papers(30, "no-such-token")) == 0


def test_code_status_filters(temp_db) -> None:
    _add_paper_with_summary()
    assert len(queries.search_papers(30, code_status="confirmed")) == 1
    assert len(queries.search_papers(30, code_status="claimed")) == 0
    assert len(queries.search_papers(30, code_status="not_found")) == 0
    assert len(queries.search_papers(30, code_status="none")) == 0
