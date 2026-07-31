# 全国居民留言分析平台

面向社会科学研究人员的全国地级市居民留言研究平台。核心能力是：

- 全国城市气泡地图：去重事件量、季度增速、热点问题和回复指标；
- 全国季度报告与城市季度简报：事实包锁定、网页编辑、审核发布、Word/PDF导出；
- 临时多轮分析对话：结构化查询计划、确定性统计、案例/政策证据和流式回答；
- 数据治理：多来源表头映射、脱敏、类型归一、去重、标签版本、回复质量和季度快照。

当前代码保留 SQLite 作为本地开发兼容层；生产部署使用 PostgreSQL，千万级事实数据按季度写入 Parquet，并由 DuckDB/Tantivy承担离线分析与全文检索。

## 快速启动

建议使用 Python 3.11 或 3.12：

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
uvicorn app.main:app --reload
```

默认访问 `http://127.0.0.1:8000`。若数据库为空且 `AUTO_IMPORT_SAMPLE=true`，系统会导入 `data/` 根目录下的第一个 Excel 样本；不会递归导入全国数据目录。

没有模型 API 时，地图、统计、报告模板和问答工具仍可运行。配置任意 OpenAI-compatible API：

```dotenv
MODEL_PROVIDER=openai-compatible
MODEL_API_KEY=...
MODEL_BASE_URL=https://api.example.com/v1
CHAT_MODEL=your-model
```

旧的 `DEEPSEEK_*` 环境变量仍兼容，但新代码不再将供应商和模型名写死。

## 全国数据接入

先盘点表头、估算行数并发现不完整下载：

```bash
python -m app.cli inventory "data/地方政府留言板块爬虫数据汇总"
```

结果默认写入 `instance/source-inventory.json`，包含文件、表头映射、缺失必需字段、城市目录提示和估算行数。

导入单个来源：

```bash
python -m app.cli import path/to/city.xlsx \
  --province 江苏省 \
  --city 苏州市 \
  --platform-code suzhou-mayor-mailbox \
  --platform-name 苏州市长信箱 \
  --city-code 320500
```

支持 `.xlsx`、`.xls`、`.csv` 和 `.parquet`。至少需要映射出时间和正文，标题缺失时使用正文摘要；常见的“留言时间/来信时间”“内容描述/事项内容”“答复意见/办理结果”等别名已内置。

导入完成并确认城市、区县映射后冻结季度：

```bash
python -m app.cli snapshot 2025-Q4
```

快照任务会：

1. 写入按省份分区、Zstandard压缩的 Parquet；
2. 在地级市和月份范围内执行 SimHash/LSH 相似留言归组；
3. 生成地级市季度聚合及主题/类型筛选切片；
4. 构建 Jieba预分词的 Tantivy/BM25索引；
5. 使用 DuckDB 复核原始量与去重事件量；
6. 写入 manifest 后原子激活新版本；
7. 为全国和有数据的地级市预生成确定性标准报告草稿；
8. 保留旧版本，历史报告不会随新数据变化。

预生成阶段不会调用外部模型，避免一次季度冻结自动产生数百次费用；研究人员需要更完整文字时可再创建模型撰写任务。可用 `PREGENERATE_STANDARD_REPORTS=false` 关闭自动预生成。

## 标签与研究口径

内置标签只是评审初稿：17个业务一级类加“其他/综合”。每条留言保存一个主标签和多个辅助标签，报表按主标签唯一计数。

标签正式发布必须同时满足：

- 双人标注并仲裁的黄金样本不少于1500条；
- 一级标签宏平均F1不低于0.85；
- 二级标签宏平均F1不低于0.75；
- 18个一级标签均有研究团队确认的定义和状态；
- 每个候选二级标签均已批准或拒绝。

门槛未通过前，页面持续显示“标签试运行”。关键词规则仅提供可解释初标，不能作为研究准确率证明。

数据管理页提供黄金样本队列。每条样本必须由两个不同账号独立提交；第二人提交前看不到第一人的标签。两人一致时自动定稿，不一致时进入第三人仲裁，仲裁人不得是原标注人。黄金样本数和宏平均F1从定稿记录自动重算，不能在页面手工填写。

统计主口径为去重事件量；原始留言量、完全重复和相似组键仍保留。城乡属性只接受行政区映射，无法可靠定位时记为“未知”。

## 对话与报告可信度

对话模型不能生成或执行 SQL。系统先构造受限的 `QueryPlan`，再调用聚合、比较、趋势、回复质量、案例、政策和报告查询工具。

检索流程为：

```text
城市/季度/主题过滤
  -> Tantivy BM25 召回
  -> 可配置API重排（失败时词法降级）或未来A100重排
  -> 5–10条脱敏证据
  -> 模型组织回答
```

会话只在当前浏览器会话中使用，默认30分钟无活动后删除，不提供长期历史列表。

报告先生成 `ReportFactPack`，其中锁定：

- 快照、标签版本和统计口径；
- 数字、图表数据和季度比较；
- 脱敏案例及来源编号；
- 已上传政策材料及出处。

