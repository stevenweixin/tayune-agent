"""把横向商品 CSV 转换成一行一个商品的 JSONL。
原始 CSV 只读，转换结果写入 data/generated/products.jsonl。
"""

import csv
import json
from pathlib import Path
from app.core.logger import setup_logging

# CSV 使用中文字段,程序内部统一使用英文键名
FIELD_MAPPING = {
    "product_id": "product_id",
    "SKU": "sku",
    "商品名称": "name",
    "商品链接": "url",
    "商品类型": "product_type",
    "茶类/器具类型": "category",
    "产区": "origin",
    "年份": "year",
    "工艺": "processing",
    "风味": "flavor",
    "适合人群": "suitable_for",
    "冲泡方法": "brewing_method",
    "规格/变体": "variants",
    "价格": "price_snapshot",
    "库存状态": "stock_status_snapshot",
    "商品描述": "description",
    "图片": "images",
    "最后更新时间": "source_updated_at",
}

REQUIRED_FIELDS = {
    "product_id",
    "sku",
    "name",
    "url",
    "product_type",
    "category",
    "description",
    "source_updated_at",
}

# 根目录
PROJECT_ROOT = Path(__file__).parent.parent
# print(f"PROJECT_ROOT: {PROJECT_ROOT}") # PROJECT_ROOT: /Users/steven/Documents/大模型应用开发学习/tayune_agent
# 输入文件位置
INPUT_FILE = (
    PROJECT_ROOT
    / "data"
    / "knowledge"
    / "products"
    / "TAYUNE已上架商品汇总-2026-09-12.csv"
)
# 输出文件位置
OUTPUT_FILE = PROJECT_ROOT / "data" / "generated" / "products.jsonl"
LOG_FILE = PROJECT_ROOT / "logs" / "normalize_products.log"


def clean_value(value: str | None) -> str | None:
    """
    清理值，如None则返回 None，否则返回清理后的值
    """
    if value is None:
        return None

    cleaned = value.strip()
    return cleaned if cleaned else None

def load_transposed_csv(csv_path: Path) -> list[dict[str, str | None]]:
    """
    行列转置. 原始文档为按列，需要转置成按行，即每行数据为一个商品
    """
    if not csv_path.exists():
        raise FileNotFoundError(f"找不到商品 CSV 文件: {csv_path}")
    # utf-8-sig 可以兼容普通 CSV 文件，也可以兼容带 BOM 的 UTF-8 CSV 文件
    with csv_path.open("r", encoding="utf-8-sig", newline="") as file:
        rows = list(csv.DictReader(file))
    if not rows:
        raise ValueError("商品 CSV 没有数据")
    if "字段" not in rows[0]:
        raise ValueError("商品 CSV 缺少“字段”列,必须包含“字段”列，请使用标准商品导入模板")

    product_columns = [column for column in rows[0] if column != "字段"]
    products = []
    for product_column in product_columns:
        product = {}
        for row in rows:
            field_name = clean_value(row.get("字段"))
            if field_name is None:
                continue
            product[field_name] = clean_value(row.get(product_column))
        products.append(product)
    return products

def normalize_product(raw_product: dict[str, str | None]) -> dict:
    """ 规范化商品数据,转换字段名,数据类型和图片格式 """
    # 字段映射与重命名
    product = {
        english_name : raw_product.get(chinese_name)
        for chinese_name, english_name in FIELD_MAPPING.items()
    }

    # product_id 在后续数据库、WooCommerce 查询中应该是整数。
    try:
        product["product_id"] = int(product["product_id"])
    except (TypeError,ValueError) as error:
        raise ValueError(
            f'商品 product_id 无效: {product.get("product_id")}'
        ) from error

    # 利用字典 Key 的唯一性进行去重,如空则空列表
    image_text = product.get("images")
    if image_text:
        # dict.fromkeys 在保留顺序的同时去除重复图片地址
        product["images"] = list(dict.fromkeys(image_text.splitlines()))
    else:
        product["images"] = []

    # 价格和库存只是 2026-09-12 的快照。
    # 生产环境回答用户时，应重新调用 WooCommerce API 获取实时值。
    product["source_type"] = "woocommerce_export"

    return product

def validate_products(products:list[dict]) -> None:
    """检查必填字段、链接格式和重复数据。"""
    seen_ids = set()
    seen_skus = set()
    seen_urls = set()

    for product in products:
        missing_fields = [
            field for field in REQUIRED_FIELDS if not product.get(field)
        ]
        if missing_fields:
            raise ValueError(
                f'商品 {product.get("name")} 缺少必填字段: {missing_fields}'
            )
        if not product["url"].startswith("https://tayunetea.com/"):
            raise ValueError(f'商品链接不属于 TAYUNE: {product["url"]}')

        duplicate_checks = (
            ("product_id", product["product_id"], seen_ids),
            ("sku", product["sku"], seen_skus),
            ("url", product["url"], seen_urls),
        )

        for field_name, value,seen_values in duplicate_checks:
            if value in seen_values:
                raise ValueError(f'发现重复{field_name}:{value}')
            seen_values.add(value)

def write_jsonl(products:list[dict],output_path : Path) -> None:
    """ 将每个商品写成 一行 JSON结构"""
    output_path.parent.mkdir(parents=True,exist_ok=True)

    with output_path.open("w",encoding="utf-8") as file:
        for product in products:
            json_line = json.dumps(product, ensure_ascii=False)
            file.write(json_line + "\n")
    # 生产环境可以先写临时文件，验证成功后再原子替换正式文件，
    # 避免程序中途失败留下不完整结果


def main() -> int:
    """组织处理流程：返回 0 表示成功，1 表示失败"""
    logger = setup_logging(LOG_FILE)
    try:
        # INFO 记录正常进度
        logger.info("开始读取商品文件:%s", INPUT_FILE)
        raw_products = load_transposed_csv(INPUT_FILE)
        logger.info("读取完成: %d 个商品", len(raw_products))

        products = [normalize_product(item) for item in raw_products]
        logger.info("字段与类型转换完成,开始校验")
        validate_products(products)
        logger.info("必填字段,链接和重复数据校验完成")

        write_jsonl(products, OUTPUT_FILE)
        logger.info("转换完成: %d 个商品,输出: %s", len(products), OUTPUT_FILE)
        return 0
    except Exception as error:
        logger.exception("转换商品时出错 请根据下面的报错检查数据或路径")
        return 1

if __name__ == '__main__':
    raise SystemExit(main())