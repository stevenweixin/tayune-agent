"""Offline product-content and evidence tests with mocked external dependencies."""
import asyncio
import json
import unittest
from unittest.mock import patch

import httpx
from langchain_core.documents import Document
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from app.integrations.product_content import extract_product_content, MAX_TEXT_CHARS, MAX_HTML_CHARS
from app.integrations.woocommerce import WooCommerceClient, BASE_URL
from app.agent.product_tool import build_product_tools
from app.agent.knowledge_tool import build_knowledge_tool
from app.agent.graph import build_graph, register_source
from app.agent.answer_guard import check_answer


PRODUCT_URL = "https://tayunetea.com/product/example-tea/"


def sample_product():
    return {
        "id": 614, "name": "Example Tea", "permalink": PRODUCT_URL,
        "prices": {"currency_minor_unit": 2, "currency_code": "USD", "price": "4590"},
        "categories": [{"id": 21, "name": "Tea", "slug": "tea"}],
        "is_in_stock": True, "is_purchasable": True,
        "description": "<p>How to Brew</p><p>For a mug, use 3 g per 250 ml for 2-3 minutes.</p>",
    }


class ContentTests(unittest.TestCase):
    def test_html_becomes_readable_text(self):
        result = extract_product_content("<p>Tea &amp; water</p><p>3 g<br>250 ml</p>")
        self.assertEqual(result["description"], "Tea & water\n3 g\n250 ml")
        self.assertFalse(result["description_truncated"])

    def test_script_style_and_templates_are_not_read_as_product_text(self):
        result = extract_product_content("<style>hidden</style><script>hidden</script><template><p>hidden</p></template><p>Tea</p>")
        self.assertEqual(result["description"], "Tea")

    def test_empty_and_missing_description_do_not_invent_a_recipe(self):
        for value in (None, "", "<p></p>"):
            self.assertEqual(extract_product_content(value), {"description": "", "description_truncated": False})

    def test_truncated_text_is_explicitly_marked(self):
        result = extract_product_content("<p>" + "x" * (MAX_TEXT_CHARS + 1) + "</p>")
        self.assertTrue(result["description_truncated"])
        self.assertEqual(len(result["description"]), MAX_TEXT_CHARS)

    def test_invalid_type_and_excessive_html_are_rejected(self):
        for value in ({"html": "unexpected"}, "x" * (MAX_HTML_CHARS + 1)):
            with self.assertRaises(ValueError):
                extract_product_content(value)

    def test_table_cells_remain_separated(self):
        result = extract_product_content("<table><tr><td>3 g</td><td>250 ml</td></tr></table>")
        self.assertEqual(result["description"], "3 g 250 ml")


class SourceTests(unittest.TestCase):
    def test_internal_chunks_get_real_ids_without_inventing_urls(self):
        sources = []
        self.assertEqual(register_source(sources, "Shop guide", None, "chunk:abc"), "S1")
        self.assertEqual(register_source(sources, "Shop guide", None, "chunk:abc"), "S1")
        self.assertEqual(register_source(sources, "Shop guide", None, "chunk:def"), "S2")
        self.assertEqual(len(sources), 2)
        self.assertIsNone(sources[0]["url"])

    def test_unknown_origin_still_cannot_get_an_id(self):
        sources = []
        self.assertIsNone(register_source(sources, "Untraceable", None))
        self.assertEqual(sources, [])

    def test_public_url_deduplication_is_preserved(self):
        sources = []
        register_source(sources, "Page", PRODUCT_URL, "chunk:one")
        self.assertEqual(register_source(sources, "Page", PRODUCT_URL, "chunk:two"), "S1")
        self.assertEqual(len(sources), 1)

    def test_internal_citations_become_title_notes_not_fake_links(self):
        sources = []
        register_source(sources, "Approved shop guide", None, "chunk:private-id")
        result = check_answer("Follow the guide. [S1]", sources)
        self.assertFalse(result["degraded"])
        self.assertEqual(result["sources"], [])
        self.assertIn("Approved shop guide", result["answer"])
        self.assertIn("no public link", result["answer"])
        self.assertNotIn("private-id", result["answer"])

    def test_mixed_citations_keep_public_links_and_dedupe_internal_titles(self):
        sources = []
        register_source(sources, "Product", PRODUCT_URL)
        register_source(sources, "Shop guide", None, "chunk:one")
        register_source(sources, "Shop guide", None, "chunk:two")
        result = check_answer("Product. [S1] Guide. [S2] [S3]", sources)
        self.assertEqual(result["sources"], [{"title": "Product", "url": PRODUCT_URL}])
        self.assertEqual(result["answer"].count("Shop guide"), 1)

    def test_local_evidence_does_not_relax_missing_or_unknown_citation_checks(self):
        sources = []
        register_source(sources, "Guide", None, "chunk:one")
        for text in ("Recipe with no citation.", "Recipe. [S9]"):
            with self.assertRaises(ValueError):
                check_answer(text, sources)


class FakeCPU:
    async def run(self, function, *args):
        return function(*args)


