"""商品查询函数 -> LangChain Tool；下一节点才由模型选择工具。

复用正式WooCommerceClient。连接由调用方管理，必须在调用工具时保持打开。
本节不调用LLM、不实现执行循环；工具描述是给模型的说明，不是权限保护。
"""
import asyncio
import json
from decimal import Decimal,InvalidOperation
from typing import Literal

import httpx
from langchain_core.tools import tool
from pydantic import BaseModel, ConfigDict, Field

from app.integrations.woocommerce import WooCommerceClient


class SearchProductsInput(BaseModel):
    # 防止模型传URL、管理参数等未知字段；严格整数避免把True当成页码1。
    model_config = ConfigDict(extra="forbid", strict=True)
    search: str = Field(default="", max_length=100,
                        description="Short English product keywords, e.g. oolong. Empty lists a catalog page.")
    page: int = Field(default=1, ge=1, le=10, description="Catalog page, from 1 to 10.")
    per_page: int = Field(default=5, ge=1, le=10, description="Number of products, from 1 to 10.")
    max_price_usd: str | None = Field(default=None, min_length=1, max_length=16,
        description="Optional positive USD budget as decimal text, e.g. 20.00. Never convert another currency by guessing.")
    product_scope: Literal["tea", "teaware", "chenpi", "all"] = Field(
        default="tea", description="tea for drinkable tea/herbal products; teaware for vessels or tea pets; chenpi for citrus peel. Use all only for an explicitly mixed catalog request.")


class ProductDetailInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    product_id: int = Field(gt=0, description="Actual product ID obtained from a product search result.")

def filter_budget(data: dict, max_price_usd: str) -> dict:
    """只筛本页，比较Decimal金额；非USD或价格未知不作为已核验预算候选。
    有规格区间时保守使用最高价，不能用最低价承诺每种规格都在预算内。
    """
    try:
        budget = Decimal(max_price_usd)
    except InvalidOperation:
        raise ValueError("预算格式无效") from None
    if not budget.is_finite() or budget <= 0:
        raise ValueError("预算必须是正数")
    matches = []
    unknown = 0
    for product in data["products"]:
        price = product["price"]
        if product.get("price_range"):
            price = product["price_range"].get("max")
        if product["currency"] != "USD" or price is None:
            unknown += 1
            continue
        if Decimal(price) <= budget:
            matches.append(product)
    result = dict(data)
    result.update(products=matches, returned_count=len(matches), budget_usd=str(budget),
                  unknown_price_or_currency=unknown, budget_scope="this_page_all_options")
    return result


def build_product_tools(store: WooCommerceClient) -> list:
    """创建两个工具；内部函数记住store，模型只看业务参数，看不到连接对象。

    这是Python闭包：内层函数可以使用外层的store。
    @tool属于LangChain，args_schema用Pydantic定义输入规则。
    仅在外部工具入口校验模型参数；底层客户端仍负责外部响应校验。
    """
    @tool(args_schema=SearchProductsInput)
    async def search_products(search: str = "", page: int = 1, per_page: int = 5,
                              max_price_usd: str | None = None, product_scope: str = "tea") -> dict:
        """Search TAYUNE public products for current listed prices, availability and links.
        Returns one catalog page, not all matching products. English keywords are literal,
        not semantic taste matching. Empty results do not prove the whole shop has no match.
        Prices exclude final shipping/tax and may depend on options. Respect currency.
        On ok=false, do not invent products or treat the result as an empty catalog.
        Default scope is drinkable tea; use teaware only when the user asks for equipment or tea pets.
        Use returned price/stock directly; do not fetch every item's detail again without missing information.
        """
        try:
            if max_price_usd is not None:
                # 先验证再查询；不会为了一个无效预算产生外部请求。
                filter_budget({"products": []}, max_price_usd)
            data = await store.search_products(search=search, page=page, per_page=per_page,
                                               product_scope=product_scope)
            if max_price_usd is not None:
                data = filter_budget(data, max_price_usd)
        except (RuntimeError, ValueError):
            # 外部服务/响应错误转成模型可读结果，不把异常正文带进对话。
            # 参数校验在函数执行前完成，后续执行节点须单独处理ValidationError。
            return {"ok": False, "error": "product_search_unavailable",
                    "message": "Product search failed. Do not infer stock or availability."}
        return {"ok": True, "data": data}

    @tool(args_schema=ProductDetailInput)
    async def get_product(product_id: int) -> dict:
        """Fetch a TAYUNE product by an actual ID from previous product results.
        Use for a specific product's price, availability, link and description.
        The description may include its brewing or storage instructions; inspect it
        for product-specific brewing questions before substituting a general guide.
        Missing description means unknown, not permission to invent a recipe.
        description_truncated=true means only the returned prefix is available.
        Do not guess IDs. Options and final checkout price need customer confirmation.
        On ok=false, report unavailable information instead of inventing product details.
        """
        try:
            data = await store.get_product(product_id)
        except (RuntimeError, ValueError):
            return {"ok": False, "error": "product_detail_unavailable",
                    "message": "Product details could not be retrieved."}
        return {"ok": True, "data": data}

    return [search_products, get_product]

async def demo():
    """直接Run：手动指定工具查询网站，不调用DeepSeek，不代表模型已经会选工具。"""
    async with httpx.AsyncClient(trust_env=False) as client:
        tools = build_product_tools(WooCommerceClient(client))
        for product_tool in tools:
            print("工具名称:", product_tool.name)
            print("参数说明:", product_tool.args)
        # async工具使用ainvoke；字典会先按args_schema校验，再进入函数体。
        result = await tools[0].ainvoke({"search": "", "page": 1, "per_page": 3})
        print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(demo())