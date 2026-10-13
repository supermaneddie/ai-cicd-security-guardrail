from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


_ENV = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


def _expand(value: Any) -> Any:
    if isinstance(value, str):
        return _ENV.sub(lambda m: os.getenv(m.group(1), m.group(2) or ""), value)
    if isinstance(value, list):
        return [_expand(item) for item in value]
    if isinstance(value, dict):
        return {key: _expand(item) for key, item in value.items()}
    return value


@dataclass(frozen=True)
class ModelConfig:
    model_name: str
    base_url: str
    api_key: str | None = None
    verify_ssl: bool = True
    temperature: float = 0
    timeout_seconds: int = 120
    call_timeout_seconds: int = 300
    final_extra_body: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Limits:
    max_upload_mb: int = 25
    max_extracted_mb: int = 100
    max_files: int = 2000
    # Accepted for backward compatibility; MCP filesystem tools manage reads.
    max_file_read_kb: int = 5120
    recursion_limit: int = 60
    # Accepted for backward compatibility; recursion_limit takes precedence.
    max_agent_iterations: int | None = None
    max_concurrent_reviews: int = 2


@dataclass(frozen=True)
class FilesystemMCPConfig:
    command: str = "npx"
    args: tuple[str, ...] = ("-y", "@modelcontextprotocol/server-filesystem")


@dataclass(frozen=True)
class PublicURLConfig:
    domain: str = "localhost"
    port: int = 8080
    ws_port: int = 8081
    docker_image: str = "codeqc:latest"
    scheme: str = "http"

    @property
    def http_url(self) -> str:
        return f"{self.scheme}://{self.domain}:{self.port}"

    @property
    def websocket_url(self) -> str:
        scheme = "wss" if self.scheme == "https" else "ws"
        return f"{scheme}://{self.domain}:{self.ws_port}"


@dataclass(frozen=True)
class AppConfig:
    default_system_prompt: str
    models: dict[str, ModelConfig]
    limits: Limits
    filesystem_mcp: FilesystemMCPConfig
    public_url: PublicURLConfig = field(default_factory=PublicURLConfig)


def load_config(path: str | Path) -> AppConfig:
    config_path = Path(path)
    if not config_path.is_file():
        raise ValueError(f"Configuration file not found: {config_path}")
    raw = _expand(yaml.safe_load(config_path.read_text(encoding="utf-8")) or {})
    model_items = raw.get("models") or []
    models: dict[str, ModelConfig] = {}
    for item in model_items:
        model_values = dict(item)
        # Ignore removed global thinking options so older configuration files still load.
        for removed_key in ("thinking_enabled", "reasoning_effort", "extra_body"):
            model_values.pop(removed_key, None)
        model = ModelConfig(**model_values)
        if not model.model_name or not model.base_url:
            raise ValueError("Every model requires model_name and base_url")
        if model.model_name in models:
            raise ValueError(f"Duplicate model_name: {model.model_name}")
        if model.timeout_seconds <= 0 or model.call_timeout_seconds <= 0:
            raise ValueError("Model timeouts must be positive")
        models[model.model_name] = model
    if not models:
        raise ValueError("At least one model must be configured")
    prompt = str(raw.get("default_system_prompt") or "").strip()
    if not prompt:
        raise ValueError("default_system_prompt must not be empty")
    limit_values = raw.get("limits") or {}
    if "recursion_limit" not in limit_values and limit_values.get("max_agent_iterations") is not None:
        limit_values["recursion_limit"] = int(limit_values["max_agent_iterations"]) * 2 + 1
    limits = Limits(**limit_values)
    if limits.recursion_limit < 4:
        raise ValueError("limits.recursion_limit must be at least 4")
    mcp_values = raw.get("filesystem_mcp") or {}
    defaults = FilesystemMCPConfig()
    mcp_command = str(mcp_values.get("command", defaults.command)).strip()
    mcp_args = tuple(str(value) for value in mcp_values.get("args", defaults.args))
    if not mcp_command:
        raise ValueError("filesystem_mcp.command must not be empty")
    if not mcp_args:
        raise ValueError("filesystem_mcp.args must not be empty")
    filesystem_mcp = FilesystemMCPConfig(command=mcp_command, args=mcp_args)
    public_values = dict(raw.get("public_url") or {})
    for key in ("port", "ws_port"):
        if key in public_values:
            public_values[key] = int(public_values[key])
    public_url = PublicURLConfig(**public_values)
    if public_url.scheme not in {"http", "https"}:
        raise ValueError("public_url.scheme must be http or https")
    if not re.fullmatch(r"[A-Za-z0-9.-]+|\[[0-9A-Fa-f:]+\]", public_url.domain):
        raise ValueError("public_url.domain must be a hostname or IP address without a scheme or port")
    if not all(1 <= port <= 65535 for port in (public_url.port, public_url.ws_port)):
        raise ValueError("public_url ports must be between 1 and 65535")
    if not public_url.docker_image or any(c.isspace() for c in public_url.docker_image):
        raise ValueError("public_url.docker_image must be a nonempty image reference without whitespace")
    return AppConfig(prompt, models, limits, filesystem_mcp, public_url)
