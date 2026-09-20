# Learning Buddy · 多模态学习智能体

> 一个面向大学生学习场景的 **Agent 型**学习助手：上传资料 → 解析与知识点抽取 → 可信度核验 →
> 向量知识库 → **自主决定要不要检索、要不要联网、要不要看图** → 交互式辅导。

**当前版本：阶段 2 完整版**（P0–P6 + 自由学习空间 + Agent Loop + MCP + 多模态）

---

## 一、它解决什么问题

**问题一：通用聊天机器人对"你的资料"一无所知。**
你把课程讲义丢给 ChatGPT，它要么说读不了 PDF，要么凭自己的印象讲一套和你老师不一样的体系。
考试考的是你老师那套，通用模型的"标准答案"反而添乱。

**问题二：模型不知道自己的边界在哪。**
它会把"记不清的最新情况"和"有把握的经典定义"用同一种确定的语气讲出来。
学生没有能力分辨哪句可信 —— 这是最危险的部分。

**问题三：回答不可溯源。**
"进程和线程的区别"它答得头头是道，但你无法确认这和你第 4 页讲义说的是不是一回事。

**问题四（本项目实测踩到的）**：问"今天几号"，模型凭记忆答了 **2024 年 6 月 13 日**，
而实际是 2026 年 9 月 20 日 —— **差了两年多，而且答得非常自信**。
用户没有理由怀疑，除非他自己知道今天几号（那他就不会问了）。

Learning Buddy 的设计取向由此确定：

- **引用有优先级**：你的资料 > 联网证据 > 模型通识。用通识必须显式标注"不在你的资料中"
- **资料里没有就说没有**：检索结果全部不相关时**直接短路、不调用模型**，而不是硬编一个答案
- **任何"实时"的东西都去查真实来源**，不靠模型记忆
- **能证明的才敢说**：每个结论都能回溯到原文页码或网页链接

---

## 二、核心功能

### 2.1 资料解析与知识点抽取（P1）

支持 **PDF / DOCX / PPTX / TXT / MD / 图片** 上传，流式落盘（单文件上限 300MB），
解析后切成带页码的语义块，再由模型抽取知识点并构建章节层级。

- 页码**绝不由模型输出** —— 模型只给块序号，页码由代码反查回填（模型报页码会编）
- 三层去重：提示词内传已知标题 → 归一化去重 → 数据库唯一约束
- 局部失败不导致整体失败：单页/单批失败只记 warning，其余照常产出

### 2.2 可信度核验（P2）

三层校验，**结果分开存放**而不是合并成一个分数：

| 层 | 手段 | 成本 |
|---|---|---|
| L1 规则 | 格式、长度、明显异常 | 零 |
| L2 模型自评 | 让模型判断这个知识点是否可疑 | 一次调用 |
| L3 联网核验 | 只对**可疑**的知识点联网查证 | 有搜索配额成本 |

⚠️ **刻意不产出 `outdated`（过时）状态** —— 比对手段不足时，宁可少下结论。

### 2.3 RAG 检索（P3）

- **双路索引**：Chroma（向量）+ 关系库中的章节结构，检索时两条路都能走
- **`RAG_MAX_DISTANCE` 距离门槛**：Top-K 永远返回 K 条，没有门槛就会拿无关片段硬编答案。
  低于门槛的片段**直接被丢弃**，并在响应里带回丢弃原因
- **上下文为空时短路**：不调用模型，直接返回「这部分不在你的资料中」
- **集合记录向量化指纹**（`provider:model:dim`），不只是维度 ——
  维度相同不代表语义空间相同，换模型后混杂了也不报错

### 2.4 Tutor Agent 教学循环（P4）

面向"我给你辅导这门课"的场景，状态机驱动：

```
装载学习状态 → 装载长期记忆 → 检索 → 决策教学动作 → 执行 → 等学生作答
   → 评估 → 更新状态 → 下一轮
```

**六种教学动作，一轮只能选一个**：追问 / 讲解 / 换讲法 / 升难度 / 降难度 / 总结。
阈值硬约束在状态机里：连对 2 次升难度、连错 2 次换讲法、连错 3 次回退前置知识点。

