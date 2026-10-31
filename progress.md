# 项目进度

> 只记录实际执行过的内容；未执行或未验证的内容放在“未完成/未验证”，不写成已完成。  
> 最后更新：2026-10-31 16:20（Asia/Shanghai）

## 当前状态

- 项目目录：`C:\Users\24343\Documents\deepseek-harness\default-workspace`
- 代码结构：`app.py` + `src/pinn_radar/` + `tests/` + `scripts/`
- 本地数据库：33 篇真实 arXiv 论文，0 条摘要。
- DeepSeek key：未配置；真实摘要 API 尚未调用。
- 自动化测试：`17 passed`。
- 可靠性模拟：`14 / 14` 通过。
- Streamlit 页面：可启动，健康检查 200。
- GitHub：尚未创建/上传；当前环境没有 git 命令。

## 开发阶段记录

### 阶段 0：环境与骨架

实际完成：

- 创建 `.venv`（Python 3.12.14）和基础目录；
- 安装 Streamlit、python-dotenv、requests、feedparser、openai、httpx、pydantic、pytest；
- 创建最小 Streamlit 页面、配置、`.env.example`、`.gitignore`、`README`；
- 处理系统 `python` 是 Microsoft Store 占位符的问题，改用 DSH 自带 Python；
- 验证页面健康检查 200、AppTest 无异常。

### 阶段 1：arXiv 检索与 SQLite

实际完成：

- 创建 `src/pinn_radar/models.py`、`db.py`、`ingest.py`；
- 支持 7/30/90 天和 1–365 天窗口；
- PINN 主词 + 动态系统/系统辨识/工程应用三个主题组；
- 分页、3 秒 API 间隔、重试、base ID 去重；
- 标题、作者、英文摘要、提交时间、原文/PDF 链接入库；
- 原文链接 2xx 检查；
- 实际运行：7 天 2 篇、30 天 10 篇、90 天 34 条去重为 33 篇；
- 重复运行不重复入库；
- 33 条原文链接逐条检查均返回 2xx。

### 阶段 2：DeepSeek 摘要代码

实际完成：

- 创建 `src/pinn_radar/summarize.py` 和 `prompts/summarize_v1.md`；
- 结构化 JSON：核心贡献、methods、datasets、code；
- 逐字 evidence 校验、Pydantic schema、最多 3 次尝试；
- 失败写 `summaries.failed`，论文不删除；
- 新增 `summaries` 表、代码状态字段；
- 对 5 篇真实论文做了 dry-run；
- 缺少 key 时命令返回配置错误；
- 测试覆盖非法 JSON、字段缺失、失败重试等。

**未完成**：没有真实 DeepSeek API key，因此没有实际调用 DeepSeek，也没有真实中文摘要。

### 阶段 3：Streamlit 文献页面

实际完成：

- `src/pinn_radar/queries.py` 承担统计、搜索、筛选和摘要 JSON 解析；
- 页面顶部统计：论文总数、当前窗口论文数、有公开代码证据数；
- 卡片：英文标题、UTC+8 时间、作者、中文贡献、研究方法、代码状态、原文链接；
- 搜索真实查询标题、英文摘要、作者、中文摘要；
- 时间窗口 1–365 天、代码状态筛选；
- 手动刷新真实调用 arXiv；
- 无 key 时页面可浏览已有论文，摘要按钮禁用；
- 用临时 fixture 验证过摘要卡片和 `confirmed` 筛选；
- 生产库仍未写入任何摘要。

### 阶段 4：可靠性检查

实际完成：

- 新增 `scripts/reliability_check.py`，使用独立临时数据库和 mock，不访问真实接口；
- 覆盖：arXiv 超时/连接失败/客户端重试/空结果/异常 totalResults；DeepSeek 503/连接失败/402/非法 JSON/字段缺失；重复入库、成功摘要重复选中、无 key 页面、页面反复刷新、刷新失败；
- 结果：**14 / 14 通过**；
- 修复：arXiv `totalResults>0` 但无条目时不再静默成功，改为抛 `ArxivError`；
- 修复：网页刷新 0 篇时显示 warning，不再误报“更新完成”。

### 阶段 5：最终整理

实际完成：

