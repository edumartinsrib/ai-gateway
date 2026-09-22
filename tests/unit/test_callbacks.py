import io
import json
import logging

from prometheus_client import REGISTRY

from ai_gateway.callbacks import CostAuditLogger, build_audit_event, classify_client


def payload(**overrides):
    base = {
        "id": "req-1",
        "status": "success",
        "call_type": "acompletion",
        "model_group": "gpt-5.4-mini",
        "model": "azure/gpt-5.4-mini",
        "prompt_tokens": 1000,
        "completion_tokens": 500,
        "total_tokens": 1500,
        "response_cost": 0.00125,
        "startTime": 100.0,
        "endTime": 101.5,
        "user_agent": "Mozilla/5.0 Code/1.104.0 Electron",
        "metadata": {
            "user_api_key_user_id": "alice",
            "user_api_key_team_id": "platform",
            "user_api_key_team_alias": "platform-engineering",
            "user_api_key_alias": "alice-vscode",
            "user_api_key_auth_metadata": {},
        },
        "messages": [{"role": "user", "content": "segredo"}],
    }
    base.update(overrides)
    return base


def sample(name, **labels):
    return REGISTRY.get_sample_value(name, labels) or 0.0


def test_client_from_key_metadata_has_priority():
    p = payload()
    p["metadata"]["user_api_key_auth_metadata"] = {"client": "GitHub-App"}
    assert classify_client(p) == "github-app"


def test_client_from_user_agent():
    assert classify_client(payload(user_agent="GitHubCopilotChat/0.31.0")) == "vscode"
    assert classify_client(payload()) == "vscode"
    assert classify_client(payload(user_agent="ai-gateway-github-app/1.0")) == "github-app"
    assert classify_client(payload(user_agent="curl/8.5.0")) == "curl"
    assert classify_client(payload(user_agent="")) == "other"


def test_audit_event_never_contains_prompt():
    event = build_audit_event(payload())
    assert event["user_id"] == "alice"
    assert event["team_alias"] == "platform-engineering"
    assert event["cost_usd"] == 0.00125
    assert event["latency_ms"] == 1500
    assert "segredo" not in json.dumps(event)


def test_record_emits_json_and_metrics():
    labels = {"client": "vscode", "team": "platform-engineering", "model": "gpt-5.4-mini"}
    before_spend = sample("ai_gateway_spend_usd_total", **labels)
    before_req = sample("ai_gateway_requests_total", **labels, status="success")

    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    audit = logging.getLogger("ai_gateway.audit")
    audit.addHandler(handler)
    try:
        CostAuditLogger().record({"standard_logging_object": payload()})
    finally:
        audit.removeHandler(handler)

    line = json.loads(stream.getvalue().strip().splitlines()[-1])
    assert line["event"] == "llm_request"
    assert line["client"] == "vscode"
    assert sample("ai_gateway_spend_usd_total", **labels) - before_spend == 0.00125
    assert sample("ai_gateway_requests_total", **labels, status="success") - before_req == 1
    assert sample("ai_gateway_tokens_total", **labels, type="completion") >= 500


def test_record_ignores_missing_payload():
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    audit = logging.getLogger("ai_gateway.audit")
    audit.addHandler(handler)
    try:
        CostAuditLogger().record({})
    finally:
        audit.removeHandler(handler)
    assert stream.getvalue() == ""
