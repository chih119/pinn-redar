"""DeepSeek 结构化中文摘要模块。

只根据论文标题、英文摘要和 arXiv comment 提取信息；失败时保存失败记录，
不修改论文本身，也不让单篇失败中断整批任务。

运行示例：
    python -m pinn_radar.summarize --limit 5
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from typing import Any, Literal

import requests
from openai import (
    APIConnectionError,
    APITimeoutError,
    AuthenticationError,
    OpenAI,
)
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from pinn_radar import config, db
from pinn_radar.models import PaperRecord, SummaryResult

PROMPT_PATH = config.PROJECT_ROOT / "prompts" / "summarize_v1.md"
PROMPT_VERSION = config.SUMMARY_PROMPT_VERSION
MAX_ATTEMPTS = config.SUMMARY_MAX_ATTEMPTS

CODE_URL_RE = re.compile(
    r"https?://(?:www\.)?(?:github\.com|gitlab\.com|huggingface\.co|zenodo\.org|"
    r"figshare\.com|codeocean\.com|anonymous\.4open\.science|bitbucket\.org)"
    r"[^\s<>'\"\]\)]*",
    re.IGNORECASE,
)

PROMPT_CACHE: str | None = None


class SummaryError(RuntimeError):
    """摘要配置或调用错误。"""


class SummaryConfigError(SummaryError):
    """缺少 API key 等配置问题。"""


class NamedItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name_zh: str = Field(min_length=1)
    term_en: str = Field(min_length=1)
    evidence_en: str = Field(min_length=1)


class CodeOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["link_provided", "claimed", "not_found"]
    urls: list[str] = Field(default_factory=list)
    evidence_en: str = ""


class SummaryPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    core_contribution_zh: str = Field(min_length=1)
    core_contribution_evidence_en: str = Field(min_length=1)
    methods: list[NamedItem] = Field(default_factory=list)
    datasets: list[NamedItem] = Field(default_factory=list)
    code: CodeOutput


def _normalize(text: str) -> str:
    value = (text or "").replace("\u00a0", " ")
    value = re.sub(r"\s+", " ", value).strip().lower()
    return value.replace("$", "")


def _grounded(quote: str, source: str) -> bool:
    normalized_quote = _normalize(quote)
    return bool(normalized_quote) and normalized_quote in _normalize(source)


def facts_source(paper: PaperRecord) -> str:
    return f"{paper.title}\n{paper.abstract}"


def code_source(paper: PaperRecord) -> str:
    return f"{paper.title}\n{paper.abstract}\n{paper.comment}"


def _strip_url(url: str) -> str:
    return url.rstrip(".,;:!?)]}'\"")


def extract_code_urls(text: str) -> list[str]:
    urls = [_strip_url(match) for match in CODE_URL_RE.findall(text or "")]
    return list(dict.fromkeys(url for url in urls if url))


def _url_reachable(url: str) -> bool:
    try:
        response = requests.head(
            url,
            headers={"User-Agent": "pinn-radar/0.1 (code link check)"},
            timeout=config.CODE_CHECK_TIMEOUT_SECONDS,
            allow_redirects=True,
        )
        if response.status_code in (403, 405, 501):
            response = requests.get(
                url,
                headers={"User-Agent": "pinn-radar/0.1 (code link check)"},
                timeout=config.CODE_CHECK_TIMEOUT_SECONDS,
                allow_redirects=True,
                stream=True,
            )
        return 200 <= response.status_code < 300
    except requests.RequestException:
        return False


def _build_code_info(payload: CodeOutput, paper: PaperRecord, check_links: bool) -> dict:
    source = code_source(paper)
    source_urls = extract_code_urls(source)
    model_urls = [
        _strip_url(url) for url in payload.urls if _grounded(_strip_url(url), source)
    ]
    urls = list(dict.fromkeys([*model_urls, *source_urls]))
    evidence = payload.evidence_en if _grounded(payload.evidence_en, source) else ""

    if urls:
        if not evidence:
            evidence = urls[0]
        reachable = any(_url_reachable(url) for url in urls) if check_links else False
        if reachable:
            return {
                "status": "confirmed",
                "urls": urls,
                "evidence_en": evidence,
                "note_zh": "输入文字提供了代码链接，且链接当前可访问。",
            }
        note = (
            "输入文字提供了代码链接，但当前链接检查未通过；未确认可访问。"
            if check_links
            else "输入文字提供了代码链接，但本次未检查链接可达性。"
        )
        return {
            "status": "claimed",
            "urls": urls,
            "evidence_en": evidence,
            "note_zh": note,
        }

    if payload.status == "claimed" and evidence:
        return {
            "status": "claimed",
            "urls": [],
            "evidence_en": evidence,
            "note_zh": "输入文字声称代码可用或将会开源，但没有提供代码链接。",
        }

    return {
        "status": "not_found",
        "urls": [],
        "evidence_en": "",
        "note_zh": "未在标题、摘要或 comment 中查到公开代码证据；这不等于作者没有公开代码。",
    }


def _extract_json(content: str) -> dict:
    text = (content or "").strip()
    if not text:
        raise ValueError("模型返回为空")
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("没有找到 JSON 对象")
    data = json.loads(text[start : end + 1])
    if not isinstance(data, dict):
        raise ValueError("JSON 顶层不是对象")
    return data


def _validate_payload(payload: SummaryPayload, paper: PaperRecord) -> list[str]:
    errors: list[str] = []
    fact_source = facts_source(paper)
    code_text = code_source(paper)

    if not _grounded(payload.core_contribution_evidence_en, fact_source):
        errors.append("core_contribution_evidence_en 不是标题/摘要中的逐字原文")

    for label, items in (("methods", payload.methods), ("datasets", payload.datasets)):
        for index, item in enumerate(items, 1):
            if not _grounded(item.evidence_en, fact_source):
                errors.append(f"{label}[{index}].evidence_en 不是逐字原文")
            if not _grounded(item.term_en, fact_source):
                errors.append(f"{label}[{index}].term_en 不在原文中")

    if payload.code.status == "link_provided" and not payload.code.urls:
        errors.append("code.status=link_provided 但没有 urls")
    if payload.code.status == "claimed" and not payload.code.evidence_en:
        errors.append("code.status=claimed 但 evidence_en 为空")
    if payload.code.evidence_en and not _grounded(payload.code.evidence_en, code_text):
        errors.append("code.evidence_en 不是逐字原文")
    for index, url in enumerate(payload.code.urls, 1):
        if not _grounded(_strip_url(url), code_text):
            errors.append(f"code.urls[{index}] 不在原文中")
    return errors


def _load_prompt() -> str:
    global PROMPT_CACHE
    if PROMPT_CACHE is None:
        PROMPT_CACHE = PROMPT_PATH.read_text(encoding="utf-8")
    return PROMPT_CACHE


def _is_retryable(exc: Exception) -> bool:
    if isinstance(exc, (APIConnectionError, APITimeoutError)):
        return True
    status = getattr(exc, "status_code", None)
    if isinstance(status, int):
        return status in (408, 409, 429) or status >= 500
    return False


class DeepSeekClient:
    def __init__(self, api_key: str | None = None) -> None:
        key = (api_key or os.getenv("DEEPSEEK_API_KEY", "")).strip()
        if not key:
            raise SummaryConfigError(
                "未找到 DEEPSEEK_API_KEY。请在项目根目录 .env 中设置，"
                "或先在 PowerShell 里设置环境变量；不要把 key 写进代码。"
            )
        self.model = config.DEEPSEEK_MODEL
        self.client = OpenAI(
            api_key=key,
            base_url=config.DEEPSEEK_BASE_URL,
            timeout=config.DEEPSEEK_TIMEOUT_SECONDS,
        )

    def complete(self, messages: list[dict[str, str]]) -> tuple[str, int, int]:
        response = self.client.chat.completions.create(
            model=self.model,
            messages=messages,
            response_format={"type": "json_object"},
            temperature=config.SUMMARY_TEMPERATURE,
            max_tokens=config.SUMMARY_MAX_TOKENS,
        )
        content = response.choices[0].message.content or ""
        usage = response.usage
        return (
            content,
            int(getattr(usage, "prompt_tokens", 0) or 0),
            int(getattr(usage, "completion_tokens", 0) or 0),
        )


def _user_content(paper: PaperRecord) -> str:
    return (
        "请从下面这篇论文的文字中提取信息，并且只输出一个 JSON 对象。\n\n"
        f"TITLE:\n{paper.title}\n\n"
        f"ABSTRACT:\n{paper.abstract or '(empty)'}\n\n"
        f"COMMENT:\n{paper.comment or '(empty)'}"
    )


def _failed_result(
    paper: PaperRecord,
    *,
    attempts: int,
    error: str,
    raw_response: str,
    input_tokens: int,
    output_tokens: int,
    model: str,
) -> SummaryResult:
    return SummaryResult(
        arxiv_id=paper.arxiv_id,
        status="failed",
        summary=None,
        raw_response=raw_response,
        model=model,
        prompt_version=PROMPT_VERSION,
        attempts=attempts,
        error=error,
        title_en=paper.title,
        abstract_en=paper.abstract,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
    )


def summarize_one(
    paper: PaperRecord,
    client: DeepSeekClient | None = None,
    *,
    check_code_links: bool = True,
) -> SummaryResult:
    """摘要单篇论文；任何异常都转换为 failed 结果，不向上抛出。"""
    client = client or DeepSeekClient()
    system_prompt = _load_prompt()
    messages: list[dict[str, str]] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": _user_content(paper)},
    ]
    last_raw = ""
    last_error = ""
    input_tokens = 0
    output_tokens = 0

    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            content, in_tokens, out_tokens = client.complete(messages)
            input_tokens += in_tokens
            output_tokens += out_tokens
            last_raw = content
            try:
                data = _extract_json(content)
                payload = SummaryPayload.model_validate(data)
            except (ValueError, json.JSONDecodeError, ValidationError) as exc:
                last_error = f"JSON/schema 校验失败: {exc}"
                if attempt < MAX_ATTEMPTS:
                    if content:
                        messages.append(
                            {"role": "assistant", "content": content[:6000]}
                        )
                    messages.append(
                        {
                            "role": "user",
                            "content": (
                                "上一次输出无法通过 JSON/schema 校验。"
                                f"错误：{last_error}。请严格按 system prompt 的字段和 JSON 示例，"
                                "只输出一个合法 JSON 对象，不要解释，不要代码围栏。"
                            ),
                        }
                    )
                continue

            errors = _validate_payload(payload, paper)
            if errors:
                last_error = "证据校验失败: " + "; ".join(errors)
                if attempt < MAX_ATTEMPTS:
                    messages.append({"role": "assistant", "content": content[:6000]})
                    messages.append(
                        {
                            "role": "user",
                            "content": (
                                "上一次输出没有通过证据校验。"
                                f"错误：{last_error}。"
                                "请只使用标题/摘要/comment 中的逐字原文作为 evidence_en，"
                                "并严格按 schema 重新输出一个 JSON 对象。"
                            ),
                        }
                    )
                continue

            summary = {
                "core_contribution_zh": payload.core_contribution_zh.strip(),
                "core_contribution_evidence_en": (
                    payload.core_contribution_evidence_en.strip()
                ),
                "methods": [item.model_dump() for item in payload.methods],
                "datasets": [item.model_dump() for item in payload.datasets],
                "code": _build_code_info(
                    payload.code, paper, check_links=check_code_links
                ),
            }
            return SummaryResult(
                arxiv_id=paper.arxiv_id,
                status="success",
                summary=summary,
                raw_response=content,
                model=client.model,
                prompt_version=PROMPT_VERSION,
                attempts=attempt,
                error="",
                title_en=paper.title,
                abstract_en=paper.abstract,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
            )

        except AuthenticationError as exc:
            return _failed_result(
                paper,
                attempts=attempt,
                error=f"DeepSeek 认证失败: {exc}",
                raw_response=last_raw,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                model=client.model,
            )
        except Exception as exc:  # noqa: BLE001 - 单篇失败要保留记录
            last_error = f"API 调用失败: {exc}"
            if _is_retryable(exc) and attempt < MAX_ATTEMPTS:
                time.sleep(2 ** (attempt - 1))
                continue
            return _failed_result(
                paper,
                attempts=attempt,
                error=last_error,
                raw_response=last_raw,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                model=client.model,
            )

    return _failed_result(
        paper,
        attempts=MAX_ATTEMPTS,
        error=last_error or "超过最大重试次数",
        raw_response=last_raw,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        model=client.model,
    )


def summarize_papers(
    limit: int = 5,
    arxiv_ids: list[str] | None = None,
    *,
    check_code_links: bool = True,
    dry_run: bool = False,
) -> list[SummaryResult]:
    db.init_db()
    papers = db.get_papers_for_summary(limit=limit, arxiv_ids=arxiv_ids)
    if not papers:
        print("没有待摘要论文（可能都已有成功摘要）。")
        return []

    if dry_run:
        for index, paper in enumerate(papers, 1):
            print("=" * 78)
            print(f"[{index}/{len(papers)}] {paper.arxiv_id}")
            print(_user_content(paper))
        print("\nDRY RUN：未调用 DeepSeek，未写 summaries 表。")
        return []

    client = DeepSeekClient()
    results: list[SummaryResult] = []
    for index, paper in enumerate(papers, 1):
        print(f"[{index}/{len(papers)}] 摘要 {paper.arxiv_id} ...", flush=True)
        try:
            result = summarize_one(
                paper, client, check_code_links=check_code_links
            )
        except Exception as exc:  # noqa: BLE001 - 单篇异常不能中断整批
            result = SummaryResult(
                arxiv_id=paper.arxiv_id,
                status="failed",
                summary=None,
                raw_response="",
                model=client.model,
                prompt_version=PROMPT_VERSION,
                attempts=0,
                error=f"未预期异常: {exc}",
                title_en=paper.title,
                abstract_en=paper.abstract,
            )
        db.save_summary(result)
        results.append(result)
        if result.status == "success":
            print(f"  成功（attempts={result.attempts}）", flush=True)
        else:
            print(f"  失败（attempts={result.attempts}）: {result.error}", flush=True)
    return results


def _print_result(result: SummaryResult) -> None:
    print("=" * 78)
    print(f"{result.arxiv_id}  status={result.status}  attempts={result.attempts}")
    print(f"英文标题: {result.title_en}")
    print(f"英文摘要: {result.abstract_en}")
    if result.status == "success":
        print("中文结构化摘要:")
        print(json.dumps(result.summary, ensure_ascii=False, indent=2))
    else:
        print(f"错误: {result.error}")
        if result.raw_response:
            print(f"原始响应片段: {result.raw_response[:800]}")


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
    parser = argparse.ArgumentParser(description="用 DeepSeek 为已入库论文生成结构化中文摘要")
    parser.add_argument(
        "--limit",
        type=int,
        default=5,
        help="本次最多处理多少篇，默认 5（用于小样本测试）",
    )
    parser.add_argument(
        "--ids",
        type=str,
        default="",
        help="可选：逗号分隔的 arXiv ID，例如 2610.33562,2610.06230",
    )
    parser.add_argument(
        "--no-code-check",
        action="store_true",
        help="不检查代码链接可达性（仅调试用）",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只打印将要发送给 DeepSeek 的论文输入，不调用 API、不写摘要",
    )
    args = parser.parse_args(argv)

    ids = [value.strip() for value in args.ids.split(",") if value.strip()]
    try:
        results = summarize_papers(
            limit=max(1, args.limit),
            arxiv_ids=ids or None,
            check_code_links=not args.no_code_check,
            dry_run=args.dry_run,
        )
    except SummaryConfigError as exc:
        print(f"配置错误: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001 - 命令行要给出可读错误
        print(f"摘要任务失败: {exc}", file=sys.stderr)
        return 2

    if args.dry_run:
        return 0

    for result in results:
        _print_result(result)

    success = sum(1 for result in results if result.status == "success")
    print("\n" + "=" * 78)
    print(
        f"完成：成功 {success}，失败 {len(results) - success}；"
        f"数据库成功摘要 {db.count_summaries('success')} 条，"
        f"失败 {db.count_summaries('failed')} 条。"
    )
    print(f"数据库: {config.DB_PATH}")
    return 0 if success == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
