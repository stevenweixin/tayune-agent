"""低频单进程SQLite：随机会话凭据、过期清理、历史和每日请求预算。
这个数据库只属于TAYUNE，和Milvus、会话令牌是访问凭据。

图只处理单次请求(TeaState 的注释说过),那"顾客上一轮说了什么"存在哪?
答案就是这里:用 SQLite 数据库保存会话历史,并顺手做了两件事:会话凭据、每日请求预算。

SQLite 是一个嵌入在程序里的小型数据库,整个数据库就是一个文件(比如 data/tayune.db),不需要单独安装、启动服务器。
CREATE TABLE IF NOT EXISTS	建表,已存在就跳过	初始化
SELECT ... WHERE	        查询	按 token 取数据
INSERT	                    插入一行
INSERT OR REPLACE	        插入,主键冲突就替换	保存会话
INSERT OR IGNORE	        插入,主键冲突就忽略	初始化当天预算
UPDATE ... SET ... WHERE	修改已有行	预算 +1
DELETE FROM ... WHERE	    删除	清理过期会话

"""

import json
import os
import secrets
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path


class SessionStore:
    def __init__(self, path: Path, max_sessions=100, cleanup_interval=60):
        # Runtime选择服务容量；默认100保留直接构造调用者的旧行为。
        if type(max_sessions) is not int or not 1 <= max_sessions <= 100000:
            raise ValueError("Invalid session capacity")
        if not 1 <= cleanup_interval <= 3600:
            raise ValueError("Invalid cleanup interval")
        self.max_sessions = max_sessions
        self.cleanup_interval = cleanup_interval
        self.next_cleanup = 0.0
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        os.chmod(path, 0o600)
        # WAL改善读写互扰；仍是单进程、单写者，不降低synchronous持久性设置。
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("CREATE TABLE IF NOT EXISTS sessions (token TEXT PRIMARY KEY, updated REAL, data TEXT)")
        self.db.execute("CREATE TABLE IF NOT EXISTS budget (day TEXT PRIMARY KEY, count INTEGER)")
        self.db.execute("CREATE INDEX IF NOT EXISTS sessions_updated ON sessions(updated)")
        self.db.commit()
        self.runs = None

    def cleanup(self):
        # 全局清理只定期执行，避免3000个被拒请求各自扫描/提交所有历史表。
        if time.monotonic() < self.next_cleanup:
            return False
        if self.runs is not None:
            self.runs.cleanup()
        self.db.execute("DELETE FROM sessions WHERE updated < ?", (time.time() - 86400,))
        self.db.commit()
        self.next_cleanup = time.monotonic() + self.cleanup_interval
        return True

    def create(self):
        self.cleanup()
        token = secrets.token_urlsafe(32)
        now = time.time()
        data = {"summary": "", "history": [], "known_product_ids": [], "last_request": 0, "preferences": {}}
        # 容量检查和插入同一条SQL；只计未过期会话，不等待定期清理腾位置。
        with self.db:
            cursor = self.db.execute("""INSERT INTO sessions(token,updated,data)
                SELECT ?,?,? WHERE (SELECT COUNT(*) FROM sessions WHERE updated>=?) < ?""",
                (token, now, json.dumps(data), now - 86400, self.max_sessions))
            if cursor.rowcount != 1:
                raise RuntimeError("会话已满")
        return token

    def get(self, token):
        row = self.db.execute("SELECT updated,data FROM sessions WHERE token = ?", (token,)).fetchone()
        if not row:
            return None
        if row[0] < time.time() - 86400:
            # 清理节流不能延长令牌有效期；访问过期会话立即删除其私有历史/轨迹。
            self.delete(token)
            return None
        return json.loads(row[1])

    def save(self, token, data):
        self.db.execute("INSERT OR REPLACE INTO sessions VALUES (?, ?, ?)",
                        (token, time.time(), json.dumps(data, ensure_ascii=False)))
        self.db.commit()

    def reserve_request(self, limit):
        # 跨进程重启保留限额；限制请求数不是精确美元账单，平台也应配置消费提醒。
        day = datetime.now(timezone.utc).date().isoformat()
        self.db.execute("INSERT OR IGNORE INTO budget VALUES (?, 0)", (day,))
        cursor = self.db.execute("UPDATE budget SET count=count+1 WHERE day=? AND count < ?", (day, limit))
        self.db.commit()
        return cursor.rowcount == 1

    def close(self):
        self.db.close()

    def delete(self, token):
        if self.runs is not None:
            self.runs.erase(token)
        self.db.execute("DELETE FROM sessions WHERE token=?", (token,))
        self.db.commit()
