"""把 WooCommerce 商品详情里的 HTML 转成有长度上限的纯文本，再附带一个"是否被截断"的标志

输入：WooCommerce description 字符串；输出：description 和 description_truncated。
"""

from html.parser import HTMLParser


MAX_HTML_CHARS = 100000
MAX_TEXT_CHARS = 6000


class ProductTextParser(HTMLParser):
    """复用 Python 标准库解析 HTML，不自己写正则去猜标签嵌套。"""

    def __init__(self):
        # 父类会在解析文本时还原 &amp; 等字符实体，不需要再次反复 unescape。
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.hidden_depth = 0

    def handle_starttag(self, tag, attrs):
        # 这些方法由 HTMLParser.feed 自动调用，不是我们逐行手动调用。
        # 忽略脚本/样式/模板正文；这里是文本提取，不是任意 HTML 的安全渲染器。
        if tag in ("script", "style", "template", "noscript"):
            self.hidden_depth += 1
        if self.hidden_depth == 0 and tag in ("p", "div", "li", "br", "h1", "h2", "h3", "tr"):
            self.parts.append("\n")
        if self.hidden_depth == 0 and tag in ("td", "th"):
            self.parts.append(" ")  # 表格相邻单元格不能粘成一个数字或单词。

    def handle_endtag(self, tag):
        if tag in ("script", "style", "template", "noscript"):
            self.hidden_depth = max(0, self.hidden_depth - 1)
        if self.hidden_depth == 0 and tag in ("p", "div", "li", "h1", "h2", "h3", "tr"):
            self.parts.append("\n")

    def handle_data(self, data):
        if self.hidden_depth == 0:
            self.parts.append(data)


def extract_product_content(raw_html: str | None) -> dict:
    """外部接口入口检查类型和长度；缺失正文就留空，绝不补写配方。"""
    if raw_html is None:
        raw_html = ""
    if not isinstance(raw_html, str):
        raise ValueError("商品正文必须是字符串")
    if len(raw_html) > MAX_HTML_CHARS:
        # 项目输入上限，不是 WooCommerce 限制。防止异常大正文无限进入解析器。
        raise ValueError("商品HTML正文超过本项目输入上限")

    parser = ProductTextParser()
    parser.feed(raw_html)
    parser.close()
    raw_text = "".join(parser.parts)
    lines = []
    for line in raw_text.splitlines():
        clean_line = " ".join(line.split())
        if clean_line:
            lines.append(clean_line)
    text = "\n".join(lines)
    # 模型还有原有上下文总上限；这里先限制单个商品正文，避免一次详情吃光上下文。
    # 截断并不保证保留冲泡段落，所以必须告诉模型正文是否完整。
    return {
        "description": text[:MAX_TEXT_CHARS],
        "description_truncated": len(text) > MAX_TEXT_CHARS,
    }