> 教学动作决策是 **Agent 的决策输出，不是 Tool** —— 做成 Tool 会把"Agent 会自己判断
> 该怎么教"这件事降级成"Agent 会调工具"。

### 2.5 自由学习空间（阶段 1–2）

一个更开放的入口：**问什么都可以**，由 Agent 自己决定用什么能力。

- 多轮对话、SSE 流式、会话与消息落库
- **要求登录**（对话与上传的附件属于私人内容）
- 附件上传：图片 / PDF / DOCX / PPTX / TXT / MD

### 2.6 Agent Loop（阶段 2 核心）

```
        ┌─────────────────────────────────────────────┐
        │                                             │
  START → OBSERVE ──→ DECIDE ──→ 有 tool? ──yes──→ EXECUTE → OBSERVE_RESULT ──┐
            ↑                        │                                          │
            │                        no                                         │
            │                        ↓                                          │
            └────── 达到上限 ───── FINAL_ANSWER ←────────────────────────────────┘
```

**与 P4 的 Tutor 状态机并存**，不是替换。P4 的状态机里 `DECIDE_ACTION` **一轮只执行一次** ——
那是刻意的（教学动作一轮只能选一个）。而自由学习需要"看完工具结果再决定要不要继续"，
两种模型塞进同一个状态机会让「教学策略」和「工具编排」互相牵制。

**四条硬限制，缺一不可**：

| 限制 | 默认 | 少了它会怎样 |
|---|---|---|
| `LOOP_MAX_STEPS` | 6 | 模型可反复"不调工具也不回答"，空转 |
| `LOOP_MAX_TOOL_CALLS` | 3 | 一步里并发调多个工具，成本失控 |
| `LOOP_TIMEOUT_S` | 30 | 每次调用都快但次数多，用户干等 |
| 同一工具重试上限 | 2 | 反复撞同一堵墙（**这条是实测后加的**） |

**超限不报错，降级到最终回答** —— 拿已有信息回答并说明"还有一步没做完"，
比抛一个超时错误有用得多。

> ⚠️ `LOOP_TIMEOUT_S` **不约束最终生成**，这是刻意的：生成**就是**降级目标本身，
> 给它设时限超时后就什么都输出不了。所以 30s 应理解为
> "愿意在**工具编排**上花多久"。实测一个完整回合约 30s（编排 ~26s + 生成 ~4s）。

**工具是统一注册的**，不是 `if/else` 路由：

| Tool | 说明 | 计入调用配额 |
|---|---|---|
| `retrieve_knowledge` | 在用户自己的资料里检索 | ✅ |
| `web_search` | 联网搜索（MCP 或 Tavily REST） | ✅ |
| `document_analysis` | 在指定的某一份资料里找内容 | ✅ |
| `image_analysis` | 看图（需支持视觉的模型） | ✅ |
| `current_time` | 当前日期时间 | ❌ 本地读，不占配额 |

**留给模型的工具清单只含"当下真的能用"的** —— 联网没配 Key 时 `web_search` 不出现，
模型不支持视觉时 `image_analysis` 不出现。让它看见一个调不通的工具，只会浪费一次决策。

**快通道**：没提资料、没有时效信号、无附件的问题（如"什么是 JVM"）**不进循环**，
直接生成 —— 保住"简单问题一次 LLM 调用"。**要不要花一次决策是成本决策，不该由模型定。**

### 2.7 MCP Web Search

联网走 **Tavily 官方 MCP Server**（`github.com/tavily-ai/tavily-mcp`），
连接方式为**远程 HTTP**：`https://mcp.tavily.com/mcp/`

```
initialize → serverInfo {"name":"tavily-mcp","version":"4.0.4"}  协议 2025-06-18
tools/list → 5 个工具
tools/call → tavily_search(...)
```

**工具名运行时动态发现，不硬编码。** 实测踩过：

| | 工具名 |
|---|---|
| 官方 README / 各 MCP 目录站 | `tavily-search`（**连字符**） |
| `tools/list` **实际返回** | `tavily_search`（**下划线**） |

