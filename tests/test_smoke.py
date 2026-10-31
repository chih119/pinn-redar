"""阶段 0 的环境冒烟测试：不访问网络，不读取真实 API key。"""

from __future__ import annotations

import pinn_radar
from pinn_radar import config


def test_package_has_version() -> None:
    assert pinn_radar.__version__ == "0.1.0"


def test_project_paths_are_absolute() -> None:
    assert config.PROJECT_ROOT.is_absolute()
    assert config.DATA_DIR.is_absolute()
    assert config.DB_PATH.name == "papers.db"


def test_defaults_are_sane() -> None:
    assert config.DEFAULT_DAYS > 0
    assert config.DEEPSEEK_BASE_URL.startswith("https://")
