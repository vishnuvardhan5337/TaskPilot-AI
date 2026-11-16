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


## Added segment if the Groq LLM is enabled (about 45 seconds, after the multi-page demo)
Start the app with `export GROQ_API_KEY=...` set (do NOT show the key on screen).
1. Point at the sidebar: "The Groq toggle is on and shows the model name."
2. Run the multi-page crawl. Show the summary with the "Summarised by Groq" caption, the answer box and the key facts.
3. Type a follow-up with no URL: `What are the main topics on that site?` and show the answer.
4. Say: "The LLM only sees the crawled text, treats it as untrusted data and can only return a JSON summary. It never chooses commands or URLs. If Groq is down or the key is missing, the app falls back to the extractive summary and says so."
5. Update the disclosure: "Runtime AI: an optional Groq-hosted model; planning and coding help from Claude and ChatGPT."
