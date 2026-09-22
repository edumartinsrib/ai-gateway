import pytest
from fastapi import HTTPException
from litellm.proxy._types import LitellmUserRoles, UserAPIKeyAuth

from ai_gateway.hooks import PolicyHook


async def test_key_without_team_is_rejected():
    hook = PolicyHook(require_team=True, max_output_tokens=1000)
    with pytest.raises(HTTPException) as exc:
        await hook.async_pre_call_hook(UserAPIKeyAuth(user_id="alice"), None, {}, "completion")
    assert exc.value.status_code == 403


async def test_admin_without_team_is_allowed():
    hook = PolicyHook(require_team=True, max_output_tokens=1000)
    admin = UserAPIKeyAuth(user_role=LitellmUserRoles.PROXY_ADMIN)
    assert await hook.async_pre_call_hook(admin, None, {"model": "m"}, "completion") == {"model": "m"}


async def test_team_requirement_can_be_disabled():
    hook = PolicyHook(require_team=False, max_output_tokens=1000)
    assert await hook.async_pre_call_hook(UserAPIKeyAuth(user_id="alice"), None, {}, "completion") == {}


async def test_output_tokens_are_clamped():
    hook = PolicyHook(require_team=True, max_output_tokens=1000)
    data = {"max_tokens": 5000, "max_completion_tokens": 500, "max_output_tokens": 99999}
    result = await hook.async_pre_call_hook(UserAPIKeyAuth(team_id="platform"), None, data, "completion")
    assert result == {"max_tokens": 1000, "max_completion_tokens": 500, "max_output_tokens": 1000}


async def test_env_configuration(monkeypatch):
    monkeypatch.setenv("GATEWAY_REQUIRE_TEAM", "false")
    monkeypatch.setenv("GATEWAY_MAX_OUTPUT_TOKENS", "256")
    hook = PolicyHook()
    assert hook.require_team is False
    assert hook.max_output_tokens == 256
