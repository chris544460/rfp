#!/usr/bin/env python3
"""
completions_client.py

A standalone script to call either OpenAI's API or the custom CompletionsClient
and print the final response. Uses service-account credentials
(aladdin_user/aladdin_passwd) from .env when using the custom framework.

Environment variables required in your .env for the custom framework:

    aladdin_studio_api_key   ← your API key
    defaultWebServer         ← e.g. https://webster.bfm.com
    aladdin_user             ← your service account username
    aladdin_passwd           ← your service account password

Environment variables required for OpenAI:

    OPENAI_API_KEY           ← your OpenAI API key

Usage:
    python3 backend/llm/completions_client.py --framework openai
    python3 backend/llm/completions_client.py --framework aladdin
"""

import argparse
import datetime
import json
import os
import time
import uuid

import requests
from requests.auth import HTTPBasicAuth
from backend.utils.dotenv import load_dotenv

# Load all .env variables at startup
load_dotenv(override=True)


class CompletionsClient:
    """Client for the custom Aladdin chat-completion service."""

    def __init__(self, model: str = "gpt-5-nano"):
        self.model = model
        self.service_costs = 0.0

        # 1) Load API key + base URL
        self.api_key = os.environ.get("aladdin_studio_api_key")
        self.base_url = os.environ.get("defaultWebServer", "").rstrip("/")

        if not self.api_key or not self.base_url:
            raise RuntimeError(
                "Missing aladdin_studio_api_key or defaultWebServer in environment."
            )

        # 2) Build headers with a fresh Request-ID + timestamp
        self.header = {
            "Content-Type": "application/json",
            "VND.com.blackrock.Request-ID": str(uuid.uuid1()),
            "VND.com.blackrock.Origin-Timestamp": datetime.datetime.utcnow()
            .replace(microsecond=0)
            .astimezone()
            .isoformat(),
            "VND.com.blackrock.API-Key": self.api_key,
        }

        # 3) Use service-account creds (aladdin_user/aladdin_passwd) for BasicAuth
        aladdin_user = os.environ.get("aladdin_user")
        aladdin_pass = os.environ.get("aladdin_passwd")
        if not aladdin_user or not aladdin_pass:
            raise RuntimeError(
                "Missing aladdin_user or aladdin_passwd in environment."
            )

        self.auth = HTTPBasicAuth(aladdin_user, aladdin_pass)

        # 4) Pricing table: USD per million tokens
        self.pricing = {
            "gpt-5-nano": {"input": 0.05, "output": 0.4},
            "gpt-35-turbo": {"input": 0.50, "output": 1.50},
            "gpt-4": {"input": 60.0, "output": 120.0},
            "gpt-4-32k-0613": {"input": 60.0, "output": 120.0},
            "gpt-4o": {"input": 5.0, "output": 15.0},
        }

    def get_completion(self, prompt: str, json_output: bool = False) -> tuple[str, dict]:
        """Send a single chat completion request and return (reply, usage)."""

        # Try the regular async endpoint first
        try:
            endpoint = (
                f"{self.base_url}/api/ai-platform/toolkit/chat-completion/v1/chatCompletions:compute"
            )

            payload = {
                "chatCompletionMessages": [
                    {"prompt": prompt, "promptRole": "assistant"}
                ],
                "modelId": self.model,
            }
            if json_output:
                payload["response_format"] = {"type": "json_object"}

            response = requests.post(
                endpoint,
                json=payload,
                headers=self.header,
                auth=self.auth,
                timeout=30,
            )
            data = response.json()

            operation_id = data.get("id")
            done_flag = data.get("done", False)

            if done_flag:
                return self._finalize_and_extract(data)
            if operation_id is None:
                # If we can't poll but have a result, try to extract it directly
                if "response" in data and "chatCompletion" in data["response"]:
                    return self._finalize_and_extract(data)
                raise RuntimeError(
                    f"No 'id' in POST response; cannot poll LRO. Full response: {json.dumps(data)}"
                )

            poll_url = (
                f"{self.base_url}/api/ai-platform/toolkit/completion/v1/longRunningOperations/"
                f"{operation_id}"
            )
            completed_data = self._poll_until_done(poll_url)
            return self._finalize_and_extract(completed_data)

        except Exception as e:
            # If async fails, fall back to synchronous endpoint
            print(f"Async endpoint failed: {e}")
            print("Falling back to synchronous endpoint...")
            return self._get_completion_sync(prompt, json_output)

    def _get_completion_sync(self, prompt: str, json_output: bool = False) -> tuple[str, dict]:
        """Send a single chat completion request using sync endpoint."""

        endpoint = (
            f"{self.base_url}/api/ai-platform/toolkit/chat-completion/v1/chatCompletionsSync:compute"
        )

        payload = {
            "chatCompletionMessages": [
                {"prompt": prompt, "promptRole": "assistant"}
            ],
            "modelId": self.model,
        }
        if json_output:
            payload["response_format"] = {"type": "json_object"}

        try:
            response = requests.post(
                endpoint,
                json=payload,
                headers=self.header,
                auth=self.auth,
                timeout=60,
            )
            response.raise_for_status()
            data = response.json()

            return self._finalize_and_extract_sync(data)

        except Exception as e:
            raise RuntimeError(f"Sync API call failed: {str(e)}")

    def _poll_until_done(self, url: str, initial_sleep: int = 5) -> dict:
        """Poll the given LRO URL until 'done': True."""
        attempts = 0
        sleep_time = initial_sleep

        while attempts < 50:
            resp = requests.get(url, headers=self.header, auth=self.auth)
            data = resp.json()
            if data.get("done", False):
                return data
            attempts += 1
            if attempts == 20:
                sleep_time = 10
            time.sleep(sleep_time)

        raise TimeoutError("Chat-completion operation timed out after 50 polls.")

    def _finalize_and_extract(self, data: dict) -> tuple[str, dict]:
        """Compute cost for async endpoint and return (reply, usage)."""
        try:
            # Handle the actual response structure from async endpoint
            chat_completion = data.get("response", {}).get("chatCompletion", {})
            if not chat_completion:
                raise RuntimeError("No chat completion data in response")

            metadata = chat_completion.get("chatCompletionMetadata", {})
            prompt_tokens = metadata.get("promptTokenCount", 0)
            completion_tokens = metadata.get("completionTokenCount", 0)
            content = chat_completion.get("chatCompletionContent", "")

            # If we have a JSON string, parse it
            if isinstance(content, str) and content.startswith("{"):
                try:
                    parsed_content = json.loads(content)
                    content = json.dumps(parsed_content, indent=2)
                except Exception:
                    pass

            model_props = self.pricing.get(self.model, {})
            prompt_cost = prompt_tokens * model_props.get("input", 0) / 1_000_000
            completion_cost = completion_tokens * model_props.get("output", 0) / 1_000_000
            self.service_costs += prompt_cost + completion_cost

            usage = {
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
            }

            return content, usage

        except Exception as e:
            print(f"Error processing async response: {data}")
            raise RuntimeError(
                f"Failed to extract result from async response: {str(e)}"
            )

    def _finalize_and_extract_sync(self, data: dict) -> tuple[str, dict]:
        """Compute cost for sync endpoint and return (reply, usage)."""
        try:
            # Handle the actual response structure from sync endpoint
            chat_completion = data.get("chatCompletion", {})

            if not chat_completion:
                raise RuntimeError("No chat completion data in response")

            metadata = chat_completion.get("chatCompletionMetadata", {})
            prompt_tokens = metadata.get("promptTokenCount", 0)
            completion_tokens = metadata.get("completionTokenCount", 0)
            content = chat_completion.get("chatCompletionContent", "")

            # If we have a JSON string, parse it
            if isinstance(content, str) and content.startswith("{"):
                try:
                    parsed_content = json.loads(content)
                    content = json.dumps(parsed_content, indent=2)
                except Exception:
                    pass  # Keep original string if parsing fails

            model_props = self.pricing.get(self.model, {})
            prompt_cost = prompt_tokens * model_props.get("input", 0) / 1_000_000
            completion_cost = completion_tokens * model_props.get("output", 0) / 1_000_000
            self.service_costs += prompt_cost + completion_cost

            usage = {
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
            }

            return content, usage

        except Exception as e:
            print(f"Error processing sync response: {data}")
            raise RuntimeError(
                f"Failed to extract result from sync response: {str(e)}"
            )