模型只能根据事实包写正文。校验失败时回退到确定性模板；草稿必须人工审核发布后才能导出 Word/PDF。
报告不会自动引用政策库中的全部材料；只有创建报告时明确勾选的政策版本才进入事实包和出处校验。

模型调用记录供应商、模型、提示词版本、耗时和 Token。配置
`MODEL_INPUT_COST_PER_MILLION`、`MODEL_OUTPUT_COST_PER_MILLION` 后还会记录估算费用；日志不保存提示词正文。

## 生产部署

复制环境变量并设置强密码：

```bash
export POSTGRES_PASSWORD='...'
export BOOTSTRAP_ADMIN_USERNAME='...'
export BOOTSTRAP_ADMIN_PASSWORD='...'
export APP_DOMAIN='research.example.org'
docker compose up --build -d
```

生产配置默认：

- PostgreSQL持久化业务数据；
- 单个应用进程和单任务工作器，避免快照并发写入；
- Caddy自动HTTPS；
- 账号角色、HttpOnly/SameSite Cookie、登录限速和审计日志；
- `data/` 以只读卷挂载；
- 原始文件、快照和导出物写入独立持久卷。

外部云部署应继续通过VPN或IP白名单限制入口，并将 `instance/archive`、`instance/snapshots`、PostgreSQL和导出物备份到对象存储。

## 主要 API

| API | 用途 |
|---|---|
| `POST /api/import-batches` | 创建服务器端文件导入任务 |
| `POST /api/snapshots` | 创建季度快照任务 |
| `GET /api/jobs/{id}` | 查询任务进度和结果 |
| `GET /api/map` | 获取地图预聚合数据 |
| `POST /api/reports` | 创建全国/城市报告任务 |
| `PUT /api/reports/{id}` | 保存报告新版本 |
| `POST /api/reports/{id}/publish` | 审核发布 |
| `GET /api/reports/{id}/export` | 导出已发布Word/PDF |
| `POST /api/chat/sessions` | 创建临时多轮会话 |
| `POST /api/chat/sessions/{id}/messages/stream` | SSE流式问答 |
| `DELETE /api/chat/sessions/{id}` | 清除当前会话 |
| `POST /api/policies/upload` | 上传并版本化政策材料 |
| `GET /api/taxonomy` | 查看标签版本和发布门槛 |
| `PUT /api/taxonomy/labels/{id}` | 审核标签名称、定义、边界和状态 |
| `POST /api/taxonomy/{id}/publish` | 校验黄金样本、F1和标签审核后冻结版本 |
| `POST/GET /api/taxonomy/{id}/gold-samples` | 建立和读取脱敏黄金样本队列 |
| `POST /api/taxonomy/{id}/gold-samples/{sample}/annotations` | 提交双人独立标注 |
| `POST /api/taxonomy/{id}/gold-samples/{sample}/arbitrate` | 第三人仲裁分歧 |

完整接口文档在运行后的 `/docs`。

## 验证

```bash
pytest -q
```

自动化测试覆盖导入幂等、表头映射、脱敏、主辅标签、完全/相似去重、快照原子切换、DuckDB校验、地图聚合、回复质量、查询计划继承、会话清理、权限密码、报告事实锁定、Word/PDF导出和政策链接内网防护。

1200万条全量性能验收必须在目标云服务器上执行。目标为地图常用筛选P95不超过1.5秒、已生成报告P95不超过2秒、对话首字P95不超过5秒、常见问答完成P95不超过30秒。代码结构已按预聚合和不可变快照设计，但不能用苏州样本代替全量压测结论。

## 当前仓库数据盘点

截至 2026-07-30，本地 `data/地方政府留言板块爬虫数据汇总` 实际盘点到：

- 405个 XLSX 文件；
- 约1,976,196行、1,040,539,216字节（约992 MiB）；
- 187种表头结构；
- 8个文件缺失时间或正文等最低必需字段；
- 未发现仍带 `.downloading` 标记的文件。

详细清单保存在本地忽略目录 `instance/source-inventory.json`。这批数据明显少于规划口径的约1200万条、5GB、600个来源，因此当前只能证明架构和小规模集成链路可运行，不能视为全国全量性能验收。需要确认其余数据是否尚未提供，或原规划数字是否需要修订。

## 项目仍需要的研究输入

- 600个来源平台到省、地级市、区县行政码的确认表；
- 两名标注人和一名仲裁人完成黄金样本；
- 17个一级类的最终定义及二级标签审核结果；
- 城乡行政映射覆盖率和可接受阈值；
- 回复质量五项指标的最终权重；
- 正式报告模板、Logo、字体和审核发布人；
- 首批权威政策材料；
- 模型API、预算和数据使用条款；
- A100规格、到位时间，以及混合模式或全本地模式的最终选择。
- 本地405个文件是否就是当前完整数据，还是仍有其余来源待补充；
- 当前8个结构不可用文件的补采或舍弃决定。
