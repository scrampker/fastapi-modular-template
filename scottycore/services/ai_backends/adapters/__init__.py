"""Provider adapter implementations for AI backends.

Each adapter satisfies the ProviderAdapter Protocol and wraps one SDK or
subprocess call. Adapters are imported lazily to avoid hard-coding SDK
dependencies — a missing SDK just means that adapter will fail at call time,
not at import time.
"""

from __future__ import annotations

from scottycore.services.ai_backends.adapters.base import ProviderAdapter
from scottycore.services.ai_backends.adapters.claude_api import ClaudeApiAdapter
from scottycore.services.ai_backends.adapters.claude_cli import ClaudeCliAdapter
from scottycore.services.ai_backends.adapters.ollama import OllamaAdapter
from scottycore.services.ai_backends.adapters.dgx import DgxAdapter
from scottycore.services.ai_backends.adapters.openai import OpenAIAdapter
from scottycore.services.ai_backends.adapters.azure_openai import AzureOpenAIAdapter
from scottycore.services.ai_backends.adapters.custom import CustomAdapter

__all__ = [
    "ProviderAdapter",
    "ClaudeApiAdapter",
    "ClaudeCliAdapter",
    "OllamaAdapter",
    "DgxAdapter",
    "OpenAIAdapter",
    "AzureOpenAIAdapter",
    "CustomAdapter",
]
