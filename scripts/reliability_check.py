"""可靠性冒烟测试：模拟 arXiv / DeepSeek 异常，不访问真实接口。

运行：
    python scripts/reliability_check.py
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from unittest import mock

import feedparser
import httpx
import openai
import requests
from streamlit.testing.v1 import AppTest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from pinn_radar import config, db, ingest, queries, summarize  # noqa: E402
from pinn_radar.models import FetchResult, PaperRecord, RunResult, SummaryResult  # noqa: E402

TEST_DB = ROOT / "data" / "_reliability_test.db"
ORIGINAL_DB = config.DB_PATH
RESULTS: list[tuple[str, bool, str]] = []
CHECKS: list[tuple[str, object]] = []


def cleanup_db() -> None:
    for suffix in ("", "-wal", "-shm"):
        Path(str(TEST_DB) + suffix).unlink(missing_ok=True)


def reset_db() -> None:
    cleanup_db()
    config.DB_PATH = TEST_DB
    db.init_db()


def sample_paper(arxiv_id: str = "2610.00097") -> PaperRecord:
    return PaperRecord(
        arxiv_id=arxiv_id,
        version=1,
        title="Physics-Informed Neural Network for Reliability Test",
        abstract=(
            "We propose a physics-informed neural network for parameter identification."
        ),
        authors=["Reliability Author"],
        categories=["cs.LG"],
        primary_category="cs.LG",
        published_at="2026-10-30T00:00:00+00:00",
        updated_at="2026-10-30T00:00:00+00:00",
        comment="",
        journal_ref="",
        doi="",
        abs_url=f"https://arxiv.org/abs/{arxiv_id}",
        pdf_url=None,
        has_abstract=True,
        source_queries=["test"],
        fetched_at="2026-10-31T00:00:00+00:00",
    )


def check(name: str):
    def decorator(func):
        CHECKS.append((name, func))
        return func

    return decorator


def empty_feed(total: int = 0):
    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
    <feed xmlns="http://www.w3.org/2005/Atom"
          xmlns:opensearch="http://a9.com/-/spec/opensearch/1.1/">
      <opensearch:totalResults>{total}</opensearch:totalResults>
    </feed>
    """
    return feedparser.parse(xml)


@check("arXiv 超时/连接失败：记录失败，不丢已有论文，不崩溃")
def check_arxiv_timeout() -> None:
    reset_db()
    db.upsert_papers([sample_paper()])
    before = db.count_papers()
    with mock.patch.object(
        ingest.ArxivClient,
        "fetch",
        side_effect=ingest.ArxivError("simulated arXiv timeout"),
    ):
        try:
            ingest.run_retrieval(7)
        except ingest.ArxivError:
            pass
        else:
            raise AssertionError("arXiv 超时没有抛出 ArxivError")
    assert db.count_papers() == before, "arXiv 失败后论文数量发生变化"
    last = db.get_last_run()
    assert last is not None and last["status"] == "failed", "运行记录没有标记 failed"


@check("arXiv 客户端超时重试：有限次数后抛出 ArxivError")
def check_arxiv_client_timeout() -> None:
    class TimeoutSession:
        def __init__(self) -> None:
            self.calls = 0

        def get(self, *args, **kwargs):
            self.calls += 1
            raise requests.Timeout("simulated socket timeout")

    session = TimeoutSession()
    client = ingest.ArxivClient(delay=0)
    client.session = session  # type: ignore[assignment]
    with mock.patch.object(config, "ARXIV_MAX_RETRIES", 2), mock.patch.object(
        ingest.time, "sleep"
    ):
        try:
            client.fetch("all:test", 0, 1)
        except ingest.ArxivError as exc:
            assert "已重试" in str(exc)
        else:
            raise AssertionError("客户端超时没有抛出 ArxivError")
    assert session.calls == 2, f"重试次数不正确: {session.calls}"


@check("arXiv 返回空数据：正常结束，不写论文、不崩溃")
def check_arxiv_empty() -> None:
    reset_db()
    with mock.patch.object(
        ingest.ArxivClient,
        "fetch",
        return_value=empty_feed(total=0),
    ):
        result = ingest.run_retrieval(7)
    assert result.status == "success"
    assert result.unique_count == 0
    assert db.count_papers() == 0


@check("arXiv totalResults>0 但无条目：识别为异常，不静默当空结果")
def check_arxiv_anomaly() -> None:
    reset_db()
    with mock.patch.object(
        ingest.ArxivClient,
        "fetch",
        return_value=empty_feed(total=5),
    ):
        try:
            ingest.run_retrieval(7)
        except ingest.ArxivError as exc:
            assert "没有可解析" in str(exc)
        else:
            raise AssertionError("异常空响应没有被识别")


