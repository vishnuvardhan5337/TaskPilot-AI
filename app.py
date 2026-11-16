"""
app.py - TaskPilot AI: chat interface + orchestration.

Flow:  chat prompt -> intent_parser -> orchestrator (subprocess: crawler_cli.py)
       -> validate JSON -> summary (Groq LLM if enabled, else extractive) -> chat response

Groq is optional: set GROQ_API_KEY before starting the app. The key stays in this process;
the crawler child process never receives it.
"""
from __future__ import annotations

import json
import re
import uuid
from datetime import datetime
from pathlib import Path

import streamlit as st

import llm_summarizer
from intent_parser import parse_request
from orchestrator import run_crawler, validate_result
from summarizer import summarize

OUTPUT_DIR = Path(__file__).resolve().parent / "outputs"
STATUS_UI = {"success": ("✅", "Completed"), "partial": ("⚠️", "Partial result"), "failed": ("❌", "Failed")}
EXAMPLES = {
    "Mandatory demo (example.com)": "Crawl https://example.com, extract the important information, give me a summary, and return the extracted data in JSON format.",
    "Multi-page crawl": "Summarize https://quotes.toscrape.com and crawl up to 3 pages. Return the data as JSON.",
    "Blocked target (safety demo)": "Crawl http://127.0.0.1 and return JSON",
    "Unreachable site (error demo)": "Crawl https://this-domain-does-not-exist-12345.com and summarize it",
}

st.set_page_config(page_title="TaskPilot AI", page_icon="🧭", layout="wide")

_MD_SPECIAL = re.compile(r"([\\`*_{}\[\]()#+!|<>~$:-])")


def md_escape(text: str) -> str:
    """Website text is untrusted: neutralise markdown (links, images, emoji codes) before display."""
    return _MD_SPECIAL.sub(r"\\\1", text or "")


def render_summary(s: dict, note: str | None = None, title: str = "Summary"):
    """Everything here derives from crawled web text, so it is escaped before display."""
    st.markdown(f"#### {title}")
    if s.get("headline"):
        st.markdown(f"**{md_escape(s['headline'])}**")
    if s.get("answer"):
        st.info(md_escape(s["answer"]))
    for point in s.get("points", []):
        st.markdown(f"- {md_escape(point)}")
    if s.get("key_facts"):
        st.dataframe([{"Fact": f["label"], "Value": f["value"]} for f in s["key_facts"]], hide_index=True)
    if s.get("source") == "groq":
        st.caption(f"✨ Summarised by Groq · `{s.get('model')}` (from the crawled text only)")
    else:
        st.caption("Extractive summary: sentences copied from the crawled pages (no LLM).")
    if note:
        st.caption(f"⚠️ {note}")


def init_state():
    st.session_state.setdefault("messages", [])
    st.session_state.setdefault("history", [])


# --------------------------------------------------------------------------- rendering
def render_payload(p: dict):
    if p["kind"] == "error":
        st.warning(p["text"])
        return
    if p["kind"] == "answer":
        if p["error"]:
            st.warning(f"Groq could not answer: {md_escape(p['error'])}")
        else:
            render_summary(p["summary"], title=f"Answer (from the last crawl of {md_escape(p['site'] or 'the site')})")
        return

    icon, label = STATUS_UI[p["status"]]
    data, intent, run = p["data"], p["intent"], p["run"]
    banner = f"{icon} **{label}** — `{intent['url']}`"
    {"success": st.success, "partial": st.warning, "failed": st.error}[p["status"]](banner)

    if p["problem"]:
        st.markdown(f"**What went wrong:** {md_escape(p['problem'])}")
    for note in intent["notes"]:
        st.caption(f"ℹ️ {note}")

    if p["status"] != "failed" and data:
        if intent["want_summary"] and p["summary"]["points"]:
            render_summary(p["summary"], p.get("summary_note"))
        elif intent["want_summary"]:
            st.info("The pages were crawled but contained too little text to summarise.")

        rows = [{"URL": pg["url"], "Status": pg["status"], "HTTP": pg["http_status"],
                 "Title": pg["title"], "Words": pg["word_count"], "Note": pg["error"] or ""}
                for pg in data["pages"]]
        st.markdown("#### Pages")
        st.dataframe(rows, hide_index=True)

        with st.expander("Extracted data (JSON)", expanded=intent["want_json"]):
            st.code(json.dumps(data, indent=2, ensure_ascii=False), language="json")
            st.download_button("Download JSON", json.dumps(data, indent=2, ensure_ascii=False),
                               file_name=f"taskpilot-{p['id'][:8]}.json", mime="application/json",
                               key=f"dl-{p['id']}")

    with st.expander("Execution details (CLI command, exit code, logs)"):
        c1, c2, c3 = st.columns(3)
        c1.metric("Exit code", "—" if run["exit_code"] is None else run["exit_code"])
        c2.metric("Duration", f"{run['duration']}s")
        c3.metric("Timed out", "yes" if run["timed_out"] else "no")
        st.markdown("**Command executed (no shell, fixed argument list)**")
        st.code(run["command"], language="bash")
        tab_err, tab_out = st.tabs(["stderr (progress log)", "stdout (raw output)"])
        tab_err.code(run["stderr"] or "(empty)", language="text")
        tab_out.code((run["stdout"][:6000] + ("\n… truncated" if len(run["stdout"]) > 6000 else "")) or "(empty)",
                     language="json")
        if p.get("saved"):
            st.caption(f"Saved to `outputs/{p['saved']}`")