照文档硬编码会**静默不工作**，报错是一句难懂的 "tool not found"。

**三种后端模式**（`WEB_SEARCH_BACKEND`，默认 `auto`）：

| 模式 | 行为 |
|---|---|
| `auto` | 优先 MCP；**运行时故障**（连不上/401/超时）回退 Tavily REST，**并上报回退原因** |
| `mcp` | 只用 MCP。失败就明确失败，不偷偷换 |
| `tavily` | 只用 Tavily，**不发起任何 MCP 请求** |

⚠️ **回退规则里有一条是刻意的**：MCP 连上了但**能力清单里没有搜索工具**时**不回退** ——
那是配置/兼容性错误，不是网络问题。静默回退会把"工具名对不上"永远掩盖成"网络偶尔不好"，
你会一直以为 MCP 在工作，直到某天备用通道也不可用。

**回退必须在界面上看得见**：每条引用带 `provider` / `provider_detail` / `fell_back` /
`fallback_reason`，界面显示「经由 MCP」或「备用通道」。

### 2.8 Time Tool

**为什么需要它**：实测把「今天几号」交给了模型记忆，它答了 2024 年（实际 2026 年）。

**为什么不是"触发联网"**：问日期该查**本机时钟** —— 它权威、免费、瞬时。
联网查日期反而引入网络延迟和不靠谱的来源。

**两层保障**：

1. `_TIME_HINTS` 让「今天/几号/星期几/几点/哪一天/今年…」这类问题进 Agent 循环
2. ⚠️ **更关键：在进循环之前就把真实时间放进观察里**

> 只做"识别 + 进循环"是**不够的** —— 那仍然**指望模型主动去调**时钟工具，而实测它就是不调。
> 预注入之后，模型看到的素材里就有正确日期，**想答错都难**。

`current_time` 工具 `counted=False`（本地读，不该跟联网抢 3 次配额）、零参数、
星期几自己查表（不依赖系统 locale）。

### 2.9 图片上传与多模态

**上传链路**：`POST /api/study/attachments`

- **复用书房上传的同一套存储与摄取** —— 一份文件不管从哪传都走同一条处理链。
  两套实现迟早会在"去重规则 / 大小限制 / 格式判定"上分叉
- **复用 `documents` 表，不另建 `attachments` 表** —— 上传的图**就是一份资料**，
  它该出现在书房、也该能被检索。另建表会产生"删了对话里的图、书房里还在"这类不一致
- 四个入口：点 `＋` 选文件 / **粘贴截图** / 拖拽 / 后续分享

**多模态消息**：`Message` 的 `content` 支持两种形态，**扩宽类型而不是改结构**
（OpenAI 兼容的多模态格式本身就是 content 可以是 parts 数组）：

```python
{"role": "user", "content": "什么是 JVM？"}                    # 纯文本（默认）
{"role": "user", "content": [                                  # 多模态
    {"type": "text", "text": "这张图里是什么？"},
    {"type": "image_url", "image_url": {"url": "data:image/png;base64,..."}}
]}
```

**图片以 data URI 发送**：图片存在本机磁盘上，模型服务商访问不到本机路径。

**视觉用单独的模型**（`LLM_VISION_MODEL`），不整体换掉主模型 ——
图只在"用户真的传了"时出现，让每一轮纯文本对话都付 VL 的价钱不划算。

⚠️ **视觉能力必须显式声明**（`LLM_SUPPORTS_VISION`，默认 `false`）：
不支持视觉的模型**接口不报错**，只回一句"我看不到图"，
工具于是返回 `ok=True` 的废结果，Agent 还会白转好几轮。
**不能靠"发过去看行不行"。** 未开启时 `image_analysis` 不进给模型的清单，
并且如实告诉用户"这条看不了"。

---

## 三、技术栈