class StatusError(Exception):
    def __init__(self, status_code: int) -> None:
        super().__init__(f"simulated HTTP {status_code}")
        self.status_code = status_code


class FakeDeepSeek:
    def __init__(self, *, error: Exception | None = None, outputs: list[str] | None = None):
        self.model = "fake-deepseek"
        self.error = error
        self.outputs = outputs or []

    def complete(self, messages):
        if self.error:
            raise self.error
        return self.outputs.pop(0), 10, 10


@check("DeepSeek 503：有限重试后失败，摘要记 failed，论文保留")
def check_deepseek_503() -> None:
    reset_db()
    db.upsert_papers([sample_paper()])
    fake = FakeDeepSeek(error=StatusError(503))
    with mock.patch.object(summarize, "DeepSeekClient", return_value=fake), mock.patch.object(
        summarize.time, "sleep"
    ):
        results = summarize.summarize_papers(limit=1)
    assert len(results) == 1
    assert results[0].status == "failed"
    assert results[0].attempts == summarize.MAX_ATTEMPTS
    assert db.count_papers() == 1, "摘要失败导致论文丢失"
    row = db.get_papers(limit=1)[0]
    assert row["title"] == sample_paper().title
    assert row["abstract"] == sample_paper().abstract
    assert db.count_summaries("failed") == 1


@check("DeepSeek 网络连接失败：有限重试后失败，论文保留")
def check_deepseek_connection_error() -> None:
    reset_db()
    db.upsert_papers([sample_paper()])
    request = httpx.Request("POST", "https://api.deepseek.com/chat/completions")
    fake = FakeDeepSeek(error=openai.APIConnectionError(request=request))
    with mock.patch.object(summarize, "DeepSeekClient", return_value=fake), mock.patch.object(
        summarize.time, "sleep"
    ):
        results = summarize.summarize_papers(limit=1)
    assert results[0].status == "failed"
    assert results[0].attempts == summarize.MAX_ATTEMPTS
    assert db.count_papers() == 1


@check("DeepSeek 余额/额度错误 402：不重试，记录失败，论文保留")
def check_deepseek_quota() -> None:
    reset_db()
    db.upsert_papers([sample_paper()])
    fake = FakeDeepSeek(error=StatusError(402))
    with mock.patch.object(summarize, "DeepSeekClient", return_value=fake):
        results = summarize.summarize_papers(limit=1)
    assert results[0].status == "failed"
    assert results[0].attempts == 1
    assert db.count_papers() == 1


@check("DeepSeek 返回非法 JSON：重试后失败，不中断，不写假摘要")
def check_deepseek_invalid_json() -> None:
    reset_db()
    db.upsert_papers([sample_paper()])
    fake = FakeDeepSeek(outputs=["not json"] * summarize.MAX_ATTEMPTS)
    with mock.patch.object(summarize, "DeepSeekClient", return_value=fake), mock.patch.object(
        summarize.time, "sleep"
    ):
        results = summarize.summarize_papers(limit=1)
    assert results[0].status == "failed"
    assert results[0].summary is None
    assert db.count_papers() == 1
    assert db.count_summaries("failed") == 1


@check("DeepSeek 返回合法 JSON 但字段不完整：重试后失败，不写假摘要")
def check_deepseek_schema_mismatch() -> None:
    reset_db()
    db.upsert_papers([sample_paper()])
    fake = FakeDeepSeek(outputs=[json.dumps({"core_contribution_zh": "x"})] * summarize.MAX_ATTEMPTS)
    with mock.patch.object(summarize, "DeepSeekClient", return_value=fake), mock.patch.object(
        summarize.time, "sleep"
    ):
        results = summarize.summarize_papers(limit=1)
    assert results[0].status == "failed"
    assert results[0].summary is None
    assert db.count_papers() == 1
    assert db.count_summaries("failed") == 1


@check("重复检索：同一论文只保存一行；成功摘要不会被重复选中")
def check_duplicates() -> None:
    reset_db()
    paper = sample_paper()
    first = db.upsert_papers([paper])
    second = db.upsert_papers([sample_paper()])
    assert first.new == 1
    assert second.new == 0
    assert second.updated == 0
    assert second.unchanged == 1
    assert db.count_papers() == 1

    successful = SummaryResult(
        arxiv_id=paper.arxiv_id,
        status="success",
        summary={
            "core_contribution_zh": "测试摘要。",
            "code": {"status": "not_found", "urls": [], "evidence_en": ""},
        },
        raw_response="{}",
        model="fake",
        prompt_version="test",
        attempts=1,
        error="",
        title_en=paper.title,
        abstract_en=paper.abstract,
    )
    db.save_summary(successful)
    assert db.get_papers_for_summary(limit=5) == [], "成功摘要论文被重复选中"


