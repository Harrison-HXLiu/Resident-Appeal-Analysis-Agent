# 居民留言分析 Agent 工程原理说明

本文档说明本项目的软件工程结构、数据处理链路、RAG 检索增强、Embedding 语义检索、DeepSeek 生成逻辑，以及未来扩展到多城市/全国时的演进方式。

## 1. 项目目标

本项目面向政府平台居民留言数据，提供一个可运行的分析 Agent Demo。当前数据源为苏州留言 Excel，字段包括：

```text
来件时间 | 回复时间 | 来件类型 | 来件标题 | 来件内容 | 回复部门 | 回复内容 | 信件编号
```

系统支持：

- 数据导入与清洗
- 敏感信息脱敏
- 规则主题初标
- 统计仪表盘
- 智能问答
- RAG 检索增强
- DashScope Embedding 语义召回
- DeepSeek 生成回答和报告
- 报告生成与下载

核心原则是：**统计事实由数据库计算，具体案例由 RAG 检索，表达与归纳由大模型完成**。这样可以减少模型幻觉，并方便未来扩展到多城市数据。

## 2. 技术栈

| 层级 | 技术 |
|---|---|
| Web 后端 | FastAPI |
| 页面模板 | Jinja2 |
| 前端样式 | 原生 HTML/CSS/JS |
| 数据库 | SQLite |
| ORM | SQLAlchemy |
| Excel 读取 | pandas + openpyxl |
| 关键词检索 | SQLite FTS5 |
| 语义检索 | DashScope `text-embedding-v3` |
| LLM 生成 | DeepSeek OpenAI-compatible API |
| Markdown 渲染 | markdown + bleach |
| 测试 | pytest |

当前是单机 Demo 架构。后续生产化可迁移到 PostgreSQL、对象存储、后台任务队列和正式鉴权系统。

## 3. 文件结构

```text
.
├── app/
│   ├── main.py                  # FastAPI 入口、页面路由、数据导入、报告、问答接口
│   ├── cli.py                   # 命令行工具：导入数据、重建 RAG、构建 embedding
│   ├── config.py                # 环境变量配置
│   ├── db.py                    # SQLAlchemy engine/session，数据库初始化与轻量迁移
│   ├── models.py                # 数据库表模型
│   ├── services/
│   │   ├── analytics.py         # 统计聚合、仪表盘数据
│   │   ├── agent.py             # 智能问答主流程
│   │   ├── ai_annotation.py     # DeepSeek 批量主题复核
│   │   ├── classification.py    # 规则主题分类
│   │   ├── deepseek.py          # DeepSeek API 封装
│   │   ├── embeddings.py        # DashScope embedding 构建与语义检索
│   │   ├── importer.py          # Excel 导入、清洗、标签与 chunk 同步
│   │   ├── markdown.py          # Markdown 安全渲染
│   │   ├── privacy.py           # 脱敏规则
│   │   ├── rag.py               # RAG chunk、FTS5、混合检索、证据构建
│   │   └── reports.py           # 报告生成
│   ├── templates/
│   │   ├── base.html            # 全局页面布局
│   │   ├── dashboard.html       # 数据概览
│   │   ├── ask.html             # 智能问答
│   │   ├── reports.html         # 报告生成
│   │   └── data.html            # 数据管理
│   └── static/
│       ├── styles.css           # 页面样式
│       ├── favicon.svg          # 站点图标
│       └── logo.png             # 机构 logo
├── data/
│   └── 苏州.xlsx                # 原始 Excel 数据
├── instance/
│   └── appeals.db               # SQLite 数据库，本地运行生成
├── tests/
│   └── test_services.py         # 服务层测试
├── uploads/                     # 上传文件暂存
├── .env                         # 本地密钥和运行配置，不应提交
├── .env.example                 # 配置模板
├── requirements.txt             # Python 依赖
├── README.md                    # 使用说明
└── ARCHITECTURE.md              # 本文档
```

## 4. 数据库模型

### 4.1 基础业务表