class ModelSequence:
    def __init__(self, responses):
        self.responses = list(responses)

    async def ask(self, messages):
        return self.responses.pop(0)


class PipelineTests(unittest.IsolatedAsyncioTestCase):
    async def test_detail_has_brewing_text_but_list_stays_small(self):
        paths = []

        def handler(request):
            paths.append(request.url.path)
            data = sample_product()
            if request.url.path.endswith("/614"):
                return httpx.Response(200, json=data)
            return httpx.Response(200, json=[data])

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            store = WooCommerceClient(client)
            listing = await store.search_products(search="tea", product_scope="tea")
            detail = await store.get_product(614)
        self.assertNotIn("description", listing["products"][0])
        self.assertIn("3 g per 250 ml", detail["description"])
        self.assertEqual(detail["price"], "45.90")
        self.assertEqual(paths, ["/wp-json/wc/store/v1/products", "/wp-json/wc/store/v1/products/614"])

    async def test_missing_detail_description_does_not_fail_price_lookup(self):
        data = sample_product()
        data.pop("description")
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, json=data))) as client:
            detail = await WooCommerceClient(client).get_product(614)
        self.assertEqual(detail["description"], "")
        self.assertEqual(detail["price"], "45.90")

    async def test_detail_keeps_fixed_host_and_does_not_follow_redirect(self):
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(
            302, headers={"Location": "https://example.com/private"}))) as client:
            with self.assertRaises(RuntimeError):
                await WooCommerceClient(client).get_product(614)

    async def test_internal_knowledge_tool_and_graph_complete_without_public_url(self):
        document = Document(page_content="Approved recipe.", metadata={
            "title": "Approved shop guide", "chunk_id": "one", "source_url": None,
            "source_file": "/private/location/guide.md", "review_status": "approved",
        })
        with patch("app.agent.knowledge_tool.retrieve", return_value=([(document, 0.9)], 0.9)):
            tool = build_knowledge_tool(None, None, FakeCPU())
            model = ModelSequence([
                AIMessage(content="", tool_calls=[{"name": "search_knowledge", "args": {"query": "brewing"}, "id": "one"}]),
                AIMessage(content='{"selections":[{"source_id":"S1","evidence_id":"E1"}]}'),
            ])
            with patch("app.agent.graph.setup_logging"):
                graph = build_graph(model, [tool])
            state = await graph.ainvoke({"messages": [HumanMessage(content="Brewing?")], "sources": []})
        self.assertFalse(state["degraded"])
        self.assertEqual(state["tool_count"], 1)
        self.assertIn("internal guide", state["answer"])
        self.assertEqual(state["sources"], [])
        for message in state["messages"]:
            self.assertNotIn("/private/", message.content)

    async def test_known_product_follow_up_has_brewing_evidence_and_product_source(self):
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, json=sample_product()))) as client:
            tools = build_product_tools(WooCommerceClient(client))
            model = ModelSequence([
                AIMessage(content="", tool_calls=[{"name": "get_product", "args": {"product_id": 614}, "id": "detail"}]),
                AIMessage(content='{"selections":[{"source_id":"S1","evidence_id":"E5"}]}'),
            ])
            with patch("app.agent.graph.setup_logging"):
                graph = build_graph(model, tools)
            state = await graph.ainvoke({"messages": [HumanMessage(content="How should I brew that tea in a mug?")],
                                         "known_product_ids": [614], "sources": []})
        self.assertFalse(state["degraded"])
        self.assertEqual(state["sources"], [{"title": "Example Tea", "url": PRODUCT_URL}])
        tool_messages = []
        for message in state["messages"]:
            if isinstance(message, ToolMessage):
                tool_messages.append(json.loads(message.content))
        units = tool_messages[0]["data"]["evidence_units"]
        self.assertIn("3 g per 250 ml", units[-1]["text"])
        self.assertIn(units[-1]["text"], state["answer"])
        self.assertNotIn("description", tool_messages[0]["data"])

    async def test_product_id_still_must_come_from_a_known_result(self):
        def handler(request):
            raise AssertionError("Unknown product ID must not reach HTTP")

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            model = ModelSequence([
                AIMessage(content="", tool_calls=[{"name": "get_product", "args": {"product_id": 614}, "id": "no-id"}]),
                AIMessage(content="<reply:unavailable>"),
            ])
            with patch("app.agent.graph.setup_logging"):
                graph = build_graph(model, build_product_tools(WooCommerceClient(client)))
            state = await graph.ainvoke({"messages": [HumanMessage(content="Brewing?")], "sources": [], "known_product_ids": []})
        self.assertTrue(state["degraded"])
        self.assertEqual(state["tool_count"], 1)

    async def test_cancelled_knowledge_work_is_not_swallowed(self):
        class CancelledCPU:
            async def run(self, *args):
                raise asyncio.CancelledError()

        tool = build_knowledge_tool(None, None, CancelledCPU())
        with self.assertRaises(asyncio.CancelledError):
            await tool.ainvoke({"query": "brewing"})


if __name__ == "__main__":
    unittest.main()
