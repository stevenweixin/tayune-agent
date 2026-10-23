"""
复用LangChain消息和ChatOpenAI，不重新实现HTTP协议，不引入模型工厂。
注意：直接Run会向DeepSeek发送固定演示题，可能产生费用；单元测试不联网。
密钥只从后端环境/.env读取，不打印配置、完整请求或第三方异常正文。
此处只做文本调用，不处理流式、工具调用或结构化输出，后续按需扩展。
"""
import asyncio
import os
import time
from pathlib import Path

import httpx
from dotenv import dotenv_values
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from app.core.logger import setup_logging

PROJECT_ROOT = Path(__file__).resolve().parents[2]
REQUEST_TIMEOUT = 30.0
TOTAL_TIMEOUT = 65.0
MAX_OUTPUT_TOKENS = 512


def read_config() -> dict:
    """不导入尚未实现的Settings；环境变量优先，.env作本地补充。"""
    values = dotenv_values(PROJECT_ROOT / ".env", interpolate=False)
    config = {}
    defaults = {"DEEPSEEK_API_KEY": "", "DEEPSEEK_BASE_URL": "https://api.deepseek.com/v1",
                "DEEPSEEK_MODEL": "deepseek-flash"}
    for name, default in defaults.items():
        value = os.environ.get(name, values.get(name) or default)
        config[name] = value.strip()
    if not config["DEEPSEEK_API_KEY"]:
        raise ValueError("缺少DEEPSEEK_API_KEY，请在后端.env中填写，不要贴到聊天或示例文件")
    base_url = config["DEEPSEEK_BASE_URL"].rstrip("/")
    # 密钥发送目的地限制，防止配置错误把密钥传给陌生服务器。
    if base_url not in ("https://api.deepseek.com", "https://api.deepseek.com/v1"):
        raise ValueError("本节点仅接受DeepSeek官方HTTPS地址")
    if not config["DEEPSEEK_MODEL"]:
        raise ValueError("DEEPSEEK_MODEL不能为空，请核对平台可用模型名")
    config["DEEPSEEK_BASE_URL"] = base_url
    return config


def create_chat_model(config, sync_client, async_client):
    """这只是本项目的构造函数；ChatOpenAI来自langchain-openai。"""
    from langchain_openai import ChatOpenAI
    return ChatOpenAI(
        model=config["DEEPSEEK_MODEL"], api_key=config["DEEPSEEK_API_KEY"],
        base_url=config["DEEPSEEK_BASE_URL"], timeout=REQUEST_TIMEOUT,
        max_retries=0, max_tokens=MAX_OUTPUT_TOKENS, use_responses_api=False,
        http_client=sync_client, http_async_client=async_client,
        # 普通摘要/问答先关闭思考模式，避免小输出预算全用于推理；以平台支持为准。
        extra_body={"thinking": {"type": "disabled"}},
    )


async def generate_text(messages: list) -> dict:
    """输入LangChain文本消息；输出text、usage和elapsed_seconds。

    await等待网络期间可让其他协程运行，不等于无限并发。SDK重试关闭，
    网关仅对可恢复的服务错误重试一次；重试不能保证没有重复计费。
    HTTP各阶段30秒不等于整次操作30秒，因此另设65秒总等待上限。
    """
    if not messages or len(messages) > 20:
        raise ValueError("消息数必须在1到20条之间")
    total_chars = 0
    for message in messages:
        if not isinstance(message, (HumanMessage, AIMessage, SystemMessage)):
            raise ValueError("本节点不接受工具消息")
        if not isinstance(message.content, str) or not message.content.strip():
            raise ValueError("只接受非空文本消息")
        if isinstance(message, AIMessage) and (message.tool_calls or message.invalid_tool_calls or message.additional_kwargs.get("tool_calls")):
            raise ValueError("本节点不接受工具调用")
        total_chars += len(message.content)
    if total_chars > 12000:
        raise ValueError("输入超过12000字符，请先裁剪历史；字符限制不是精确Token预算")
    config = read_config()
    start = time.monotonic()
    try:
        # 同时传入两种客户端，避免SDK初始化同步客户端时仍读取系统代理。
        # trust_env=False绕过之前的socks5h环境代理；需要代理时再显式配置。
        # 当前低频阶段每次调用建连接并关闭，易理解；服务化后可在应用生命周期复用。
        with httpx.Client(trust_env=False) as sync_client:
            async with httpx.AsyncClient(trust_env=False) as async_client:
                llm = create_chat_model(config, sync_client, async_client)
                from app.integrations.model_gateway import ModelGateway
                gateway = ModelGateway({"summary": llm}, {"summary": config["DEEPSEEK_MODEL"]})
                response = await asyncio.wait_for(gateway.invoke("summary", messages), timeout=TOTAL_TIMEOUT)
    except ImportError:
        raise RuntimeError("缺少langchain-openai，请按本节说明在tayune_agent环境安装") from None
    except Exception as error:
        status = getattr(error, "status_code", None)
        if status in (401, 403):
            reason = "密钥或访问权限错误"
        elif status == 429:
            reason = "请求限流，请稍后再试"
        elif isinstance(error, (TimeoutError, httpx.TimeoutException)):
            reason = "请求超时"
        else:
            reason = "模型服务调用失败，请核对模型名、余额和网络"
        # 不附带原异常正文或链，避免SDK把请求数据或密钥带入日志。
        raise RuntimeError(reason) from None
    if response.response_metadata.get("finish_reason") != "stop":
        raise RuntimeError("响应没有正常结束，可能输出被截断；本次不把部分文本当作完整结果")
    if not isinstance(response.content, str) or not response.content.strip():
        raise RuntimeError("模型未返回有效文本")
    return {"text": response.content.strip(), "usage": response.usage_metadata,
            "elapsed_seconds": round(time.monotonic() - start, 3)}


def main() -> int:
    logger = setup_logging(PROJECT_ROOT / "logs/deepseek.log")
    try:
        messages = [SystemMessage(content="Reply briefly in English."),
                    HumanMessage(content="Say hello to a visitor to a tea shop in one sentence.")]
        # asyncio.run只用于脚本入口；后续FastAPI的async函数应直接await。
        result = asyncio.run(generate_text(messages))
        print(result["text"])
        logger.info("LLM调用完成，用量=%s，耗时=%.3f秒", result["usage"], result["elapsed_seconds"])
        return 0
    except Exception as error:
        logger.error("演示失败: %s", str(error))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
