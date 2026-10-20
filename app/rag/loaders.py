"""加载商品、茶知识、冲泡资料和网站政策。
输出普通字典列表，不依赖 LangChain，也不生成向量。
商品仍通过商品模块处理；这里仅加载文章、冲泡指南和政策。

validate_record():
只检查

load_documents():
检查通过以后，决定怎么处理
"""

import json
from pathlib import Path

from app.core.logger import setup_logging

PROJECT_ROOT = Path(__file__).resolve().parents[2]
INPUT_FILES = [
    PROJECT_ROOT / "data/generated/articles.jsonl",
    PROJECT_ROOT / "data/generated/brewing.jsonl",
    PROJECT_ROOT / "data/generated/policies.jsonl",
]

def validate_record(record:object, seen_ids:set[str],location:str) -> None:
    """检查一条记录;失败就报错，通过则正常结束，不修改记录或编号集合。
        JSON 也可能解析成列表或数字，必须先确认是字典才能调用 get。"""
    if not isinstance(record,dict):
        raise ValueError(f"{location} 必须是 JSON 对象")

    required_fields = (
        "document_id", "title","content","source_type",
        "source_updated_at","category","language"
    )
    for field in required_fields:
        value = record.get(field)
        if not isinstance(value,str) or not value.strip():
            raise ValueError(f"{location} 缺少字段 {field}")

    if record["source_type"] not in ("article","brewing","policy"):
        raise ValueError(f"{location} 只接受文章、冲泡指南和政策")
    if not record.get("source_url") and not record.get("source_file"):
        raise ValueError(f"{location} 文档缺少可追溯来源")
    if record["document_id"] in seen_ids:
        raise ValueError(f"{location} 文档编号重复: {record['document_id']}")
    if record.get("review_status") not in ("pending","approved","rejected"):
        raise ValueError(f"{location}缺失或未知审核状态: {record['document_id']}")


def load_documents(input_files: list[Path], * , include_pending:bool = False) -> list[dict]:
    """读取 JSONL，默认只加载 approved 文档；当前这批资料已由人工审核通过。

    include_pending=True 额外加载待审核资料，仅供离线检查。
    星号表示该参数必须写名字，例如 include_pending=True，避免误传。
    """
    documents = []
    seen_ids = set()
    allowed_statuses = {"approved"}
    if include_pending:
        allowed_statuses.add("pending")

    for input_file in input_files:
        # 文件不存在时直接报错，不用缺失的知识库继续执行。
        with input_file.open("r",encoding="utf-8") as file:
            for line_number, line in enumerate(file, start=1):
                if not line.strip():
                    continue

                # 1. 把 1 行JSON文本 变成Python对象
                location = f"{input_file.name} 第 {line_number} 行"
                try:
                    record = json.loads(line)
                except json.JSONDecodeError as error:
                    raise ValueError(f"{location} JSON 无效") from error

                # 2. 校验:具体规则交给独立函数,通过后记录编号
                validate_record(record,seen_ids,location)
                seen_ids.add(record["document_id"])

                # 3. 筛选:rejuected 永远不再允许集合
                if record["review_status"] not in allowed_statuses:
                    continue

                # 4. 整理: 复制字典,再从副本取出正文,原record 不受影响.
                metadata = record.copy()
                content = metadata.pop("content")
                documents.append({"page_content": content,"metadata":metadata})
    return documents

def main() -> int:
    logger = setup_logging(PROJECT_ROOT / "logs/loaders.log")
    try:
        documents = load_documents(INPUT_FILES)
        logger.info("可供正式知识库使用的已审核文档: %d 篇", len(documents))
        if not documents:
            logger.warning("没有可用文档，请先完成人工审核；不要通过关闭审核过滤来上线")
        return 0
    except Exception:
        logger.exception("知识加载失败")
        return 1

if __name__ == '__main__':
    raise SystemExit(main())