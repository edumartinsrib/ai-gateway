"""Preparação do ambiente antes de subir o LiteLLM Proxy.

Responsabilidades:

1. Carregar segredos entregues como arquivo (`<VAR>_FILE`), para quem usa Vault Agent
   Injector ou Secrets Store CSI Driver em vez de variáveis de ambiente.
2. Renderizar o config final a partir de `config/config.yaml`, aplicando ajustes que
   dependem do ambiente (Redis, OTEL).
3. Validar que toda referência `os.environ/NOME` do config existe no ambiente — assim um
   pod cujo segredo não foi injetado pelo vault falha no boot com uma mensagem clara.
"""

from __future__ import annotations

import copy
import logging
import os
from collections.abc import Iterator, MutableMapping
from pathlib import Path
from typing import Any

import yaml

log = logging.getLogger("ai_gateway.bootstrap")

ENV_REF_PREFIX = "os.environ/"

# Variáveis que podem ser entregues como arquivo via `<VAR>_FILE`.
SECRET_ENV_VARS = (
    "AZURE_API_KEY",
    "LITELLM_MASTER_KEY",
    "LITELLM_SALT_KEY",
    "DATABASE_URL",
    "REDIS_PASSWORD",
)

# Obrigatórias mesmo que o config não as referencie diretamente.
ALWAYS_REQUIRED = ("LITELLM_SALT_KEY",)


class ConfigError(RuntimeError):
    """Configuração ou ambiente inválido; o gateway não deve subir."""


def load_secret_files(env: MutableMapping[str, str]) -> list[str]:
    """Preenche `VAR` a partir do arquivo em `VAR_FILE` quando `VAR` não está definida.

    Retorna os nomes das variáveis carregadas (nunca os valores).
    """
    loaded = []
    for name in SECRET_ENV_VARS:
        path = env.get(f"{name}_FILE")
        if not path or env.get(name):
            continue
        try:
            value = Path(path).read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise ConfigError(f"{name}_FILE aponta para {path!r}, que não pôde ser lido: {exc}") from exc
        if not value:
            raise ConfigError(f"{name}_FILE aponta para {path!r}, que está vazio")
        env[name] = value
        loaded.append(name)
    return loaded


def iter_env_refs(node: Any) -> Iterator[str]:
    """Percorre o config e devolve cada NOME referenciado como `os.environ/NOME`."""
    if isinstance(node, dict):
        for value in node.values():
            yield from iter_env_refs(value)
    elif isinstance(node, list):
        for value in node:
            yield from iter_env_refs(value)
    elif isinstance(node, str) and node.startswith(ENV_REF_PREFIX):
        yield node.removeprefix(ENV_REF_PREFIX)


def render_config(config: dict[str, Any], env: MutableMapping[str, str]) -> dict[str, Any]:
    """Aplica ao config os ajustes que dependem do ambiente. Não altera o dict original."""
    rendered = copy.deepcopy(config)
    litellm_settings = rendered.setdefault("litellm_settings", {})
    general_settings = rendered.setdefault("general_settings", {})
    router_settings = rendered.setdefault("router_settings", {})

    # Sem endpoint OTLP o LiteLLM exporta spans para o console, poluindo o log.
    callbacks = litellm_settings.get("callbacks") or []
    has_otlp_endpoint = env.get("OTEL_EXPORTER_OTLP_ENDPOINT") or env.get("OTEL_ENDPOINT")
    if "otel" in callbacks and not has_otlp_endpoint:
        log.warning("OTEL_EXPORTER_OTLP_ENDPOINT não definido: callback 'otel' desativado")
        litellm_settings["callbacks"] = [cb for cb in callbacks if cb != "otel"]

    # Com mais de uma réplica, o Redis mantém rate limits, cooldowns e o buffer de gasto
    # consistentes entre os pods (evita estourar orçamento entre gravações no banco).
    if env.get("REDIS_HOST"):
        router_settings.setdefault("redis_host", f"{ENV_REF_PREFIX}REDIS_HOST")
        router_settings.setdefault("redis_port", f"{ENV_REF_PREFIX}REDIS_PORT")
        env.setdefault("REDIS_PORT", "6379")
        if env.get("REDIS_PASSWORD"):
            router_settings.setdefault("redis_password", f"{ENV_REF_PREFIX}REDIS_PASSWORD")
        general_settings.setdefault("use_redis_transaction_buffer", True)

    return rendered


def validate(config: dict[str, Any], env: MutableMapping[str, str]) -> None:
    """Falha se faltar alguma variável exigida pelo config ou se algum segredo for inválido."""
    required = set(iter_env_refs(config)) | set(ALWAYS_REQUIRED)
    missing = sorted(name for name in required if not env.get(name))
    if missing:
        raise ConfigError(
            "Variáveis de ambiente ausentes: "
            + ", ".join(missing)
            + ". Verifique se o segredo foi sincronizado do vault (kubectl describe externalsecret)."
        )

    master_key = env.get("LITELLM_MASTER_KEY", "")
    if master_key and not master_key.startswith("sk-"):
        raise ConfigError("LITELLM_MASTER_KEY deve começar com 'sk-'")

    if not config.get("model_list"):
        raise ConfigError("model_list está vazio: nenhum deployment configurado")


def prepare(config_path: Path, output_path: Path, env: MutableMapping[str, str] | None = None) -> Path:
    """Executa todo o bootstrap e grava o config renderizado em `output_path`."""
    env = os.environ if env is None else env
    loaded = load_secret_files(env)
    if loaded:
        log.info("Segredos carregados de arquivo: %s", ", ".join(loaded))

    try:
        config = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise ConfigError(f"Não foi possível ler {config_path}: {exc}") from exc

    rendered = render_config(config, env)
    validate(rendered, env)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(yaml.safe_dump(rendered, sort_keys=False), encoding="utf-8")
    return output_path
