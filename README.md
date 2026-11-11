# Escalate

**Local first. Cloud through [Otari](https://github.com/mozilla-ai/otari) only when needed.**

Escalate is a tiny OpenAI-compatible proxy that developers point their tools at instead of a cloud provider. Every request goes to a model running on your own machine first. It escalates to a cloud model through Mozilla.ai's Otari gateway only when the local answer looks weak. Prompts that contain secrets or personal data never leave the device.

Built for **MozFest 2026 Developers Wilding: Online** (Mozilla Otari hackathon).

## Why

Most developer AI traffic is small stuff: commit messages, regexes, renames, quick explanations. A 3B model on a laptop handles it fine, yet it all gets sent to a frontier model in someone else's cloud. That costs money and leaks code, keys and customer data.

Escalate puts the decision in a few lines of open, auditable code:

| Situation | Route |
|---|---|
| Local answer passes checks | **Local**, free and private |
| Local answer is empty, truncated, invalid JSON or hedged ("I'm not sure…") | **Escalate** to cloud through Otari |
| Local model is down | **Escalate** to cloud through Otari |
| Prompt contains an API key, private key, password, JWT, email or card number | **Always local**, even if the client asks for cloud |

Otari handles the cloud side: one key for 40+ providers, per-key **budgets** that cap what escalations can cost, and usage tracking in its dashboard. Escalate's own dashboard shows what stayed home.

## Quick demo (no setup)

Needs only Python 3.9+. It has no dependencies.

```bash
python3 escalate.py --mock          # fake local model + fake Otari gateway
python3 bench.py                    # in a second terminal
open http://127.0.0.1:8787          # live dashboard
```

Sample run:

```
local 200  Write a one-line commit message for: fixed typo in README   local answer passed checks
cloud 200  Return nested JSON listing items with name and tags.        escalated: invalid JSON
cloud 200  Design a distributed rate limiter and explain the trade-of  escalated: model hedged
local 200  Debug this config: OPENAI_KEY=sk-proj-abcdefghijklmnopqrst  private: API key
local 200  Draft a reply to jane.doe@example.com about the outage.     private: email address
...
16 requests: 12 local (75%), 4 cloud, 0 errors, 4 private prompts kept on-device
```

The mock models are stand-ins for the demo. Use the real setup below for actual answers.

## Real setup

**1. Local model.** Any OpenAI-compatible local server works.

```bash
# Ollama
ollama pull llama3.2:3b && ollama serve           # http://localhost:11434/v1
# or Mozilla llamafile
./Llama-3.2-3B-Instruct.llamafile --server        # http://localhost:8080/v1
```

**2. Otari gateway.**

```bash
docker run -v otari:/data -p 127.0.0.1:8000:8000 mzdotai/otari:latest
```

Open the setup link it prints, add a provider (for example OpenAI), then create an API key (`tk-...`) and **set a budget on it**. That budget is the hard cap on what escalations can ever spend.

**3. Escalate.**

```bash
export OTARI_API_KEY=tk-...
export CLOUD_MODEL=openai:gpt-4o-mini               # any provider:model Otari knows
export LOCAL_MODEL=llama3.2:3b
# export LOCAL_BASE_URL=http://localhost:8080/v1    # if using llamafile
python3 escalate.py
```

**4. Point your tools at it.** Use base URL `http://127.0.0.1:8787/v1` with any API key.

```python
from openai import OpenAI
client = OpenAI(base_url="http://127.0.0.1:8787/v1", api_key="unused")
client.chat.completions.create(model="auto", messages=[{"role": "user", "content": "..."}])
```

## Controlling routing

Set the `model` field, or the `X-Escalate` header, to one of these:

- `auto` (default): local first, escalate on weak answers
- `local`: never use the cloud
- `cloud`: skip local. Sensitive prompts still stay local.

Every response carries `X-Escalate-Route` (`local`/`cloud`/`error`) and `X-Escalate-Reason` headers.

## Configuration

| Env var | Default |
|---|---|
| `ESCALATE_PORT` | `8787` |
| `LOCAL_BASE_URL` | `http://localhost:11434/v1` |
| `LOCAL_MODEL` | `llama3.2:3b` |
| `OTARI_BASE_URL` | `http://localhost:8000/api/v1` |
| `OTARI_API_KEY` | none (required for escalation) |
| `CLOUD_MODEL` | `openai:gpt-4o-mini` |
| `CLOUD_PRICE_IN` / `CLOUD_PRICE_OUT` | `0.15` / `0.60` USD per 1M tokens, for the savings estimate |

## Endpoints

- `POST /v1/chat/completions`: the proxy
- `GET /`: dashboard
- `GET /stats`: counters and recent events as JSON
- `POST /stats/reset`: reset counters

## Limitations

- Streaming is not supported yet. Requests are answered in one response.
- The quality checks are simple heuristics. A future version could use a local judge model, or compare log-probs.
- "Saved" is an estimate: locally served tokens priced at the cloud model's rate.
- The sensitive-data detection is regex-based, so treat it as a safety net, not a guarantee.

## License

Apache 2.0