| 层 | 选型 |
|---|---|
| 前端 | React 18 + TypeScript + Vite + TailwindCSS |
| 后端 | Python 3.13 + FastAPI + Pydantic v2 |
| 数据库 | MySQL 9.6 + SQLAlchemy 2.0 + Alembic |
| 向量库 | Chroma（embedded，进程内，无需独立服务） |
| LLM | OpenAI 兼容网关（通义千问 / DeepSeek / 智谱 / OpenAI 均可切换） |
| Agent | **自研轻量 Runtime**（状态机 + JSON 结构化工具调用），不用 LangGraph |
| 联网 | MCP（Tavily 官方 Server，远程 HTTP）/ Tavily REST 双通道 |
| 解析 | PyMuPDF / python-docx / python-pptx / 多模态 LLM |
| 通信 | REST + SSE |

**Agent 特征全部体现在自研 Runtime 里**：自主决策、Tool Calling、Workflow、RAG、Memory。

**明确不引入**：Multi-Agent、复杂微服务、消息队列、K8s、图数据库。
**结构化工具调用走项目自己的 JSON 通道**，不依赖各家的原生 function calling ——
供应商支持程度不一，且会多一轮往返。

**运行时依赖**：Python ≥ 3.11、Node.js ≥ 20、MySQL ≥ 8.0。Chroma 随 pip 依赖安装。

---

## 四、项目结构

```
.
├─ apps/
│  ├─ api/                          # 后端 FastAPI
│  │  ├─ app/
│  │  │  ├─ main.py                 # 入口、CORS、路由挂载、生命周期
│  │  │  ├─ api/routes/             # health / chat / documents / knowledge / rag
│  │  │  │                          #   / tutor / study / auth
│  │  │  ├─ core/                   # config、llm（网关 + 多模态）、logging
│  │  │  ├─ agent/                  # ★ Agent 运行时
│  │  │  │  ├─ runtime.py           #   P4 Tutor 状态机（九态线性流水线）
│  │  │  │  ├─ runtime_loop.py      #   阶段 2 Agent 循环（可循环决策）
│  │  │  │  ├─ policy.py            #   教学动作决策与阈值
│  │  │  │  ├─ free_study.py        #   自由学习入口（快通道 + 循环编排）
│  │  │  │  ├─ tool_specs.py        #   五个 Tool 的包装与注册
│  │  │  │  ├─ tools/               #   ToolSpec / ToolRegistry / ToolRunner
│  │  │  │  └─ prompts/             #   提示词模板
│  │  │  ├─ ingestion/              # PDF/DOCX/PPTX/TXT/MD/图片解析、分块、存储
│  │  │  ├─ search/                 # ★ 联网：mcp_client（JSON-RPC over HTTP/SSE）
│  │  │  │                          #   + provider（MCP/Tavily REST 路由与回退）
│  │  │  ├─ rag/                    #   向量库封装、embedding provider
│  │  │  ├─ services/               #   业务逻辑（文档/索引/关系/校验/会话/记忆）
│  │  │  ├─ models/  schemas/  db/
│  │  ├─ alembic/                   # 迁移环境与版本
│  │  ├─ tests/                     # 34 个测试文件
│  │  └─ requirements.txt / pyproject.toml
│  └─ web/                          # 前端 React + Vite
│     └─ src/
│        ├─ api/                    # 接口封装（含 SSE 增量帧解析，纯逻辑可单测）
│        ├─ features/               # today / study / learn / chat / library
│        │                          #   / graph / knowledge / profile / auth
│        ├─ app/                    # Shell、AuthProvider、LearningProvider
│        └─ ui/  components/  lib/  motion/
├─ skills/                          # ★ Skill 包（与代码解耦的决策策略文档）
│  ├─ free-study/        web-research/         # 阶段 2 新增
│  └─ doc-ingestion/  knowledge-extraction/  knowledge-verification/
│     socratic-tutoring/  answer-assessment/  learning-report/
├─ data/                            # 运行时数据（.gitignore）
│  └─ uploads/  chroma/  cache/  logs/
├─ docs/                            # 30 份设计与验收文档（01…29）
├─ scripts/                         # 建库、冒烟、校准、扫描、端到端验证
├─ samples/
├─ docker-compose.yml               # 备用（本机用本机 MySQL）
├─ .env.example
└─ README.md
```

