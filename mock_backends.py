"""Fake OpenAI-compatible backends for demos and tests without Docker or Ollama.

The mock local model is deliberately small-model-shaped: it handles short,
everyday prompts but hedges on hard reasoning tasks and fumbles nested JSON.
The mock Otari gateway answers everything and requires a Bearer key.
"""
import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HARD = re.compile(r"(?i)\b(prove|architecture|design a|trade-?offs?|step by step|optimi[sz]e|distributed)\b")


def usage(prompt, answer):
    p, c = max(1, len(prompt) // 4), max(1, len(answer) // 4)
    return {"prompt_tokens": p, "completion_tokens": c, "total_tokens": p + c}


def completion(model, prompt, answer):
    return {
        "id": "chatcmpl-mock", "object": "chat.completion", "model": model,
        "choices": [{"index": 0, "finish_reason": "stop",
                     "message": {"role": "assistant", "content": answer}}],
        "usage": usage(prompt, answer),
    }


def local_answer(req, prompt):
    wants_json = (req.get("response_format") or {}).get("type") == "json_object"
    if wants_json:
        if "nested" in prompt.lower():
            return '{"items": [{"name": "a", "tags": ["x"'  # truncated JSON
        return json.dumps({"answer": "ok", "source": "local"})
    if HARD.search(prompt):
        return "I'm not sure I can give a reliable answer to that."
    return "Local answer: " + prompt.strip().splitlines()[-1][:120]


def cloud_answer(req, prompt):
    if (req.get("response_format") or {}).get("type") == "json_object":
        return json.dumps({"items": [{"name": "a", "tags": ["x", "y"]}], "source": "cloud"})
    return ("Cloud answer with a detailed, careful response to: "
            + prompt.strip().splitlines()[-1][:120] + " " + "Reasoning... " * 20)


def make_handler(kind):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            req = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
            prompt = "\n".join(m.get("content", "") for m in req.get("messages", [])
                               if isinstance(m.get("content"), str))
            if kind == "cloud" and not (self.headers.get("Authorization") or "").startswith("Bearer "):
                status, body = 401, {"detail": "missing API key"}
            else:
                answer = local_answer(req, prompt) if kind == "local" else cloud_answer(req, prompt)
                status, body = 200, completion(req.get("model"), prompt, answer)
            data = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
    return H


def start():
    """Start both mocks on free ports. Returns (local_base_url, otari_base_url)."""
    urls = []
    for kind, prefix in (("local", "/v1"), ("cloud", "/api/v1")):
        srv = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(kind))
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        urls.append("http://127.0.0.1:%d%s" % (srv.server_address[1], prefix))
    return tuple(urls)