# --------------------------------------------------------------------------- task execution
def save_output(run_id: str, intent: dict, run: dict, result: dict) -> str | None:
    if not result["data"]:
        return None
    OUTPUT_DIR.mkdir(exist_ok=True)
    name = f"run-{datetime.now():%Y%m%d-%H%M%S}-{run_id[:6]}.json"
    doc = {"intent": intent, "command": run["command"], "exit_code": run["exit_code"],
           "status": result["status"], "result": result["data"]}
    (OUTPUT_DIR / name).write_text(json.dumps(doc, indent=2, ensure_ascii=False), encoding="utf-8")
    return name


def make_summary(prompt: str, pages: list, wants_summary: bool, use_llm: bool):
    """Groq when enabled and working; otherwise the extractive summariser. Never fails the task."""
    note = None
    if use_llm and wants_summary:
        if llm_summarizer.is_available():
            with st.spinner(f"Summarising with Groq ({llm_summarizer.model_name()})…"):
                try:
                    return llm_summarizer.summarize_with_llm(prompt, pages), None
                except llm_summarizer.LLMError as exc:
                    note = f"Groq summary unavailable ({exc}) Showing the extractive summary instead."
        else:
            note = "GROQ_API_KEY is not set, so the extractive summary is shown."
    extractive = summarize(pages)
    extractive.update(answer=None, key_facts=[], source="extractive", model=None)
    return extractive, note


def handle_followup(prompt: str) -> dict:
    """A question without a URL, answered by Groq from the pages of the last successful crawl."""
    with st.spinner(f"Asking Groq ({llm_summarizer.model_name()}) about the last crawled site…"):
        try:
            return {"kind": "answer", "error": None, "site": st.session_state.get("last_url"),
                    "summary": llm_summarizer.summarize_with_llm(prompt, st.session_state["last_pages"])}
        except llm_summarizer.LLMError as exc:
            return {"kind": "answer", "error": str(exc), "summary": None, "site": st.session_state.get("last_url")}


def handle_prompt(prompt: str, default_pages: int, timeout: int, use_llm: bool = False) -> dict:
    intent = parse_request(prompt, default_pages)
    if intent.error:
        if intent.url is None and prompt.strip() and st.session_state.get("last_pages"):
            if use_llm and llm_summarizer.is_available():
                return handle_followup(prompt)
            return {"kind": "error", "text": intent.error + " (To ask follow-up questions about the last crawl, "
                    "set GROQ_API_KEY and turn on the Groq toggle.)"}
        return {"kind": "error", "text": intent.error}

    run_id = uuid.uuid4().hex
    with st.status("Task accepted — preparing execution", expanded=True) as status:
        st.markdown("**Execution plan**")
        for i, step in enumerate(intent.steps, 1):
            st.markdown(f"{i}. {step}")
        log_box = st.empty()
        log_lines: list = []

        def on_log(_stream, line):
            log_lines.append(line)
            log_box.code("".join(log_lines[-14:]), language="text")

        status.update(label="Running crawler_cli.py …", state="running")
        run = run_crawler(intent.url, intent.max_pages, timeout=timeout, on_log=on_log)
        result = validate_result(run)
        icon, label = STATUS_UI[result["status"]]
        status.update(label=f"{icon} {label} — exit code {run['exit_code']} in {run['duration']}s",
                      state="error" if result["status"] == "failed" else "complete", expanded=False)

    summary, summary_note = {"headline": "", "points": [], "source": None}, None
    if result["status"] != "failed":
        summary, summary_note = make_summary(prompt, result["data"]["pages"], intent.want_summary, use_llm)
        st.session_state["last_pages"] = result["data"]["pages"]
        st.session_state["last_url"] = intent.url
    saved = save_output(run_id, intent.as_dict(), run, result)
    run_view = {k: run[k] for k in ("command", "exit_code", "duration", "timed_out", "stdout", "stderr")}
    return {"kind": "result", "id": run_id, "intent": intent.as_dict(), "run": run_view,
            "status": result["status"], "problem": result["problem"], "data": result["data"],
            "summary": summary, "summary_note": summary_note, "saved": saved}


