"""Offline tests: python -m unittest discover -s tests -v"""
import http.server
import json
import sys
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import crawler_cli  # noqa: E402
from intent_parser import parse_request  # noqa: E402
from orchestrator import run_crawler, validate_result  # noqa: E402
from summarizer import summarize  # noqa: E402

PAGES = {
    "/": '<html><head><title>Home</title><meta name="description" content="A tiny demo shop selling handmade candles."></head>'
         '<body><h1>Welcome</h1><p>We sell handmade candles made from natural soy wax.</p>'
         '<a href="/about">About</a><a href="/secret">Secret</a><a href="https://other.example/x">Other</a>'
         '<script>alert(1)</script></body></html>',
    "/about": "<html><head><title>About</title></head><body><h2>Our story</h2>"
              "<p>The shop was founded in 2020 by two friends who love candles.</p><a href=\"/\">Home</a></body></html>",
    "/spans": '<html><title>Quotes</title><body><div class="quote"><span class="text">The world as we have created it is a process of our thinking.</span>'
              '<small>Einstein</small></div><div class="quote"><span class="text">It is our choices that show what we truly are, far more than our abilities.</span></div>'
              '<a href="/login">Login</a><a href="/page/2/">Next</a></body></html>',
    "/secret": "<html><title>Secret</title><body><p>This page must never be crawled by well-behaved bots.</p></body></html>",
}


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/robots.txt":
            body, ctype = b"User-agent: *\nDisallow: /secret\n", "text/plain"
        elif self.path in PAGES:
            body, ctype = PAGES[self.path].encode(), "text/html; charset=utf-8"
        else:
            self.send_response(404); self.end_headers(); return
        self.send_response(200); self.send_header("Content-Type", ctype); self.end_headers(); self.wfile.write(body)

    def log_message(self, *a):
        pass


class IntentTests(unittest.TestCase):
    def test_mandatory_prompt(self):
        i = parse_request("Crawl https://example.com, extract the important information, give me a summary, and return the extracted data in JSON format.")
        self.assertEqual(i.url, "https://example.com")
        self.assertTrue(i.want_summary and i.want_json)
        self.assertEqual(i.max_pages, 3)

    def test_pages_number_and_clamp(self):
        self.assertEqual(parse_request("crawl https://a.com up to five pages").max_pages, 5)
        self.assertEqual(parse_request("crawl https://a.com 50 pages").max_pages, 10)
        self.assertEqual(parse_request("just this page https://a.com").max_pages, 1)

    def test_bare_domain_and_missing_url(self):
        self.assertEqual(parse_request("summarize example.org please").url, "https://example.org")
        self.assertIsNotNone(parse_request("summarize the internet").error)
        self.assertIsNotNone(parse_request("").error)

    def test_json_only(self):
        i = parse_request("crawl https://a.com and return JSON")
        self.assertTrue(i.want_json and not i.want_summary)


class SafetyTests(unittest.TestCase):
    def test_blocked(self):
        for bad in ["http://localhost", "http://127.0.0.1", "http://10.0.0.5", "http://192.168.1.1",
                    "http://169.254.169.254/latest/meta-data", "http://[::1]/", "file:///etc/passwd",
                    "ftp://example.com", "http://user:pw@example.com", "http://example.com:22",
                    "http://2130706433/", "http://foo.internal", ""]:
            with self.subTest(url=bad), self.assertRaises(crawler_cli.BlockedURL):
                crawler_cli.validate_url(bad)

    def test_cli_refuses_and_still_returns_json(self):
        run = run_crawler("http://127.0.0.1:8080", 3, timeout=30)
        self.assertEqual(run["exit_code"], 2)
        result = validate_result(run)
        self.assertEqual(result["status"], "failed")
        self.assertIn("refused", result["problem"].lower())

    def test_child_env_has_no_secrets(self):
        import os
        os.environ["SECRET_TEST_KEY"] = "hunter2"
        run = run_crawler("", 1, timeout=30, cmd=[sys.executable, "-c", "import os;print(os.environ.get('SECRET_TEST_KEY'))"])
        self.assertEqual(run["stdout"].strip(), "None")

    def test_timeout_kills_process(self):
        run = run_crawler("", 1, timeout=1, cmd=[sys.executable, "-c", "import time;time.sleep(30)"])
        self.assertTrue(run["timed_out"])
        self.assertEqual(validate_result(run)["status"], "failed")

    def test_garbage_output_is_failure(self):
        run = run_crawler("", 1, timeout=10, cmd=[sys.executable, "-c", "print('not json')"])
        self.assertEqual(validate_result(run)["status"], "failed")


class CrawlTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}/"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def test_crawl_respects_robots_and_domain(self):
        res = crawler_cli.crawl(self.base, 5, delay=0, validator=lambda u: u)
        self.assertEqual(res["status"], "success")
        urls = {p["url"]: p for p in res["pages"]}
        self.assertEqual(urls[self.base]["title"], "Home")
        self.assertEqual(urls[self.base]["status"], "ok")
        self.assertEqual(urls[self.base + "secret"]["status"], "skipped")
        self.assertEqual(urls[self.base + "about"]["headings"][0]["text"], "Our story")
        self.assertNotIn("alert", json.dumps(res))          # scripts stripped
        self.assertTrue(all("other.example" not in p["url"] for p in res["pages"]))

    def test_partial_when_a_link_404s(self):
        PAGES["/broken-home"] = '<html><title>H</title><body><p>Some reasonably long paragraph of text.</p><a href="/missing">m</a></body></html>'
        res = crawler_cli.crawl(self.base + "broken-home", 3, delay=0, validator=lambda u: u)
        self.assertEqual(res["status"], "partial")

    def test_text_in_spans_and_login_not_followed(self):
        res = crawler_cli.crawl(self.base + "spans", 5, delay=0, validator=lambda u: u)
        first = res["pages"][0]
        self.assertGreaterEqual(len(first["paragraphs"]), 2)       # text found without <p> tags
        self.assertIn("world as we have created", " ".join(first["paragraphs"]))
        self.assertFalse(any("/login" in p["url"] for p in res["pages"]))
        self.assertFalse(any("/login" in l for l in first["internal_links"]))

    def test_same_page_not_crawled_twice(self):
        res = crawler_cli.crawl(self.base.rstrip("/"), 5, delay=0, validator=lambda u: u)
        homes = [p for p in res["pages"] if p["title"] == "Home"]
        self.assertEqual(len(homes), 1)
        self.assertEqual(len({crawler_cli.url_key(p["url"]) for p in res["pages"]}), len(res["pages"]))

    def test_trim_and_tag_lists(self):
        self.assertTrue(crawler_cli.trim("word " * 200, 50).endswith("…"))
        self.assertFalse(crawler_cli.trim("word " * 200, 50)[:-1].endswith("wor"))
        pages = [{"status": "ok", "url": "http://x.com/", "title": "X", "description": "",
                  "paragraphs": ["Tags: change deep-thoughts thinking world",
                                 "The shop sells handmade candles and soaps."], "headings": []}]
        self.assertEqual(summarize(pages)["points"], ["The shop sells handmade candles and soaps."])

    def test_summary_comes_from_crawled_text(self):
        res = crawler_cli.crawl(self.base, 3, delay=0, validator=lambda u: u)
        summary = summarize(res["pages"])
        text = " ".join(summary["points"]).lower()
        self.assertTrue("candles" in text)
        corpus = json.dumps(res).lower()
        for point in summary["points"]:
            self.assertIn(point.lower()[:30], corpus)


if __name__ == "__main__":
    unittest.main()
