"""WooCommerce 商品查询客户端。

MVP 先做只读查询：商品列表、商品详情、价格、库存和链接。
只读公开商品查询：后续商品Tool的底层能力，不是完整Agent。

复用调用方提供的httpx.AsyncClient，服务启动时创建、退出时关闭。
固定网站和GET路径，不接收模型提供的URL，不调用购物车、订单或写入接口。
Store API是公开商品接口，不需要WooCommerce管理密钥，也不使用DeepSeek密钥。

"""
import asyncio
import html
import json
from datetime import datetime, timezone
from decimal import Decimal
from urllib.parse import urlparse

import httpx
from app.integrations.product_content import extract_product_content

BASE_URL = "https://tayunetea.com/wp-json/wc/store/v1/products"

# 2026-10-20只读核实的TAYUNE分类ID。迁站/删除重建分类后需重新核对。
# Travel Sets(36)同时包含茶与茶具，不能单凭这个分类判断是可饮用商品。
PRODUCT_CATEGORY_IDS = {
    "tea": (21, 28, 29, 30, 34, 16, 17, 26, 35, 31, 33, 32, 40, 18),
    "teaware": (19, 27, 38, 39, 37),
    "chenpi": (23,),
}


def matches_product_scope(product: dict, product_scope: str) -> bool:
    """业务规则：茶叶范围须命中茶类，且不能同时被标为茶具或茶宠。"""
    if product_scope == "all":
        return True
    category_ids = set()
    for category in product["categories"]:
        category_ids.add(category["id"])
    if product_scope == "tea" and category_ids.intersection(PRODUCT_CATEGORY_IDS["teaware"]):
        return False
    return bool(category_ids.intersection(PRODUCT_CATEGORY_IDS[product_scope]))


def format_price(value, minor_unit: int):
    """接口价格是最小货币单位的字符串：USD的8990表示89.90，而不是8990美元。

    缺少价格返回None，不伪造0元；Decimal避免浮点金额误差。
    小数位由接口提供，不假设所有货币都除以100。
    """
    if value is None or value == "":
        return None
    if not isinstance(value, str) or not value.isascii() or not value.isdigit():
        raise ValueError("商品价格格式异常")
    return format(Decimal(value).scaleb(-minor_unit), f".{minor_unit}f")


def normalize_product(raw: dict) -> dict:
    """在外部响应入口检查关键字段；业务层接收统一字典，不直接使用全部API字段。"""
    product_id = raw.get("id")
    name = raw.get("name")
    link = raw.get("permalink")
    if type(product_id) is not int or product_id <= 0:
        raise ValueError("商品编号无效")
    if not isinstance(name, str) or not name.strip():
        raise ValueError("商品名称缺失")
    if not isinstance(link, str):
        raise ValueError("商品链接缺失")
    url = urlparse(link)
    if url.scheme != "https" or url.netloc != "tayunetea.com":
        raise ValueError("商品链接不是TAYUNE HTTPS地址")
    prices = raw.get("prices")
    if not isinstance(prices, dict):
        raise ValueError("商品价格信息缺失")
    unit = prices.get("currency_minor_unit")
    currency = prices.get("currency_code")
    if type(unit) is not int or not 0 <= unit <= 6 or not isinstance(currency, str) or not currency:
        raise ValueError("商品货币信息无效")
    price_range = prices.get("price_range")
    normalized_range = None
    if price_range is not None:
        if not isinstance(price_range, dict):
            raise ValueError("商品价格区间格式异常")
        normalized_range = {"min": format_price(price_range.get("min_amount"), unit),
                            "max": format_price(price_range.get("max_amount"), unit)}
    result = {"id": product_id, "name": html.unescape(name), "url": link,
              "currency": currency, "price": format_price(prices.get("price"), unit),
              "price_range": normalized_range, "type": raw.get("type"),
              "fetched_at": datetime.now(timezone.utc).isoformat()}
    # 分类属于外部响应，在客户端入口校验；不把整份API响应交给业务层。
    raw_categories = raw.get("categories", [])
    if not isinstance(raw_categories, list):
        raise ValueError("商品分类格式异常")
    categories = []
    for category in raw_categories:
        if not isinstance(category, dict) or type(category.get("id")) is not int or category["id"] <= 0:
            raise ValueError("商品分类编号无效")
        if not isinstance(category.get("name"), str) or not isinstance(category.get("slug"), str):
            raise ValueError("商品分类名称或slug无效")
        categories.append({"id": category["id"], "name": html.unescape(category["name"]),
                           "slug": category["slug"]})
    result["categories"] = categories
    # 未知状态保留None，不把字段缺失说成有货或缺货。
    for field in ("is_in_stock", "is_purchasable", "is_on_backorder", "has_options"):
        value = raw.get(field)
        if value is not None and type(value) is not bool:
            raise ValueError(f"商品状态字段异常: {field}")
        result[field] = value
    return result


