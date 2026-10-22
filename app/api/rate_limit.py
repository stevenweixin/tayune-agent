"""匿名客服入口的简单限频，不是登录认证，也不能防御分布式攻击。

配合现有会话8秒间隔、全站每日20次预算使用；不依赖Redis或额外第三方库。
仅适用于当前单进程服务。重启会清空本模块计数，但不会清空SQLite每日预算。
"""
from collections import deque
from ipaddress import ip_address
from math import ceil
from time import monotonic

def get_visitor_ip(peer_host: str, cloudflare_ip: str | None) -> str:
    """输入真实连接地址及CF头，输出规范化IP；IP仅作限频键，不写日志。

    前提：后端只监听127.0.0.1，公网唯一入口是本项目Cloudflare Tunnel。
    Uvicorn必须设置proxy_headers=False，防止连接地址先被X-Forwarded-For改写。
    本机程序仍可伪造头：本机属于可信环境，这不是防本机用户的身份认证。
    以后换公网服务器/其他反代，必须重新审查这一信任前提，不能照搬。
    """

    try:
        peer = ip_address(peer_host)
    except ValueError:
        # 不明来源共用一个桶，不按任意文本无限创建计数项。
        return "unknown"
    if peer.is_loopback and cloudflare_ip:
        try:
            return str(ip_address(cloudflare_ip))
        except ValueError:
            pass    # 无效CF头不直接作为键，退回真实连接地址。
        # 非本机连接不信任CF头；不使用客户端可随意填写的X-Forwarded-For。
    return str(peer)

class RequestRateLimiter:
    def __init__(self,clock=monotonic):
        self.clock = clock # 默认单调时钟；测试时可替换，不需要真的等一分钟。
        self.window_seconds = 60
        self.max_keys = 1000
        self.limits = {"/api/session": 5, "/api/chat": 10}
        self.requests = {}

    def check(self,visitor_ip:str, path:str) -> int:
        """返回0表示放行，否则返回建议等待秒数；每次放行都会占一次额度。"""
        if path not in self.limits:
            return 0
        now = self.clock()
        cutoff = now - self.window_seconds
        # 只保留最近一分钟；防止访客离开后IP和时间记录一直占内存。
        for old_key in list(self.requests):
            timestamps = self.requests[old_key]
            while timestamps and timestamps[0] <= cutoff:
                timestamps.popleft()
            if not timestamps:
                del self.requests[old_key]

        key = (visitor_ip,path)
        if key not in self.requests:
            # 这是低频服务的内存预算，不是数据库要求；满了拒绝新键而非无限增长。
            if len(self.requests) >= self.max_keys:
                return self.window_seconds
            self.requests[key] = deque()
        timestamps = self.requests[key]
        if len(timestamps) >= self.limits[path]:
            return max(1, ceil(timestamps[0] + self.window_seconds - now))
        timestamps.append(now)
        # 本方法没有await；当前单事件循环中检查与计数之间不会切换请求。
        return 0
