# TaskPilot AI — CLI-driven chat execution for website crawling

A chat application that turns a natural-language request ("Crawl https://example.com, summarise it and give me JSON")
into a **real CLI execution**: the backend starts a separate crawler process, monitors it, captures
stdout / stderr / exit code, validates the result and shows it in the chat.

Built for BuildSprint Round 2. The core (crawler CLI, orchestration, extractive summary) is free and fully local.
An **optional Groq-hosted LLM** (free-tier API key) adds better summaries and follow-up questions about the crawled site;
if no key is set, or Groq fails, the app falls back to the local extractive summariser.

## How it works

```mermaid
flowchart LR
    U[User in Streamlit chat] --> I[intent_parser.py<br/>rule-based intent + URL + limits]
    I --> O[orchestrator.py<br/>fixed argument list, no shell,<br/>minimal env, timeout]
    O -- subprocess --> C[crawler_cli.py<br/>separate process]
    C -- stdout: JSON<br/>stderr: log<br/>exit code --> O
    O --> V[validate_result<br/>JSON + exit code check]
    V --> S[summary<br/>Groq LLM if enabled<br/>else extractive summarizer.py]
    S --> R[Chat response:<br/>status, summary, JSON,<br/>execution details]
```

| File | Role |
|---|---|
| `app.py` | Streamlit chat UI and orchestration of one task |
| `intent_parser.py` | Rule-based intent understanding: URL, summary/JSON wanted, page limit |
| `orchestrator.py` | Starts the approved CLI with `subprocess.Popen`, streams logs, enforces timeout, validates output |
| `crawler_cli.py` | **The CLI tool.** Same-domain crawler; prints one JSON document to stdout, logs to stderr |
| `summarizer.py` | Extractive summariser (sentences are copied from the crawled text) — the always-available fallback |
| `llm_summarizer.py` | Optional Groq LLM summary / follow-up answers over the crawled text; validated and length-limited output |
| `tests/test_core.py` | Offline tests (safety checks, parser, crawler, timeout, env isolation) |

### Why a custom CLI?
It keeps the demo free, deterministic and fully explainable: `crawler_cli.py` has a documented contract
(`stdout` = exactly one JSON document, `stderr` = progress log, exit codes `0` success / `2` refused input /
`3` failed / `4` partial). The orchestrator starts it as a **separate process** — it is never imported.

### Is there an LLM?
**Optional, and never in control.**
* The *intent layer* is deterministic (URL / keyword / number extraction). The LLM never decides what gets executed.
* After the crawl, if `GROQ_API_KEY` is set and the sidebar toggle is on, a Groq-hosted model (default `llama-3.3-70b-versatile`,
  override with `GROQ_MODEL`) writes the summary, a direct answer to your question, and key facts **from the crawled text only**.
  You can also ask follow-up questions about the last crawled site.
* If the key is missing, Groq is rate-limited/down, or the model returns malformed output, the app shows the extractive summary
  and says why. The crawl result is never lost because of the LLM.
* The API key is read from the environment, lives only in the app process, is not passed to the crawler child process,
  and is never logged or shown.

## Prerequisites
* Python 3.10+ (check with `python3 --version`)
* Internet access (to reach the site being crawled)

## Setup and run
```bash
git clone <your-repo-url> TaskPilot-AI
cd TaskPilot-AI
python3 -m venv venv
source venv/bin/activate          # Windows: venv\Scripts\activate
pip install -r requirements.txt
streamlit run app.py              # opens http://localhost:8501
```
No environment variables are needed for the core app. To enable the optional Groq LLM:
```bash
export GROQ_API_KEY="your-key-from-console.groq.com"     # never commit this
# optional: export GROQ_MODEL="another-groq-model-id"
python -m streamlit run app.py
```
The sidebar then shows the model name and the **Use Groq LLM** toggle becomes active.

## Try it
1. Click **Mandatory demo (example.com)** in the sidebar, or type:
   `Crawl https://example.com, extract the important information, give me a summary, and return the extracted data in JSON format.`
2. Open **Execution details** to see the exact command, exit code, stderr log and raw stdout.
3. Other prompts: `Summarize https://quotes.toscrape.com and crawl up to 3 pages`,
   `Crawl http://localhost:8501` (blocked), `Crawl https://this-domain-does-not-exist-12345.com` (failure handling).

With the LLM on, ask a follow-up (no URL needed), e.g. `What are the main topics on that site?`

Run the CLI on its own:
```bash
python crawler_cli.py --url https://example.com --max-pages 3 --output json
echo "exit code: $?"
```

Run the tests (offline):
```bash
python -m unittest discover -s tests -v
```

## Execution monitoring and failure handling
* **Status** is `success`, `partial` (some pages failed) or `failed`; the exit code is authoritative and the JSON is cross-checked.
  The app never reports success unless at least one page was really crawled.
* **Timeout:** the orchestrator kills the process after the configured timeout (default 120 s); the CLI also has its own
  90 s time budget and 10 s per-request timeout.
* **Cancellation:** the child is killed in a `finally` block, so pressing Stop in Streamlit does not leave orphan processes.
* **Bad output:** non-JSON or wrongly-shaped output is reported as a failure with the CLI's last stderr line.

## Security and responsible execution
| Risk | Mitigation |
|---|---|
| Shell injection | `shell=False`, fixed argument list; user text is never passed to the CLI — only the extracted URL and a bounded integer |
| SSRF / internal access | http(s) only; host is DNS-resolved and every IP must be public (blocks localhost, private, link-local/cloud-metadata ranges); redirects re-validated each hop; only ports 80/443/8080/8443; no embedded credentials |
| Crawling beyond scope | Same-domain only, default 3 pages (hard max 10), 2 MB per page, 90 s budget, 0.5 s delay between requests |
| robots.txt | Honoured; if robots.txt is unreachable or forbidden (401/403/5xx) the crawl is refused |
| Secrets leakage | Child process gets a minimal environment allow-list (no API keys/credentials inherited) |
| Untrusted web content | Treated as data only; scripts stripped; page text is markdown-escaped before display; it never reaches a shell or alters instructions |
| LLM prompt injection | Crawled text is wrapped as untrusted data and the model is told never to follow instructions in it; it can only return a JSON summary (type-checked, length-limited) and cannot trigger any action, URL fetch or command |
| API key leakage | Key only in the app process environment; excluded from the crawler child's environment; not logged; error messages never contain it |
| Dishonest status | Honest `partial`/`failed` states with the reason shown in chat |

Known limitations: JavaScript-rendered sites are not supported (static HTML only); DNS-rebinding between validation and
connection is not fully eliminated; the summariser is extractive and English-oriented.

## Optional: headless-browser engine (Crawl4AI)
The CLI also supports `--engine crawl4ai` for JavaScript-rendered sites (not used by the app UI, not in `requirements.txt`):
```bash
pip install crawl4ai && crawl4ai-setup
python crawler_cli.py --url https://example.com --max-pages 3 --output json --engine crawl4ai
```
The same network policy is enforced on **every** browser request (navigation, redirects and sub-resources); if the policy
cannot be installed the crawl is refused. Tested locally against a JavaScript-rendered page, including a page script that
tried to reach a blocked host.

## AI tools and services used
* **Claude (Anthropic)** and **ChatGPT (OpenAI)** — used for architecture planning and to help write and test the code.
  I reviewed and can explain every component.
* **Groq API (optional, runtime)** — hosts the LLM used for summaries and follow-up answers when `GROQ_API_KEY` is set.
* Libraries: Streamlit, Requests, BeautifulSoup4 (Crawl4AI optional, CLI only).
