"""应用配置：路径、默认值、arXiv 查询组和 API 参数。"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[2]
load_dotenv(PROJECT_ROOT / ".env")

APP_NAME = "PINN 论文追踪站"
DEFAULT_DAYS = int(os.getenv("PINN_RADAR_DEFAULT_DAYS", "30"))

DATA_DIR = PROJECT_ROOT / "data"
DB_PATH = DATA_DIR / "papers.db"

DEEPSEEK_BASE_URL = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
DEEPSEEK_MODEL = os.getenv("DEEPSEEK_MODEL", "deepseek-flash")
DEEPSEEK_TIMEOUT_SECONDS = 60
SUMMARY_PROMPT_VERSION = "summarize_v1"
SUMMARY_MAX_ATTEMPTS = 3
SUMMARY_MAX_TOKENS = 1600
SUMMARY_TEMPERATURE = 0.1
CODE_CHECK_TIMEOUT_SECONDS = 8

# arXiv API 约束：分页大小、请求间隔、超时和重试。
ARXIV_API_URL = "https://export.arxiv.org/api/query"
ARXIV_PAGE_SIZE = 200
ARXIV_REQUEST_DELAY_SECONDS = 3.0
ARXIV_TIMEOUT_SECONDS = 30
ARXIV_MAX_RETRIES = 3
LINK_CHECK_WORKERS = 2
LINK_CHECK_TIMEOUT_SECONDS = 10

# 主关键词：要求论文明确涉及 PINN / physics-informed neural network，
# 避免把只出现 "physics-informed" 的其他主题也算进来。
PINN_QUERY_TERMS = (
    '(abs:"physics-informed neural network" OR abs:"physics-informed neural networks" '
    'OR abs:"physics informed neural network" OR abs:"physics informed neural networks" '
    'OR abs:"physics-informed neural" OR abs:"physics informed neural" '
    'OR abs:PINN OR abs:PINNs '
    'OR ti:"physics-informed neural" OR ti:PINN OR ti:PINNs)'
)

# 每个主题单独查询，最后按 arXiv base ID 去重；组内用 OR，组间取交集。
TOPIC_QUERY_GROUPS: tuple[tuple[str, str], ...] = (
    (
        "dynamical systems",
        '(abs:"dynamical system" OR abs:"dynamical systems" OR abs:"dynamic system" '
        'OR abs:"dynamic systems" OR ti:"dynamical system" OR ti:"dynamical systems" '
        'OR ti:"dynamic system" OR ti:"dynamic systems")',
    ),
    (
        "system identification",
        '(abs:"system identification" OR abs:"parameter identification" '
        'OR abs:"parameter estimation" OR abs:"state estimation" '
        'OR abs:"model identification" OR ti:"system identification" '
        'OR ti:"parameter identification" OR ti:"parameter estimation")',
    ),
    (
        "engineering applications",
        '(abs:"digital twin" OR abs:"reduced-order model" OR abs:"reduced order model" '
        'OR abs:"surrogate model" OR ti:"digital twin" OR ti:"reduced-order model")',
    ),
)



def ensure_data_dir() -> Path:
    """确保 data/ 存在；不创建数据库，数据库由后续阶段初始化。"""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    return DATA_DIR
