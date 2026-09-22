"""Políticas aplicadas antes de cada chamada ao LLM.

- Toda chave (exceto a master key) precisa pertencer a um time: é o time que paga a conta
  e é por ele que o custo é rateado. Desative com `GATEWAY_REQUIRE_TEAM=false`.
- Limita o máximo de tokens de saída por requisição (`GATEWAY_MAX_OUTPUT_TOKENS`), o que
  impõe um teto ao custo de uma única chamada.
"""

from __future__ import annotations

import os

from fastapi import HTTPException
from litellm.integrations.custom_logger import CustomLogger
from litellm.proxy._types import LitellmUserRoles, UserAPIKeyAuth

OUTPUT_TOKEN_FIELDS = ("max_tokens", "max_completion_tokens", "max_output_tokens")


def _env_bool(name: str, default: bool) -> bool:
    return os.getenv(name, str(default)).strip().lower() in {"1", "true", "yes", "on"}


class PolicyHook(CustomLogger):
    def __init__(self, require_team: bool | None = None, max_output_tokens: int | None = None) -> None:
        super().__init__()
        self.require_team = _env_bool("GATEWAY_REQUIRE_TEAM", True) if require_team is None else require_team
        self.max_output_tokens = (
            int(os.getenv("GATEWAY_MAX_OUTPUT_TOKENS", "32768"))
            if max_output_tokens is None
            else max_output_tokens
        )

    async def async_pre_call_hook(self, user_api_key_dict: UserAPIKeyAuth, cache, data: dict, call_type):
        is_admin = user_api_key_dict.user_role == LitellmUserRoles.PROXY_ADMIN
        if self.require_team and not user_api_key_dict.team_id and not is_admin:
            raise HTTPException(
                status_code=403,
                detail={
                    "error": "Esta chave não pertence a nenhum time. Gere uma chave de time "
                    "(gatewayctl key create --team ...) para que o custo seja atribuído."
                },
            )

        for field in OUTPUT_TOKEN_FIELDS:
            value = data.get(field)
            if isinstance(value, int) and value > self.max_output_tokens:
                data[field] = self.max_output_tokens
        return data


policy_hook = PolicyHook()
