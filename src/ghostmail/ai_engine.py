"""AI Engine - Model router with MiniMax, Kimi, and DeepSeek Reasoner."""

import asyncio
import json
import logging
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum
from typing import Any, Optional

import httpx

from .config import get_settings

logger = logging.getLogger(__name__)


class ModelProvider(str, Enum):
    """Available AI model providers."""

    OPENCODE_MINIMAX = "opencode_minimax"  # MiniMax 2.5 Free via OpenCode Zen
    OPENCODE_KIMI = "opencode_kimi"  # Kimi K2.5 Free via OpenCode Zen
    DEEPSEEK_REASONER = "deepseek_reasoner"  # DeepSeek Reasoner via API


@dataclass
class LLMResponse:
    """Standardized LLM response."""

    content: str
    provider: ModelProvider
    model: str
    tokens_used: Optional[int] = None
    latency_ms: Optional[int] = None
    raw_response: Optional[dict] = None


@dataclass
class LLMError(Exception):
    """LLM-specific error."""

    provider: ModelProvider
    message: str
    status_code: Optional[int] = None
    is_rate_limited: bool = False


class BaseLLMClient(ABC):
    """Abstract base class for LLM clients."""

    def __init__(self, provider: ModelProvider, model: str):
        self.provider = provider
        self.model = model
        self.settings = get_settings()

    @abstractmethod
    async def chat(
        self,
        messages: list[dict[str, str]],
        system_prompt: Optional[str] = None,
        temperature: float = 0.3,
        max_tokens: int = 2048,
    ) -> LLMResponse:
        """Send chat request and get response."""
        pass

    @abstractmethod
    async def chat_with_json(
        self,
        messages: list[dict[str, str]],
        system_prompt: str,
        temperature: float = 0.3,
        max_tokens: int = 2048,
    ) -> tuple[dict, LLMResponse]:
        """Send chat request and parse JSON response."""
        pass

    @abstractmethod
    async def close(self):
        """Close the HTTP client."""
        pass

    def _check_sensitive_content(self, text: str) -> bool:
        """Check if content contains sensitive keywords."""
        text_lower = text.lower()
        return any(keyword in text_lower for keyword in self.settings.sensitive_keywords)


class OpenCodeClient(BaseLLMClient):
    """
    OpenCode Zen client for MiniMax and Kimi free tier.

    Note: OpenCode Zen provides free access to MiniMax and Kimi models.
    You need an API key from https://opencode.cn (or similar endpoint).
    """

    def __init__(self, provider: ModelProvider, model: str):
        super().__init__(provider, model)
        self.base_url = self.settings.opencode_base_url
        self.api_key = self.settings.opencode_api_key

        if not self.api_key:
            raise ValueError(
                f"OpenCode API key not set. Set GHOSTMAIL_OPENCODE_API_KEY environment variable."
            )

        self.client = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=120.0,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
        )

    async def chat(
        self,
        messages: list[dict[str, str]],
        system_prompt: Optional[str] = None,
        temperature: float = 0.3,
        max_tokens: int = 2048,
    ) -> LLMResponse:
        """Send chat request to OpenCode."""
        start_time = time.time()

        # Build request payload
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }

        if system_prompt:
            # Prepend system message
            payload["messages"] = [{"role": "system", "content": system_prompt}] + messages

        try:
            response = await self.client.post("/chat/completions", json=payload)
            response.raise_for_status()

            data = response.json()
            content = data["choices"][0]["message"]["content"]
            latency_ms = int((time.time() - start_time) * 1000)

            return LLMResponse(
                content=content,
                provider=self.provider,
                model=self.model,
                tokens_used=data.get("usage", {}).get("total_tokens"),
                latency_ms=latency_ms,
                raw_response=data,
            )

        except httpx.HTTPStatusError as e:
            is_ratelimited = e.response.status_code == 429
            raise LLMError(
                provider=self.provider,
                message=f"OpenCode API error: {e.response.text}",
                status_code=e.response.status_code,
                is_rate_limited=is_ratelimited,
            ) from e

        except Exception as e:
            raise LLMError(
                provider=self.provider,
                message=f"OpenCode request failed: {str(e)}",
            ) from e

    async def chat_with_json(
        self,
        messages: list[dict[str, str]],
        system_prompt: str,
        temperature: float = 0.3,
        max_tokens: int = 2048,
    ) -> tuple[dict, LLMResponse]:
        """Send chat request expecting JSON response."""
        # Add JSON formatting instruction to system prompt
        json_prompt = system_prompt + (
            "\n\nIMPORTANT: Your response must be valid JSON only. "
            "No markdown, no explanations, no code blocks. Just pure JSON."
        )

        response = await self.chat(
            messages=messages,
            system_prompt=json_prompt,
            temperature=temperature,
            max_tokens=max_tokens,
        )

        # Parse JSON from response
        try:
            # Try to extract JSON from potential markdown wrapper
            content = response.content.strip()
            if content.startswith("```json"):
                content = content[7:]
            elif content.startswith("```"):
                content = content[3:]
            if content.endswith("```"):
                content = content[:-3]

            parsed = json.loads(content.strip())
            return parsed, response

        except json.JSONDecodeError as e:
            raise LLMError(
                provider=self.provider,
                message=f"Failed to parse JSON response: {e}. Response: {response.content[:200]}",
            ) from e

    async def close(self):
        """Close the HTTP client."""
        await self.client.aclose()


