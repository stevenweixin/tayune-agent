# TAYUNE Tea Concierge

面向 TAYUNE WooCommerce 独立站的茶叶选购与客服智能体。

当前状态：第一版已在 [tayunetea.com](https://tayunetea.com/) 开放小范围试运行，右下角悬浮客服可查询商品与已审核知识。1.2.0版本通过两条真实网站问答验收；这不是整体准确率或全天可用性承诺。后端仍运行在本机，经Cloudflare Tunnel提供HTTPS，电脑需保持开机、联网且服务运行。

2026-10-23：1.4.0候选代码补入执行检查点、请求幂等、模型/工具网关、显式偏好、任务审计、业务轨迹重放和知识库版本发布。后端已重启并完成三条授权本机真实问答，但仍有证据外扩写问题；网站插件保持1.3.1，1.4.0插件尚未上传。见[本机验收与待修问题](docs/public/LOCAL_ACCEPTANCE.md)和[核心可靠性说明](docs/public/CORE_RELIABILITY.md)。

2026-10-24：正式图使用证据单元选择与服务端原文渲染，限制模型自行补写；补入末次模型收尾约束和短关键词提示。用户重启后，一组“选茶→同会话杯泡追问”本机真实案例通过：商品价格/库存、标题内链和冲泡原文均核对一致。此前预算耗尽和验收脚本触发冷却的记录保留，没有绕过限额或自动追加付费重试。152项Python、7项Node及32条离线契约重放通过；不是整体准确率或新版网站SSE验收。本例未真实触发强制收尾，资料正确性/相关性仍需评估。见[回答约束与取舍](docs/public/CORE_RELIABILITY.md#extractive-answer-contract)及[完成后的两轮记录](docs/public/LOCAL_ACCEPTANCE.md#october24-authorized-original-session-follow-up--completed)。

前端1.4.0已部署，未登录页面加载1.4.0资源。此前162项Python及8项Node离线预检通过；本次三条授权网站请求中，商品与政策两答通过，一条杯泡追问出现网络失败且未找到后端任务，原因尚未确认，没有自动重试。手机宽度与普通搜索正常；不能宣称新版完整验收通过。见[线上部署与验收记录](docs/public/WIDGET_140_ACCEPTANCE.md)及[安装包预检记录](docs/public/WIDGET_PREFLIGHT.md)。

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
右下角悬浮茶叶客服（线上1.4.0，小范围试运行）
  -> FastAPI：输入校验、随机会话凭据、请求限制
  -> TeaService：会话历史、分类、受限意图复核与摘要
  -> LangGraph：模型网关 -> 受约束工具计划/网关 -> ToolMessage -> 再决定
       -> WooCommerce：公开商品查询
       -> BGE-M3 + Milvus：稠密/稀疏混合召回 -> BGE Reranker
  -> DeepSeek：选择证据单元或澄清代码 -> 服务端生成正文与标题链接
  -> SQLite：精简对话、摘要、显式偏好、每日预算、任务检查点与审计
```

图每次新请求使用新State，多轮历史由SQLite提供；1.4.0候选在节点边界持久化消息状态和下一节点，不依赖未接入的MemorySaver，不是任意时刻的自动恢复。
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
deploy/             独立Milvus配置、隧道启动与WordPress悬浮客服插件
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
- 已加入有限窗口精排、商品类别与预算筛选、来源标记选择和受限意图复核，并完成针对性回归。首版已通过两条真实网站验收；首次政策失败的原始原因、更多答案质量与长期可用性仍需继续核实。见[当前状态](docs/public/STATUS.md)和[修复日志](docs/public/FIXLOG.md)。
- 合成数据上的分类成绩不代表真实线上准确率；尚无可发布的端到端通过率或业务收益。
- WordPress悬浮客服插件1.2.0已启用聊天，普通搜索保持独立。隧道只公开健康检查与两个聊天API，不公开Milvus、演示页或OpenAPI。电脑离线时客服不可用，购物流程不依赖AI。插件构建与回滚见[接入说明](deploy/wordpress/README.md)。

首版发布时间、真实验证结果、保护范围与回滚见[发布记录](docs/public/RELEASE.md)。

提交历史记录实际变更；修复日志保留问题、技术决策、验证证据与已知限制。
