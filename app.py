"""PINN 文献雷达 · Streamlit 页面。

页面只做展示和交互；检索、摘要和 SQLite 查询分别在
pinn_radar.ingest / summarize / queries 中。
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import streamlit as st

# 支持直接从项目根目录 `streamlit run app.py`，不要求先安装包。
sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from pinn_radar import __version__, db, ingest, queries, summarize  # noqa: E402
from pinn_radar.config import (
    APP_NAME,
    DEFAULT_DAYS,
    DEEPSEEK_BASE_URL,
    DEEPSEEK_MODEL,
)

UTC_PLUS_8 = timezone(timedelta(hours=8))
CODE_LABELS = {
    "confirmed": "🟢 已确认可访问",
    "claimed": "🟠 有声明 / 链接未验证",
    "not_found": "⚪ 未查到公开代码证据",
    "none": "⚪ 暂无摘要",
}
CODE_OPTIONS = {
    "全部": "",
    "已确认可访问": "confirmed",
    "有声明 / 链接未验证": "claimed",
    "未查到公开代码证据": "not_found",
    "暂无摘要": "none",
}


def _local_time(iso_value: str) -> str:
    try:
        return (
            datetime.fromisoformat(iso_value)
            .astimezone(UTC_PLUS_8)
            .strftime("%Y-%m-%d %H:%M")
        )
    except (TypeError, ValueError):
        return iso_value or ""


def _flash(kind: str, message: str) -> None:
    st.session_state["_flash"] = (kind, message)


def _show_flash() -> None:
    flash = st.session_state.pop("_flash", None)
    if not flash:
        return
    kind, message = flash
    if kind == "success":
        st.success(message)
    elif kind == "warning":
        st.warning(message)
    else:
        st.error(message)


def _author_text(authors: list[str]) -> str:
    if not authors:
        return "作者未提供"
    text = ", ".join(authors[:4])
    if len(authors) > 4:
        text += " et al."
    return text


def _refresh_data(days: int) -> None:
    with st.status(f"正在从 arXiv 获取最近 {days} 天的论文…", expanded=True) as status:
        try:
            result = ingest.run_retrieval(days)
            status.update(label="论文数据更新完成", state="complete")
            if result.unique_count == 0:
                _flash(
                    "warning",
                    "更新完成，但本次时间窗口没有匹配论文；数据库中已有论文未被删除。",
                )
            elif result.new_count == 0:
                _flash(
                    "success",
                    f"更新完成：没有新增论文，当前窗口 {result.unique_count} 篇均已在库中"
                    f"（更新 {result.updated_count} 篇）。",
                )
            else:
                _flash(
                    "success",
                    f"更新完成：新增 {result.new_count} 篇，更新 {result.updated_count} 篇，"
                    f"未变 {result.unchanged_count} 篇；链接可访问 {result.link_ok} 篇。",
                )
        except Exception as exc:  # noqa: BLE001 - UI 要显示可读错误
            status.update(label="论文数据更新失败", state="error")
            _flash("error", f"更新失败：{exc}")
    st.rerun()


def _summarize_pending(arxiv_ids: list[str]) -> None:
    with st.status(f"正在生成 {len(arxiv_ids)} 篇摘要…", expanded=True) as status:
        try:
            results = summarize.summarize_papers(
                arxiv_ids=arxiv_ids,
                limit=len(arxiv_ids),
            )
            success = sum(1 for result in results if result.status == "success")
            failed = len(results) - success
            status.update(label="摘要任务完成", state="complete")
            if success:
                _flash(
                    "success",
                    f"摘要完成：成功 {success} 篇，失败 {failed} 篇。",
                )
            else:
                _flash("warning", f"本次摘要全部失败，请查看侧边栏错误信息。")
        except Exception as exc:  # noqa: BLE001
            status.update(label="摘要任务失败", state="error")
            _flash("error", f"摘要任务失败：{exc}")
    st.rerun()


def _code_label(status: str) -> str:
    return CODE_LABELS.get(status, CODE_LABELS["none"])


def _render_card(paper: queries.PaperView) -> None:
    with st.container(border=True):
        title_col, link_col = st.columns([0.82, 0.18])
        with title_col:
            st.markdown(f"#### {paper.title}")
            st.caption(
                f"{paper.published_local}  ·  {_author_text(paper.authors)}"
                + (f"  ·  {paper.primary_category}" if paper.primary_category else "")
            )
        with link_col:
            st.link_button(
                "打开 arXiv",
                paper.abs_url,
                use_container_width=True,
                key=f"arxiv_link_{paper.arxiv_id}",
            )

        if paper.one_liner:
            st.write(paper.one_liner)
        elif paper.summary_status == "failed":
            st.caption("中文摘要生成失败；原始论文和英文摘要保留。")
        else:
            st.caption("中文摘要尚未生成。")

        info_col, code_col = st.columns([0.68, 0.32])
        with info_col:
            methods = "、".join(paper.methods[:5]) if paper.methods else "摘要未提及"
            st.markdown(f"**研究方法**：{methods}")
        with code_col:
            st.markdown(f"**代码状态**：{_code_label(paper.code_status)}")

        with st.expander("英文摘要与详情"):
            st.markdown("**English abstract**")
            st.write(paper.abstract or "摘要未提供")
            if paper.datasets:
                st.markdown(
                    "**数据集 / 基准**：" + "、".join(paper.datasets[:8])
                )
            if paper.code_note:
                st.caption(paper.code_note)
            if paper.code_urls:
                st.markdown(
                    "**代码链接**："
                    + "  ".join(f"[{url}]({url})" for url in paper.code_urls)
                )
            if paper.summary_status == "failed" and paper.summary_error:
                st.warning(f"摘要失败原因：{paper.summary_error[:300]}")
            st.caption(f"arXiv ID：{paper.arxiv_id}")
            if paper.pdf_url:
                st.markdown(f"[下载 PDF]({paper.pdf_url})")


def main() -> None:
    st.set_page_config(
        page_title=f"{APP_NAME} · 文献雷达",
        page_icon="📚",
        layout="wide",
    )
    st.markdown(
        """
        <style>
        .block-container {max-width: 1180px; padding-top: 2.2rem;}
        [data-testid="stMetricValue"] {font-size: 1.8rem;}
        </style>
        """,
        unsafe_allow_html=True,
    )

    db.init_db()
    _show_flash()

    key_configured = bool(os.getenv("DEEPSEEK_API_KEY", "").strip())

    st.title("📚 PINN 文献雷达")
    st.caption(
        "物理信息机器学习 · arXiv 论文检索、中文摘要与代码线索"
    )

    with st.sidebar:
        st.header("筛选与更新")
        days = st.number_input(
            "检索最近多少天",
            min_value=1,
            max_value=365,
            value=DEFAULT_DAYS,
            step=1,
            key="days",
        )
        code_choice = st.selectbox(
            "代码状态",
            options=list(CODE_OPTIONS.keys()),
            index=0,
            key="code_filter",
        )
        code_status = CODE_OPTIONS[code_choice]

        st.divider()
        st.caption("数据更新")
        if st.button("刷新论文数据", type="primary", use_container_width=True):
            _refresh_data(int(days))

        pending_ids = queries.get_pending_ids(int(days), limit=5)
        summarize_disabled = not key_configured or not pending_ids
        if not key_configured:
            summarize_label = "生成摘要（需配置 key）"
            summarize_help = "先配置 DEEPSEEK_API_KEY 才能调用 DeepSeek。"
        elif not pending_ids:
            summarize_label = "生成摘要（暂无待处理）"
            summarize_help = "当前窗口没有待生成摘要的论文。"
        else:
            summarize_label = f"生成摘要（{len(pending_ids)} 篇待处理）"
            summarize_help = "只处理当前时间窗内还没有成功摘要的论文。"
        if st.button(
            summarize_label,
            disabled=summarize_disabled,
            use_container_width=True,
            help=summarize_help,
        ):
            _summarize_pending(pending_ids)

        if not key_configured:
            st.caption("未配置 DEEPSEEK_API_KEY：只能浏览已有摘要。")
        elif not pending_ids:
            st.caption("当前窗口没有待生成摘要的论文。")
        else:
            st.caption(f"待处理 {len(pending_ids)} 篇；每次最多 5 篇。")

        last_run = queries.get_last_run()
        if last_run:
            st.divider()
            st.caption("上次检索")
            st.write(
                f"{_local_time(last_run.get('finished_at') or last_run['started_at'])}"
            )
            st.caption(
                f"窗口 {last_run['days']} 天 · 新增 {last_run['new_count']} · "
                f"更新 {last_run['updated_count']}"
            )

    stats = queries.get_stats(int(days))
    metric_cols = st.columns(3)
    metric_cols[0].metric("论文总数", stats["total"])
    metric_cols[1].metric(f"最近 {int(days)} 天", stats["window_count"])
    metric_cols[2].metric(
        "有公开代码证据",
        stats["code_evidence"],
        help="来自已生成摘要中的 confirmed / claimed；不是对论文是否开源的最终判断。",
    )

    if stats["summarized"] == 0:
        st.info(
            "当前还没有中文摘要。论文列表、搜索和筛选可用；"
            "配置 DEEPSEEK_API_KEY 后，可在侧边栏生成摘要。"
        )

    search_text = st.text_input(
        "搜索标题、摘要、作者或中文摘要",
        placeholder="例如：parameter estimation、PINN、参数辨识",
        key="search_text",
    )
    papers = queries.search_papers(
        int(days),
        keyword=search_text,
        code_status=code_status,
    )
    st.caption(
        f"筛选结果：**{len(papers)}** 篇"
        f"  ·  代码状态：{code_choice}"
        f"  ·  时间窗口：最近 {int(days)} 天"
        + (f"  ·  关键词：{search_text}" if search_text.strip() else "")
    )

    if not papers:
        if stats["total"] == 0:
            st.warning("数据库还没有论文。点击侧边栏“刷新论文数据”开始。")
        else:
            st.warning("当前筛选条件下没有匹配论文，请调整关键词、天数或代码状态。")
        return

    for paper in papers:
        _render_card(paper)

    st.divider()
    st.caption(
        f"当前版本：{__version__}  ·  "
        f"DeepSeek 配置：{DEEPSEEK_MODEL} @ {DEEPSEEK_BASE_URL}  ·  "
        "代码证据只来自摘要/comment；“未查到”不等于未开源。"
    )


if __name__ == "__main__":
    main()
