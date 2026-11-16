"""App-level tests (Streamlit AppTest, no network, Groq mocked): python -m unittest tests.test_app -v"""
import http.server
import sys
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import llm_summarizer  # noqa: E402
import orchestrator  # noqa: E402
import test_core  # noqa: E402
from streamlit.testing.v1 import AppTest  # noqa: E402

FAKE = {"headline": "Candle shop", "points": ["Sells handmade soy candles."], "answer": "Soy candles.",
        "key_facts": [{"label": "Product", "value": "Candles"}], "source": "groq", "model": "fake-model"}


class AppTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), test_core.Handler)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{cls.server.server_address[1]}/"
        code = (f"import sys; sys.path.insert(0, {str(ROOT)!r}); import crawler_cli as c; "
                f"r = c.crawl({base!r}, 3, delay=0, validator=lambda u: u); c.emit(r); "
                "sys.exit({'success': 0, 'partial': 4}.get(r['status'], 3))")
        cls._orig = (orchestrator.build_command, llm_summarizer.is_available, llm_summarizer.summarize_with_llm)
        # Crawl the local test server for "example.com" prompts; everything else uses the real command.
        real = orchestrator.build_command
        orchestrator.build_command = lambda url, n, engine="requests": (
            [sys.executable, "-c", code] if "example.com" in url else real(url, n, engine))

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        orchestrator.build_command, llm_summarizer.is_available, llm_summarizer.summarize_with_llm = cls._orig

    def tearDown(self):
        llm_summarizer.is_available, llm_summarizer.summarize_with_llm = self._orig[1], self._orig[2]

    def run_prompt(self, *prompts):
        at = AppTest.from_file(str(ROOT / "app.py"), default_timeout=90).run()
        for prompt in prompts:
            at.chat_input[0].set_value(prompt).run()
            self.assertFalse(at.exception, at.exception)
        return at

    def test_llm_summary_used_and_followup_answered(self):
        llm_summarizer.is_available = lambda: True
        calls = []

        def fake(prompt, pages, **kw):
            calls.append((prompt, len(pages)))
            return dict(FAKE)
        llm_summarizer.summarize_with_llm = fake
        at = self.run_prompt("Crawl https://example.com and summarize it", "what do they sell?")
        msgs = [m for m in at.session_state["messages"] if m["role"] == "assistant"]
        self.assertEqual(msgs[0]["payload"]["summary"]["source"], "groq")
        self.assertEqual(msgs[0]["payload"]["status"], "success")
        self.assertEqual(msgs[1]["payload"]["kind"], "answer")
        self.assertEqual(msgs[1]["payload"]["summary"]["answer"], "Soy candles.")
        self.assertEqual(calls[1][0], "what do they sell?")
        self.assertGreaterEqual(calls[1][1], 1)

    def test_llm_failure_falls_back_to_extractive(self):
        llm_summarizer.is_available = lambda: True

        def boom(prompt, pages, **kw):
            raise llm_summarizer.LLMError("Groq rate limit reached (429).")
        llm_summarizer.summarize_with_llm = boom
        at = self.run_prompt("Crawl https://example.com and summarize it")
        payload = at.session_state["messages"][-1]["payload"]
        self.assertEqual(payload["status"], "success")
        self.assertEqual(payload["summary"]["source"], "extractive")
        self.assertIn("429", payload["summary_note"])
        self.assertTrue(payload["summary"]["points"])

    def test_without_key_no_llm_and_followup_explains(self):
        llm_summarizer.is_available = lambda: False
        called = []
        llm_summarizer.summarize_with_llm = lambda *a, **k: called.append(1)
        at = self.run_prompt("Crawl https://example.com and summarize it", "what do they sell?")
        msgs = [m["payload"] for m in at.session_state["messages"] if m["role"] == "assistant"]
        self.assertEqual(msgs[0]["summary"]["source"], "extractive")
        self.assertEqual(msgs[1]["kind"], "error")
        self.assertIn("GROQ_API_KEY", msgs[1]["text"])
        self.assertEqual(called, [])  # the LLM is never called when disabled

    def test_failed_crawl_never_calls_llm(self):
        llm_summarizer.is_available = lambda: True
        called = []
        llm_summarizer.summarize_with_llm = lambda *a, **k: called.append(1) or dict(FAKE)
        at = self.run_prompt("Crawl http://127.0.0.1 and summarize it")
        self.assertEqual(at.session_state["messages"][-1]["payload"]["status"], "failed")
        self.assertEqual(called, [])


if __name__ == "__main__":
    unittest.main()
