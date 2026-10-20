"""MockTransport模拟HTTP响应：测试不访问真实网站。"""
import unittest
import httpx
from app.integrations import woocommerce as module


def product():
    return {"id": 12, "name": "Tea &amp; cup", "permalink": "https://tayunetea.com/product/tea/",
            "prices": {"price": "1990", "currency_code": "USD", "currency_minor_unit": 2},
            "is_in_stock": True, "has_options": False}

class ProductTests(unittest.IsolatedAsyncioTestCase):
    def test_normalize_price_and_unknown(self):
        result = module.normalize_product(product())
        self.assertEqual(result["price"], "19.90")
        self.assertEqual(result["name"], "Tea & cup")
        self.assertIsNone(result["is_purchasable"])
        self.assertEqual(module.format_price("1000", 0), "1000")
        self.assertIsNone(module.format_price("", 2))

    def test_range_and_bad_link(self):
        data = product()
        data["prices"]["price_range"] = {"min_amount": "1990", "max_amount": "2990"}
        self.assertEqual(module.normalize_product(data)["price_range"], {"min": "19.90", "max": "29.90"})
        data["permalink"] = "https://example.com/product/tea/"
        with self.assertRaises(ValueError):
            module.normalize_product(data)

    async def test_search(self):
        def handler(request):
            self.assertEqual(request.method, "GET")
            self.assertEqual(request.url.params["search"], "green tea")
            self.assertEqual(request.url.params["page"], "2")
            return httpx.Response(200, json=[product()])
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            result = await module.WooCommerceClient(client).search_products("green tea", page=2)
        self.assertEqual(result["returned_count"], 1)
        self.assertEqual(result["scope"], "one_page")
    async def test_empty_is_not_error(self):
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=[]))) as client:
            result = await module.WooCommerceClient(client).search_products()
        self.assertEqual(result["products"], [])

    async def test_http_and_json_errors(self):
        for response in (httpx.Response(503), httpx.Response(200, text="not json")):
            async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: response)) as client:
                with self.assertRaises(RuntimeError):
                    await module.WooCommerceClient(client).search_products()

    async def test_detail_and_validation(self):
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=product()))) as client:
            store = module.WooCommerceClient(client)
            self.assertEqual((await store.get_product(12))["id"], 12)
            with self.assertRaises(ValueError):
                await store.get_product(13)
            with self.assertRaises(ValueError):
                await store.search_products(page=0)
            with self.assertRaises(ValueError):
                await store.get_product(True)


if __name__ == "__main__":
    unittest.main()