# --------------------------------------------------------------------------- page
init_state()

with st.sidebar:
    st.title("🧭 TaskPilot AI")
    st.caption("Chat → intent → **real CLI process** → validated result")
    st.subheader("Settings")
    default_pages = st.slider("Default max pages", 1, 10, 3, help="A number in your request (e.g. 'up to 5 pages') overrides this.")
    timeout = st.slider("CLI timeout (seconds)", 10, 180, 120)
    llm_ok = llm_summarizer.is_available()
    use_llm = st.toggle("Use Groq LLM", value=llm_ok, disabled=not llm_ok,
                        help="Summaries and follow-up answers written by a Groq-hosted model from the crawled text. "
                             "Falls back to the extractive summariser if Groq is unavailable.")
    st.caption(f"Model: `{llm_summarizer.model_name()}`" if llm_ok
               else "LLM off: set the GROQ_API_KEY environment variable and restart the app to enable it.")
    st.subheader("Try an example")
    for label, text in EXAMPLES.items():
        if st.button(label, use_container_width=True, key=f"ex-{label}"):
            st.session_state["queued_prompt"] = text
    st.subheader("Task history")
    if not st.session_state["history"]:
        st.caption("No tasks yet.")
    for item in reversed(st.session_state["history"]):
        st.markdown(f"{STATUS_UI[item['status']][0]} `{item['time']}` {item['url']}")
    if st.button("Clear chat"):
        st.session_state["messages"], st.session_state["history"] = [], []
        st.session_state.pop("last_pages", None)
        st.rerun()
    st.divider()
    st.caption("Safeguards: http/https only · public hosts only · same-domain · robots.txt · "
               "page/time/size limits · no shell · minimal child environment.")

st.title("What should I do?")
st.caption("Give me a website and a task. I'll run a crawler CLI and report what actually happened.")

if not st.session_state["messages"]:
    with st.chat_message("assistant"):
        st.markdown("Hi! Try: *Crawl https://example.com, extract the important information, give me a summary, "
                    "and return the extracted data in JSON format.* — or use an example from the sidebar. "
                    "With the Groq LLM on, you can also ask follow-up questions about the last site I crawled.")

for msg in st.session_state["messages"]:
    with st.chat_message(msg["role"]):
        if msg["role"] == "user":
            st.markdown(md_escape(msg["text"]))
        else:
            render_payload(msg["payload"])

prompt = st.chat_input("Describe the task and include a URL…") or st.session_state.pop("queued_prompt", None)
if prompt:
    with st.chat_message("user"):
        st.markdown(md_escape(prompt))
    with st.chat_message("assistant"):
        payload = handle_prompt(prompt, default_pages, timeout, use_llm)
        render_payload(payload)
    # Save the question and answer together, so a cancelled run never leaves an orphan question.
    st.session_state["messages"].append({"role": "user", "text": prompt})
    st.session_state["messages"].append({"role": "assistant", "payload": payload})
    if payload["kind"] in ("result", "answer"):
        st.session_state["history"].append({"time": datetime.now().strftime("%H:%M"),
                                            "url": payload["intent"]["url"] if payload["kind"] == "result" else "(follow-up question)",
                                            "status": payload["status"] if payload["kind"] == "result"
                                            else ("failed" if payload["error"] else "success")})
        st.rerun()
