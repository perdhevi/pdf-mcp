import anyio
from mcp import Client
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from pdf_mcp.server import create_server, file_routes


def test_mcp_tools_end_to_end(app, corpus):
    app.sync()
    server = create_server(app)

    async def main():
        async with Client(server) as client:
            tools = {t.name for t in (await client.list_tools()).tools}
            assert {"list_customers", "list_documents", "find_documents", "search", "ask",
                    "read_pages", "get_document", "ingest_pdf", "reindex", "delete_document"} <= tools

            r = await client.call_tool("list_customers", {})
            codes = {c["customer_code"] for c in r.structured_content["customers"]}
            assert codes == {"ACME", "GLOBEX"}

            r = await client.call_tool("find_documents", {"customer_code": "ACME", "query": "ERP support SLA priority incidents"})
            top = r.structured_content["documents"][0]
            assert top["filename"] == "ACME Master Services Agreement.pdf"

            r = await client.call_tool("read_pages", {"customer_code": "ACME", "doc_id": top["doc_id"]})
            assert "30 minutes" in r.structured_content["pages"][0]["text"]

            r = await client.call_tool("ask", {"customer_code": "ACME", "question": "payment terms?"})
            assert r.structured_content["sources"]

            # Isolation: asking for an ACME doc under GLOBEX fails.
            r = await client.call_tool("read_pages", {"customer_code": "GLOBEX", "doc_id": top["doc_id"]})
            assert r.is_error

            r = await client.call_tool("search", {"customer_code": "bad code!", "query": "x"})
            assert r.is_error

    anyio.run(main)


def test_file_route_serves_original_pdf(app, corpus):
    app.sync()
    rec = app.registry.by_path(str(corpus["acme_nda"].resolve()))
    web = Starlette(routes=[Route(p, e) for p, e in file_routes(app)])
    with TestClient(web) as c:
        url = app.file_url(rec).replace(app.s.base_url, "")
        r = c.get(url)
        assert r.status_code == 200 and r.content[:4] == b"%PDF"
        assert r.headers["content-type"] == "application/pdf"
        # Wrong customer -> 404 (no cross-customer access by guessing doc_ids).
        assert c.get(f"/files/GLOBEX/{rec.doc_id}").status_code == 404
        assert c.get("/health").json()["status"] == "ok"
