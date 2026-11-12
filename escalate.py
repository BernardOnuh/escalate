#!/usr/bin/env python3
"""Escalate: a local-first LLM router that sits in front of the Otari gateway.

Every chat request is tried on a local model first. It is escalated to a cloud
model through Otari only when the local answer looks weak, and prompts that
contain secrets or personal data never leave the machine.

Zero dependencies: Python 3.9+ standard library only.
"""
import argparse
import json
import os
import re
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent

CFG = {
    "port": int(os.environ.get("ESCALATE_PORT", "8787")),
    "local_url": os.environ.get("LOCAL_BASE_URL", "http://localhost:11434/v1"),
    "local_model": os.environ.get("LOCAL_MODEL", "llama3.2:3b"),
    "otari_url": os.environ.get("OTARI_BASE_URL", "http://localhost:8000/api/v1"),
    "otari_key": os.environ.get("OTARI_API_KEY", ""),
    "cloud_model": os.environ.get("CLOUD_MODEL", "openai:gpt-4o-mini"),
    # USD per 1M tokens for the cloud model, used for the savings estimate.
    "price_in": float(os.environ.get("CLOUD_PRICE_IN", "0.15")),
    "price_out": float(os.environ.get("CLOUD_PRICE_OUT", "0.60")),
    "timeout": float(os.environ.get("ESCALATE_TIMEOUT", "120")),
    # Ask the local model to rate its own answer; escalate below this score (1-5).
    "self_check": os.environ.get("ESCALATE_SELF_CHECK", "1") != "0",
    "min_confidence": int(os.environ.get("ESCALATE_MIN_CONFIDENCE", "4")),
    # Prompts longer than this skip the local model and go straight to the cloud.
    "heavy_chars": int(os.environ.get("ESCALATE_HEAVY_CHARS", "2000")),
}