- 将 Python 包从根目录移动到 `src/pinn_radar/`；
- 修改 `app.py`，运行时自动把 `src/` 加入 `sys.path`；
- 修改 `scripts/reliability_check.py`、`pytest.ini` 适配 `src` 布局；
- 更新 `README.md`、`研究说明.md`、`技术设计文档.md` 中的路径；
- 重写 `progress.md`（本文件）；
- 运行最终检查：`17 passed`、可靠性 `14/14`、页面指标 33/10/0；
- 扫描项目源码、文档、配置和数据库：未发现 `sk-...` 形式的 API key，`.env` 不存在；
- `pip check`：无损坏依赖；
- 未执行 GitHub 上传（没有 git 命令）。
- [x] 用一份“仅上传内容”的干净副本（不含 `.venv`、`.env`、`data/papers.db`、无关目录）做下载后模拟：
  - `pytest -q`：17 passed；
  - `scripts/reliability_check.py`：14/14 通过；
  - 空数据库页面：0 exceptions，指标为 0/0/0；
  - 测试后删除副本。
- [x] 生成上传压缩包 `pinn-radar-github.zip`：
  - 顶层目录为 `pinn-radar/`，共 34 个条目；
  - 不包含 `.venv`、`.env`、`data/papers.db`、`.tmp`、`ngsim_analysis`、无关文献笔记、`__pycache__`；
  - 解压后重新执行 `pytest -q`：17 passed；AppTest：0 exceptions。

**打包说明**：没有采用 editable install。曾尝试 `pip install -e .`，但 DSH 沙箱的临时目录权限导致构建失败；最终不依赖安装包，网页由 `app.py` 自动加入 `src`，命令行通过 `$env:PYTHONPATH="src"` 运行。这不是最终用户环境的限制，但 README 以更稳妥的 `PYTHONPATH` 方式为准。

## 对照《研究说明》的实现状态

| 要求 | 状态 |
|---|---|
| 自动检索 arXiv、时间窗口可调 | 已实现并实测 |
| 保存标题、作者、时间、摘要、原文链接 | 已实现并实测 |
| 去重、重复运行不重复入库 | 已实现并测试 |
| 原文链接可打开 | 已实现；当前 33/33 检查 2xx |
| DeepSeek 结构化中文摘要 | 代码已实现，**未真实调用验证** |
| 核心贡献/方法/数据集/代码状态 | 代码已实现；无真实摘要样本 |
| 代码开源状态 | 部分实现：confirmed/claimed/not_found；无独立 code_checks/manual |
| 网页列表、搜索、时间/代码筛选 | 已实现并 AppTest 验证 |
| 网页手动刷新 | 已实现并 AppTest/模拟验证 |
| 无 key 时正常显示论文 | 已实现并测试 |
| 种子库 ≥50 篇 | **未完成**；本地 33 篇，且未提交数据库 |
| A1 固定窗口 ≥30 篇 | **未满足**：30 天约 10 篇，90 天约 33 篇 |
| A2 200 篇/10 分钟 | **未做正式性能测试** |
| A3 10 篇摘要质量 | **未验证**（无 key、无真实摘要） |
| A4 代码状态准确率 | **未验证**（无真实摘要） |
| A5 分类筛选 | 未实现分类筛选；有搜索、时间、代码状态筛选 |
| A6 新机器可复现 | README/依赖已整理；未做全新机器复现；无 ≥50 篇种子库 |
| A7 成本 <$1 | **未做真实 API 成本测试** |

## 对照《技术设计文档》的差距

已实现：四层模块、SQLite、arXiv 限流重试、摘要 JSON/证据校验、页面查询分离、错误提示、无 key 降级、基本测试。

未实现或与设计有差距：

- 独立 `codecheck.py`、`code_checks` 表、四态中的 `manual`；
- 启动时更新检查；
- 运行锁/并发保护；
- 按 `source_hash` 判断摘要过期；
- 200 篇规模和 4–6 并发摘要未实测；
- `runs` 没有汇总 token/费用字段；
- 部分文件超过 300 行软限制（`db.py`、`ingest.py`）。

## 测试命令

```powershell
cd "C:\Users\24343\Documents\deepseek-harness\default-workspace"
& ".\.venv\Scripts\python.exe" -m pytest -q
& ".\.venv\Scripts\python.exe" scripts/reliability_check.py
```

## 下一步

1. 配置 `DEEPSEEK_API_KEY`，真实处理 5 篇，验证摘要质量和代码状态；
2. 根据真实结果决定是否调整摘要字段/校验；
3. 补齐代码线索模块和人工修正；
4. 视需要扩大检索主题、补种子库或调整验收口径。