class WooCommerceClient:
    def __init__(self, client: httpx.AsyncClient):
        self.client = client

    async def _get_json(self, url: str, params=None):
        """内部共用请求逻辑；超时/HTTP失败不返回空列表，避免误报没有商品。

        不自动重试，以后由Agent统一限制工具调用次数；避免层层重试放大请求。
        禁止自动跟随重定向，防止站点配置异常时请求跳到未知地址。
        """
        try:
            response = await self.client.get(url, params=params, timeout=15, follow_redirects=False)
            response.raise_for_status()
        except httpx.HTTPError:
            raise RuntimeError("商品服务暂不可用，请稍后重试") from None
        try:
            return response.json()
        except ValueError:
            raise RuntimeError("商品服务未返回有效JSON") from None

    async def search_products(self, search: str = "", page: int = 1, per_page: int = 5,
                              product_scope: str = "all") -> dict:
        """search是商品关键词，不是语义检索。每次只取一页，不能把一页说成全站。

        最多查前10页、每页10项是首版成本上限，不是WooCommerce限制。
        此处不做口味推断或预算筛选，后续工具节点结合知识和实际货币处理。
        """
        if not isinstance(search, str) or len(search) > 100:
            raise ValueError("搜索词必须是最多100字符的文本")
        if type(page) is not int or not 1 <= page <= 10:
            raise ValueError("页码必须在1到10之间")
        if type(per_page) is not int or not 1 <= per_page <= 10:
            raise ValueError("每页数量必须在1到10之间")
        if product_scope not in ("tea", "teaware", "chenpi", "all"):
            raise ValueError("未知商品范围")
        params = {"search": search.strip(), "page": page, "per_page": per_page,
                  "catalog_visibility": "visible"}
        if product_scope != "all":
            # 先在网站过滤再分页，避免第一页被茶具占满；模型不能传任意分类ID。
            params["category"] = ",".join(str(category_id) for category_id in PRODUCT_CATEGORY_IDS[product_scope])
            params["category_operator"] = "in"
        data = await self._get_json(BASE_URL, params)
        if not isinstance(data, list):
            raise ValueError("商品列表响应格式异常")
        products = []
        excluded = 0
        for raw in data:
            if not isinstance(raw, dict):
                raise ValueError("商品记录格式异常")
            if raw.get("is_password_protected"):
                continue
            product = normalize_product(raw)
            # 再按响应中的真实分类核实，防止商品误标为茶类和茶具两者而被推荐饮用。
            if not matches_product_scope(product, product_scope):
                excluded += 1
                continue
            products.append(product)
        return {"products": products, "page": page, "per_page": per_page,
                "scope": "one_page", "product_scope": product_scope,
                "excluded_by_scope": excluded, "returned_count": len(products)}

    async def get_product(self, product_id: int) -> dict:
        """按真实商品ID复核当前信息；模型不能编造ID，之后由工具结果取得。"""
        if type(product_id) is not int or product_id <= 0:
            raise ValueError("商品ID必须是正整数")
        data = await self._get_json(f"{BASE_URL}/{product_id}")
        if not isinstance(data, dict) or data.get("is_password_protected"):
            raise ValueError("商品信息不可用")
        result = normalize_product(data)
        if result["id"] != product_id:
            raise ValueError("商品响应编号与请求不一致")
        # 只在详情入口附上正文；保留原价格/库存/分类校验，列表响应仍保持简短。
        # 网站正文仍是不可信工具数据，不能改变系统指令或操作权限。
        result.update(extract_product_content(data.get("description")))
        return result


async def demo():
    # 脚本在这里管理连接；以后FastAPI lifespan管理，多个工具调用复用客户端。
    async with httpx.AsyncClient(trust_env=False) as client:
        store = WooCommerceClient(client)
        result = await store.search_products(per_page=3)
        print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    # 直接Run只查询公开商品，不调用LLM、不写网站、不连接Milvus。
    asyncio.run(demo())