# Patterns that keep a prompt on-device no matter what.
SENSITIVE = [
    ("private key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("API key", re.compile(r"\b(?:sk|tk|pk|rk)-[A-Za-z0-9_\-]{16,}")),
    ("AWS key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("GitHub token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}")),
    ("JWT", re.compile(r"\beyJ[\w-]{10,}\.[\w-]{10,}\.[\w-]{10,}")),
    ("email address", re.compile(r"\b[\w.+-]+@[\w-]+\.[A-Za-z]{2,}\b")),
    ("password", re.compile(r"(?i)\b(?:password|passwd|secret)\s*[:=]\s*\S+")),
    ("card number", re.compile(r"\b(?:\d[ -]?){15,16}\b")),
]

# Phrases that suggest the local model could not really answer.
HEDGES = re.compile(
    r"(?i)\b(?:i (?:don't|do not) know|i'm not sure|i am not sure|"
    r"i (?:can't|cannot) (?:help|answer)|beyond my (?:knowledge|abilities))"
)

# Tasks a small local model reliably does badly, so they skip it entirely.
HEAVY = re.compile(
    r"(?i)\b(design (?:a|an|the)|architecture|prove|proof|trade-?offs?|distributed|"
    r"step[- ]by[- ]step|threat model|migration plan)\b"
)

SELF_CHECK_PROMPT = (
    "On a scale of 1 to 5, how confident are you that your previous answer is "
    "correct and complete? Reply with only the number."
)


def prompt_text(messages):
    parts = []
    for m in messages or []:
        content = m.get("content")
        if isinstance(content, str):
            parts.append(content)
        elif isinstance(content, list):
            parts.extend(p.get("text", "") for p in content if isinstance(p, dict))
    return "\n".join(parts)


def find_sensitive(text):
    return [name for name, rx in SENSITIVE if rx.search(text)]


def weak_answer(req, resp):
    """Return a reason string if the local answer should be escalated, else None."""
    try:
        choice = resp["choices"][0]
    except (KeyError, IndexError, TypeError):
        return "malformed response"
    msg = choice.get("message") or {}
    text = msg.get("content") or ""
    if msg.get("tool_calls"):
        return None
    if not text.strip():
        return "empty answer"
    if choice.get("finish_reason") == "length":
        return "answer truncated"
    fmt = (req.get("response_format") or {}).get("type")
    if fmt in ("json_object", "json_schema"):
        try:
            json.loads(text)
        except ValueError:
            return "invalid JSON"
    if HEDGES.search(text):
        return "model hedged"
    return None


def heavy_task(text):
    """Return why a prompt is too heavy for the local model, else None."""
    if len(text) > CFG["heavy_chars"]:
        return "long prompt"
    m = HEAVY.search(text)
    return m.group(1).lower() if m else None


def confidence(req, resp):
    """Ask the local model to score its own answer 1-5. None if it can't say."""
    msg = resp["choices"][0].get("message") or {}
    if msg.get("tool_calls") or not CFG["self_check"]:
        return None
    body = {
        "model": CFG["local_model"], "stream": False, "temperature": 0, "max_tokens": 4,
        "messages": list(req.get("messages") or []) + [
            {"role": "assistant", "content": msg.get("content") or ""},
            {"role": "user", "content": SELF_CHECK_PROMPT},
        ],
    }
    try:
        reply = call_upstream(CFG["local_url"], "", body)["choices"][0]["message"]["content"] or ""
    except (urllib.error.URLError, OSError, ValueError, KeyError, IndexError, TypeError):
        return None
    m = re.search(r"[1-5]", reply)
    return int(m.group()) if m else None


def call_upstream(base_url, key, body):
    req = urllib.request.Request(
        base_url.rstrip("/") + "/chat/completions",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
    )
    if key:
        req.add_header("Authorization", "Bearer " + key)
    with urllib.request.urlopen(req, timeout=CFG["timeout"]) as resp:
        return json.loads(resp.read())


def cost(usage):
    usage = usage or {}
    return (usage.get("prompt_tokens", 0) * CFG["price_in"]
            + usage.get("completion_tokens", 0) * CFG["price_out"]) / 1e6


class Stats:
    def __init__(self):
        self.lock = threading.Lock()
        self.reset()

    def reset(self):
        with self.lock:
            self.requests = 0
            self.local = 0
            self.cloud = 0
            self.private_kept = 0
            self.errors = 0
            self.cloud_spend = 0.0
            self.saved = 0.0
            self.local_tokens = 0
            self.cloud_tokens = 0
            self.events = deque(maxlen=50)

    def record(self, route, reason, usage, latency, sensitive):
        tokens = (usage or {}).get("total_tokens", 0)
        with self.lock:
            self.requests += 1
            if route == "local":
                self.local += 1
                self.saved += cost(usage)
                self.local_tokens += tokens
            elif route == "cloud":
                self.cloud += 1
                self.cloud_spend += cost(usage)
                self.cloud_tokens += tokens
            else:
                self.errors += 1
            if sensitive:
                self.private_kept += 1
            self.events.appendleft({
                "t": time.time(), "route": route, "reason": reason,
                "tokens": tokens, "ms": int(latency * 1000),
                "sensitive": sensitive,
            })

    def snapshot(self):
        with self.lock:
            return {
                "requests": self.requests, "local": self.local, "cloud": self.cloud,
                "errors": self.errors, "private_kept": self.private_kept,
                "cloud_spend_usd": round(self.cloud_spend, 6),
                "saved_usd": round(self.saved, 6),
                "all_cloud_usd": round(self.cloud_spend + self.saved, 6),
                "local_tokens": self.local_tokens, "cloud_tokens": self.cloud_tokens,
                "local_model": CFG["local_model"], "cloud_model": CFG["cloud_model"],
                "events": list(self.events),
            }


STATS = Stats()


def route(req, mode):
    """Run one chat request. Returns (status, body, route, reason, sensitive)."""
    body = dict(req)
    body["stream"] = False
    text = prompt_text(req.get("messages"))
    sensitive = find_sensitive(text)
    heavy = heavy_task(text) if mode == "auto" and not sensitive else None

    if mode == "cloud" and not sensitive:
        escalate_reason = "forced cloud"
    elif heavy:
        escalate_reason = "escalated: heavy task (%s)" % heavy
    else:
        try:
            resp = call_upstream(CFG["local_url"], "", dict(body, model=CFG["local_model"]))
        except (urllib.error.URLError, OSError, ValueError) as e:
            if sensitive or mode == "local":
                msg = "local model unavailable and prompt cannot leave the device" if sensitive else "local model unavailable"
                return 503, {"error": {"message": msg, "detail": str(e)}}, "error", msg, sensitive
            escalate_reason = "escalated: local model unavailable"
        else:
            if sensitive:
                reason = "private: " + ", ".join(sensitive)
                if mode == "cloud":
                    reason += " (cloud blocked)"
                return 200, resp, "local", reason, sensitive
            if mode == "local":
                return 200, resp, "local", "forced local", sensitive
            weak = weak_answer(req, resp)
            score = None if weak else confidence(req, resp)
            if score is not None and score < CFG["min_confidence"]:
                weak = "low confidence (%d/5)" % score
            if not weak:
                reason = "local answer passed checks"
                if score is not None:
                    reason += " (confidence %d/5)" % score
                return 200, resp, "local", reason, sensitive
            escalate_reason = "escalated: " + weak

    try:
        resp = call_upstream(CFG["otari_url"], CFG["otari_key"], dict(body, model=CFG["cloud_model"]))
        return 200, resp, "cloud", escalate_reason, sensitive
    except urllib.error.HTTPError as e:
        # Otari rejects over-budget or unauthorized keys; surface its error as-is.
        detail = e.read().decode(errors="replace")
        return e.code, {"error": {"message": "Otari gateway error", "detail": detail}}, "error", "otari HTTP %d" % e.code, sensitive
    except (urllib.error.URLError, OSError, ValueError) as e:
        return 502, {"error": {"message": "Otari gateway unreachable", "detail": str(e)}}, "error", "otari unreachable", sensitive


class Handler(BaseHTTPRequestHandler):
    server_version = "Escalate/0.1"

    def log_message(self, fmt, *args):
        pass

    def send_json(self, status, obj, headers=None):
        data = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path in ("/", "/dashboard"):
            data = (HERE / "dashboard.html").read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        elif self.path == "/stats":
            self.send_json(200, STATS.snapshot())
        elif self.path in ("/v1/models", "/models"):
            self.send_json(200, {"object": "list", "data": [
                {"id": m, "object": "model", "owned_by": "escalate"} for m in ("auto", "local", "cloud")]})
        else:
            self.send_json(404, {"error": {"message": "not found"}})

    def do_POST(self):
        if self.path == "/stats/reset":
            STATS.reset()
            return self.send_json(200, {"ok": True})
        if self.path not in ("/v1/chat/completions", "/chat/completions"):
            return self.send_json(404, {"error": {"message": "not found"}})
        try:
            length = int(self.headers.get("Content-Length", 0))
            req = json.loads(self.rfile.read(length))
        except ValueError:
            return self.send_json(400, {"error": {"message": "invalid JSON body"}})

        # Mode comes from the X-Escalate header or the model name: auto | local | cloud.
        mode = (self.headers.get("X-Escalate") or req.get("model") or "auto").lower()
        if mode not in ("local", "cloud"):
            mode = "auto"

        start = time.time()
        status, resp, where, reason, sensitive = route(req, mode)
        latency = time.time() - start
        STATS.record(where, reason, resp.get("usage") if status == 200 else None, latency, sensitive)
        print("[%s] %-5s %4dms  %s" % (time.strftime("%H:%M:%S"), where, latency * 1000, reason), flush=True)
        self.send_json(status, resp, {"X-Escalate-Route": where, "X-Escalate-Reason": reason})


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--port", type=int, default=CFG["port"])
    ap.add_argument("--mock", action="store_true",
                    help="start fake local and Otari backends (no Docker or Ollama needed)")
    args = ap.parse_args()

    if args.mock:
        import mock_backends
        local_url, otari_url = mock_backends.start()
        CFG.update(local_url=local_url, otari_url=otari_url, otari_key="tk-mock",
                   local_model="mock-local-3b", cloud_model="mock:cloud-large")

    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print("Escalate on http://127.0.0.1:%d  (dashboard at /, API at /v1)" % args.port)
    print("  local: %s  [%s]" % (CFG["local_url"], CFG["local_model"]))
    print("  cloud: %s  [%s] via Otari" % (CFG["otari_url"], CFG["cloud_model"]))
    if not CFG["otari_key"]:
        print("  warning: OTARI_API_KEY is not set; escalations will be rejected by Otari")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