| 表 | 作用 |
|---|---|
| `regions` | 省、市、区县维度 |
| `import_batches` | 每次 Excel 导入记录 |
| `appeals` | 原始留言主表 |
| `appeal_annotations` | 主题、关键词、摘要、紧急程度等分析标签 |
| `reports` | 生成的分析报告 |
| `chat_sessions` | 问答会话 |
| `chat_messages` | 问答消息 |
| `analysis_jobs` | AI 复核任务记录 |

### 4.2 RAG 相关表

| 表 | 作用 |
|---|---|
| `appeal_chunks` | 每条留言对应的检索文本 chunk |
| `appeal_chunks_fts` | SQLite FTS5 虚拟表，用于关键词检索 |
| `retrieval_logs` | 每次问答的检索记录 |
| `rag_answer_sources` | 某次回答引用了哪些留言来源 |
| `appeal_embeddings` | chunk 的 embedding 向量 |

### 4.3 留言主表关键字段

`appeals` 同时保存原文和脱敏文本：

```text
title / content / reply_content
redacted_title / redacted_content / redacted_reply
```

系统发送给外部模型和 embedding 服务时，优先使用脱敏文本。

## 5. 数据导入流程

入口：

```text
app/services/importer.py
```

流程：

```text
Excel
  -> pandas 读取
  -> 字段校验
  -> 省市地区写入 regions
  -> 创建 import_batches
  -> 按 “地区 + 信件编号” 去重
  -> 写入 appeals
  -> 脱敏生成 redacted_* 字段
  -> 规则主题初标
  -> 同步 appeal_chunks
  -> 重建或补齐 RAG 索引
```

如果没有 `信件编号`，系统会根据主要字段生成稳定哈希编号。当前苏州数据含 `信件编号`，且无重复。

## 6. 脱敏逻辑

入口：

```text
app/services/privacy.py
```

当前会脱敏：

- 手机号
- 固定电话
- 身份证号
- 邮箱
- 显式地址字段

脱敏后的文本用于：

- RAG chunk
- Embedding
- DeepSeek 问答
- DeepSeek 报告生成

原始文本仍保存在本地数据库中，方便内部追溯，但不直接发送给外部模型。

## 7. 主题分类逻辑

入口：

```text
app/services/classification.py
```

第一版使用关键词规则初标，例如：

| 主题 | 典型关键词 |
|---|---|
| 住房建设 | 物业、小区、交房、违建、公积金 |
| 交通出行 | 公交、地铁、停车、道路、拥堵 |
| 城市管理 | 城管、噪音、垃圾、施工、占道 |
| 教育服务 | 学校、入学、学区、幼儿园 |
| 医疗卫生 | 医院、医保、药品、诊所 |

规则标签用于统计和初始筛选。后续可通过 `ai_annotation.py` 调用 DeepSeek 复核，覆盖为更细的结构化标签。

## 8. 统计分析架构

入口：

```text
app/services/analytics.py
```

统计由数据库聚合完成，而不是交给大模型计算。包括：

- 总留言量
- 已回复量
- 回复率
- 平均回复耗时
- 月度趋势
- 来件类型排行
- 主题排行
- 回复部门排行

为了加速页面访问，仪表盘统计有短时缓存。导入数据或运行 AI 标注后，会清理缓存。

## 9. 智能问答总流程

入口：

```text
app/services/agent.py
```

整体流程：

```text
用户问题
  -> 自动识别时间范围
  -> 数据库统计摘要
  -> RAG 检索相关留言
  -> 构造证据文本
  -> DeepSeek 生成回答
  -> Markdown 安全渲染
  -> 页面展示回答与检索依据
```

### 9.1 时间识别

如果用户问：

```text
2025年诉求量变化趋势如何？
25年物业问题有哪些？
```

系统会自动识别：

```text
2025-01-01 至 2025-12-31
```

如果用户在页面上手动选择了起止日期，则手动日期优先生效。

### 9.2 统计摘要

传给模型的统计摘要包含：

- 数据范围
- 总量、回复率、平均回复耗时
- 分月趋势
- 主题排行
- 来件类型排行
- 回复部门排行
- 标签来源

这样模型回答趋势问题时有明确的分时段依据。

## 10. RAG 检索增强架构

入口：

