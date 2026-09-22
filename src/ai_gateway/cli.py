"""gatewayctl — administração do gateway pela linha de comando.

Variáveis de ambiente:
  GATEWAY_URL          URL do gateway (padrão: http://localhost:4000)
  LITELLM_MASTER_KEY   master key (necessária para todas as operações)
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

import yaml

from ai_gateway.admin import GatewayAdmin, GatewayAdminError


def load_tenants(path: Path) -> dict[str, Any]:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    users = {u["id"]: u for u in data.get("users") or []}
    for team in data.get("teams") or []:
        for member in team.get("members") or []:
            if member["user"] not in users:
                raise SystemExit(f"Time {team['id']!r} referencia usuário inexistente: {member['user']!r}")
    return data


def _money(value: float | None) -> str:
    return "∞" if value is None else f"{value:.2f}"


def cmd_apply(gw: GatewayAdmin, args: argparse.Namespace) -> None:
    tenants = load_tenants(Path(args.file))
    changes = []
    for user in tenants.get("users") or []:
        changes.append(
            gw.ensure_user(
                user["id"],
                email=user.get("email"),
                max_budget=user.get("max_budget"),
                budget_duration=user.get("budget_duration"),
                models=user.get("models"),
                tpm_limit=user.get("tpm_limit"),
                rpm_limit=user.get("rpm_limit"),
            )
        )
    for team in tenants.get("teams") or []:
        changes.append(
            gw.ensure_team(
                team["id"],
                alias=team.get("alias"),
                max_budget=team.get("max_budget"),
                soft_budget=team.get("soft_budget"),
                budget_duration=team.get("budget_duration"),
                models=team.get("models"),
                tpm_limit=team.get("tpm_limit"),
                rpm_limit=team.get("rpm_limit"),
                metadata=team.get("metadata"),
            )
        )
        for member in team.get("members") or []:
            changes.append(
                gw.ensure_team_member(
                    team["id"],
                    member["user"],
                    role=member.get("role", "user"),
                    max_budget_in_team=member.get("max_budget_in_team"),
                    budget_duration=member.get("budget_duration", team.get("budget_duration")),
                )
            )
    prefix = "[dry-run] " if args.dry_run else ""
    for change in changes:
        print(f"{prefix}{change}")


def cmd_key_create(gw: GatewayAdmin, args: argparse.Namespace) -> None:
    metadata = {"client": args.client} if args.client else None
    result = gw.generate_key(
        user_id=args.user,
        team_id=args.team,
        key_alias=args.alias,
        max_budget=args.max_budget,
        budget_duration=args.budget_duration,
        duration=args.duration,
        models=args.models,
        rpm_limit=args.rpm_limit,
        tpm_limit=args.tpm_limit,
        metadata=metadata,
    )
    if args.json:
        print(json.dumps(result, indent=2, default=str))
        return
    print(f"alias:        {result.get('key_alias')}")
    print(f"time/usuário: {result.get('team_id')} / {result.get('user_id')}")
    print(f"orçamento:    {result.get('max_budget')} USD a cada {result.get('budget_duration')}")
    print(f"expira em:    {result.get('expires')}")
    print(f"\nchave (exibida apenas agora):\n{result['key']}", file=sys.stderr)


def cmd_key_list(gw: GatewayAdmin, args: argparse.Namespace) -> None:
    keys = gw.list_keys(user_id=args.user, team_id=args.team)
    print(f"{'ALIAS':<32} {'TIME':<20} {'USUÁRIO':<20} {'GASTO':>10} {'ORÇAMENTO':>10}  EXPIRA")
    for key in keys:
        budget = key.get("max_budget")
        print(
            f"{(key.get('key_alias') or key.get('key_name') or '-'):<32} {(key.get('team_id') or '-'):<20} "
            f"{(key.get('user_id') or '-'):<20} {key.get('spend') or 0:>10.4f} "
            f"{_money(budget):>10}  {key.get('expires') or '-'}"
        )


def cmd_key_delete(gw: GatewayAdmin, args: argparse.Namespace) -> None:
    gw.delete_keys(aliases=args.aliases)
    print(f"Removidas: {', '.join(args.aliases)}")


def cmd_spend(gw: GatewayAdmin, args: argparse.Namespace) -> None:
    team_ids = args.team or [t["id"] for t in load_tenants(Path(args.file)).get("teams") or []]
    print(f"{'ESCOPO':<8} {'NOME':<40} {'GASTO (USD)':>12} {'ORÇAMENTO':>10} {'USO':>6}  RESET")
    for row in gw.spend_summary(team_ids):
        budget = row["max_budget"]
        usage = f"{row['spend'] / budget:.0%}" if budget else "-"
        print(
            f"{row['scope']:<8} {row['name']:<40} {row['spend']:>12.4f} "
            f"{_money(budget):>10} {usage:>6}  {row['budget_reset_at'] or '-'}"
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="gatewayctl", description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument("--url", default=os.getenv("GATEWAY_URL", "http://localhost:4000"))
    sub = parser.add_subparsers(dest="command", required=True)

    apply = sub.add_parser(
        "apply", help="Aplica times, usuários e orçamentos de um arquivo YAML (idempotente)"
    )
    apply.add_argument("-f", "--file", default="provisioning/tenants.yaml")
    apply.add_argument("--dry-run", action="store_true", help="Mostra o que mudaria sem aplicar")
    apply.set_defaults(func=cmd_apply)

    key = sub.add_parser("key", help="Virtual keys").add_subparsers(dest="key_command", required=True)

    create = key.add_parser("create", help="Gera uma virtual key para um usuário em um time")
    create.add_argument("--team", required=True)
    create.add_argument("--user", help="Omitir para chave de serviço (ex.: GitHub App)")
    create.add_argument("--alias", required=True, help="Nome único, ex.: alice-vscode")
    create.add_argument("--client", help="Cliente para métricas de custo, ex.: vscode, github-app")
    create.add_argument("--max-budget", type=float, help="Orçamento da chave em USD")
    create.add_argument("--budget-duration", help="Período de reset do orçamento, ex.: 30d")
    create.add_argument("--duration", help="Validade da chave, ex.: 90d")
    create.add_argument("--models", nargs="+", help="Restringe modelos (padrão: os do time)")
    create.add_argument("--rpm-limit", type=int)
    create.add_argument("--tpm-limit", type=int)
    create.add_argument("--json", action="store_true")
    create.set_defaults(func=cmd_key_create)

    listing = key.add_parser("list", help="Lista chaves com gasto e orçamento")
    listing.add_argument("--team")
    listing.add_argument("--user")
    listing.set_defaults(func=cmd_key_list)

    delete = key.add_parser("delete", help="Revoga chaves pelo alias")
    delete.add_argument("aliases", nargs="+")
    delete.set_defaults(func=cmd_key_delete)

    spend = sub.add_parser("spend", help="Gasto x orçamento por time e por membro")
    spend.add_argument("--team", nargs="+", help="Times a consultar (padrão: todos do arquivo)")
    spend.add_argument("-f", "--file", default="provisioning/tenants.yaml")
    spend.set_defaults(func=cmd_spend)
    return parser


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    master_key = os.getenv("LITELLM_MASTER_KEY")
    if not master_key:
        raise SystemExit("Defina LITELLM_MASTER_KEY")
    try:
        with GatewayAdmin(args.url, master_key, dry_run=getattr(args, "dry_run", False)) as gw:
            args.func(gw, args)
    except GatewayAdminError as exc:
        raise SystemExit(f"Erro do gateway: {exc}") from exc


if __name__ == "__main__":
    main()