**前端页面（6 个）**：今天 / 自由学习 / 辅导 / 资料 / 知识地图 / 我的

---

## 五、本地启动

### 1. 准备数据库

```bash
# MySQL 需已启动并监听 3306
mysql -u root -p -e "CREATE DATABASE IF NOT EXISTS learning_buddy DEFAULT CHARSET utf8mb4;"
```

### 2. 安装后端

```bash
python -m venv .venv
source .venv/Scripts/activate        # Windows Git Bash
# source .venv/bin/activate          # macOS / Linux
pip install -r apps/api/requirements.txt
```

### 3. 配置环境变量

```bash
cp .env.example .env
```

编辑 `.env`，**至少确认这几项**（其余可留默认）：

```ini
MYSQL_PASSWORD=你的MySQL口令

# 对话与 Tool Calling 必需。留空则进入 mock 模式（仍可跑通整条链路，零 API 消耗）
LLM_API_KEY=你的密钥
LLM_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
LLM_MODEL=qwen-plus

# RAG 检索必需。留空则索引与问答接口明确返回 409，不静默降级
EMBEDDING_API_KEY=你的密钥

# 联网搜索必需（MCP 与 Tavily 共用同一个 Key）
TAVILY_API_KEY=你的密钥
```

### 4. 建库与迁移

```bash
python scripts/init_db.py
python scripts/init_db.py --status    # 查看当前迁移版本
```

### 5. 启动后端

```bash
cd apps/api
python -m uvicorn app.main:app --reload --port 8000
```

- 接口文档：<http://127.0.0.1:8000/docs>
- 健康检查：<http://127.0.0.1:8000/api/health>（含 LLM / MySQL / Chroma / Embedding 四组件状态）

### 6. 启动前端

```bash
cd apps/web
npm install
npm run dev
```

打开 <http://localhost:5173>

### 7. 自检

```bash
python scripts/smoke_test.py        # 配置 / LLM / MySQL / Chroma 全组件
python scripts/scan_secrets.py      # 凭据泄漏扫描（涉及凭据的改动后请务必跑）
```

---

## 六、环境变量

所有配置通过 `.env` 注入，**代码中不存在任何硬编码的密钥或口令**。
完整清单见 `.env.example`。

### 基础

| 变量 | 说明 | 默认 |
|---|---|---|
| `APP_ENV` | dev / test / prod | dev |
| `API_PREFIX` | 接口前缀 | `/api` |
| `CORS_ORIGINS` | 允许的前端来源，逗号分隔 | `http://localhost:5173` |

### 数据库

| 变量 | 说明 | 默认 |
|---|---|---|
| `MYSQL_HOST` / `MYSQL_PORT` | 地址与端口 | `127.0.0.1` / `3306` |
| `MYSQL_USER` / `MYSQL_PASSWORD` | 账号口令 | `root` / 空 |
| `MYSQL_DATABASE` | 库名 | `learning_buddy` |
| `DATABASE_URL` | 完整连接串，优先级最高 | 空 |

### LLM

| 变量 | 说明 | 默认 |
|---|---|---|
| `LLM_MODE` | `auto` / `live` / `mock`。auto = 有 Key 走真实，无 Key 降级 | auto |
| `LLM_BASE_URL` | OpenAI 兼容端点 | `https://api.deepseek.com/v1` |
| `LLM_API_KEY` | 密钥。**留空进入 mock 模式** | 空 |
| `LLM_MODEL` | 主模型（文本） | `deepseek-chat` |
| `LLM_VISION_MODEL` | 视觉模型。**留空表示不支持看图** | 空 |
| `LLM_SUPPORTS_VISION` | 视觉能力开关。**必须按部署显式声明** | `false` |

> 上表是**代码里的默认值**（DeepSeek 端点 + DeepSeek 模型，两者配套）。
> 本项目开发与验收时实际用的是**阿里云百炼**：
> `LLM_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1` + `LLM_MODEL=qwen-plus`。
> ⚠️ **`LLM_BASE_URL` 与 `LLM_MODEL` 必须配套** —— 换了端点就要同时换模型名，
> 只改一个会得到一个跑不通的配置。其余可选组合见下方「常见服务商取值」。

