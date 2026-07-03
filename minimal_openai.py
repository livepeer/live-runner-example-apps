# Minimal Livepeer demo: OpenAI reading key + url from ENV (safe for live demos —
# the key never appears in the command or the code).
#   export OPENAI_BASE_URL=https://YOUR-GATEWAY/v1
#   export OPENAI_API_KEY=sk_YOUR_KEY
#   uv run --with openai python minimal_openai.py
# (override with --url/--key/--model/--prompt if you want)
import argparse
import os

from openai import OpenAI

p = argparse.ArgumentParser(description="Minimal Livepeer OpenAI call")
p.add_argument("--url", default=os.environ.get("OPENAI_BASE_URL", "http://localhost:8080/v1"))
p.add_argument("--key", default=os.environ.get("OPENAI_API_KEY"))
p.add_argument("--model", default=os.environ.get("MODEL", "Qwen/Qwen2.5-0.5B-Instruct"))
p.add_argument("--prompt", default="Hello!")
args = p.parse_args()
if not args.key:
    raise SystemExit("set OPENAI_API_KEY (or pass --key)")

client = OpenAI(base_url=args.url, api_key=args.key)
resp = client.chat.completions.create(
    model=args.model,
    messages=[{"role": "user", "content": args.prompt}],
)
print(resp.choices[0].message.content)
