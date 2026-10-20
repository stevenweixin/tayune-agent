"""
将固定格式的文章汇总 Markdown 转成一篇一行的 JSONL。
这不是通用 Markdown 解析器；资料模板变化时，需要同步调整解析规则。
"""
import hashlib
import json
import re
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse
from app.core.logger import setup_logging

PROJECT_ROOT = Path(__file__).resolve().parents[1]

INPUT_FILE = (
    PROJECT_ROOT / "data/knowledge/article/TAYUNE已发布文章汇总-2026-09-12.md"
)
OUTPUT_FILE = PROJECT_ROOT / "data/generated/articles.jsonl"
LOG_FILE = PROJECT_ROOT / "logs/normalize_articles.log"

# 用正则提取文章中的标题,和小标题
ARTICLE_PATTERN = re.compile(r"^## \d+\. 标题：[ \t]*(.+)$", re.MULTILINE)
SECTION_NAMES = ["内容分类","正文","来源链接", "适用茶类", "语言", "更新时间", "审核状态"]
SECTION_PATTERN = re.compile(
    r"^### (" + "|".join(SECTION_NAMES) + r")[ \t]*$", re.MULTILINE
)

def split_articles(text: str) -> list[tuple[str,str]]:
    """输入汇总文本,返回[(文章标题,这一篇的全部内容)],...]"""
    headings = list(ARTICLE_PATTERN.finditer(text))
    # print(f'headings:{headings}')
    if not headings:
        raise ValueError("没有找到文章标题,请检查是否使用了约定模板")

    articles = []
    for index, heading in enumerate(headings):
        # 当前标题结束处是内容起点; 下一篇标题开始出是内容终点
        end = headings[index + 1].start() if index + 1 < len(headings) else len(text)
        body = text[heading.end() : end].strip()
        # 去掉篇与篇之间的分割线,不删除正文内部的分割线
        if body.endswith("\n---"):
            body = body[:-4].rstrip()
        articles.append((heading.group(1).strip(), body))
    return articles

def extract_sections(body:str) -> dict[str,str]:
    """根据固定栏目名提取正文内容，正文里的图片、表格和小标题原样保留。"""
    headings = list(SECTION_PATTERN.finditer(body))
    names = [heading.group(1) for heading in headings]
    # 不跳过缺失或重复栏目,避免把元数据错误的混入正文
    if names != SECTION_NAMES:
        raise ValueError(f"文章栏目缺失、重复或顺序不符: {names}")

    sections = {}
    for index, heading in enumerate(headings):
        end = headings[index + 1].start() if index + 1 < len(headings) else len(body)
        sections[heading.group(1)] = body[heading.end() : end].strip()
    return sections

def normalize_article(title:str,body:str) -> dict:
    """输入一篇原始文章,输出字段统一的字典"""
    sections = extract_sections(body)
    source = sections["来源链接"]
    # 当前模板是 [原文链接](https://...)，也允许直接填写 URL。
    link = re.fullmatch(r"\[[^\]]+\]\((https://[^\s)]+)\)", source)
    source_url = link.group(1) if link else source
    parsed = urlparse(source_url)
    if parsed.scheme != "https" or parsed.netloc != "tayunetea.com":
        raise ValueError(f"文章来源必须是 TAYUNE HTTPS 链接: {title}")

    review = sections["审核状态"]
    # Steven 于 2026-09-17 确认当前这批资料可用；换新资料时需重新确认。
    review_status = review or "approved"
    if review_status not in {"pending", "approved", "rejected"}:
        raise ValueError(f"未知审核状态: {title} ({review_status})")

    # 相同 URL 始终得到相同编号，不受文章排列顺序影响。
    # 这只是生成编号，不是加密；正式系统最好使用网站文章 ID。
    document_id = "article_" + hashlib.sha256(source_url.encode("utf-8")).hexdigest()
    return {
        "document_id" : document_id,
        "title" : title,
        "content" : sections["正文"],
        "source_url": source_url,
        "category": sections["内容分类"],
        "applicable_tea_types": sections["适用茶类"],
        "language": sections["语言"],
        "source_updated_at": sections["更新时间"],
        "review_status": review_status,
        "source_type": "article",
    }

def validata_articles(articles:list[dict]) -> None:
    """检查必填值,时间格式和重复来源;不替代人工内容审核"""
    if not articles:
        raise ValueError("没有可写入的文章")
    seen_urls = set()
    for article in articles:
        for field in ("title","content","category","language","source_updated_at"):
            if not article[field]:
                raise ValueError(f'文章:{article["title"]} 缺少{field}')
        update_at = datetime.fromisoformat(article["source_updated_at"])
        if update_at.tzinfo is None:
            raise ValueError(f'更新时间缺少时区:{article["title"]}')
        if article["source_url"] in seen_urls:
            raise ValueError(f'重复来源:{article["source_url"]}')
        seen_urls.add(article["source_url"])

def write_jsonl(articles:list[dict], output_path:Path) -> None:
    """所有文章校验通过后才写出,一行一个json对象"""
    output_path.parent.mkdir(parents=True,exist_ok=True)
    with output_path.open("w",encoding="utf-8") as file:
        for article in articles:
            file.write((json.dumps(article, ensure_ascii=False)+"\n"))

def main()-> int:
    logger = setup_logging(LOG_FILE)
    try:
        logger.info("开始读取文章: %s", INPUT_FILE)
        text = INPUT_FILE.read_text(encoding="utf-8-sig")
        raw_articles = split_articles(text)
        logger.info("识别到 %d 篇文章", len(raw_articles))
        articles = [normalize_article(title, body) for title, body in raw_articles]
        validata_articles(articles)
        write_jsonl(articles, OUTPUT_FILE)
        pending_count = sum(item["review_status"] == "pending" for item in articles)
        logger.info("写入成功: %d 篇,输出:%s",len(articles),OUTPUT_FILE)
        if pending_count:
            logger.warning("%d 篇待人工审核，不应直接进入正式问答知识库", pending_count)
        return 0
    except Exception:
        logger.exception("文章转换失败，请检查资料格式和路径")
        return 1

if __name__ == '__main__':
    # text = INPUT_FILE.read_text(encoding="utf-8-sig")
    # result = split_articles(text)
    # print(result)

    raise SystemExit(main())
