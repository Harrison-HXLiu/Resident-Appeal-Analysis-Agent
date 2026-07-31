# 全国居民留言分析平台架构

## 1. 设计边界

目标规模约1200万条、5GB原始表格、约600个来源平台。平台使用人数少、并发低，但地图、已生成报告和常见统计需要快速响应。

因此采用单节点分析架构，而不是微服务或实时流平台：

```text
来源文件
  -> 单任务工作器
  -> PostgreSQL业务元数据
  -> Parquet季度事实快照
  -> DuckDB离线分析
  -> Tantivy/BM25案例检索
  -> 地图聚合 / 报告事实包 / QueryPlan工具
  -> 可配置模型API或未来A100
```

PostgreSQL不承担每次页面请求扫描1200万条正文的工作；地图和标准报告读取预计算结果。Parquet快照不可变，历史报告固定绑定快照和标签版本。

## 2. 数据域

### 业务元数据

- `regions`：省、市、区县及行政码、地级市归并、宏观区域、城市层级、城乡和坐标；
- `source_platforms`：约600个来源平台及归属；
- `import_batches`：源文件哈希、归档路径、表头映射、坏行和导入结果；
- `analysis_jobs`：导入、快照和报告任务；
- `users`、`user_sessions`、`audit_logs`：账号、角色和审计。

### 留言与研究标注

- `appeals`：兼容开发环境的行级数据，同时保留原始/规范类型、脱敏文本、季度和去重字段；
- `appeal_annotations`：主标签、二级标签、辅助标签、置信度和标签版本；
- `taxonomy_versions`、`taxonomy_labels`：标签定义、状态、黄金样本和F1发布门槛；
- `gold_samples`、`gold_annotations`：脱敏样本、两人独立结论、分歧状态和第三人仲裁记录；
- `reply_quality`：五项透明判断、证据、置信度和归一得分。

生产全量分析以Parquet为事实快照；数据库行表便于迁移期和研究抽样，后续可按数据规模将历史正文只保留在Parquet。

### 快照、报告和对话

- `quarter_snapshots`：季度、版本、标签版本、Parquet路径、搜索索引、manifest和激活状态；
- `city_quarter_aggregates`：地图所需基础、主题、类型及组合切片；
- `policy_documents`：上传文件或官方链接的不可变内容版本；
- `report_documents`、`report_revisions`：事实包、草稿、版本和发布状态；
- `chat_sessions`、`chat_messages`：仅当前会话使用，超时物理删除；
- `model_invocations`：供应商、模型、用途、版本、耗时、Token和错误，不保存原始提示词。

## 3. 状态与一致性

### 季度快照

```text
building -> active -> superseded
         -> failed
```

新版本在独立临时目录完成Parquet、聚合、检索索引和manifest后才原子移动到最终目录，并在数据库事务中激活。旧报告继续引用原快照。

快照激活后，单任务工作器为全国和所有有数据的地级市创建确定性标准报告草稿。该步骤不自动调用付费模型，避免一次季度冻结触发数百次外部请求；模型增强仍由显式报告任务触发。

### 标签

```text
trial/candidate -> published -> retired
```

两名不同标注人独立提交且在第二人提交前互不可见；一致样本自动定稿，不一致样本必须由非原标注人的第三人仲裁。黄金样本数和宏平均F1从定稿记录自动重算。发布操作校验黄金样本不少于1500、一级宏平均F1不低于0.85、二级不低于0.75。未通过时API拒绝发布。

### 报告

```text
draft -> published
```

草稿每次编辑创建`ReportRevision`。发布前重新校验去重事件量、原始留言量、回复率、代表案例和政策引用。已发布版本不可直接改写。

## 4. 数据处理

导入器通过字段别名将多来源表格映射到统一模型。每行依次执行：

1. 日期和正文有效性校验，标题缺失时由正文截取；
2. 原始类型保留和规范类型映射；
3. 手机、电话、证件、邮箱、银行卡、车牌、姓名字段和详细地址脱敏；
4. 规范化SHA-256完全去重；
5. 城市和月份范围内生成SimHash相似组键；
6. 17类一级和候选二级规则初标，保存辅助标签；
7. 回复质量五项透明分析；
8. 写入兼容检索chunk。

原始文件按SHA-256归档。相同文件、地区和来源平台重复导入返回原批次，不重复写入。

## 5. 查询与性能

地图只查询`city_quarter_aggregates`。每个快照生成以下切片：

- 城市季度总量；
- 城市季度×一级主题；
- 城市季度×规范类型；
- 城市季度×一级主题×规范类型。

城市气泡默认按去重事件量缩放，颜色为季度环比；上一季度为零时返回`null`，页面显示“不可比”。

临时分析由DuckDB扫描分区Parquet并缓存；标准报告在季度快照激活后预生成。应用不允许模型直接执行SQL。

## 6. 多轮对话

每轮问题先生成受Pydantic约束的`QueryPlan`：

```text
intent
city / compare_cities
quarter / start / end
topic_l1 / appeal_type
dimension / metric
needs_cases / needs_policies
```

工具层只开放聚合、比较、趋势、回复质量、案例、政策和已发布报告查询。模型接收的是脱敏问题、结构化事实和有限证据。

案例检索先通过城市、季度和主题过滤Tantivy/BM25结果，再由可配置重排器排序。首版无A100时可使用词法或API重排；A100到位后实现相同`RerankerProvider`接口，不改变页面或问答流程。

留言内容被视为不可信数据，提示词明确禁止执行证据中的指令。回答必须标注统计范围，并使用`[案例:来源ID]`或`[政策:ID]`引用。

## 7. 模型供应商

业务代码依赖：

- `ChatModelProvider`
- `EmbeddingProvider`
- `RerankerProvider`
- `BatchClassifierProvider`

当前实现提供OpenAI-compatible聊天供应商、受限JSON重排和本地词法降级。默认档位为CPU处理、API写作与有限候选重排；A100档位用于本地批量分类、Embedding、重排和兜底模型。

任何外部模型调用前再次运行敏感信息扫描；检测到未脱敏信息时调用被拒绝。模型调用日志不存提示词正文；配置模型单价后按输入/输出Token记录估算费用。

## 8. 安全与部署

生产设置`AUTH_REQUIRED=true`，角色为：

- `researcher`：查看、分析、编辑草稿；
- `reviewer`：增加报告发布权限；
- `admin`：增加用户、数据、地区映射、快照和标签管理权限。

应用使用HttpOnly、SameSite Strict Cookie、登录限速、来源校验、安全响应头和审计日志。Caddy终止HTTPS；外部云入口仍应配置VPN或IP白名单。

Docker Compose将`data/`只读挂载，PostgreSQL、快照、归档、上传和导出分别持久化。应用保持单worker，以保证后台写任务串行。

## 9. 迁移

旧SQLite数据库启动时执行兼容列迁移，新表由SQLAlchemy创建。旧13类或旧18类标签不会被视为已发布标签；未标注行回填到`v1-review-draft`。

生产PostgreSQL建议从空库开始，通过统一导入器重放原始文件。SQLite仅用于开发和苏州样本验证，不作为全国全量生产数据库。
