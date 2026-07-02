# Minimal Livepeer demo: OpenAI, hardcoded url + key. Run: uv run --with openai python minimal_openai.py
from openai import OpenAI

client = OpenAI(
    base_url="http://localhost:8080/v1",   # your clearinghouse gateway
    api_key="sk_YOUR_KEY_HERE",                        # your Livepeer key (from signup/mint)
)

resp = client.chat.completions.create(
    model="Qwen/Qwen2.5-0.5B-Instruct",
    messages=[{"role": "user", "content": "Hello!"}],
)
print(resp.choices[0].message.content)
