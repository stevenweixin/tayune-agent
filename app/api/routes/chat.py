"""消费者聊天接口。

输入：ChatRequest。
处理：调用 Agent 图，不在路由里编写检索和 Prompt 业务逻辑。
输出：ChatResponse；后续增加 SSE 事件流。

TODO：
1. 校验消息长度和会话 ID。
2. 调用 graph 的统一入口。
3. 将项目异常转换为稳定 HTTP 响应。
4. 记录耗时，不记录未经脱敏的敏感内容。
"""

