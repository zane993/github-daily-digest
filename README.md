# GitHub 开源资源日报生成器

一个使用 Python 标准库实现的本地日报工具：从 GitHub 及公开包平台收集开源项目动态，生成中文 Markdown 和 HTML 报告。程序不会调用大模型，也不需要安装第三方 Python 依赖。

## 功能

- 汇总 GitHub 仓库、提交、Release、许可证及公开下载指标。
- 查询 npm、PyPI 和 crates.io 上配置过对应关系的包下载统计。
- 关注本周新建仓库和公开 Trending 信息；无法访问的数据源会记录限制，不以猜测补齐。
- 区分 Stars、Forks、提交数、Release 附件下载和包下载量，注明来源及查询日期。
- 同时生成 Markdown 与自包含 HTML 报告。
- 可通过命令行单次运行，或在 Windows 上使用任务计划程序定时运行。

## 环境要求

- Python 3.9 或更高版本（仅使用标准库）。
- 网络连接。未设置 GitHub Token 时仍可尝试采集，但 GitHub API 限额较低，搜索接口也可能受共享出口限流。

## 快速开始

在项目目录运行：

```powershell
python run_daily.py
```

默认按北京时间生成运行当日的日报，输出到 `reports/`：

```text
reports/GitHub开源资源日报-YYYY-MM-DD.md
reports/GitHub开源资源日报-YYYY-MM-DD.html
```

常用选项：

```powershell
# 指定报告日期
python run_daily.py --date 2026-11-05

# 临时指定输出目录
python run_daily.py --output-dir "D:\reports"

# 限制收录数
python run_daily.py --max-items 8

# 只采集并打印摘要，不生成报告
python run_daily.py --dry-run

# 输出调试日志
python run_daily.py --verbose
```

运行日志写入 `logs/`。日志和生成报告默认不纳入版本控制。

## GitHub API Token（可选）

公开 API 在未认证时有较低的请求额度。可在当前 PowerShell 会话中设置 `GITHUB_TOKEN` 后运行；程序只从环境变量读取 Token，不会将其写入报告：

```powershell
$env:GITHUB_TOKEN = "<你的 GitHub Token>"
python run_daily.py
```

不要把真实 Token 写进 `config.json`、源码、日志或提交记录。Token 权限应按 GitHub 当前说明设置为满足读取公开仓库数据的最小权限。

## 配置

编辑 `config.json` 调整输出目录、时区、收录数、采集窗口、下载链接验证及重点跟踪仓库。`output_dir` 和 `log_dir` 默认是相对于项目目录的 `reports` 与 `logs`。包名只会使用配置中明确登记的对应关系，不根据仓库名推断。

## Windows 定时运行

可以使用当前 Windows 用户注册每日任务（通常无需提升权限），在项目目录执行：

```powershell
powershell -ExecutionPolicy Bypass -File .\install_task.ps1
```

脚本默认每日 08:00 运行。查看、手动触发和移除任务的命令见 `install_task.ps1` 注释。只保留一个定时器：若其他自动化已经生成同一份日报，先停用其中一个，避免重复运行并消耗 GitHub API 额度。

## 指标与限制

- Stars、Forks 和 Watchers 是查询时的累计数，不代表本周增量或下载量。
- GitHub Release 的下载量是附件累计下载次数；可能包含多个平台文件和元数据，不等同于本周下载或独立用户数。
- 包平台的下载次数是包获取量，可能包含 CI、镜像和重复安装，不等同于用户数。
- 第三方 Trending RSS 只作为趋势线索，不等同于 GitHub 官方 Stars 增量。
- 资源及其模型权重、数据集、第三方服务可能各自采用不同许可证或使用限制；应以具体项目页面和许可文本为准。
- 网络不可用、API 限流、页面结构变化或来源缺少数据时，报告会披露限制；没有可靠数据时不推测下载量。

## 许可证

当前仓库没有声明开源许可证。公开可见不代表获准复制、修改、商用或再分发；如需采用或分发本项目，请先取得权利人许可。
