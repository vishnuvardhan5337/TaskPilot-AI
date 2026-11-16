# Video script (target 5–6 minutes)

**Setup before recording:** terminal visible next to the browser, venv active, `streamlit run app.py` running, app open.
Test the mandatory prompt once beforehand so you know it works on your network.

1. **Intro (0:30)** — "AI chat apps usually just send text to an LLM. TaskPilot AI turns a request into a real, controlled CLI execution and reports exactly what happened."
2. **Architecture (1:00)** — show the diagram in README. Walk: chat → intent parser → orchestrator → `crawler_cli.py` as a separate process → stdout/stderr/exit code → JSON validation → summary. Say why a custom CLI (deterministic, free, explainable, documented contract).
3. **Prompt → CLI (0:45)** — open `intent_parser.py` briefly: URL extraction, "up to five pages", summary/JSON flags. Open `orchestrator.py`: show the `Popen` argument list, `shell=False`, the env allow-list, the timeout.
4. **Live demo (1:30)** — click the mandatory example. Point at the execution plan, the live log lines, the ✅ status, the summary, the JSON. Open **Execution details**: command, exit code 0, stderr, raw stdout. Show the same command run manually in the terminal.
5. **Second site (0:30)** — "Summarize https://quotes.toscrape.com and crawl up to 3 pages" to show multi-page crawling.
6. **Edge cases (0:45)** — run `Crawl http://localhost:8501` (blocked, exit code 2) and the non-existent domain (honest failure). Mention timeout/kill, partial status, robots.txt.
7. **Stack + AI disclosure (0:30)** — Python, Streamlit, Requests, BeautifulSoup, subprocess. "I used Claude and ChatGPT for planning and coding help; no LLM API is called at runtime; the AI layer is rule-based plus an extractive summariser, which I chose deliberately for safety and zero cost."
