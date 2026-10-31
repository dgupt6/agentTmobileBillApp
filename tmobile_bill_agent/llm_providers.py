# LLM provider layer for the bill extraction agent.
# Developer :  Devraj Gupta
# Version 1.0 Sep 2026
#
# Every provider exposes one call: extract_json(system_prompt, user_prompt, schema) -> dict
#   - Ollama (local, free)      : native /api/chat with `format=<json schema>` (structured output)
#   - Groq / Gemini / OpenRouter: OpenAI-compatible endpoints (free tiers), JSON mode
#   - Anthropic (paid fallback) : forced tool call whose input_schema is the bill schema
import json
import os

import requests

OLLAMA_URL = os.environ.get("OLLAMA_HOST", "http://localhost:11434")

PROVIDERS = {
    "Groq (free tier)": {
        "kind": "openai",
        "base_url": "https://api.groq.com/openai/v1",
        "default_model": "openai/gpt-oss-120b",
        "key_env": "GROQ_API_KEY",
        "help": "Free key: https://console.groq.com/keys "
                "(other models: qwen/qwen3.8-27b, openai/gpt-oss-20b).",
    },
    "Ollama (local, free)": {
        "kind": "ollama",
        "default_model": "gemma3:4b",
        "key_env": None,
        "help": "Runs on your Mac, bill data never leaves the machine. "
                "Better accuracy: `ollama pull qwen2.5:7b`.",
    },
    "Google Gemini (free tier)": {
        "kind": "openai",
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai/",
        "default_model": "gemini-2.5-flash",
        "key_env": "GEMINI_API_KEY",
        "help": "Free key: https://aistudio.google.com/apikey "
                "(free tier may use prompts for training).",
    },
    "OpenRouter (free models)": {
        "kind": "openai",
        "base_url": "https://openrouter.ai/api/v1",
        "default_model": "meta-llama/llama-3.3-70b-instruct:free",
        "key_env": "OPENROUTER_API_KEY",
        "help": "Free key: https://openrouter.ai/keys (use models ending in ':free').",
    },
    "Anthropic (paid fallback)": {
        "kind": "anthropic",
        "default_model": "claude-haiku-4-5-20251001",
        "key_env": "ANTHROPIC_API_KEY",
        "help": "Paid key: https://console.anthropic.com/settings/keys",
    },
}


class LLMError(RuntimeError):
    pass


def list_ollama_models():
    try:
        resp = requests.get(f"{OLLAMA_URL}/api/tags", timeout=3)
        resp.raise_for_status()
        return [m["name"] for m in resp.json().get("models", [])]
    except Exception:
        return []


def _parse_json_text(text):
    # Models sometimes wrap JSON in ``` fences or add a sentence; keep the outermost object.
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        raise LLMError(f"Model did not return JSON. Raw response: {text[:400]!r}")
    try:
        return json.loads(text[start:end + 1])
    except json.JSONDecodeError as exc:
        raise LLMError(f"Model returned invalid JSON ({exc}). Raw response: {text[:400]!r}") from exc


class LLMClient:
    def __init__(self, provider_name, model, api_key=None):
        if provider_name not in PROVIDERS:
            raise LLMError(f"Unknown provider {provider_name!r}")
        self.provider_name = provider_name
        self.cfg = PROVIDERS[provider_name]
        self.kind = self.cfg["kind"]
        self.model = model or self.cfg["default_model"]
        self.api_key = api_key
        if self.kind != "ollama" and not self.api_key:
            raise LLMError(f"{provider_name} needs an API key. {self.cfg['help']}")

    def extract_json(self, system_prompt, user_prompt, schema):
        if self.kind == "ollama":
            return self._ollama(system_prompt, user_prompt, schema)
        if self.kind == "openai":
            return self._openai_compatible(system_prompt, user_prompt, schema)
        return self._anthropic(system_prompt, user_prompt, schema)

    def test_connection(self):
        schema = {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"]}
        result = self.extract_json("Reply with JSON only.", 'Return {"ok": true}', schema)
        return bool(result.get("ok"))

    def _ollama(self, system_prompt, user_prompt, schema):
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "format": schema,
            "stream": False,
            "options": {"temperature": 0, "num_ctx": 4096},
        }
        try:
            resp = requests.post(f"{OLLAMA_URL}/api/chat", json=payload, timeout=600)
        except requests.ConnectionError as exc:
            raise LLMError(f"Cannot reach Ollama at {OLLAMA_URL}. Start it with `ollama serve`.") from exc
        if resp.status_code != 200:
            raise LLMError(f"Ollama error {resp.status_code}: {resp.text[:300]}")
        return _parse_json_text(resp.json()["message"]["content"])

    def _openai_compatible(self, system_prompt, user_prompt, schema):
        from openai import OpenAI, BadRequestError, NotFoundError

        client = OpenAI(base_url=self.cfg["base_url"], api_key=self.api_key, timeout=180)
        messages = [
            {"role": "system", "content": system_prompt
                + "\n\nReturn ONLY a JSON object matching this JSON schema:\n" + json.dumps(schema)},
            {"role": "user", "content": user_prompt},
        ]
        try:
            try:
                resp = client.chat.completions.create(
                    model=self.model, messages=messages, temperature=0,
                    response_format={"type": "json_object"})
            except BadRequestError:
                # Some free OpenRouter models reject response_format; the schema is in the prompt anyway.
                resp = client.chat.completions.create(model=self.model, messages=messages, temperature=0)
        except NotFoundError as exc:
            raise LLMError(f"{self.provider_name}: model {self.model!r} not found. "
                           f"Available models: {self._available_models(client)}") from exc
        except Exception as exc:
            raise LLMError(f"{self.provider_name} call failed: {exc}") from exc
        return _parse_json_text(resp.choices[0].message.content or "")

    @staticmethod
    def _available_models(client):
        try:
            return ", ".join(sorted(m.id for m in client.models.list().data))
        except Exception:
            return "(could not list models)"

    def _anthropic(self, system_prompt, user_prompt, schema):
        import anthropic

        client = anthropic.Anthropic(api_key=self.api_key)
        try:
            resp = client.messages.create(
                model=self.model,
                max_tokens=4096,
                temperature=0,
                system=system_prompt,
                tools=[{"name": "submit_result",
                        "description": "Submit the extracted data.",
                        "input_schema": schema}],
                tool_choice={"type": "tool", "name": "submit_result"},
                messages=[{"role": "user", "content": user_prompt}],
            )
        except Exception as exc:
            raise LLMError(f"Anthropic call failed: {exc}") from exc
        for block in resp.content:
            if block.type == "tool_use":
                return block.input
        raise LLMError("Anthropic response contained no tool call.")
