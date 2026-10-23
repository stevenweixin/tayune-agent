"""本地只读执行报告；不输出会话凭据、问题、检查点或工具正文。"""
import argparse
import json
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def report(path, run_id=None):
    db = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        if run_id:
            rows = db.execute("SELECT time,kind,details FROM execution_events WHERE run_id=? ORDER BY rowid", (run_id,)).fetchall()
            return {"task_id": run_id, "events": [{"time": row[0], "kind": row[1], **json.loads(row[2])} for row in rows]}
        statuses = dict(db.execute("SELECT status,COUNT(*) FROM task_runs GROUP BY status").fetchall())
        return {"retained_tasks": sum(statuses.values()), "statuses": statuses,
                "note": "24-hour retained sample, not all-time site success rate; missing usage is not zero billing."}
    finally:
        db.close()


def main():
    parser = argparse.ArgumentParser(description="只读本地任务审计")
    parser.add_argument("--database", type=Path, default=ROOT / "data/generated/tayune_sessions.sqlite3")
    parser.add_argument("--run-id")
    args = parser.parse_args()
    print(json.dumps(report(args.database, args.run_id), indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