### 向量与 RAG

| 变量 | 说明 | 默认 |
|---|---|---|
| `EMBEDDING_PROVIDER` | `api`（云端）/ `mock`（本地确定性向量，零网络） | api |
| `EMBEDDING_BASE_URL` | OpenAI 兼容的 `/embeddings` 端点 | 阿里云百炼兼容模式 |
| `EMBEDDING_API_KEY` | **RAG 检索必需** | 空 |
| `EMBEDDING_MODEL` / `EMBEDDING_DIM` | 向量模型与维度，**两者必须一致** | `text-embedding-v3` / 1024 |
| `RAG_TOP_K` | 检索返回片段数 | 5 |
| `RAG_MAX_DISTANCE` | 余弦距离上限，超过即丢弃 | 0.40 |
| `CHROMA_PERSIST_DIR` | 向量库目录 | `<项目根>/data/chroma` |

### 联网搜索

| 变量 | 说明 | 默认 |
|---|---|---|
| `WEB_SEARCH_BACKEND` | `auto` / `mcp` / `tavily` | auto |
| `MCP_WEB_SEARCH_URL` | MCP 服务端点 | `https://mcp.tavily.com/mcp/` |
| `MCP_TIMEOUT_S` | MCP 单次调用超时 | 20 |
| `TAVILY_API_KEY` | **MCP 与 Tavily REST 共用** | 空 |
| `TAVILY_MAX_RESULTS` / `TAVILY_SEARCH_DEPTH` | 默认结果数与深度 | 5 / basic |

### Agent Loop

| 变量 | 说明 | 默认 |
|---|---|---|
| `LOOP_MAX_STEPS` | 循环总轮数上限 | 6 |
| `LOOP_MAX_TOOL_CALLS` | 工具调用次数上限 | 3 |
| `LOOP_TIMEOUT_S` | 工具编排总时限（秒） | 30 |

### 常见服务商取值

| 服务商 | `LLM_BASE_URL` | `LLM_MODEL` | 视觉模型 |
|---|---|---|---|
| 阿里云百炼（本项目实测所用） | `https://dashscope.aliyuncs.com/compatible-mode/v1` | `qwen-plus` | `qwen-vl-max` |
| DeepSeek | `https://api.deepseek.com/v1` | `deepseek-chat` | 无 |
| 智谱 GLM | `https://open.bigmodel.cn/api/paas/v4` | `glm-4-plus` | `glm-4v-plus` |
| OpenAI | `https://api.openai.com/v1` | `gpt-4o-mini` | `gpt-4o` |

### ⚠️ 两个必须知道的约束

**① `RAG_MAX_DISTANCE = 0.40` 是实测校准值，不是拍脑袋定的。**
用 `text-embedding-v3` 在两份语料上测距：资料内问题最近距离 0.145~0.286，
资料外问题最近距离 0.415~0.577。**换 embedding 模型后必须重新校准**：

```bash
python scripts/calibrate_rag.py --document-id <资料id> --auto-questions
```

**② 换向量模型时必须同时改 `EMBEDDING_DIM` 并重建索引。**
维度写错会在索引时明确报 `dim_mismatch`，不会静默写入错维度的向量。
集合会记录**向量化指纹**（`provider:model:dim`）而不只是维度 ——
维度相同不代表语义空间相同。

---

## 七、测试与构建

### 后端

```bash
cd apps/api
pytest -q                       # 全量（34 个测试文件 / 598 个用例）
pytest tests/agent -q           # 只跑 Agent 相关
pytest tests/search -q          # 只跑 MCP 与联网
pytest -q --tb=short            # 失败时带短回溯
```

### 前端

```bash
cd apps/web
npm run typecheck               # 仅类型检查
npm run test:unit               # 单测（Node 内置 test runner，无需额外依赖）
npm run build                   # typecheck + 生产构建
```

