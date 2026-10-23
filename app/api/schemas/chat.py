"""
系统最外面的入口
顾客在网页上输入一句话,点发送,浏览器会向你的服务器发一个请求,ChatInput 就是这段 JSON 的格式规定:
message必须是1到500字符；可选request_id用于安全重放同一次提交。
浏览器只能提交文本；role、tools、用户ID和内部状态不允许从前端覆盖。"""

from pydantic import BaseModel, ConfigDict, Field

class ChatInput(BaseModel):
    model_config = ConfigDict(extra="forbid",strict=True,str_strip_whitespace=True)
    message: str = Field(min_length=1, max_length=500)
    request_id: str | None = Field(default=None, min_length=16, max_length=64,
                                   pattern=r"^[A-Za-z0-9_-]+$")
