# pdf-mcp

[![tests](https://github.com/perdhevi/mcp-pdf/actions/workflows/tests.yml/badge.svg)](https://github.com/perdhevi/mcp-pdf/actions/workflows/tests.yml)
[![M8ven Score](https://m8ven.ai/badge/mcp/perdhevi-mcp-pdf-15dlhs?v=10e470e37d02dfd0fc0185f42e7fdc30)](https://m8ven.ai/mcp/perdhevi-mcp-pdf-15dlhs?s=readme)

Repository: [github.com/perdhevi/pdf-mcp](https://github.com/perdhevi/pdf-mcp). The Python package, CLI command
and MCP server name are all `pdf-mcp`.

## TL;DR: try it in Docker

You need Docker Desktop and [Ollama](https://ollama.com) on the host. Everything else runs in containers.

```powershell
git clone https://github.com/perdhevi/pdf-mcp.git && cd pdf-mcp

# 1. Models (Ollama runs on the host and uses your GPU)
ollama pull bge-m3          # embeddings
ollama pull qwen3-vl:8b     # vision (scanned pages, charts, figures)
ollama pull qwen3:8b        # answer model used by docker-compose.yml (PDF_MCP_LLM_MODEL)

# 2. Load the sample documents (2 customers: ACME, GLOBEX)
xcopy /E /I samples\inbox data\inbox          # macOS/Linux: mkdir -p data && cp -r samples/inbox data/

# 3. Start Qdrant + pdf-mcp
docker compose up -d --build
docker compose logs -f pdf-mcp                  # wait until the sample PDFs are ingested, then Ctrl+C

# 4. Ask something
docker compose exec pdf-mcp pdf-mcp list ACME
docker compose exec pdf-mcp pdf-mcp ask ACME "Which invoices contain the Cisco Catalyst 9200 switch?"

# 5. Explore the MCP tools in a browser
npx @modelcontextprotocol/inspector --config inspector.json
```

### Verify it works

Run each command and compare the result with the expected answer. Each one checks a different part of the pipeline.

```powershell
docker compose exec pdf-mcp pdf-mcp ask ACME "What is the notice period for termination?"
docker compose exec pdf-mcp pdf-mcp ask ACME "How much is invoice INV-4471?"
docker compose exec pdf-mcp pdf-mcp ask ACME "Which quarter had the most support tickets?"
docker compose exec pdf-mcp pdf-mcp ask ACME "Which invoice billed R750 servers?"
docker compose exec pdf-mcp pdf-mcp find ACME "FortiGate firewall"
docker compose exec pdf-mcp pdf-mcp find GLOBEX "zebra invoice 4471"
docker compose exec pdf-mcp pdf-mcp list ACME
```

| # | Expected | Checks | If it fails |
|---|---|---|---|
| 1 | 90 days | Text layer, embeddings, LLM | `docker compose logs pdf-mcp`; is Ollama reachable? |
| 2 | USD 12,600 | OCR of a scanned page | Tesseract / `PDF_MCP_OCR_LANG` |
| 3 | Q2, about 30 tickets | Vision model on a vector chart | `PDF_MCP_VISION_VECTOR_MIN_DRAWINGS` |
| 4 | INV-2026-0101 only (not 0105, which only mentions R750 in a note) | Precision between near-identical invoices | Answer model too small; try a larger `PDF_MCP_LLM_MODEL` |
| 5 | INV-2026-0104, **page 2** (link ends with `#page=2`) | Page citations and file links | |
| 6 | Only GLOBEX documents, with low scores. Never an ACME invoice. | Customer isolation | Must never fail: report it as a bug |
| 7 | 7 documents; `ACME_copy_of_0102.pdf` is not listed | Duplicate detection by content hash | |

The first `ask` can take a minute while Ollama loads the model. More scenarios are in [`samples/README.md`](samples/README.md).

- MCP endpoint: `http://localhost:8765/mcp`
- Qdrant UI: `http://localhost:6333/dashboard`

> [!WARNING]
> **No authentication.** The Docker setup publishes ports 8765 (MCP endpoint and `/files`) and 6333 (Qdrant) on all
> network interfaces. Anyone who can reach this machine can list customers, download any PDF and read Qdrant directly.
> Run it only on a trusted machine or network. See [Security](#security) before exposing it any further.

To use your own documents, drop PDFs into `data\inbox\<CUSTOMER_CODE>\`; they are picked up automatically.
To keep them somewhere else, see [Inbox outside the project folder](#inbox-outside-the-project-folder).
If Ollama can't be reached from the container, see [Troubleshooting](#troubleshooting).

---

An MCP server for **per-customer PDF document management and RAG**.

Drop PDFs into `inbox/<CUSTOMER_CODE>/`. They are parsed (text layer, OCR for scanned pages, and a vision
model for charts and figures), chunked, embedded and stored in a **Qdrant collection per customer**. MCP clients can then:

- **find related documents** for a query, with a link to the original PDF opened at the best page
- **search** passages with page citations, so the calling LLM reasons over them itself
- **ask** questions, answered server-side by your own LLM with `[n]` citations
- **read pages** of a document to interrogate it in depth

Every query is scoped to one `customer_code`, so data never crosses customers.

```
inbox/ACME/contracts/msa.pdf ──► watcher ──► PyMuPDF text ─┬─► chunks ─► embeddings ─► Qdrant  pdfmcp_acme
                                         scanned? Tesseract ┤                          (1 collection / customer)
                                   images/charts? qwen3-vl ─┘   page text ─► SQLite registry
MCP client ──(stdio | streamable HTTP)──► tools ──► retrieve(customer) ──► answer + http://…/files/ACME/<doc_id>#page=N
```

## Stack

| Concern | Choice |
|---|---|
| MCP | official Python SDK **`mcp` 2.x** (`MCPServer`), stdio + streamable HTTP |
| Vector DB | **Qdrant** (server via Docker, or embedded on-disk mode) |
| PDF parsing | **PyMuPDF** |
| OCR | **Tesseract** (`pytesseract`), or the vision model (`OCR_ENGINE=vision`) |
| Vision / LLM | **Ollama** (`qwen3-vl:8b`) or any **OpenAI-compatible** API, configurable per role |
| Embeddings | Ollama `bge-m3` (multilingual, good for Indonesian and English) or OpenAI-compatible |
| Registry | SQLite (documents, status, page text) |
| Watcher | `watchdog` (native events, or polling for Docker on Windows) |

## Quick start on Windows (local `uv` + Qdrant in Docker)

```powershell
git clone https://github.com/perdhevi/pdf-mcp.git
cd pdf-mcp
# 1. Prerequisites
winget install astral-sh.uv
winget install UB-Mannheim.TesseractOCR        # then add Indonesian: tick "ind" in the installer, or copy ind.traineddata
ollama pull bge-m3
ollama pull qwen3-vl:8b
docker compose up -d qdrant                    # Qdrant only

# 2. Install and configure
uv sync
copy .env.example .env                          # set PDF_MCP_TESSERACT_CMD if tesseract.exe is not on PATH
uv run pdf-mcp doctor                           # checks Qdrant, embeddings, LLM, vision, Tesseract

# 3. Add documents and try it
mkdir data\inbox\ACME
copy "D:\some\contract.pdf" data\inbox\ACME\
uv run pdf-mcp reindex
uv run pdf-mcp find ACME "termination clause"
uv run pdf-mcp ask  ACME "What is the notice period for termination?"
```

No Docker? Set `PDF_MCP_QDRANT_PATH=./data/qdrant` to use embedded Qdrant. It allows one process at a
time, so don't run the CLI while the server is running.

### Full stack in Docker

```powershell
ollama pull qwen3:8b                # answer model set in docker-compose.yml
docker compose up -d --build        # Qdrant + pdf-mcp (HTTP) ; Ollama stays on the host
```
Ports 8765 and 6333 are published without authentication; see [Security](#security).
The MCP endpoint is `http://localhost:8765/mcp`. Put PDFs in `.\data\inbox\<CUSTOMER>\` (or your `HOST_INBOX_DIR`); the
container polls the folder. The container reaches the host's Ollama at `http://host.docker.internal:11434`.

Run CLI commands inside the container, not with `uv run` on the host. Otherwise two watchers would process one inbox.

```powershell
docker compose exec pdf-mcp pdf-mcp reindex                          # rescan all customers
docker compose exec pdf-mcp pdf-mcp reindex --customer ACME --force  # re-extract even unchanged files
docker compose exec pdf-mcp pdf-mcp ingest /inbox/ACME/file.pdf      # paths are container paths
```

Code is baked into the image. After pulling or editing code, rebuild with `docker compose up -d --build pdf-mcp`.

### Inbox outside the project folder

The inbox is just a folder with one sub-folder per customer code. It can live anywhere.

**Docker.** Create a `.env` file next to `docker-compose.yml` (or add to the existing one):

```
HOST_INBOX_DIR=D:/Shared/customer-docs
```

Then run `docker compose up -d pdf-mcp`. Compose mounts that folder as `/inbox` in the container. Use forward slashes.
On Docker Desktop for Windows, the drive must be shared (Settings → Resources → File sharing), which is on by default for WSL2.
The SQLite registry stays in `./data`. Documents already indexed from the old location are recognised by content hash and
re-pointed to the new path without being re-embedded.

**Local (`uv run`).** Set the path in `.env`:

```
PDF_MCP_INBOX_DIR=D:/Shared/customer-docs
```

`HOST_INBOX_DIR` is read only by docker compose, and `PDF_MCP_INBOX_DIR` only by pdf-mcp, so both can sit in the same `.env`.
Network shares work, but file-change events are unreliable on them. Keep `PDF_MCP_WATCH_POLLING=true`, or run `reindex` on a schedule.

## Connecting MCP clients

**Claude Desktop / Claude Code (stdio)**. The server is spawned by the client. It also serves file links on port 8765.

```json
{
  "mcpServers": {
    "pdf-mcp": {
      "command": "uv",
      "args": ["--directory", "C:\\path\\to\\pdf-mcp", "run", "pdf-mcp", "serve"]
    }
  }
}
```
Replace `C:\path\to\pdf-mcp` with the folder you cloned into. `.env` is read from the working directory, so with
`uv --directory` it is picked up from the project folder.

**Streamable HTTP** (harness-loop-graph, LangGraph, remote agents):

```powershell
uv run pdf-mcp serve --transport http            # http://127.0.0.1:8765/mcp
```
```python
from mcp import Client
async with Client("http://localhost:8765/mcp") as c:
    r = await c.call_tool("find_documents", {"customer_code": "ACME", "query": "exclusivity East Java"})
```

Run only **one** server per data folder. Ingestion is idempotent, but two watchers on the same inbox duplicate work.
For example, use HTTP for agents and point Claude Desktop at the same HTTP endpoint, or set `PDF_MCP_WATCH_ENABLED=false` in the second instance.

## Security

pdf-mcp is built for a single trusted machine or a trusted internal network. It has **no authentication**.

- **Open HTTP endpoints.** In HTTP mode, `/mcp` and `/files/...` accept any request. `customer_code` keeps customers'
  data apart in queries, but it is not access control: any client can call any customer's tools and download their PDFs.
  The Docker setup also publishes Qdrant (port 6333) without an API key.
  - To keep it on the local machine, bind the ports to localhost in `docker-compose.yml`: `"127.0.0.1:8765:8765"` and
    `"127.0.0.1:6333:6333"`.
  - To share it, put it behind a reverse proxy with authentication (or a VPN), and set `PDF_MCP_QDRANT_API_KEY` plus
    Qdrant's `QDRANT__SERVICE__API_KEY`.
  - Never expose it directly to the internet.
- **`ingest_pdf` reads any PDF the server can access.** When `path` is outside the inbox, the file is copied into
  `inbox/<customer_code>/` and becomes searchable and downloadable. A client could use this to pull in PDFs from
  anywhere on the server's filesystem. Only connect clients you trust, or run the server as a user (or container)
  that can see nothing but the inbox. In Docker, the container sees only its mounted folders.
- **Stdio mode** also starts the file server on `127.0.0.1:8765` so links work. That is local-only by default; keep
  `PDF_MCP_HTTP_HOST=127.0.0.1`.

## Tools

| Tool | Purpose |
|---|---|
| `list_customers()` | Customer codes with document counts and pending jobs |
| `list_documents(customer_code, status?)` | A customer's documents with status, warnings and `file_url` |
| `get_document(customer_code, doc_id)` | Metadata and link for one document |
| `find_documents(customer_code, query, limit=5)` | **Related documents** ranked, with matching pages, best snippet, `file_url_best_page` |
| `search(customer_code, query, limit=8, doc_ids?)` | Passages with page citations, for the client LLM to reason over |
| `ask(customer_code, question, doc_ids?, top_k=8)` | Server-side RAG answer with `[n]` citations and sources |
| `find_reference(customer_code, reference, limit=20)` | **Exact lookup** of an invoice, PO or contract number in one customer's documents |
| `find_reference_all_customers(reference, limit=20)` | Same lookup across **all** customers. Only exposed when `PDF_MCP_ALLOW_GLOBAL_SEARCH=true` |
| `read_pages(customer_code, doc_id, start_page, end_page?)` | Full extracted page text, including OCR and figure descriptions (max 20 pages) |
| `ingest_pdf(path, customer_code?, wait=true, force=false)` | Ingest a file; files outside the inbox are copied into `inbox/<customer_code>/`. Accepts any server path: see [Security](#security) |
| `reindex(customer_code?, force=false)` | Background rescan: new/changed files are ingested and deleted files removed |
| `delete_document(customer_code, doc_id, delete_file=false)` | Remove a document from the index, and optionally delete the file |

HTTP routes: `GET /files/{customer}/{doc_id}/{filename}` (inline PDF; append `#page=N`) and `GET /health`.

## Finding invoice / reference numbers

Semantic search is unreliable for exact identifiers, so a separate full-text index covers every page,
including OCR'd scans and figure descriptions. It uses a SQLite FTS5 trigram index that is kept in sync on ingest and delete,
and is built automatically the first time an existing registry is opened.

- Separators and case are ignored: `INV-2024-0781` = `inv/2024/0781` = `INV20240781`.
- Partial numbers match: `0781` finds `INV-2024-0781`. At least 3 characters are required.
- Short partial numbers can also match longer, unrelated numbers: `0781` matches `10781` too.
  Whole matches rank first: `0781` counts as whole in `INV-2024-0781` (a separator is a boundary), not in `10781`.
- Spaces alone are not treated as separators: `INV 2024 0781` does not match `INV-2024-0781`.
- Each result lists the customer, document, pages, occurrence count, a snippet, and a link to the first matching page.
- Searching across customers is **opt-in**. Set `PDF_MCP_ALLOW_GLOBAL_SEARCH=true` to expose
  `find_reference_all_customers` to MCP clients. The CLI (`pdf-mcp ref INV-2024-0781`) always searches all customers,
  because it is the operator's tool.

```powershell
docker compose exec pdf-mcp pdf-mcp ref INV-2026-0104             # which customers/documents mention it
docker compose exec pdf-mcp pdf-mcp ref 0104 --customer ACME      # partial number, one customer
```

## How customer isolation works

- The customer code is the **first folder under the inbox** (`inbox/ACME/any/depth/file.pdf` → `ACME`). It is case-insensitive and stored in upper case.
- Each customer gets its own Qdrant collection (`pdfmcp_acme`). Queries only ever touch that collection.
- Every query tool requires `customer_code`, except the opt-in `find_reference_all_customers`. The file route checks that the doc belongs to the customer in the URL.
- Files directly in the inbox root, or in folders with invalid names, are ignored.

## Ingestion behaviour

- `doc_id` is the first 16 hex characters of the file's SHA-256, so identical content keeps the same ID.
- **Changed** file: the old vectors are replaced. **Renamed or moved** within the customer: the path is updated without re-embedding.
  **Deleted** file: removed from the index. A **duplicate** of an existing file for the same customer is skipped.
- Pages with fewer than `OCR_MIN_CHARS` characters are OCR'd. The full-page scan image is not also sent to the vision model.
- Embedded images ≥ `VISION_MIN_IMAGE_PX`, and pages with at least `VISION_VECTOR_MIN_DRAWINGS` vector drawing ops (charts drawn as paths, default 12), are described
  by the vision model. The description is indexed as `[Figure n on page p]`. The cap is `VISION_MAX_IMAGES_PER_DOC` per document.
- Changing the embedding model? Run `pdf-mcp reindex --customer ACME --recreate` (vector size changes).

## CLI

```
pdf-mcp serve [--transport stdio|http] [--host H] [--port P] [--no-watch]
pdf-mcp ingest <file|folder> [--customer CODE] [--force]
pdf-mcp reindex [--customer CODE] [--force] [--recreate]
pdf-mcp list [CODE]
pdf-mcp find|search CODE "query" [--limit N]
pdf-mcp ask CODE "question" [--doc DOC_ID ...]
pdf-mcp ref "INV-2024-0781" [--customer CODE]     # exact lookup, all customers by default
pdf-mcp doctor
```

## Configuration

All settings are environment variables prefixed with `PDF_MCP_`, or entries in `.env`. See [`.env.example`](.env.example).
Each model role (`EMBED_`, `LLM_`, `VISION_`) picks its own provider, so you can, for example, keep embeddings on Ollama and use a
cloud model for answers.

## Testing

### Sample documents

[`samples/`](samples/README.md) has PDFs for two customers. They are built to exercise each extraction path and common
retrieval traps:

| Sample | Checks |
|---|---|
| `ACME_test.pdf` p1 / p2 / p3 | Text layer / scanned page (OCR) / vector bar chart (vision) |
| `ACME_INV-2026-0101…0105` | Same template with different items: R750 vs R760, an item only on page 2, an item only mentioned in a note |
| `ACME_INV-2026-0106_scan.pdf` | Scanned invoice: items must be findable via OCR |
| `ACME_copy_of_0102.pdf` | Byte-identical duplicate: must be skipped |
| `GLOBEX/*` | Same items as ACME under another customer: isolation |

Load them with `xcopy /E /I samples\inbox data\inbox` and compare with the expected answers in `samples/README.md`. Quick checks:

```powershell
docker compose exec pdf-mcp pdf-mcp ask  ACME   "Which quarter had the most support tickets?"   # Q2 (~30), via vision
docker compose exec pdf-mcp pdf-mcp find ACME   "FortiGate firewall"                            # INV-2026-0104, page 2
docker compose exec pdf-mcp pdf-mcp find GLOBEX "zebra invoice 4471"                            # nothing: ACME-only text
```

### MCP Inspector

[`inspector.json`](inspector.json) points the Inspector at the Docker endpoint, with a 10-minute request timeout:

```powershell
npx @modelcontextprotocol/inspector --config inspector.json
```

Open the printed URL (it contains an auth token), select **pdf-mcp**, then use the **Tools** tab.
Inspector v2 sets timeouts per server in the JSON file (`requestTimeout`, in ms), not in the UI. Its default of about 60 s is too
short for `ask` on a local 8B model. Use `--catalog inspector.json` instead of `--config` if you want to edit servers in the UI.

### Troubleshooting

| Symptom | Fix |
|---|---|
| `Ollama unreachable at http://host.docker.internal:11434` | Ollama only listens on 127.0.0.1. Run `setx OLLAMA_HOST 0.0.0.0`, restart Ollama, and allow it through the firewall. Test with `docker compose exec pdf-mcp python -c "import urllib.request;print(urllib.request.urlopen('http://host.docker.internal:11434/api/tags').read()[:200])"` |
| New PDFs not picked up | Check that `PDF_MCP_WATCH_POLLING=true` (Windows bind mounts) and that the file is in `<inbox>/<CUSTOMER>/`, not the inbox root. Or run `reindex`. |
| Scanned page not searchable | Tesseract is missing or `PDF_MCP_OCR_LANG` lacks the language. `docker compose exec pdf-mcp pdf-mcp doctor` |
| A chart is answered with axis numbers only | The page didn't reach the vision model. Lower `PDF_MCP_VISION_VECTOR_MIN_DRAWINGS`, then `reindex --customer X --force` |
| `ask` is slow or times out | Run `ollama ps`. If it shows CPU, the model doesn't fit in VRAM. Set `PDF_MCP_OLLAMA_THINK: "false"` and/or `PDF_MCP_LLM_MODEL: qwen3:4b`, and raise the client timeout |
| "Which documents contain X" misses one | Raise `limit` on `search` / `find_documents`; a single document can fill the top chunks |

## Development

```powershell
uv sync --extra dev
uv run pytest
```
The tests use a deterministic fake embedder/LLM, embedded Qdrant and real Tesseract. They cover isolation, OCR, figure description,
change/rename/delete handling, the watcher, the MCP tools and the file route.

## Project layout

```
samples/        sample PDFs (2 customers) + expected answers
inspector.json  MCP Inspector config for the Docker endpoint
src/pdf_mcp/
  config.py     settings (.env / env vars)
  providers.py  Ollama + OpenAI-compatible embed/chat/vision adapters
  extract.py    PyMuPDF text, Tesseract/vision OCR, figure descriptions
  chunking.py   page-bounded overlapping chunks
  registry.py   SQLite: documents + page text
  store.py      Qdrant, one collection per customer
  service.py    ingestion pipeline, background worker, file links
  watcher.py    inbox watcher with debounce
  rag.py        find_documents / search / ask
  server.py     MCP tools, /files route, stdio + HTTP runners
  cli.py        pdf-mcp command
```
