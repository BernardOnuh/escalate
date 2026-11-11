#!/usr/bin/env python3
"""Run a mixed developer workload through Escalate and print where each request went."""
import argparse
import json
import urllib.error
import urllib.request

WORKLOAD = [
    ("Write a one-line commit message for: fixed typo in README", None),
    ("What does `git rebase --onto` do? One sentence.", None),
    ("Convert this to snake_case: getUserProfileById", None),
    ("Give me a regex that matches a US zip code.", None),
    ("Explain what HTTP 429 means.", None),
    ("Summarize: The build failed because the lockfile was out of date.", None),
    ("Rename variable `x` to something clearer: x = len(users)", None),
    ("Return JSON with a field 'answer' saying ok.", "json_object"),
    ("Return nested JSON listing items with name and tags.", "json_object"),
    ("Design a distributed rate limiter and explain the trade-offs.", None),
    ("Prove that this sorting algorithm is O(n log n) step by step.", None),
    ("Propose an architecture for multi-region failover.", None),
    ("Why does this fail? DATABASE_URL=postgres://app:password=hunter2@db/prod", None),
    ("Debug this config: OPENAI_KEY=sk-proj-abcdefghijklmnopqrstuvwx1234", None),
    ("Draft a reply to jane.doe@example.com about the outage.", None),
    ("Design a distributed cache for user sessions. Customer: bob@acme.io", None),
]


def post(url, body):
    req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=180) as r:
            return r.status, r.headers.get("X-Escalate-Route"), r.headers.get("X-Escalate-Reason")
    except urllib.error.HTTPError as e:
        return e.code, e.headers.get("X-Escalate-Route"), e.headers.get("X-Escalate-Reason")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--url", default="http://127.0.0.1:8787")
    ap.add_argument("--keep-stats", action="store_true", help="do not reset counters first")
    args = ap.parse_args()
    base = args.url.rstrip("/")

    if not args.keep_stats:
        urllib.request.urlopen(urllib.request.Request(base + "/stats/reset", data=b"", method="POST"))

    for prompt, fmt in WORKLOAD:
        body = {"model": "auto", "messages": [{"role": "user", "content": prompt}]}
        if fmt:
            body["response_format"] = {"type": fmt}
        status, where, reason = post(base + "/v1/chat/completions", body)
        print("%-5s %3s  %-58.58s  %s" % (where, status, prompt, reason))

    s = json.loads(urllib.request.urlopen(base + "/stats").read())
    share = 100.0 * s["local"] / max(1, s["requests"])
    print("\n%d requests: %d local (%.0f%%), %d cloud, %d errors, %d private prompts kept on-device"
          % (s["requests"], s["local"], share, s["cloud"], s["errors"], s["private_kept"]))
    print("Cloud spend $%.6f vs all-cloud $%.6f -> saved $%.6f"
          % (s["cloud_spend_usd"], s["all_cloud_usd"], s["saved_usd"]))


if __name__ == "__main__":
    main()
