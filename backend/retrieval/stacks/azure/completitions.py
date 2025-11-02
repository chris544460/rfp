"""Utility client for embeddings and chat completions via the Aladdin API."""

from __future__ import annotations

import concurrent.futures
import datetime
import os
import re
import time
import traceback
import uuid
from typing import Iterable, List, Optional, Sequence

import numpy as np
import requests
from dotenv import load_dotenv
from requests.auth import HTTPBasicAuth

# Ensure .env values are loaded when this module is imported.
load_dotenv(override=True)


class Completitions:
    """
    Lightweight helper that mirrors the manager-provided implementation.

    It exposes:
      * get_embedding / get_embeddings for Azure/Surface vector generation
      * get_answer / answers_batch for chat completions

    Environment variables required:
      aladdin_studio_api_key
      defaultWebServer (e.g. https://webster.bfm.com)
      aladdin_user
      aladdin_passwd
    """

    _DEFAULT_EMBED_ENDPOINT = (
        "https://dev.blackrock.com/api/ai-platform/toolkit/embedding/v1/embedding:generate"
    )
    _FALLBACK_EMBED_ENDPOINT = (
        "https://tst.blackrock.com/api/ai-platform/toolkit/embedding/v1/embedding:generate"
    )

    def __init__(self, document_name: str = "no file name provided") -> None:
        user = os.environ.get("aladdin_user")
        password = os.environ.get("aladdin_passwd")
        if not user or not password:
            raise RuntimeError("Aladdin Studio credentials not provided in environment variables.")

        self.document_name = document_name
        self.service_costs = 0.0
        self.api_key = os.environ.get("aladdin_studio_api_key")
        if not self.api_key:
            raise RuntimeError("aladdin_studio_api_key environment variable is missing.")

        request_id = str(uuid.uuid1())
        origin_timestamp = (
            datetime.datetime.utcnow().replace(microsecond=0).astimezone().isoformat()
        )
        self.header = {
            "Content-Type": "application/json",
            "VND.com.blackrock.Request-ID": request_id,
            "VND.com.blackrock.Origin-Timestamp": origin_timestamp,
            "VND.com.blackrock.API-Key": self.api_key,
        }
        self.auth = HTTPBasicAuth(user, password)

        # Pricing in USD per million tokens; matches manager-provided table.
        self.pricing = {
            "gpt-35-turbo": {"input": 0.50, "output": 1.50},
            "gpt-35-turbo-0125": {"input": 0.50, "output": 1.50},
            "gpt-4": {"input": 30.0, "output": 60.0},
            "gpt-4-turbo-2024-04-09": {"input": 60.0, "output": 120.0},
            "gpt-4o-32k-0613": {"input": 10.0, "output": 30.0},
            "gpt-4o": {"input": 5.0, "output": 15.0},
            "gpt-4o-mini": {"input": 0.15, "output": 0.6},
            "gpt-4o-mini_research": {"input": 0.15, "output": 0.6},
            "o1-mini0": {"input": 3.0, "output": 12.0},
            "o1-preview": {"input": 15.0, "output": 60.0},
            "text-embedding-3-small": {"input": 0.02},
            "text-embedding-3-large": {"input": 0.13},
            "text-embedding-ada-002": {"input": 0.1},
            "o1-mini-2024-09-12": {"input": 3.0, "output": 12.0},
            "o3-mini-2025-01-31": {"input": 1.1, "output": 4.4},
            "o3-mini-2025-01-31_research": {"input": 1.1, "output": 4.4},
            "o3-2025-04-16_research": {"input": 2.0, "output": 8.0},
            "o3-2025-04-16": {"input": 2.0, "output": 8.0},
            "o4-mini-2025-04-16_research": {"input": 1.1, "output": 4.4},
            "o4-mini-2025-04-16": {"input": 1.1, "output": 4.4},
            "gpt-4.1-nano-2025-04-14": {"input": 0.1, "output": 0.4},
            "gpt-4.1-2025-04-14": {"input": 2.0, "output": 8.0},
            "gpt-4.1-nano-2025-04-14_research": {"input": 0.1, "output": 0.4},
            "gpt-4.1-mini-2025-04-14": {"input": 0.4, "output": 1.6},
            "gpt-4.1-mini-2025-04-14_research": {"input": 0.4, "output": 1.6},
            "gpt-5-nano-2025-08-07_research": {"input": 0.05, "output": 0.4},
            "gpt-5-mini-2025-08-07_research": {"input": 0.25, "output": 2.0},
            "gpt-5-2025-08-07_research": {"input": 1.25, "output": 10.0},
            "gpt-5-chat-2025-08-07_research": {"input": 1.25, "output": 10.0},
        }

        self.embedding_models = [
            "text-embedding-3-small",
            "text-embedding-3-large",
            "text-embedding-ada-002",
        ]

        self.env = os.environ.get("defaultWebServer")
        if not self.env:
            raise RuntimeError("defaultWebServer environment variable is missing.")

    # ------------------------------------------------------------------ Embeddings

    def get_embedding(
        self,
        text: Optional[str],
        model: str = "text-embedding-3-small",
        dimensions: Optional[int] = None,
        retries: int = 5,
    ) -> List[float]:
        if not text:
            return []

        url = self._DEFAULT_EMBED_ENDPOINT
        payload = {"text": text, "modelId": model}

        for attempt in range(retries):
            response = requests.post(url, json=payload, headers=self.header, auth=self.auth)
            if response.status_code == 200:
                data = response.json()
                return self._finalize_embedding_response(data, model, dimensions)
            if response.status_code == 403:
                url = self._FALLBACK_EMBED_ENDPOINT
            else:
                time.sleep(1)

        print(f"[completitions] Failed embedding request after {retries} retries: {text}")
        return []

    def _finalize_embedding_response(
        self, data: dict, model: str, dimensions: Optional[int]
    ) -> List[float]:
        prompt_tokens = data.get("embeddingMetadata", {}).get("totalTokenCount", 0)
        cost = prompt_tokens * self.pricing.get(model, {}).get("input", 0.0) / 1_000_000
        self.service_costs += cost

        vector = data.get("vector", [])
        if dimensions is not None:
            vector = vector[:dimensions]
            vector = list(_normalize_l2(vector))
        return vector

    def get_embeddings(
        self,
        texts: Sequence[str],
        model: str = "text-embedding-3-small",
        dimensions: Optional[int] = None,
        retries: int = 5,
    ) -> List[List[float]]:
        if not texts:
            return []

        url = f"{self.env}/api/ai-platform/toolkit/embedding/v1/embeddings:generate"
        payload = {"texts": list(texts), "modelId": model}

        for attempt in range(retries):
            response = requests.post(url, json=payload, headers=self.header, auth=self.auth)
            if response.status_code == 200:
                data = response.json()
                return self._finalize_embeddings_response(data, model, dimensions)
            if response.status_code == 403:
                url = self._FALLBACK_EMBED_ENDPOINT.replace("embedding", "embeddings")
            else:
                time.sleep(1)

        print(f"[completitions] Failed embeddings request after {retries} retries.")
        return []

    def _finalize_embeddings_response(
        self, data: dict, model: str, dimensions: Optional[int]
    ) -> List[List[float]]:
        prompt_tokens = data.get("embeddingMetadata", {}).get("totalTokenCount", 0)
        cost = prompt_tokens * self.pricing.get(model, {}).get("input", 0.0) / 1_000_000
        self.service_costs += cost

        vectors = [entry.get("embeddings", []) for entry in data.get("results", [])]
        if dimensions is not None:
            vectors = [list(_normalize_l2(vec[:dimensions])) for vec in vectors]
        return vectors

    # ------------------------------------------------------------------ Chat completions

    def get_answer(
        self,
        prompt: str,
        model: str = "gpt-35-turbo-0125",
        json_output: bool = False,
        messages: Optional[List[dict]] = None,
        retries: int = 5,
    ) -> Optional[str]:
        if not prompt:
            return None

        base_url = self.env.rstrip("/")
        url = f"{base_url}/api/ai-platform/toolkit/chat-completion/v1/chatCompletionsSync:compute"

        payload = (
            {"chatCompletionMessages": messages, "modelId": model}
            if messages
            else {
                "chatCompletionMessages": [
                    {"prompt": prompt, "promptRole": "user"},
                ],
                "modelId": model,
            }
        )
        if json_output:
            payload["response_format"] = {"type": "json_object"}

        for attempt in range(retries):
            response = requests.post(
                url, json=payload, headers=self.header, auth=self.auth, timeout=300
            )
            data = response.json()
            answer = self._extract_answer_from_response(data, model)
            if answer is not None:
                return answer

            # Handle retry suggestions from service
            error = data.get("error")
            code = data.get("code")
            if isinstance(error, str) and "seconds" in error:
                match = re.search(r"(\d+)\s+seconds", error)
                wait_time = int(match.group(1)) if match else 10
                time.sleep(wait_time)
            elif code == "RESOURCE_EXHAUSTED":
                time.sleep(5)
            else:
                time.sleep(1)

        traceback.print_exc()
        return ""

    def _extract_answer_from_response(self, data: dict, model: str) -> Optional[str]:
        chat_completion = data.get("chatCompletion")
        if not chat_completion:
            return None

        metadata = chat_completion.get("chatCompletionMetadata", {})
        prompt_tokens = metadata.get("promptTokenCount", 0)
        completion_tokens = metadata.get("completionTokenCount", 0)
        self._update_costs(model, prompt_tokens, completion_tokens)

        return chat_completion.get("chatCompletionContent")

    def _update_costs(self, model: str, prompt_tokens: int, completion_tokens: int) -> None:
        pricing = self.pricing.get(model, {})
        prompt_cost = prompt_tokens * pricing.get("input", 0.0) / 1_000_000
        completion_cost = completion_tokens * pricing.get("output", 0.0) / 1_000_000
        self.service_costs += prompt_cost + completion_cost

    # ------------------------------------------------------------------ Batch helpers

    def embeddings_batch(
        self,
        text_batches: Iterable[Sequence[str]],
        model: str = "text-embedding-3-small",
        dimensions: Optional[int] = None,
    ) -> List[List[float]]:
        batches = list(text_batches)
        if not batches:
            return []
        with concurrent.futures.ThreadPoolExecutor() as executor:
            return list(
                executor.map(
                    self.get_embeddings,
                    batches,
                    [model] * len(batches),
                    [dimensions] * len(batches),
                )
            )

    def answers_batch(
        self,
        prompts: Sequence[str],
        model: str = "gpt-35-turbo-0125",
        json_output: bool = False,
        messages: Optional[List[dict]] = None,
    ) -> List[Optional[str]]:
        prompt_list = list(prompts)
        if not prompt_list:
            return []
        if messages is None:
            messages = [None] * len(prompt_list)
        with concurrent.futures.ThreadPoolExecutor() as executor:
            return list(
                executor.map(
                    self.get_answer,
                    prompt_list,
                    [model] * len(prompt_list),
                    [json_output] * len(prompt_list),
                    messages,  # type: ignore[arg-type]
                )
            )


def _normalize_l2(vector: Sequence[float]) -> np.ndarray:
    arr = np.asarray(vector, dtype="float32")
    norm = np.linalg.norm(arr)
    if norm == 0:
        return arr
    return arr / norm


__all__ = ["Completitions"]