```text
app/services/rag.py
```

### 10.1 Chunk 构造

每条留言生成一个 chunk：

```text
标题：...
来件类型：...
主题：...
回复部门：...
来件内容：...
回复内容：...
```

目前每条留言一个 chunk。因为政府留言通常不是长文档，这种方式简单且可追溯。未来如果回复或内容特别长，可以改为多 chunk。

### 10.2 FTS5 关键词检索

SQLite FTS5 表：

```text
appeal_chunks_fts
```

字段包括：

```text
title
content
reply
topic
department
```

检索时：

- 标题、来件内容、回复内容、主题、部门分别参与打分
- 回复相关问题会优先检索 `reply` 字段
- 年份、地区、泛词会从查询词中剔除
- 来源至少需要命中一定数量的有效词
- 页面会显示命中字段，例如：

```text
命中字段：来件内容、回复内容
```

### 10.3 回复内容检索

如果用户问题包含：

```text
回复
答复
办理
处理结果
部门怎么说
```

系统识别为“回复意图”，会优先查回复字段。例如：

```text
部门回复中提到核查的问题有哪些？
停车问题政府一般怎么回复？
```

这类问题不会随便退回到来件内容命中，避免把“投诉内容相关”误当成“回复内容相关”。

### 10.4 证据选择

检索候选会经过二次过滤和多样性选择：

```text
候选结果
  -> 有效词过滤
  -> 命中字段过滤
  -> 标题去重
  -> 主题/部门多样化
  -> 选出代表案例
```

页面会展示：

```text
匹配候选 N 条
语义候选 N 条
过滤后高相关 N 条
送入模型 N 条代表案例
```

这样用户可以判断本次回答的证据是否充分。

## 11. Embedding 语义检索架构

入口：

```text
app/services/embeddings.py
```

当前支持阿里 DashScope：

```text
text-embedding-v3
```

### 11.1 配置

`.env`：

```dotenv
DASHSCOPE_API_KEY=你的_dashscope_key
EMBEDDING_MODEL=text-embedding-v3
EMBEDDING_BATCH_SIZE=10
EMBEDDING_TOP_K=40
```

DashScope embedding 批量接口一次最多 10 条，因此项目将 batch size 上限硬限制为 10。

### 11.2 向量生成

命令：

```powershell
python -m app.cli build-embeddings
```

流程：

```text
appeal_chunks
  -> 提取 search_text
  -> 调用 DashScopeEmbedding.get_text_embedding_batch
  -> L2 normalize
  -> float32 bytes 序列化
  -> 写入 appeal_embeddings
```

表中保存：

```text
chunk_id
appeal_id
model_name
text_hash
vector_dim
vector
```

`text_hash` 用于判断 chunk 文本是否变化。重复运行命令时，已完成且文本未变化的向量会跳过。

### 11.3 语义召回

问答时：

```text
用户问题
  -> 生成 query embedding
  -> 与 appeal_embeddings 中向量计算余弦相似度
  -> 取 top_k 语义候选
```

当前向量量级约一万条，SQLite 内存遍历可以接受。未来全国级数据应迁移到专门向量库，例如 Milvus、pgvector、Qdrant 或 Elasticsearch dense vector。

### 11.4 混合排序

RAG 最终不是只用 embedding，也不是只用关键词，而是融合：

```text
FTS5 排名分
  +
Embedding 相似度
  +
字段命中权重
  +
多样性选择
```

这样可以兼顾：

- 精确词：物业、停车、油烟、核查
- 抽象问法：居住品质、交通秩序、办理口径

没有配置 DashScope key 或未构建向量时，系统会自动退回 FTS5 检索。

## 12. DeepSeek 生成逻辑

入口：

```text
app/services/deepseek.py
app/services/agent.py
app/services/reports.py
```

系统使用 OpenAI-compatible SDK 调用 DeepSeek。

问答 prompt 由三部分组成：

```text
1. 用户问题
2. 数据库统计事实
3. RAG 代表性证据
```

约束模型：

- 不得编造数量
- 不得扩展到未导入城市
- 引用案例时使用来源编号
- 若证据数量少，需要说明样本有限
- 若标签来源是 rule，需要说明是初步分类

