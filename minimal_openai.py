# Minimal Livepeer demo: OpenAI with url + key as inputs.
#   uv run --with openai python minimal_openai.py --key sk_YOUR_KEY
#   uv run --with openai python minimal_openai.py --url http://localhost:8080/v1 --key sk_... --model Qwen/Qwen2.5-0.5B-Instruct --prompt "Hi"
import argparse

from openai import OpenAI

p = argparse.ArgumentParser(description="Minimal Livepeer OpenAI call")
p.add_argument("--url", default="http://localhost:8080/v1", help="gateway base_url")
p.add_argument("--key", required=True, help="your Livepeer sk_ key")
p.add_argument("--model", default="Qwen/Qwen2.5-0.5B-Instruct")
p.add_argument("--prompt", default="Hello!")
args = p.parse_args()

client = OpenAI(base_url=args.url, api_key=args.key)
resp = client.chat.completions.create(
    model=args.model,
    messages=[{"role": "user", "content": args.prompt}],
)
print(resp.choices[0].message.content)
