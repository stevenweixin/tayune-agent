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
import secrets
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path


class SessionStore:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.execute("CREATE TABLE IF NOT EXISTS sessions (token TEXT PRIMARY KEY, updated REAL, data TEXT)")
        self.db.execute("CREATE TABLE IF NOT EXISTS budget (day TEXT PRIMARY KEY, count INTEGER)")
        self.db.commit()

    def cleanup(self):
        self.db.execute("DELETE FROM sessions WHERE updated < ?", (time.time() - 86400,))
        self.db.commit()

    def create(self):
        self.cleanup()
        if self.db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] >= 100:
            raise RuntimeError("会话已满")
        token = secrets.token_urlsafe(32)
        self.save(token, {"summary": "", "history": [], "known_product_ids": [], "last_request": 0})
        return token

    def get(self, token):
        self.cleanup()
        row = self.db.execute("SELECT data FROM sessions WHERE token = ?", (token,)).fetchone()
        return json.loads(row[0]) if row else None

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
