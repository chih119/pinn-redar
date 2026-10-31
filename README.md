# PINN 文献雷达

一个本地运行的单用户科研文献管理工具，面向物理信息机器学习方向：

- 从 arXiv 检索 PINN / physics-informed neural network 相关论文；
- 按基础 arXiv ID 去重，保存标题、作者、英文摘要、提交时间、原文/PDF 链接；
- 用 DeepSeek 生成结构化中文摘要；
- 用 Streamlit 展示论文列表，支持关键词搜索、时间窗口、代码状态筛选和手动刷新。

## 功能状态

**已实现**

- arXiv 检索：7/30/90 天或 1–365 天窗口，PINN + 动态系统/系统辨识/工程应用主题组，分页、限流、重试、去重；
- 原文链接检查（2xx 记为可访问）；
- SQLite：论文、检索运行、摘要表；
- Streamlit 页面：统计指标、论文卡片、英文摘要、中文贡献、研究方法、代码状态、原文链接；
- 真实可用的搜索和筛选：标题、英文摘要、作者、中文摘要、时间窗口、代码状态；
- “刷新论文数据”真实调用 arXiv；
- DeepSeek 摘要命令行：结构化 Prompt、JSON schema、逐字证据校验、最多 3 次尝试、失败保留原始论文；
- 异常模拟脚本：arXiv 超时/空结果、DeepSeek 503/402/连接失败/非法 JSON/字段缺失、重复入库、无 key 页面、页面刷新。

**未完成或未验证**

- 没有真实 DeepSeek API key，因此**没有实际调用 DeepSeek 生成摘要**；页面当前没有中文摘要；
- 没有独立的 `codecheck.py` 和 `code_checks` 表；代码状态只有 confirmed/claimed/not_found，人工修正未实现；
- 没有启动时自动更新检查、并发运行锁、按内容哈希重新摘要；
- 研究说明中的 A1“30 天≥30 篇”目前未满足：严格查询 30 天约 10 篇，90 天约 33 篇；没有为了凑数放宽主题；
- 种子库未提交到仓库；首次运行是空库，需要点击刷新或运行命令抓取；
- 部分文件超过 AGENTS 的 300 行软限制，属已知技术债。

## 目录结构

```text
.
├── app.py                    # Streamlit 入口
├── src/pinn_radar/
│   ├── config.py             # 路径、关键词组、API 参数
│   ├── models.py             # 数据对象
│   ├── db.py                 # SQLite
│   ├── ingest.py             # arXiv 检索与入库
│   ├── summarize.py          # DeepSeek 摘要
│   └── queries.py            # 页面查询/搜索/筛选
├── prompts/summarize_v1.md   # 摘要 Prompt 与 JSON 示例
├── tests/                    # pytest 单元/查询测试
├── scripts/reliability_check.py
├── data/                     # 运行时 SQLite（默认被 gitignore）
├── .env.example
├── requirements.txt
├── requirements.lock.txt
├── pytest.ini
├── AGENTS.md
├── 研究说明.md
├── 技术设计文档.md
└── progress.md
```

## 安装依赖

建议 Python 3.10+，本项目在 Python 3.12 上验证。

```powershell
cd "你的项目目录"
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

如果 PowerShell 拒绝执行激活脚本，可以跳过激活，直接使用完整解释器路径：

```powershell
& ".\.venv\Scripts\python.exe" -m pip install -r requirements.txt
```

如果执行 `python --version` 打开 Microsoft Store，说明系统没有安装 Python；请从 <https://www.python.org/downloads/windows/> 安装 Python 3.12，并在安装时勾选“Add python.exe to PATH”。

如果下载慢，可临时加国内镜像：

```powershell
& ".\.venv\Scripts\python.exe" -m pip install -r requirements.txt `
  -i https://pypi.tuna.tsinghua.edu.cn/simple
```

## 配置 DeepSeek API key

摘要功能需要 key；不配置 key 也可以正常浏览、搜索、筛选和刷新论文，只是“生成摘要”按钮不可用。

```powershell
Copy-Item .env.example .env
notepad .env
```

在 `.env` 中填写：

```text
DEEPSEEK_API_KEY=你的真实 key
DEEPSEEK_BASE_URL=https://api.deepseek.com
DEEPSEEK_MODEL=deepseek-flash
```

注意：

- `.env` 已在 `.gitignore` 中，不要提交；
- 不要把 key 写进代码、README 或截图；
- 修改 `.env` 后重启 Streamlit 进程，才会重新加载。

## 运行网页

```powershell
& ".\.venv\Scripts\python.exe" -m streamlit run app.py
```

浏览器打开：

```text
http://127.0.0.1:8501
```

页面顶部显示论文总数、当前时间窗口论文数和有公开代码证据的论文数；下方卡片支持搜索和筛选。首次运行数据库为空，点击侧边栏“刷新论文数据”即可抓取。

## 命令行检索与摘要

命令行模块在 `src/` 下，需要先设置 `PYTHONPATH`（网页入口不需要，`app.py` 会自动处理）：

```powershell
$env:PYTHONPATH = "src"

# 抓取最近 30 天；也可以 7 或 90
& ".\.venv\Scripts\python.exe" -m pinn_radar.ingest --days 30

# 生成最近 5 篇待摘要论文（需要 key）
& ".\.venv\Scripts\python.exe" -m pinn_radar.summarize --limit 5
```

`pinn_radar.ingest` 的 `--preview 0` 可以关闭论文预览。

## 测试

```powershell
& ".\.venv\Scripts\python.exe" -m pytest -q
```

当前测试数：17。

模拟异常、不访问真实接口：

```powershell
& ".\.venv\Scripts\python.exe" scripts/reliability_check.py
```

当前结果：14 / 14 通过。脚本使用独立临时数据库，不修改生产 `data/papers.db`。

## 数据说明

- 默认数据库：`data/papers.db`，首次运行自动创建；它被 `.gitignore` 忽略，不随仓库上传。
- 当前本地已有 33 篇真实 arXiv 论文、0 条摘要；这是本地数据，不是仓库内容。
- 摘要失败会写入 `summaries.status='failed'`，原始论文不会删除或改写。
- 代码状态 `not_found` 只表示“没有查到公开代码证据”，不等于作者没有开源。

## 开发文档

- [研究说明.md](研究说明.md)：目标、范围、验收标准；
- [技术设计文档.md](技术设计文档.md)：模块边界、数据流、异常策略；
- [AGENTS.md](AGENTS.md)：数据诚信、密钥安全、变更控制；
- [progress.md](progress.md)：实际开发过程、测试结果和未完成项。
