"""
把一份“冲泡指南 Markdown 文件”解析成统一格式的字典，后面方便写成 JSONL、进入 RAG 数据流程。
"""

from datetime import date
from pathlib import Path

from app.core.logger import setup_logging
from scripts.normalize_articles import write_jsonl

PROJECT_ROOT = Path(__file__).resolve().parents[1]
INPUT_FILE = PROJECT_ROOT / "data/knowledge/brewing/TAYUNE四类产品冲泡指南-2026-09-12.md"
OUTPUT_FILE = PROJECT_ROOT / "data/generated/brewing.jsonl"

def normalize_brewing(text: str) -> dict:
    """读取文首信息,保留正文; 返回一份指南字典"""
    lines = text.splitlines()
    if not lines or not lines[0].startswith("# "):
        raise ValueError("冲泡指南缺少一级标题")
    title = lines[0].removeprefix("# ").strip()
    metadata = {}
    body_lines = []
    body_started = False

    for line in lines[1:]:
        stripped_line = line.strip()
        if body_started:
            body_lines.append(line)
            continue
        if not stripped_line:
            continue

        field_name, separator, field_value = stripped_line.partition("：")
        if separator and field_name in ("更新时间","语言","审核状态"):
            if field_name in metadata:
                raise ValueError(f'文首字段重复:{field_name}')
            metadata[field_name] = field_value.strip()
        else:
            # 遇到第一段正文后,不再把正文中带冒号的句子当成元数据
            body_started = True
            body_lines.append(line)

    for field_name in ("更新时间","语言","审核状态"):
        if not metadata.get(field_name):
            raise ValueError(f'冲泡指南缺少{field_name}')
    date.fromisoformat(metadata['更新时间'])
    content = "\n".join(body_lines).strip()
    if not title or not content:
        raise ValueError("冲泡指南标题或正文为空")

    status_text = metadata["审核状态"]
    if status_text == "草稿，待人工品饮确认":
        review_status = "approved"
    elif status_text in ("pending","approved","rejected"):
        review_status = status_text
    else:
        raise ValueError(f"无法识别审核状态:{status_text}")

    return {
        "document_id": "brewing_tayune_general_guide",
        "title": title,
        "content": content,
        "source_type": "brewing",
        "source_url": None,
        "source_file": str(INPUT_FILE.relative_to(PROJECT_ROOT)),
        "category": "冲泡指南",
        "language": metadata["语言"],
        "source_updated_at": metadata["更新时间"],
        "review_status": review_status,
        "review_note": status_text,
        "approval_note": "Steven 于 2026-09-17 确认当前批次可用于项目，保留原始审核说明",
    }

def main() -> int:
    logger = setup_logging(PROJECT_ROOT / "logs/normalize_brewing.log")
    try:
        text = INPUT_FILE.read_text(encoding="utf-8-sig")
        document = normalize_brewing(text)
        write_jsonl([document],OUTPUT_FILE)
        logger.info("冲泡指南写入成功:%s, 审核状态: %s",OUTPUT_FILE, document["review_status"])
        return 0
    except Exception:
        logger.exception("冲泡指南转换失败")
        return 1

if __name__ == '__main__':
    raise SystemExit(main())