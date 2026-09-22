import json

import httpx
import pytest

from ai_gateway.admin import GatewayAdmin, GatewayAdminError


class FakeLiteLLM:
    """Implementa o subconjunto da API de gerenciamento do LiteLLM usado pelo SDK."""

    def __init__(self):
        self.teams, self.users, self.memberships, self.keys = {}, {}, {}, []
        self.writes = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer sk-master"
        path, params = request.url.path, request.url.params
        body = json.loads(request.content) if request.content else {}
        if request.method == "POST":
            self.writes.append(path)

        if path == "/team/info":
            team = self.teams.get(params["team_id"])
            if not team:
                return httpx.Response(404, json={"detail": "not found"})
            memberships = [m for (t, _), m in self.memberships.items() if t == team["team_id"]]
            return httpx.Response(200, json={"team_info": team, "team_memberships": memberships})
        if path in ("/team/new", "/team/update"):
            team = self.teams.setdefault(
                body["team_id"], {"team_id": body["team_id"], "members_with_roles": []}
            )
            team.update({k: v for k, v in body.items() if k != "metadata"})
            return httpx.Response(200, json=team)
        if path == "/team/member_add":
            member = body["member"]
            self.teams[body["team_id"]]["members_with_roles"].append(member)
            self.memberships[(body["team_id"], member["user_id"])] = {
                "user_id": member["user_id"],
                "spend": 0.0,
                "litellm_budget_table": {
                    "max_budget": body.get("max_budget_in_team"),
                    "budget_duration": body.get("budget_duration"),
                },
            }
            return httpx.Response(200, json={})
        if path == "/team/member_update":
            budget = self.memberships[(body["team_id"], body["user_id"])]["litellm_budget_table"]
            budget.update(
                max_budget=body.get("max_budget_in_team"), budget_duration=body.get("budget_duration")
            )
            return httpx.Response(200, json={})
        if path == "/user/info":
            user = self.users.get(params["user_id"])
            return httpx.Response(200, json={"user_info": user}) if user else httpx.Response(404, json={})
        if path in ("/user/new", "/user/update"):
            self.users.setdefault(body["user_id"], {}).update(body)
            return httpx.Response(200, json=body)
        if path == "/key/generate":
            key = {**body, "key": f"sk-{len(self.keys)}"}
            self.keys.append(key)
            return httpx.Response(200, json=key)
        if path == "/key/list":
            page, size = int(params["page"]), int(params["size"])
            chunk = self.keys[(page - 1) * size : page * size]
            return httpx.Response(
                200, json={"keys": chunk, "total_pages": max(1, -(-len(self.keys) // size))}
            )
        return httpx.Response(500, text=f"rota inesperada {path}")


@pytest.fixture
def api():
    return FakeLiteLLM()


def admin(api, dry_run=False):
    return GatewayAdmin("http://gw", "sk-master", transport=httpx.MockTransport(api), dry_run=dry_run)


def test_team_lifecycle(api):
    gw = admin(api)
    assert (
        gw.ensure_team("platform", max_budget=100, budget_duration="30d", models=["a", "b"]).action
        == "created"
    )
    assert (
        gw.ensure_team("platform", max_budget=100, budget_duration="30d", models=["b", "a"]).action
        == "unchanged"
    )
    change = gw.ensure_team("platform", max_budget=200, budget_duration="30d", models=["a", "b"])
    assert (change.action, change.fields) == ("updated", ["max_budget"])
    assert api.teams["platform"]["max_budget"] == 200


def test_dry_run_never_writes(api):
    gw = admin(api, dry_run=True)
    assert gw.ensure_team("platform", max_budget=1).action == "created"
    assert gw.ensure_user("alice", max_budget=1).action == "created"
    assert api.writes == []


def test_user_is_created_without_auto_key(api):
    gw = admin(api)
    gw.ensure_user("alice", email="alice@example.com", max_budget=50, budget_duration="30d")
    assert api.users["alice"]["auto_create_key"] is False
    assert (
        gw.ensure_user("alice", email="alice@example.com", max_budget=50, budget_duration="30d").action
        == "unchanged"
    )


def test_member_budget_drift_is_corrected(api):
    gw = admin(api)
    gw.ensure_team("platform")
    assert (
        gw.ensure_team_member("platform", "alice", max_budget_in_team=10, budget_duration="30d").action
        == "created"
    )
    assert (
        gw.ensure_team_member("platform", "alice", max_budget_in_team=10, budget_duration="30d").action
        == "unchanged"
    )
    change = gw.ensure_team_member("platform", "alice", max_budget_in_team=20, budget_duration="30d")
    assert (change.action, change.fields) == ("updated", ["max_budget"])
    assert api.memberships[("platform", "alice")]["litellm_budget_table"]["max_budget"] == 20


def test_generate_key_drops_unset_fields(api):
    result = admin(api).generate_key(user_id="alice", team_id="platform", key_alias="alice-vscode")
    assert result["key"] == "sk-0"
    assert set(api.keys[0]) == {"user_id", "team_id", "key_alias", "key"}


def test_list_keys_follows_pagination(api):
    api.keys = [{"key_alias": f"k{i}"} for i in range(250)]
    assert len(admin(api).list_keys()) == 250


def test_http_errors_are_raised(api):
    with pytest.raises(GatewayAdminError) as exc:
        admin(api)._request("GET", "/nao-existe")
    assert exc.value.status_code == 500