### 端到端验证脚本

```bash
python scripts/smoke_test.py              # 全组件冒烟（配置/LLM/MySQL/Chroma）
python scripts/p1_smoke.py                # P1：解析 → 分块 → 知识点抽取
python scripts/p2_smoke.py                # P2：关系构建 → 三级校验
python scripts/p3_smoke.py                # P3：索引 → 检索 → RAG
python scripts/p4_smoke.py                # P4：教学闭环
python scripts/p5_smoke.py                # P5：学习状态与长期记忆
python scripts/verify_stage2_api.py       # 阶段 2：登录门 + 三个 Agent 场景（真打 HTTP/SSE）
python scripts/verify_stage22_upload.py   # 阶段 2.2/2.3：上传 + 看图（真打 HTTP）
python scripts/verify_stage2_browser.py   # 阶段 2：浏览器端（需前后端已启动）
python scripts/verify_stage2_123_browser.py  # 阶段 2.1/2.2/2.3 浏览器端

# 加 --live 使用真实模型（默认 mock，零 API 消耗）
python scripts/p3_smoke.py --live
```

> **mock 模式的价值**：`chat_json()` 在 mock 下返回由输入派生的合法 JSON，
> 整条流水线可以**离线回归、零 API 消耗**。CI 与日常改动都跑 mock，
> 只有验收时才 `--live`。

---

## 八、当前已验证能力

### ✅ 后端

| 项 | 状态 |
|---|---|
| 后端全量测试 | **598 个用例，退出码 0** |
| 接口路径 | **46 个**（P0–P5 + 自由学习 + 鉴权） |
| 解析格式 | PDF / DOCX / PPTX / TXT / MD / 图片 |
| 上传上限 | 单文件 300MB（流式落盘，不一次性读进内存） |
| RAG | 真实云端 Embedding + Chroma，带距离门槛与短路 |
| Tutor 教学闭环 | P4 状态机 + 六种教学动作 + 作答评估 |
| 长期记忆 | P5 跨会话记忆、掌握度、薄弱点 |

### ✅ Agent（阶段 2）

| 项 | 状态 |
|---|---|
| Agent Loop | 四条硬限制全部实测生效（步数 / 调用数 / 总时限 / 同工具重试） |
| 场景 A：普通问题 | 0 工具调用、1 次 LLM 调用、约 6s |
| 场景 B：资料不足再联网 | `retrieve_knowledge → web_search → 收尾`，**循环可见** |
| 场景 C：图片 → 看图 → 回答 | 实测回答含图内标识符，并**指出了测试样例代码自身的逻辑缺陷** |
| MCP | `initialize` / `tools/list` / `tools/call` 协议级验证通过，服务端自报 `tavily-mcp v4.0.4` |
| MCP 负向测试 | `mcp` 模式 + 错误 Key → **明确失败且不回退** |
| Time Tool | "今天几号" → 返回真实日期（原为模型记忆，误差两年） |
| 图片上传 | 上传 / 去重 / 格式校验 / 越权 401 全部通过 |
| 视觉 | `qwen-vl-max` 实测能读图并识别图内标识符 |

### ✅ 前端

| 项 | 状态 |
|---|---|
| 单测 | **55 / 55** |
| typecheck / 构建 | ✅ / ✅ |
| 页面 | 今天 / 自由学习 / 辅导 / 资料 / 知识地图 / 我的 |
| SSE 流式 | 含"穷举切分点"测试（覆盖 TCP 随机分片导致帧被切断的场景） |
| 浏览器验收 | 三个 Agent 场景 + 通道标识 + 零内部术语泄漏 + 零控制台错误 |

### ⚠️ 需要留意的地方

- **视觉依赖支持 VL 的模型**。当前配置为 `qwen-vl-max`（与主模型 `qwen-plus` 分离）。
  未配置时 `image_analysis` 不进工具清单，Agent 会**如实说"这条看不了"**而不是猜
