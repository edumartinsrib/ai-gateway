"""SDK de administração do gateway (times, usuários, virtual keys e orçamentos).

Encapsula a API de gerenciamento do LiteLLM Proxy com operações idempotentes, para que o
provisionamento possa ser declarativo (`gatewayctl apply -f provisioning/tenants.yaml`).

Exemplo:

    from ai_gateway.admin import GatewayAdmin

    with GatewayAdmin("https://ai-gateway.example.com", master_key) as gw:
        gw.ensure_team("platform", alias="Platform", max_budget=500, budget_duration="30d")
        gw.ensure_user("alice", email="alice@example.com", max_budget=50)
        gw.ensure_team_member("platform", "alice", max_budget_in_team=40)
        key = gw.generate_key(user_id="alice", team_id="platform", key_alias="alice-vscode")
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import httpx


class GatewayAdminError(RuntimeError):
    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


@dataclass
class Change:
    kind: str  # team | user | member
    name: str
    action: str  # created | updated | unchanged
    fields: list[str] = field(default_factory=list)

    def __str__(self) -> str:
        suffix = f" ({', '.join(self.fields)})" if self.fields else ""
        return f"{self.kind:<6} {self.name:<32} {self.action}{suffix}"


def _diff(current: dict[str, Any], desired: dict[str, Any]) -> list[str]:
    changed = []
    for key, value in desired.items():
        if value is None:
            continue
        have = current.get(key)
        if isinstance(value, list):
            if sorted(value) != sorted(have or []):
                changed.append(key)
        elif have != value:
            changed.append(key)
    return changed


class GatewayAdmin:
    def __init__(
        self,
        base_url: str,
        master_key: str,
        *,
        timeout: float = 30.0,
        transport: httpx.BaseTransport | None = None,
        dry_run: bool = False,
    ) -> None:
        self.dry_run = dry_run
        self._http = httpx.Client(
            base_url=base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {master_key}"},
            timeout=timeout,
            transport=transport,
        )

    def __enter__(self) -> GatewayAdmin:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        self._http.close()

    # ------------------------------------------------------------------ HTTP
    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        response = self._http.request(method, path, **kwargs)
        if response.status_code >= 400:
            raise GatewayAdminError(
                f"{method} {path} -> {response.status_code}: {response.text[:500]}", response.status_code
            )
        return response.json() if response.content else None

    def _get_or_none(self, path: str, params: dict[str, str]) -> dict[str, Any] | None:
        try:
            return self._request("GET", path, params=params)
        except GatewayAdminError as exc:
            # O LiteLLM responde 400 ou 404 para entidade inexistente, dependendo da rota.
            if exc.status_code in (400, 404):
                return None
            raise

    # ----------------------------------------------------------------- Times
    def get_team(self, team_id: str) -> dict[str, Any] | None:
        info = self._get_or_none("/team/info", {"team_id": team_id})
        return info.get("team_info") if info else None

    def ensure_team(
        self,
        team_id: str,
        *,
        alias: str | None = None,
        max_budget: float | None = None,
        soft_budget: float | None = None,
        budget_duration: str | None = None,
        models: list[str] | None = None,
        tpm_limit: int | None = None,
        rpm_limit: int | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> Change:
        desired = {
            "team_alias": alias or team_id,
            "max_budget": max_budget,
            "soft_budget": soft_budget,
            "budget_duration": budget_duration,
            "models": models,
            "tpm_limit": tpm_limit,
            "rpm_limit": rpm_limit,
        }
        body = {"team_id": team_id, **{k: v for k, v in desired.items() if v is not None}}
        if metadata:
            body["metadata"] = metadata

        current = self.get_team(team_id)
        if current is None:
            if not self.dry_run:
                self._request("POST", "/team/new", json=body)
            return Change("team", team_id, "created")

        changed = _diff(current, desired)
        if changed and not self.dry_run:
            self._request("POST", "/team/update", json=body)
        return Change("team", team_id, "updated" if changed else "unchanged", changed)

    def ensure_team_member(
        self,
        team_id: str,
        user_id: str,
        *,
        role: str = "user",
        max_budget_in_team: float | None = None,
        budget_duration: str | None = None,
    ) -> Change:
        """Garante o usuário no time, com teto de gasto próprio dentro do time.

        `budget_duration` define quando o gasto do membro reinicia; sem ele o teto do
        membro nunca é zerado. O papel "admin" de time exige LiteLLM Enterprise.
        """
        name = f"{team_id}/{user_id}"
        info = self._get_or_none("/team/info", {"team_id": team_id}) or {}
        team = info.get("team_info") or {}
        members = {m.get("user_id"): m for m in team.get("members_with_roles") or []}
        memberships = {m.get("user_id"): m for m in info.get("team_memberships") or []}

        if user_id not in members:
            body: dict[str, Any] = {"team_id": team_id, "member": {"user_id": user_id, "role": role}}
            if max_budget_in_team is not None:
                body["max_budget_in_team"] = max_budget_in_team
            if budget_duration is not None:
                body["budget_duration"] = budget_duration
            if not self.dry_run:
                self._request("POST", "/team/member_add", json=body)
            return Change("member", name, "created")

        budget = (memberships.get(user_id) or {}).get("litellm_budget_table") or {}
        current = {"role": members[user_id].get("role"), **budget}
        desired = {"role": role, "max_budget": max_budget_in_team, "budget_duration": budget_duration}
        changed = _diff(current, desired)
        if changed and not self.dry_run:
            body = {"team_id": team_id, "user_id": user_id, "role": role}
            if max_budget_in_team is not None:
                body["max_budget_in_team"] = max_budget_in_team
            if budget_duration is not None:
                body["budget_duration"] = budget_duration
            self._request("POST", "/team/member_update", json=body)
        return Change("member", name, "updated" if changed else "unchanged", changed)

    # -------------------------------------------------------------- Usuários
    def get_user(self, user_id: str) -> dict[str, Any] | None:
        info = self._get_or_none("/user/info", {"user_id": user_id})
        if not info or not info.get("user_info"):
            return None
        return info["user_info"]

    def ensure_user(
        self,
        user_id: str,
        *,
        email: str | None = None,
        max_budget: float | None = None,
        budget_duration: str | None = None,
        models: list[str] | None = None,
        tpm_limit: int | None = None,
        rpm_limit: int | None = None,
        role: str = "internal_user",
    ) -> Change:
        desired = {
            "user_email": email,
            "max_budget": max_budget,
            "budget_duration": budget_duration,
            "models": models,
            "tpm_limit": tpm_limit,
            "rpm_limit": rpm_limit,
            "user_role": role,
        }
        body = {"user_id": user_id, **{k: v for k, v in desired.items() if v is not None}}

        current = self.get_user(user_id)
        if current is None:
            if not self.dry_run:
                # auto_create_key=False: chaves são emitidas explicitamente, uma por cliente.
                self._request("POST", "/user/new", json={**body, "auto_create_key": False})
            return Change("user", user_id, "created")

        changed = _diff(current, desired)
        if changed and not self.dry_run:
            self._request("POST", "/user/update", json=body)
        return Change("user", user_id, "updated" if changed else "unchanged", changed)

    # ---------------------------------------------------------- Virtual keys
    def generate_key(
        self,
        *,
        user_id: str | None = None,
        team_id: str | None = None,
        key_alias: str | None = None,
        max_budget: float | None = None,
        budget_duration: str | None = None,
        duration: str | None = None,
        models: list[str] | None = None,
        tpm_limit: int | None = None,
        rpm_limit: int | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Gera uma virtual key. O valor (`key`) só é devolvido nesta chamada."""
        body = {
            "user_id": user_id,
            "team_id": team_id,
            "key_alias": key_alias,
            "max_budget": max_budget,
            "budget_duration": budget_duration,
            "duration": duration,
            "models": models,
            "tpm_limit": tpm_limit,
            "rpm_limit": rpm_limit,
            "metadata": metadata,
        }
        return self._request("POST", "/key/generate", json={k: v for k, v in body.items() if v is not None})

    def list_keys(self, *, user_id: str | None = None, team_id: str | None = None) -> list[dict[str, Any]]:
        params = {"return_full_object": "true", "size": "100", "page": "1"}
        if user_id:
            params["user_id"] = user_id
        if team_id:
            params["team_id"] = team_id
        keys: list[dict[str, Any]] = []
        while True:
            page = self._request("GET", "/key/list", params=params)
            keys.extend(page.get("keys") or [])
            if int(params["page"]) >= int(page.get("total_pages") or 1):
                return keys
            params["page"] = str(int(params["page"]) + 1)

    def delete_keys(self, *, aliases: list[str]) -> dict[str, Any]:
        return self._request("POST", "/key/delete", json={"key_aliases": aliases})

    # ----------------------------------------------------------------- Gasto
    def spend_summary(self, team_ids: list[str]) -> list[dict[str, Any]]:
        """Gasto x orçamento de cada time e de cada membro."""
        rows = []
        for team_id in team_ids:
            info = self._get_or_none("/team/info", {"team_id": team_id})
            if not info:
                continue
            team = info.get("team_info") or {}
            rows.append(
                {
                    "scope": "team",
                    "name": team_id,
                    "spend": team.get("spend") or 0.0,
                    "max_budget": team.get("max_budget"),
                    "budget_reset_at": team.get("budget_reset_at"),
                }
            )
            for membership in info.get("team_memberships") or []:
                budget = membership.get("litellm_budget_table") or {}
                rows.append(
                    {
                        "scope": "member",
                        "name": f"{team_id}/{membership.get('user_id')}",
                        "spend": membership.get("spend") or 0.0,
                        "max_budget": budget.get("max_budget"),
                        "budget_reset_at": budget.get("budget_reset_at"),
                    }
                )
        return rows
