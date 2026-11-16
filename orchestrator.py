"""
orchestrator.py - runs the approved CLI as a separate process and monitors it.

Security properties
    * Only ONE approved program is ever started: crawler_cli.py via the current Python.
    * The command is a fixed argument list (shell=False); user text never reaches a shell.
    * The child gets a minimal environment, so API keys/credentials in the parent's
      environment are not inherited and can't leak into logs or chat.
    * Hard timeout; the process is killed on timeout, on cancellation, and on any error.
"""
from __future__ import annotations

import json
import os
import queue
import shlex
import subprocess
import sys
import threading
import time
from pathlib import Path

CLI_PATH = Path(__file__).resolve().with_name("crawler_cli.py")
SAFE_ENV_KEYS = ("PATH", "HOME", "LANG", "LC_ALL", "SYSTEMROOT", "TMPDIR",
                 "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "no_proxy",
                 "SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "PLAYWRIGHT_BROWSERS_PATH")
ENGINES = ("requests", "crawl4ai")
EXIT_STATUS = {0: "success", 4: "partial"}


def build_command(url: str, max_pages: int, engine: str = "requests") -> list:
    if engine not in ENGINES:  # never let free text choose what gets executed
        raise ValueError(f"unsupported engine {engine!r}")
    return [sys.executable, str(CLI_PATH), "--url", url, "--max-pages", str(int(max_pages)),
            "--output", "json", "--engine", engine]


def display_command(cmd: list) -> str:
    shown = ["python"] + [CLI_PATH.name if part == str(CLI_PATH) else part for part in cmd[1:]]
    return shlex.join(shown)


def run_crawler(url: str, max_pages: int, timeout: float = 120, on_log=None, cmd: list | None = None,
                engine: str = "requests", cancel_event=None) -> dict:
    """Start the CLI, stream its stderr to `on_log(stream, line)`, return a run record.

    `cancel_event` (threading.Event) lets another thread stop the run: the process is killed.
    """
    cmd = cmd or build_command(url, max_pages, engine)
    env = {k: v for k, v in os.environ.items() if k in SAFE_ENV_KEYS}
    env.update(PYTHONUNBUFFERED="1", PYTHONIOENCODING="utf-8")
    record = {"command": display_command(cmd), "exit_code": None, "stdout": "", "stderr": "",
              "duration": 0.0, "timed_out": False, "cancelled": False, "spawn_error": None}
    started = time.monotonic()
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                encoding="utf-8", errors="replace", env=env, shell=False)
    except OSError as exc:
        record["spawn_error"] = f"could not start the CLI: {exc}"
        return record

    lines: "queue.Queue" = queue.Queue()
    out_parts, err_parts = [], []

    def pump(stream, tag):
        for line in iter(stream.readline, ""):
            lines.put((tag, line))
        lines.put((tag, None))

    threads = [threading.Thread(target=pump, args=(proc.stdout, "stdout"), daemon=True),
               threading.Thread(target=pump, args=(proc.stderr, "stderr"), daemon=True)]
    for t in threads:
        t.start()

    open_streams = 2
    try:
        while open_streams:
            if cancel_event is not None and cancel_event.is_set():
                record["cancelled"] = True
                proc.kill()
                break
            if time.monotonic() - started > timeout:
                record["timed_out"] = True
                proc.kill()
                break
            try:
                tag, line = lines.get(timeout=0.1)
            except queue.Empty:
                continue
            if line is None:
                open_streams -= 1
                continue
            (out_parts if tag == "stdout" else err_parts).append(line)
            if tag == "stderr" and on_log:
                on_log(tag, line)
        proc.wait(timeout=5)
    finally:  # also runs if Streamlit cancels the script mid-run
        if proc.poll() is None:
            proc.kill()
            proc.wait()
    for t in threads:
        t.join(timeout=1)
    for stream in (proc.stdout, proc.stderr):
        try:
            stream.close()
        except Exception:
            pass
    while not lines.empty():  # drain anything left
        tag, line = lines.get_nowait()
        if line:
            (out_parts if tag == "stdout" else err_parts).append(line)

    record.update(exit_code=proc.returncode, stdout="".join(out_parts), stderr="".join(err_parts),
                  duration=round(time.monotonic() - started, 2))
    return record


def validate_result(run: dict) -> dict:
    """Turn a raw run record into {status, data, problem}. Never claims success without evidence."""
    if run.get("spawn_error"):
        return {"status": "failed", "data": None, "problem": run["spawn_error"]}
    if run.get("cancelled"):
        return {"status": "failed", "data": None, "problem": "The task was cancelled; the crawler process was stopped."}
    if run.get("timed_out"):
        return {"status": "failed", "data": None,
                "problem": f"The crawler did not finish in time and was stopped after {run['duration']}s."}
    try:
        data = json.loads(run.get("stdout", ""))
    except (json.JSONDecodeError, TypeError):
        tail = (run.get("stderr") or "").strip().splitlines()[-1:] or ["no error output"]
        return {"status": "failed", "data": None,
                "problem": f"The CLI exited with code {run.get('exit_code')} and did not return valid JSON ({tail[0][:200]})."}
    if not isinstance(data, dict) or not isinstance(data.get("pages"), list) or "status" not in data:
        return {"status": "failed", "data": None, "problem": "The CLI output was valid JSON but not in the expected format."}

    status = EXIT_STATUS.get(run.get("exit_code"), "failed")  # exit code is authoritative
    problem = data.get("error") if status == "failed" else None
    if status != "failed" and not any(p.get("status") == "ok" for p in data["pages"]):
        status, problem = "failed", "The CLI reported success but no page was actually crawled."
    if status != data.get("status"):
        problem = problem or f"Exit code ({run.get('exit_code')}) disagrees with reported status ({data.get('status')!r})."
    return {"status": status, "data": data, "problem": problem}
