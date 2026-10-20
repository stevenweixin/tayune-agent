"""统一日志配置：控制台、轮换文件，以及常见邮箱和具名密钥的脱敏。

注意：正则不能识别所有敏感数据，不要主动记录密钥、地址或完整订单。
后续再补充 request_id / session_id、节点耗时和 Token 用量。
"""
import logging
import re
from logging.handlers import RotatingFileHandler
from pathlib import Path


class SensitiveDataFormatter(logging.Formatter):
    """先格式化完整日志，再脱敏；同时覆盖消息参数和异常堆栈。"""

    def __init__(self, fmt: str):
        super().__init__(fmt)
        # 整个邮箱替换，包含 a@example.com 这种短用户名。
        self.email_pattern = re.compile(
            r"[a-zA-Z0-9_.+-]+@[\w.-]+\.[\w-]+"
        )
        # 支持 api_key=xxx、"password": "xxx" 等常见写法。
        # 带引号的值可以包含空格；不带引号的值在空白或常见分隔符处结束。
        self.secret_pattern = re.compile(
            r"\b(api[_-]?key|secret|token|password)\b['\"]?\s*[:=]\s*"
            r"(?:\"[^\"\r\n]*\"|'[^'\r\n]*'|[^\s,;}\]]+)",
            re.IGNORECASE
        )

    def format(self, record: logging.LogRecord) -> str:
        # 父类先完成 %s 参数替换，并附上 logger.exception 的 traceback。
        message = super().format(record)
        message = self.secret_pattern.sub(r"\1=[REDACTED]", message)
        return self.email_pattern.sub("[EMAIL]", message)


def setup_logging(log_file: Path) -> logging.Logger:
    """输入日志路径,返回配置好的项目 logger 入口调用一次即可"""
    logger = logging.getLogger("tayune_agent")
    logger.setLevel(logging.INFO)
    logger.propagate = False

    # 在程序入口配置一次；重复调用复用首次配置，不会切换日志路径。
    if logger.handlers:
        return logger

    log_file.parent.mkdir(parents=True, exist_ok=True)
    formatter = SensitiveDataFormatter(
        "%(asctime)s | %(levelname)s | %(name)s | %(message)s"
    )

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)

    file_handler = RotatingFileHandler(
        log_file, maxBytes=2 * 1024 * 1024, backupCount=3, encoding="utf-8"
    )
    file_handler.setFormatter(formatter)
    # 两个 handler 都使用脱敏 formatter，子 logger 传来的消息也会处理。

    logger.addHandler(console_handler)
    logger.addHandler(file_handler)
    return logger


if __name__ == '__main__':
    project_root = Path(__file__).resolve().parents[2]
    logger = setup_logging(project_root / "logs" / "app.log")

    # 测试带有敏感信息的日志
    logger.info("测试邮箱: %s", "a@example.com")
    logger.info("测试密钥: api_key=%s", "fake_test_key_123")
