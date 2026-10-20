

import hashlib
import re
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

from app.core.logger import setup_logging
from scripts.normalize_articles import split_articles,write_jsonl

PROJECT_ROOT = Path(__file__).resolve().parents[1]
INPUT_FILE = PROJECT_ROOT / "data/knowledge/policies/TAYUNE网站政策汇总-2026-09-12.md"
OUTPUT_FILE = PROJECT_ROOT / "data/generated/policies.jsonl"
SECTION_NAMES = ["正文", "来源网页", "适用国家或地区", "更新时间", "审核状态"]

def extract_policy_sections(body: str) -> dict[str, str]:
    """逐行读取固定栏目"""
    section_lines = {}
    found_names = []
    current_name = None
    for line in body.splitlines():
        if line.startswith("### "):
            possible_name = line.removeprefix("### ").strip()
            if possible_name in SECTION_NAMES:
                if possible_name in section_lines:
                    raise ValueError(f'政策栏目重复:{possible_name}')
                current_name = possible_name
                section_lines[current_name] = []
                found_names.append(current_name)
                continue
        if current_name is not None:
            section_lines[current_name].append(line)
        elif line.strip():
            raise ValueError("政策栏目之前出现未识别内容")

    if found_names != SECTION_NAMES:
        raise ValueError(f"政策栏目缺失或顺序错误:{found_names}")
    sections = {}
    for sections_name, lines in section_lines.items():
        sections[sections_name] = "\n".join(lines).strip()
    return sections

def normalize_policy(title:str,body:str) -> dict:
    sections = extract_policy_sections(body)
    if not title or not sections["正文"]:
        raise ValueError("政策标题或正文为空")
    source = sections["来源网页"]
    # 括号中的捕获组提取链接地址，不把“原网页”这段标签作为 URL。
    match = re.fullmatch(r"\[[^\]]+\]\((https://[^\s)]+)\)", source)
    if match:
        source = match.group(1)
    parsed_url = urlparse(source)
    if parsed_url.scheme != "https" or parsed_url.netloc != "tayunetea.com":
        raise ValueError(f"政策来源不是 TAYUNE HTTPS 地址: {title}")
    updated_at = datetime.fromisoformat(sections["更新时间"])
    if updated_at.tzinfo is None:
        raise ValueError("政策页面更新时间缺少时区")
    review_status = sections["审核状态"] or "approved"
    if review_status not in ("pending", "approved", "rejected"):
        raise ValueError(f"未知审核状态: {review_status}")
    document_id = "policy_" + hashlib.sha256(source.encode("utf-8")).hexdigest()
    return {
        "document_id": document_id,
        "title": title,
        "content": sections["正文"],
        "source_url": source,
        "source_type": "policy",
        "category": "网站政策",
        "language": "en",
        "applicable_regions": sections["适用国家或地区"] or None,
        "source_updated_at": sections["更新时间"],
        "updated_at_note": "页面记录更新时间，不代表条款最近修订时间",
        "review_status": review_status,
    }

def main() -> int:
    logger = setup_logging(PROJECT_ROOT / "logs/normalize_policies.log")
    try:
        text = INPUT_FILE.read_text(encoding="utf-8-sig")
        # 政策和文章使用相同的“## 数字. 标题：”篇章格式，因此复用分篇函数。
        raw_policies = split_articles(text)
        policies = []
        seen_ids = set()
        for title, body in raw_policies:
            policy = normalize_policy(title,body)
            if policy["document_id"] in seen_ids:
                raise ValueError(f"政策来源重复:{title}")
            seen_ids.add(policy["document_id"])
            policies.append(policy)
        write_jsonl(policies,OUTPUT_FILE)
        logger.info("政策转换成功: %d 篇，输出: %s", len(policies), OUTPUT_FILE)
        return 0
    except Exception:
        logger.exception("政策转换失败")
        return 1

if __name__ == '__main__':
    raise SystemExit(main())