@check("未配置 key：页面仍可显示已保存论文，摘要按钮禁用")
def check_no_key_page() -> None:
    reset_db()
    db.upsert_papers([sample_paper()])
    with mock.patch.dict(os.environ, {"DEEPSEEK_API_KEY": ""}):
        at = AppTest.from_file(str(ROOT / "app.py"), default_timeout=30).run()
    assert len(at.exception) == 0, f"页面异常: {[e.value for e in at.exception]}"
    assert any(metric.label == "论文总数" and metric.value == "1" for metric in at.metric)
    summary_buttons = [button for button in at.button if "生成摘要" in button.label]
    assert summary_buttons and all(button.disabled for button in summary_buttons)


@check("网页反复刷新：不触发 arXiv 检索，也不触发 DeepSeek 摘要")
def check_page_refresh_no_calls() -> None:
    reset_db()
    db.upsert_papers([sample_paper()])
    with mock.patch.object(ingest, "run_retrieval", wraps=ingest.run_retrieval) as run_mock, mock.patch.object(
        summarize, "summarize_papers", wraps=summarize.summarize_papers
    ) as summary_mock:
        at = AppTest.from_file(str(ROOT / "app.py"), default_timeout=30).run()
        at.run()
    assert len(at.exception) == 0
    assert not run_mock.called, "网页刷新触发了 arXiv 检索"
    assert not summary_mock.called, "网页刷新触发了 DeepSeek 摘要"
    assert db.count_papers() == 1


@check("arXiv 返回空结果：网页显示提示，不崩溃，不清空已有论文")
def check_ui_empty_result() -> None:
    reset_db()
    db.upsert_papers([sample_paper()])
    empty_result = RunResult(
        run_id=1,
        days=7,
        window_start_utc="2026-10-24T00:00:00+00:00",
        window_end_utc="2026-10-31T00:00:00+00:00",
        raw_count=0,
        unique_count=0,
        new_count=0,
        updated_count=0,
        unchanged_count=0,
        link_ok=0,
        link_unverified=0,
        status="success",
    )
    at = AppTest.from_file(str(ROOT / "app.py"), default_timeout=30).run()
    with mock.patch.object(ingest, "run_retrieval", return_value=empty_result):
        refresh = next(button for button in at.button if "刷新论文数据" in button.label)
        refresh.click()
        at.run(timeout=60)
    assert len(at.exception) == 0
    warnings = [warning.value for warning in at.warning]
    assert any("没有匹配论文" in warning for warning in warnings), warnings
    assert db.count_papers() == 1


@check("网页刷新失败：显示错误提示，不崩溃，不清空已有论文")
def check_ui_refresh_error() -> None:
    reset_db()
    db.upsert_papers([sample_paper()])
    at = AppTest.from_file(str(ROOT / "app.py"), default_timeout=30).run()
    assert len(at.exception) == 0
    with mock.patch.object(
        ingest,
        "run_retrieval",
        side_effect=ingest.ArxivError("simulated refresh timeout"),
    ):
        refresh = next(button for button in at.button if "刷新论文数据" in button.label)
        refresh.click()
        at.run(timeout=60)
    assert len(at.exception) == 0, f"页面崩溃: {[e.value for e in at.exception]}"
    errors = [error.value for error in at.error]
    assert any("更新失败" in error and "simulated refresh timeout" in error for error in errors), errors
    assert db.count_papers() == 1


def main() -> int:
    print("可靠性模拟测试（独立临时数据库，不调用真实接口）")
    print(f"临时数据库: {TEST_DB}")
    try:
        for name, func in CHECKS:
            try:
                func()
            except Exception as exc:  # noqa: BLE001 - 测试脚本需要收集所有失败
                RESULTS.append((name, False, f"{type(exc).__name__}: {exc}"))
                print(f"[FAIL] {name}: {type(exc).__name__}: {exc}")
            else:
                RESULTS.append((name, True, ""))
                print(f"[PASS] {name}")
    finally:
        config.DB_PATH = ORIGINAL_DB
        cleanup_db()

    failed = [item for item in RESULTS if not item[1]]
    print("\n" + "=" * 70)
    print(f"通过 {len(RESULTS) - len(failed)} / {len(RESULTS)}")
    for name, ok, error in RESULTS:
        print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  ({error})" if error else ""))
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
