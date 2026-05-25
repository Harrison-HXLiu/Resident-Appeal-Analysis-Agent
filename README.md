# 居民留言分析 Agent Demo

这是一个面向政府平台居民留言的分析 demo。当前数据源为 `data/苏州.xlsx`，应用首次启动时会自动导入该文件，并为每条留言生成可解释的初始主题标签。

## 已实现功能

- 数据概览：留言量、回复率、平均回复耗时、趋势、热点主题和部门排行。
- 智能问答：以数据库统计为依据回答问题；配置 DeepSeek 后生成自然语言研判回答。
- 报告生成：按地区和时间生成 Markdown 分析报告并下载。
- 数据管理：上传新增城市 Excel，记录导入批次，按批运行 DeepSeek 主题复核。
- 隐私处理：手机号、电话、身份证号、邮箱和显式地址在送往模型前脱敏。

当前的主题排行在 AI 复核前使用关键词规则初标，适合演示统计链路，不应直接作为正式治理结论。

## 技术结构

```text
Browser
  -> FastAPI + Jinja2 + Plotly
      -> SQLite + SQLAlchemy
      -> Excel import / redaction / rule annotation
      -> DeepSeek API (Q&A, report writing, topic refinement)
```

数据库模型已包含地区层级和导入批次，因此加入其他城市数据后可沿用相同页面与 API。规模扩大后，可将 `DATABASE_URL` 更换为 PostgreSQL，并将 AI 标注任务迁移到异步任务队列。

## 启动方式

要求 Python 3.11 或更高版本。

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
Copy-Item .env.example .env
python -m uvicorn app.main:app --host 0.0.0.0 --port 8000
```

本机访问 `http://127.0.0.1:8000`。同一局域网内其他电脑可通过 `http://<运行本应用的电脑IP>:8000` 访问；需要确保 Windows 防火墙允许该端口的入站访问。

## 分享到公网

可以让外地用户通过公网访问，但当前 demo 尚无登录鉴权，且本地数据库包含留言原文，不能直接作为公开生产服务发布。

用于短期演示时，可使用 Cloudflare Quick Tunnel 将本机服务临时映射到一个 HTTPS 公网地址：

```powershell
.\.venv\Scripts\python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
cloudflared tunnel --url http://localhost:8000
```

第二条命令会打印一个随机的 `https://*.trycloudflare.com` 地址，可发给异地访问者。Cloudflare 官方将 Quick Tunnel 定位为测试与开发用途，不承诺生产可用性。

准备持续对外开放时，建议先完成以下工作：

- 增加登录鉴权与管理员权限，将“数据导入”和“AI 标注”限制为管理员访问。
- 对公开页面只展示汇总指标和脱敏内容，避免原始留言泄露。
- 部署到云服务器，使用正式域名与 HTTPS，并将数据库迁移到 PostgreSQL。
- 增加调用额度限制、操作审计、备份和异常监控。

## 配置 DeepSeek

编辑 `.env`：

```dotenv
DEEPSEEK_API_KEY=
DEEPSEEK_BASE_URL=https://api.deepseek.com
DEEPSEEK_MODEL=deepseek-v4-flash
```

API key 只在后端读取，不会下发给浏览器。未配置 API key 时，仪表盘、导入、规则主题分析、本地问答摘要和模板报告仍可使用。

截至 2026-05-25，DeepSeek 官方 API 使用 OpenAI 兼容调用方式，默认模型在本项目中配置为 `deepseek-v4-flash`；模型名称保存在环境变量中，便于后续随官方版本变化调整。

## 数据格式

上传的 `.xlsx` 文件必须包含以下列：

```text
来件时间 | 回复时间 | 来件类型 | 来件标题 | 来件内容 | 回复部门 | 回复内容
```

建议附带 `信件编号` 列，系统会按“地区 + 信件编号”识别唯一留言。如果缺少该列，系统将根据记录内容生成稳定编号。

目前已有文件实测为 10,895 条苏州留言，另含 `信件编号` 列；回复时间和回复内容各有 21 条缺失记录，系统会按待完整回复处理。

## 常用操作

手动导入文件：

```powershell
python -m app.cli import .\data\苏州.xlsx --province 江苏省 --city 苏州市
```

运行测试：

```powershell
pytest
```

## 生产化注意事项

- 当前网页无登录鉴权，仅适合内网 demo，不应直接暴露到公网。
- 原始留言保存在本地数据库，生产环境需增加访问权限、审计日志、备份和加密策略。
- 全国规模应使用 PostgreSQL、后台任务队列和更严格的数据分级/脱敏审查。

## DeepSeek 官方资料

- <https://api-docs.deepseek.com/>
- <https://api-docs.deepseek.com/quick_start/pricing>
- <https://api-docs.deepseek.com/guides/json_mode>
- <https://api-docs.deepseek.com/guides/tool_calls>

## 部署参考

- <https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/do-more-with-tunnels/trycloudflare/>
- <https://fastapi.tiangolo.com/deployment/https/>