- **MCP 默认 `auto`**，回退到 Tavily REST 时会在界面上显示「备用通道」+ 原因
- **`data/chroma` 只支持单写者**。两个进程同时访问同一目录会让检索间歇性失败，
  且报错（`attempt to write a readonly database`）很难联想到并发问题。部署时请保证单写者
- **`LOOP_TIMEOUT_S` 只约束工具编排**，不约束最终生成（生成是降级目标本身）

---

## 九、已知遗留问题

**功能缺口**

| 项 | 说明 |
|---|---|
| 知识保存与复杂 Memory | **阶段 3 的内容**，当前自由学习不保存"学到哪了" |
| 附件管理 | 只能删除，**不能重命名**；多附件排序未定 |
| 对话附件与正式资料未区分 | 上传的图会同时进书房与检索范围（刻意的，但若需区分要加标记字段） |
| MCP 限流未知 | Tavily 的 `tavily_research` 文档提到 20 req/min，`tavily_search` 未确认 |

**工程债**

| 项 | 说明 |
|---|---|
| 右侧面板泄露实现细节 | "它做了什么"里显示 `document_ids=[7 项]` 这类技术信息，按设计规矩该收进开发者面板 |
| 原生 function calling | 当前用项目自己的 JSON 通道。若实测发现模型工具选择质量不够，可作独立阶段引入 |
| `docker-compose.yml` | 备用方案，未实测（本机无 Docker） |

---

## 十、故障排查

| 现象 | 原因与处理 |
|---|---|
| 界面显示 `LLM 网关: mock` | 未配置 `LLM_API_KEY`。填好后重启后端 |
| `MySQL` 徽标为黄色 | 数据库连不上。检查服务是否启动、`.env` 里口令是否正确 |
| 启动报 `Access denied for user` | 口令错误。MySQL 8+ 默认 `caching_sha2_password`，已依赖 `cryptography` 包支持 |
| RAG 接口返回 **409** | 未配置 `EMBEDDING_API_KEY`。这是**刻意的**——不静默降级成假向量 |
| 回答一次性全部出现、没有逐字效果 | 代理缓冲了 SSE。开发期走 Vite 代理；Nginx 部署需关 `proxy_buffering` |
| 联网引用显示「备用通道」 | MCP 运行时故障，已自动回退 Tavily。看 `fallback_reason` 定位 |
| 联网直接明确失败（无回退） | `mcp` 模式下的预期行为。或 MCP 能力清单里没有搜索工具（配置错误） |
| 问"今天几号"答错年份 | 不应发生。若发生，检查 `_TIME_HINTS` 是否命中、预注入是否执行 |
| 传图后回答"看不到图" | 未配置 `LLM_VISION_MODEL` 或 `LLM_SUPPORTS_VISION=false` |
| `alembic.ini` 报编码错误 | 该文件**必须保持纯 ASCII**（Alembic 按 GBK 读取） |

---

## 十一、安全约定

- **凭据只允许写入项目根 `.env`**（已 gitignore）。
  **禁止**出现在源码、README、验收报告、日志、测试文件中
- 汇报与截图时**一律脱敏**（如 `sk-***47`）
- 涉及凭据的改动后，请复跑 `python scripts/scan_secrets.py`
- 自由学习的对话与附件**要求登录**；P1–P6 的接口保持原有的"未登录可用"行为

---

## 十二、路线

| 阶段 | 目标 | 状态 |
|---|---|---|
| P0 | 地基与最小 LLM 流式闭环 | ✅ |
| P1 | 资料解析与知识点抽取 | ✅ |
| P2 | 可信度核验与联网核验 | ✅ |
| P3 | RAG 检索与知识库 | ✅ |
| P4 | Tutor Agent 教学循环 | ✅ |
| P5 | 学习状态与长期记忆 | ✅ |
| P6 | 打磨与上线 | ✅ |
| 阶段 1 | 自由学习空间 | ✅ |
| 阶段 2 | Agent 化 + MCP + 多模态 | ✅ |
| **阶段 3** | **知识保存与复杂 Memory** | 计划中 |

完整设计见 `docs/01-技术方案-v1.md`，各阶段方案与验收见 `docs/` 下 30 份文档。
