"""应用配置。

输入：环境变量和 .env。
输出：经过 Pydantic 校验的 Settings 对象。

TODO：
1. 继承 BaseSettings 定义配置类。
2. 区分必填密钥和带默认值的普通配置。
3. 不打印或记录任何密钥。
4. 为 development、test、production 预留环境区分。
"""