未配置 DeepSeek API key 时，系统返回本地统计和检索证据，不会中断页面。

## 13. Markdown 渲染

入口：

```text
app/services/markdown.py
```

模型回答支持 Markdown，包括：

- 标题
- 列表
- 表格
- 引用
- 代码块

渲染后使用 `bleach` 清洗 HTML，避免模型输出脚本被浏览器执行。

## 14. 报告生成流程

入口：

```text
app/services/reports.py
```

流程：

```text
选择地区和时间范围
  -> 数据库统计
  -> 构造报告摘要
  -> DeepSeek 生成 Markdown 报告
  -> 保存到 reports
  -> 页面预览/下载
```

未配置 DeepSeek 时，会使用本地模板生成报告。

## 15. 页面结构

| 页面 | 路由 | 作用 |
|---|---|---|
| 数据概览 | `/` | 总量、趋势、主题、部门 |
| 智能问答 | `/ask` | RAG + LLM 问答 |
| 报告生成 | `/reports` | 生成和下载分析报告 |
| 数据管理 | `/data` | 上传 Excel、查看导入批次、AI 标注 |
| 健康检查 | `/health` | 服务可用性检查 |

智能问答页面会展示：

- Markdown 分析结果
- 生成来源
- 关键词候选数
- 语义候选数
- 高相关来源数
- 代表案例
- 命中字段
- 来件摘要
- 回复摘要

## 16. 常用命令

安装依赖：

```powershell
python -m pip install -r requirements.txt
```

启动服务：

```powershell
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

导入 Excel：

```powershell
python -m app.cli import .\data\苏州.xlsx --province 江苏省 --city 苏州市
```

重建 RAG 关键词索引：

```powershell
python -m app.cli rebuild-rag
```

小批量构建 embedding：

```powershell
python -m app.cli build-embeddings --limit 20
```

全量构建 embedding：

```powershell
python -m app.cli build-embeddings
```

运行测试：

```powershell
pytest
```

## 17. 现有限制

### 17.1 数据层

- 当前主要针对苏州单城市 demo。
- SQLite 适合单机演示，不适合全国级高并发。
- 上传数据没有复杂的数据质量审计。

### 17.2 检索层

- 当前每条留言一个 chunk，长文本细粒度不足。
- Embedding 检索目前在 SQLite 内遍历向量，适合一万级数据，不适合百万级。
- 混合排序为工程启发式规则，不是学习排序模型。

### 17.3 模型层

- DeepSeek 生成结果依赖 prompt 和证据质量。
- 主题标签初始来源为规则分类，不等同于正式人工标注。
- 外部 API 可能受网络、额度、限流影响。

### 17.4 安全层

- 当前 demo 无登录鉴权。
- 不建议直接公网公开。
- 若要对外提供服务，需要增加身份认证、权限控制、审计日志和数据脱敏策略。

## 18. 未来演进建议

### 18.1 多城市/全国扩展

建议迁移：

| 当前 | 未来 |
|---|---|
| SQLite | PostgreSQL |
| 本地文件 | 对象存储 |
| 本地任务 | Celery/RQ/后台队列 |
| SQLite FTS5 | Elasticsearch/OpenSearch |
| SQLite 向量遍历 | pgvector/Milvus/Qdrant |

### 18.2 算法增强

可继续增加：

- 更细主题体系
- DeepSeek JSON 查询规划
- 回复质量分析
- 办理结果分类
- 相似案例聚类
- 热点事件自动发现
- 时间序列异常检测
- 部门办理口径对比

### 18.3 可解释性增强

建议补充：

- 每个答案的来源引用
- 检索得分展示
- 关键词命中高亮
- 向量召回来源标识
- 报告中的来源脚注

## 19. 总结

本项目的核心架构可以概括为：

```text
结构化统计保证事实准确
RAG 检索保证回答有证据
Embedding 提升语义召回
DeepSeek 负责归纳表达
页面展示来源增强可信度
```

它不是一个“把 Excel 全部塞给模型”的系统，而是一个面向政务留言分析的、可扩展的数据应用原型。
