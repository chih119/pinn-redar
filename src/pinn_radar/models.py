"""跨模块数据对象。

阶段 1 只包含 arXiv 检索需要的对象；摘要和代码线索对象后续再加。
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class PaperRecord:
    """一篇通过 arXiv API 获取并规范化的论文。"""

    arxiv_id: str
    version: int
    title: str
    abstract: str
    authors: list[str]
    categories: list[str]
    primary_category: str
    published_at: str
    updated_at: str
    comment: str
    journal_ref: str
    doi: str
    abs_url: str
    pdf_url: str | None
    has_abstract: bool
    source_queries: list[str] = field(default_factory=list)
    fetched_at: str = ""
    link_status: str = "unverified"
    link_checked_at: str | None = None


@dataclass
class GroupResult:
    """单个主题查询的统计。"""

    name: str
    total_results: int
    pages: int
    errors: list[str] = field(default_factory=list)


@dataclass
class FetchResult:
    """所有主题查询合并、去重后的结果。"""

    papers: list[PaperRecord]
    groups: list[GroupResult]
    window_start_utc: str
    window_end_utc: str
    requested_days: int
    raw_count: int = 0


@dataclass
class UpsertStats:
    """入库统计。"""

    new: int = 0
    updated: int = 0
    unchanged: int = 0

    @property
    def total(self) -> int:
        return self.new + self.updated + self.unchanged


@dataclass
class RunResult:
    """一次检索运行的汇总。"""

    run_id: int
    days: int
    window_start_utc: str
    window_end_utc: str
    raw_count: int
    unique_count: int
    new_count: int
    updated_count: int
    unchanged_count: int
    link_ok: int
    link_unverified: int
    status: str
    error_message: str = ""


@dataclass
class SummaryResult:
    """单篇论文的摘要结果；失败时保留原始论文和错误信息。"""

    arxiv_id: str
    status: str
    summary: dict | None
    raw_response: str
    model: str
    prompt_version: str
    attempts: int
    error: str
    title_en: str
    abstract_en: str
    input_tokens: int = 0
    output_tokens: int = 0
