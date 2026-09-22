"""Testes de ponta a ponta contra um gateway rodando com o mock da Azure.

    make up && make test-integration          # docker compose
    GATEWAY_URL=http://localhost:4000 METRICS_URL=http://localhost:9090 \
        pytest -m integration tests/integration

Usam a master key para provisionar e o SDK oficial da OpenAI como cliente, exatamente
como VS Code e GitHub App consomem o gateway.
"""

from __future__ import annotations

import os
import time
import uuid
from pathlib import Path

import httpx
import openai
import pytest

from ai_gateway.admin import GatewayAdmin
from ai_gateway.cli import main as gatewayctl

pytestmark = pytest.mark.integration

GATEWAY_URL = os.getenv("GATEWAY_URL", "http://localhost:4000")
METRICS_URL = os.getenv("METRICS_URL", "http://localhost:9090")
MASTER_KEY = os.getenv("LITELLM_MASTER_KEY", "sk-local-master-key")
TENANTS = Path(__file__).resolve().parents[2] / "provisioning" / "tenants.yaml"
RUN = uuid.uuid4().hex[:6]


@pytest.fixture(scope="module")
def admin():
    try:
        httpx.get(f"{GATEWAY_URL}/health/liveliness", timeout=5).raise_for_status()
    except httpx.HTTPError:
        pytest.skip(f"gateway indisponível em {GATEWAY_URL}")
    with GatewayAdmin(GATEWAY_URL, MASTER_KEY) as gw:
        yield gw


@pytest.fixture(scope="module")
def provisioned(admin, monkeypatch_module):
    monkeypatch_module.setenv("LITELLM_MASTER_KEY", MASTER_KEY)
    gatewayctl(["--url", GATEWAY_URL, "apply", "-f", str(TENANTS)])
    return admin


@pytest.fixture(scope="module")
def monkeypatch_module():
    with pytest.MonkeyPatch.context() as mp:
        yield mp


def client_for(key: str, user_agent: str | None = None) -> openai.OpenAI:
    headers = {"User-Agent": user_agent} if user_agent else None
    return openai.OpenAI(base_url=f"{GATEWAY_URL}/v1", api_key=key, default_headers=headers, max_retries=0)


def new_key(admin: GatewayAdmin, **kwargs) -> str:
    return admin.generate_key(key_alias=f"e2e-{RUN}-{uuid.uuid4().hex[:4]}", **kwargs)["key"]


def test_apply_is_idempotent(provisioned, capsys):
    gatewayctl(["--url", GATEWAY_URL, "apply", "-f", str(TENANTS)])
    out = capsys.readouterr().out
    assert "created" not in out and "updated" not in out, out


def test_user_key_chat_and_cost(provisioned):
    key = new_key(provisioned, user_id="alice", team_id="platform", metadata={"client": "vscode"})
    raw = client_for(key).chat.completions.with_raw_response.create(
        model="gpt-5.4", messages=[{"role": "user", "content": "oi"}]
    )
    completion = raw.parse()
    assert completion.choices[0].message.content
    # 1000 tokens de entrada + 500 de saída no preço do gpt-5.4 na Azure.
    assert float(raw.headers["x-litellm-response-cost"]) == pytest.approx(0.01)


def test_streaming(provisioned):
    key = new_key(provisioned, user_id="bob", team_id="platform")
    stream = client_for(key, "Code/1.104.0").chat.completions.create(
        model="gpt-5.4-mini", messages=[{"role": "user", "content": "oi"}], stream=True
    )
    text = "".join(chunk.choices[0].delta.content or "" for chunk in stream if chunk.choices)
    assert "mock" in text


def test_embeddings(provisioned):
    key = new_key(provisioned, user_id="alice", team_id="platform")
    result = client_for(key).embeddings.create(model="text-embedding-3-small", input=["a", "b"])
    assert len(result.data) == 2


def test_team_model_restriction(provisioned):
    key = new_key(provisioned, team_id="github-app", metadata={"client": "github-app"})
    client = client_for(key, "ai-gateway-github-app/1.0")
    assert client.chat.completions.create(model="gpt-5.4-mini", messages=[{"role": "user", "content": "oi"}])
    with pytest.raises(openai.PermissionDeniedError):
        client.chat.completions.create(model="gpt-5.4", messages=[{"role": "user", "content": "oi"}])


def test_key_without_team_is_rejected(provisioned):
    key = new_key(provisioned, user_id="bob")
    with pytest.raises(openai.PermissionDeniedError, match="time"):
        client_for(key).chat.completions.create(
            model="gpt-5.4-mini", messages=[{"role": "user", "content": "oi"}]
        )


def test_key_budget_is_enforced(provisioned):
    key = new_key(provisioned, user_id="alice", team_id="platform", max_budget=0.005)
    client = client_for(key)
    client.chat.completions.create(
        model="gpt-5.4", messages=[{"role": "user", "content": "oi"}]
    )  # custa 0.01
    with pytest.raises(openai.RateLimitError, match=r"[Bb]udget"):
        for _ in range(5):  # o gasto é contabilizado de forma assíncrona
            client.chat.completions.create(model="gpt-5.4", messages=[{"role": "user", "content": "oi"}])
            time.sleep(0.5)


def test_prometheus_metrics(provisioned):
    body = httpx.get(f"{METRICS_URL}/metrics/", timeout=10, follow_redirects=True).text
    assert "litellm_spend_metric_total" in body
    assert 'ai_gateway_spend_usd_total{client="vscode"' in body
    assert "litellm_remaining_team_budget_metric" in body