def get_openai_completion(prompt: str, model: str, json_output: bool = False) -> tuple[str, dict]:
    """Fetch a completion from OpenAI's API and return (reply, usage)."""
    from openai import OpenAI

    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("Missing OPENAI_API_KEY in environment.")
    client = OpenAI(api_key=api_key)
    params = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
    }
    if json_output:
        params["response_format"] = {"type": "json_object"}
    resp = client.chat.completions.create(**params)
    usage = {
        "prompt_tokens": resp.usage.prompt_tokens,
        "completion_tokens": resp.usage.completion_tokens,
    }
    return resp.choices[0].message.content, usage


def main(prompt: str, framework: str, model: str, json_output: bool = False) -> str:
    """Dispatch to the requested framework and return the completion."""
    if framework == "aladdin":
        client = CompletionsClient(model=model)
        content, _ = client.get_completion(prompt, json_output=json_output)
        return content
    if framework == "openai":
        content, _ = get_openai_completion(prompt, model, json_output=json_output)
        return content
    raise ValueError(f"Unknown framework: {framework}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Call an LLM using different frameworks")
    parser.add_argument(
        "--framework",
        choices=["openai", "aladdin"],
        default=os.getenv("ANSWER_FRAMEWORK", "aladdin"),
        help="Which completion framework to use",
    )
    parser.add_argument(
        "--model",
        default=os.getenv("OPENAI_MODEL", "gpt-4.1-nano-2025-04-14_research"),
        help="Model name for the chosen framework",
    )
    parser.add_argument(
        "--prompt",
        default="Explain the convergence of gradient descent.",
        help="Prompt to send to the model",
    )
    args = parser.parse_args()

    reply = main(args.prompt, args.framework, args.model)
    print("\n=== Assistant Reply ===\n")
    print(reply)
