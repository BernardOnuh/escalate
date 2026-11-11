"""Tests for Escalate. Run with: python3 -m unittest -v"""
import json
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import escalate
import mock_backends


class SensitiveDetection(unittest.TestCase):
    def test_detects_secrets_and_personal_data(self):
        cases = {
            "API key": "key is sk-proj-abcdefghijklmnopqrstuvwx",
            "AWS key": "AKIAIOSFODNN7EXAMPLE",
            "GitHub token": "ghp_" + "a" * 36,
            "private key": "-----BEGIN RSA PRIVATE KEY-----",
            "JWT": "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N",
            "email address": "send it to jane.doe@example.com",
            "password": "DB password: hunter2",
            "card number": "card 4111 1111 1111 1111",
        }
        for name, text in cases.items():
            with self.subTest(name):
                self.assertIn(name, escalate.find_sensitive(text))

    def test_ordinary_dev_prompts_are_not_flagged(self):
        for text in [
            "Write a commit message for: fixed typo in README",
            "Why is port 8080 already in use?",
            "Convert getUserProfileById to snake_case",
            "Explain HTTP 429 and retry-after",
            "Sort these: 10, 200, 3000, 40000",
            "Unix timestamp 1731360000 to ISO date",
        ]:
            with self.subTest(text):
                self.assertEqual(escalate.find_sensitive(text), [])

    def test_reads_multipart_message_content(self):
        msgs = [{"role": "user", "content": [{"type": "text", "text": "mail bob@acme.io"}]}]
        self.assertIn("email address", escalate.find_sensitive(escalate.prompt_text(msgs)))


def resp(content="fine", finish="stop", tool_calls=None):
    msg = {"role": "assistant", "content": content}
    if tool_calls:
        msg["tool_calls"] = tool_calls
    return {"choices": [{"message": msg, "finish_reason": finish}]}


class QualityChecks(unittest.TestCase):
    def test_good_answer_passes(self):
        self.assertIsNone(escalate.weak_answer({}, resp("Use git rebase --onto main.")))

    def test_weak_answers_are_caught(self):
        json_req = {"response_format": {"type": "json_object"}}
        cases = [
            ({}, resp(""), "empty answer"),
            ({}, resp("partial", finish="length"), "answer truncated"),
            (json_req, resp('{"a": [1,'), "invalid JSON"),
            ({}, resp("I'm not sure about that."), "model hedged"),
            ({}, {"choices": []}, "malformed response"),
        ]
        for req, r, reason in cases:
            with self.subTest(reason):
                self.assertEqual(escalate.weak_answer(req, r), reason)

    def test_valid_json_and_tool_calls_pass(self):
        self.assertIsNone(escalate.weak_answer({"response_format": {"type": "json_object"}}, resp('{"ok": true}')))
        self.assertIsNone(escalate.weak_answer({}, resp(None, tool_calls=[{"id": "1"}])))


class EndToEnd(unittest.TestCase):
    """Runs the real proxy against the mock local model and mock Otari gateway."""

    @classmethod
    def setUpClass(cls):
        cls.saved_cfg = dict(escalate.CFG)
        cls.local_url, cls.otari_url = mock_backends.start()
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), escalate.Handler)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.base = "http://127.0.0.1:%d" % cls.server.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        escalate.CFG.clear()
        escalate.CFG.update(cls.saved_cfg)

    def setUp(self):
        escalate.CFG.update(local_url=self.local_url, otari_url=self.otari_url, otari_key="tk-test",
                            local_model="mock-local", cloud_model="mock:cloud", timeout=5)
        escalate.STATS.reset()

    def chat(self, prompt, model="auto", headers=None, **extra):
        body = dict({"model": model, "messages": [{"role": "user", "content": prompt}]}, **extra)
        req = urllib.request.Request(self.base + "/v1/chat/completions", data=json.dumps(body).encode(),
                                     headers=dict({"Content-Type": "application/json"}, **(headers or {})))
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, r.headers["X-Escalate-Route"], r.headers["X-Escalate-Reason"], json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, e.headers["X-Escalate-Route"], e.headers["X-Escalate-Reason"], json.loads(e.read())

    def test_easy_prompt_stays_local(self):
        status, route, reason, body = self.chat("Rename x to user_count")
        self.assertEqual((status, route), (200, "local"))
        self.assertEqual(body["model"], "mock-local")

    def test_hard_prompt_escalates_to_otari(self):
        status, route, reason, body = self.chat("Design a distributed rate limiter")
        self.assertEqual((status, route, reason), (200, "cloud", "escalated: model hedged"))
        self.assertEqual(body["model"], "mock:cloud")

    def test_broken_json_escalates(self):
        status, route, reason, body = self.chat("Return nested JSON", response_format={"type": "json_object"})
        self.assertEqual((route, reason), ("cloud", "escalated: invalid JSON"))
        json.loads(body["choices"][0]["message"]["content"])

    def test_sensitive_hard_prompt_never_leaves_device(self):
        status, route, reason, _ = self.chat("Design a distributed cache. Contact bob@acme.io")
        self.assertEqual((status, route), (200, "local"))
        self.assertIn("private", reason)

    def test_forced_cloud_still_blocks_secrets(self):
        status, route, reason, _ = self.chat("OPENAI_KEY=sk-proj-abcdefghijklmnopqrstuvwx", model="cloud")
        self.assertEqual(route, "local")
        self.assertIn("cloud blocked", reason)

    def test_forced_routes(self):
        self.assertEqual(self.chat("hi", model="cloud")[1:3], ("cloud", "forced cloud"))
        self.assertEqual(self.chat("Prove P != NP", headers={"X-Escalate": "local"})[1:3], ("local", "forced local"))

    def test_local_down_falls_back_to_cloud(self):
        escalate.CFG["local_url"] = "http://127.0.0.1:9/v1"
        status, route, reason, _ = self.chat("hello")
        self.assertEqual((status, route, reason), (200, "cloud", "escalated: local model unavailable"))

    def test_local_down_with_secret_refuses_instead_of_leaking(self):
        escalate.CFG["local_url"] = "http://127.0.0.1:9/v1"
        status, route, _, body = self.chat("password: hunter2")
        self.assertEqual((status, route), (503, "error"))
        self.assertIn("cannot leave the device", body["error"]["message"])

    def test_otari_rejection_is_passed_through(self):
        escalate.CFG["otari_key"] = ""
        status, route, reason, _ = self.chat("Design a distributed queue")
        self.assertEqual((status, route, reason), (401, "error", "otari HTTP 401"))

    def test_stats_and_dashboard(self):
        self.chat("Rename x")
        self.chat("Design a distributed lock")
        self.chat("mail jane@example.com")
        s = json.loads(urllib.request.urlopen(self.base + "/stats").read())
        self.assertEqual((s["requests"], s["local"], s["cloud"], s["private_kept"]), (3, 2, 1, 1))
        self.assertGreater(s["saved_usd"], 0)
        self.assertAlmostEqual(s["all_cloud_usd"], s["saved_usd"] + s["cloud_spend_usd"], places=6)
        page = urllib.request.urlopen(self.base + "/").read()
        self.assertIn(b"<title>Escalate</title>", page)

    def test_rejects_bad_json(self):
        req = urllib.request.Request(self.base + "/v1/chat/completions", data=b"{nope")
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(req)
        self.assertEqual(ctx.exception.code, 400)


if __name__ == "__main__":
    unittest.main()
