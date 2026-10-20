# TAYUNE Tea Concierge

面向 TAYUNE WooCommerce 独立站的茶叶选购与客服智能体。

当前状态：开发基线，本地请求链路已运行，首轮业务验收存在待修复问题，尚未正式对外上线。

## 第一版范围

- 需求式选茶
- 商品比较与商品问答
- 冲泡建议
- 配送、退换货等公开政策问答
- 证据不足时拒答或引导人工联系
- 回答来源与必要运行日志

第一版不查询具体订单，不自动下单、发布文章、修改商品或执行退款。

## 实际架构

```text
站内搜索框 Ask AI（待部署）
  -> FastAPI：输入校验、随机会话凭据、请求限制
  -> TeaService：会话历史、分类与摘要
  -> LangGraph：模型选择工具 -> 执行 -> ToolMessage -> 再决定
       -> WooCommerce：公开商品查询
       -> BGE-M3 + Milvus：稠密/稀疏混合召回 -> BGE Reranker
  -> DeepSeek：回答或澄清
  -> SQLite：精简对话、摘要与每日请求预算
```

图每次请求使用新State，多轮历史由SQLite提供；当前没有启用持久化LangGraph检查点。
模型选择工具，工具白名单、参数校验、次数和超时由代码控制。
站点普通搜索与结账保持独立，AI入口故障时需要前端提示并保留普通搜索。

## 目录

```text
app/agent/          状态、提示词、工具、执行图与服务
app/api/            请求结构与正文限制
app/core/           资源生命周期、CPU任务、记忆与日志
app/database/       会话与请求预算
app/integrations/   DeepSeek、WooCommerce客户端
app/rag/            文档、向量、混合检索与精排
app/main.py         FastAPI入口
scripts/            数据处理、建库、训练及检查脚本
deploy/             独立Milvus配置与WordPress接入模板
web/chat.html       本地接口演示页
docs/public/        可公开的项目状态与文档
```

## 推荐环境

- Python 3.11
- 单独创建虚拟环境，向量服务和数据目录也独立配置

```bash
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -r requirements.txt
pip install -r requirements-dev.txt
```

只有准备在本机运行 BGE-M3 时，才安装：

```bash
pip install -r requirements-ml.txt
```

## 本地运行

1. 复制`.env.example`为`.env`，填写自己的DeepSeek密钥与TAYUNE专属Milvus配置。
2. 准备本地BGE-M3、BGE Reranker和三分类模型。模型权重不随仓库分发，不自动下载。
3. 准备并审核知识数据，运行规范化、切块和离线建库流程。输入资料与生成数据不随仓库分发。
4. 启动独立Milvus，确认已建立所需混合集合；应用启动不会自动建库。
5. 在项目根目录运行：

```bash
python -m app.main
```

浏览器打开`http://127.0.0.1:8011/demo`；不要直接打开磁盘HTML。
聊天会调用付费API。默认单进程、每日全站20次聊天请求、会话间隔8秒。

主要配置：`DEEPSEEK_API_KEY`、`DEEPSEEK_BASE_URL`、`DEEPSEEK_MODEL`、
`TAYUNE_MILVUS_URI`、`TAYUNE_MILVUS_ISOLATED`、`TAYUNE_BGE_MODEL_PATH`、`TAYUNE_RERANK_MODEL_PATH`。
本机向量服务映射`127.0.0.1:19532`；不要将Milvus端口公开到互联网。

## 可复现性与限制

- 当前仓库不包含私有知识资料、训练数据、生成文件、模型权重或会话数据库；克隆后不能直接启动完整服务。
- 分类模型当前从`data/generated/query_classifier_model_v2`读取；训练/数据脚本仍有本地路径，需要按实际目录配置。
- 商品预算参数接线和类别筛选、长片段精排、政策工具失败正在排查；见[当前状态](docs/public/STATUS.md)。
- 合成数据上的分类成绩不代表真实线上准确率；尚无可发布的端到端通过率或业务收益。
- WordPress搜索框插件是接入模板，固定HTTPS后端入口及真实站点验收尚未完成。

本项目结合学习实践、公开文档与AI辅助迭代。提交历史记录实际变更，不代表所有实现均由作者独立从零完成。
