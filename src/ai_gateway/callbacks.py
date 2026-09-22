"""Auditoria de custo por requisição.

Para cada chamada ao LLM emite:

- uma linha JSON no logger `ai_gateway.audit` (stdout), pronta para Loki/Elastic/Azure Monitor,
  com quem chamou (usuário, time, chave, cliente), modelo, tokens, custo e latência — nunca
  o conteúdo do prompt ou da resposta;
- métricas Prometheus `ai_gateway_*` com o recorte por *cliente* (VS Code, GitHub App...),
  que as métricas nativas do LiteLLM não oferecem.
"""

from __future__ import annotations

import json
import logging
import re
import sys
from typing import Any

from litellm.integrations.custom_logger import CustomLogger
from prometheus_client import Counter

LABELS = ("client", "team", "model")

REQUESTS = Counter("ai_gateway_requests", "Requisições ao LLM por cliente", [*LABELS, "status"])
SPEND = Counter("ai_gateway_spend_usd", "Custo estimado (USD) por cliente", LABELS)
TOKENS = Counter("ai_gateway_tokens", "Tokens consumidos por cliente", [*LABELS, "type"])

# Ordem importa: a primeira regra que casar com o User-Agent define o cliente.
USER_AGENT_RULES = (
    ("github-app", re.compile(r"github-app|octokit|probot", re.I)),
    ("vscode", re.compile(r"vscode|visual studio code|\bcode/\d|copilot|continue|cline|roo-?code", re.I)),
    ("sdk-python", re.compile(r"python|openai/python|httpx", re.I)),
    ("sdk-js", re.compile(r"node|openai/js|axios", re.I)),
    ("curl", re.compile(r"^curl/", re.I)),
)

_SAFE_LABEL = re.compile(r"[^a-z0-9_.-]")


def _label(value: Any, default: str = "none", max_len: int = 64) -> str:
    text = _SAFE_LABEL.sub("-", str(value or "").strip().lower())[:max_len]
    return text or default


def classify_client(payload: dict[str, Any]) -> str:
    """Identifica o cliente: metadata `client` da chave > User-Agent > "other"."""
    metadata = payload.get("metadata") or {}
    key_metadata = metadata.get("user_api_key_auth_metadata") or {}
    if key_metadata.get("client"):
        return _label(key_metadata["client"], max_len=32)

    user_agent = payload.get("user_agent") or metadata.get("user_agent") or ""
    for client, pattern in USER_AGENT_RULES:
        if pattern.search(user_agent):
            return client
    return "other"


def build_audit_event(payload: dict[str, Any]) -> dict[str, Any]:
    """Monta o evento de auditoria a partir do StandardLoggingPayload do LiteLLM."""
    metadata = payload.get("metadata") or {}
    error = payload.get("error_information") or {}
    start, end = payload.get("startTime"), payload.get("endTime")
    return {
        "event": "llm_request",
        "request_id": payload.get("id"),
        "trace_id": payload.get("trace_id"),
        "status": payload.get("status"),
        "client": classify_client(payload),
        "user_id": metadata.get("user_api_key_user_id"),
        "user_email": metadata.get("user_api_key_user_email"),
        "team_id": metadata.get("user_api_key_team_id"),
        "team_alias": metadata.get("user_api_key_team_alias"),
        "key_alias": metadata.get("user_api_key_alias"),
        "key_hash": metadata.get("user_api_key_hash"),
        "call_type": payload.get("call_type"),
        "model_group": payload.get("model_group"),
        "model": payload.get("model"),
        "api_base": payload.get("api_base"),
        "stream": payload.get("stream"),
        "cache_hit": payload.get("cache_hit"),
        "prompt_tokens": payload.get("prompt_tokens") or 0,
        "completion_tokens": payload.get("completion_tokens") or 0,
        "total_tokens": payload.get("total_tokens") or 0,
        "cost_usd": payload.get("response_cost") or 0.0,
        "latency_ms": round((end - start) * 1000) if start and end else None,
        "key_spend_usd": metadata.get("user_api_key_spend"),
        "key_max_budget_usd": metadata.get("user_api_key_max_budget"),
        "team_spend_usd": metadata.get("user_api_key_team_spend"),
        "team_max_budget_usd": metadata.get("user_api_key_team_max_budget"),
        "error_class": error.get("error_class"),
        "error_code": error.get("error_code"),
    }


class CostAuditLogger(CustomLogger):
    def __init__(self) -> None:
        super().__init__()
        self.audit = logging.getLogger("ai_gateway.audit")
        if not self.audit.handlers:
            handler = logging.StreamHandler(sys.stdout)
            handler.setFormatter(logging.Formatter("%(message)s"))
            self.audit.addHandler(handler)
            self.audit.setLevel(logging.INFO)
            self.audit.propagate = False

    async def async_log_success_event(self, kwargs, response_obj, start_time, end_time):
        self.record(kwargs)

    async def async_log_failure_event(self, kwargs, response_obj, start_time, end_time):
        self.record(kwargs)

    def record(self, kwargs: dict[str, Any]) -> None:
        payload = kwargs.get("standard_logging_object")
        if not payload:
            return
        try:
            event = build_audit_event(payload)
            labels = {
                "client": event["client"],
                "team": _label(event["team_alias"] or event["team_id"]),
                "model": _label(event["model_group"]),
            }
            REQUESTS.labels(**labels, status=_label(event["status"], "unknown")).inc()
            if event["cost_usd"]:
                SPEND.labels(**labels).inc(event["cost_usd"])
            TOKENS.labels(**labels, type="prompt").inc(event["prompt_tokens"])
            TOKENS.labels(**labels, type="completion").inc(event["completion_tokens"])
            self.audit.info(json.dumps(event, default=str, ensure_ascii=False))
        except Exception:  # auditoria nunca pode derrubar a requisição
            logging.getLogger(__name__).exception("Falha ao registrar evento de auditoria")


cost_audit_logger = CostAuditLogger()