class DeepSeekClient(BaseLLMClient):
    """
    DeepSeek API client for Reasoner model.

    DeepSeek is cheap but powerful. Get your API key from:
    https://platform.deepseek.com
    """

    def __init__(self, model: str = "deepseek-reasoner"):
        super().__init__(ModelProvider.DEEPSEEK_REASONER, model)
        self.base_url = self.settings.deepseek_base_url
        self.api_key = self.settings.deepseek_api_key

        if not self.api_key:
            raise ValueError(
                "DeepSeek API key not set. Set GHOSTMAIL_DEEPSEEK_API_KEY environment variable."
            )

        self.client = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=180.0,  # Reasoner can take longer
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
        )

    async def chat(
        self,
        messages: list[dict[str, str]],
        system_prompt: Optional[str] = None,
        temperature: float = 0.3,
        max_tokens: int = 2048,
    ) -> LLMResponse:
        """Send chat request to DeepSeek."""
        start_time = time.time()

        # Build messages with system prompt
        all_messages = []
        if system_prompt:
            all_messages.append({"role": "system", "content": system_prompt})
        all_messages.extend(messages)

        payload = {
            "model": self.model,
            "messages": all_messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }

        try:
            response = await self.client.post("/v1/chat/completions", json=payload)
            response.raise_for_status()

            data = response.json()
            content = data["choices"][0]["message"]["content"]
            latency_ms = int((time.time() - start_time) * 1000)

            return LLMResponse(
                content=content,
                provider=self.provider,
                model=self.model,
                tokens_used=data.get("usage", {}).get("total_tokens"),
                latency_ms=latency_ms,
                raw_response=data,
            )

        except httpx.HTTPStatusError as e:
            is_ratelimited = e.response.status_code == 429
            raise LLMError(
                provider=self.provider,
                message=f"DeepSeek API error: {e.response.text}",
                status_code=e.response.status_code,
                is_rate_limited=is_ratelimited,
            ) from e

        except Exception as e:
            raise LLMError(
                provider=self.provider,
                message=f"DeepSeek request failed: {str(e)}",
            ) from e

    async def chat_with_json(
        self,
        messages: list[dict[str, str]],
        system_prompt: str,
        temperature: float = 0.3,
        max_tokens: int = 2048,
    ) -> tuple[dict, LLMResponse]:
        """Send chat request expecting JSON response."""
        return await self._chat_json_helper(messages, system_prompt, temperature, max_tokens)

    async def _chat_json_helper(
        self,
        messages: list[dict[str, str]],
        system_prompt: str,
        temperature: float,
        max_tokens: int,
    ) -> tuple[dict, LLMResponse]:
        """Helper for JSON response with retry logic."""
        json_prompt = system_prompt + (
            "\n\nIMPORTANT: Respond ONLY with valid JSON. No markdown formatting, "
            "no explanations. Just the JSON object."
        )

        response = await self.chat(
            messages=messages,
            system_prompt=json_prompt,
            temperature=temperature,
            max_tokens=max_tokens,
        )

        try:
            content = response.content.strip()
            # Strip markdown if present
            if content.startswith("```json"):
                content = content[7:]
            elif content.startswith("```"):
                content = content[3:]
            if content.endswith("```"):
                content = content[:-3]

            parsed = json.loads(content.strip())
            return parsed, response

        except json.JSONDecodeError as e:
            raise LLMError(
                provider=self.provider,
                message=f"Failed to parse JSON: {e}. Content: {response.content[:300]}",
            ) from e

    async def close(self):
        """Close the HTTP client."""
        await self.client.aclose()


