"""
系统最外面的入口
顾客在网页上输入一句话,点发送,浏览器会向你的服务器发一个请求,ChatInput 就是这段 JSON 的格式规定:
只能有一个字段 message,必须是 1 到 500 字符的文本。不符合就直接拒绝,根本进不到后面的流程。
浏览器只能提交文本；role、tools、用户ID和内部状态不允许从前端覆盖。"""

from pydantic import BaseModel, ConfigDict, Field

class ChatInput(BaseModel):
    model_config = ConfigDict(extra="forbid",strict=True,str_strip_whitespace=True)
    message: str = Field(min_length=1, max_length=500)