class ModelRouter:
    """
    Intelligent router that selects the best model for each task.

    Priority order:
    1. MiniMax 2.5 via OpenCode (for long contexts)
    2. Kimi K2.5 via OpenCode (for reasoning)
    3. DeepSeek Reasoner (for complex reasoning tasks)
    """

    def __init__(self):
        self.settings = get_settings()
        self._clients: dict[ModelProvider, BaseLLMClient] = {}
        self._init_clients()

    def _init_clients(self):
        """Initialize available clients based on API keys."""
        # OpenCode clients (MiniMax and Kimi)
        if self.settings.opencode_api_key:
            try:
                # MiniMax 2.5 - large context window
                self._clients[ModelProvider.OPENCODE_MINIMAX] = OpenCodeClient(
                    provider=ModelProvider.OPENCODE_MINIMAX,
                    model="minimax-2.5-free",  # Adjust based on actual OpenCode model name
                )
                logger.info("Initialized OpenCode MiniMax client")

                # Kimi K2.5 - strong reasoning
                self._clients[ModelProvider.OPENCODE_KIMI] = OpenCodeClient(
                    provider=ModelProvider.OPENCODE_KIMI,
                    model="kimi-k2.5-free",  # Adjust based on actual OpenCode model name
                )
                logger.info("Initialized OpenCode Kimi client")

            except ValueError as e:
                logger.warning(f"OpenCode client initialization failed: {e}")

        # DeepSeek client
        if self.settings.deepseek_api_key:
            try:
                self._clients[ModelProvider.DEEPSEEK_REASONER] = DeepSeekClient()
                logger.info("Initialized DeepSeek Reasoner client")
            except ValueError as e:
                logger.warning(f"DeepSeek client initialization failed: {e}")

    def get_client(
        self,
        task_type: str = "general",
        prefer_reasoning: bool = False,
    ) -> BaseLLMClient:
        """
        Get the best client for the task.

        Args:
            task_type: "general", "long_context", "reasoning", "fast"
            prefer_reasoning: If True, prefer models good at reasoning
        """
        # Priority based on task type
        if task_type == "long_context":
            # MiniMax has the largest context (4M tokens)
            if ModelProvider.OPENCODE_MINIMAX in self._clients:
                return self._clients[ModelProvider.OPENCODE_MINIMAX]

        if prefer_reasoning or task_type == "reasoning":
            # Try DeepSeek Reasoner first, then Kimi
            if ModelProvider.DEEPSEEK_REASONER in self._clients:
                return self._clients[ModelProvider.DEEPSEEK_REASONER]
            if ModelProvider.OPENCODE_KIMI in self._clients:
                return self._clients[ModelProvider.OPENCODE_KIMI]

        # Default: try MiniMax, then Kimi, then DeepSeek
        for provider in [
            ModelProvider.OPENCODE_MINIMAX,
            ModelProvider.OPENCODE_KIMI,
            ModelProvider.DEEPSEEK_REASONER,
        ]:
            if provider in self._clients:
                return self._clients[provider]

        raise RuntimeError("No LLM clients available. Configure at least one provider.")

    def get_available_providers(self) -> list[ModelProvider]:
        """Get list of available providers."""
        return list(self._clients.keys())

    async def close_all(self):
        """Close all client connections."""
        for client in self._clients.values():
            await client.close()


# Global router instance
_router: Optional[ModelRouter] = None


def get_router() -> ModelRouter:
    """Get singleton ModelRouter instance."""
    global _router
    if _router is None:
        _router = ModelRouter()
    return _router
