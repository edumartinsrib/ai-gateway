import hashlib
import hmac
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "clients" / "github-app"))
import app as gh_app

SECRET = "webhook-secret"


@pytest.fixture(scope="module")
def rsa_key():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode()
    return pem, key.public_key()


def sign(body: bytes) -> str:
    return "sha256=" + hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()


def pr_payload(action="opened", draft=False):
    return {
        "action": action,
        "installation": {"id": 42},
        "repository": {"full_name": "acme/api"},
        "pull_request": {"number": 7, "title": "Adiciona cache", "body": "desc", "draft": draft},
    }


def test_verify_signature():
    body = b'{"a": 1}'
    assert gh_app.verify_signature(SECRET, body, sign(body))
    assert not gh_app.verify_signature(SECRET, body, sign(b"outro"))
    assert not gh_app.verify_signature(SECRET, body, None)


def test_app_jwt_is_valid_for_github(rsa_key):
    pem, public_key = rsa_key
    token = gh_app.app_jwt("123", pem, now=1_000_000)
    claims = jwt.decode(
        token, public_key, algorithms=["RS256"], options={"verify_exp": False, "verify_iat": False}
    )
    assert claims["iss"] == "123"
    assert claims["exp"] - claims["iat"] <= 600


def test_truncate_diff():
    assert gh_app.truncate_diff("abc", 10) == "abc"
    assert gh_app.truncate_diff("a" * 20, 10).startswith("a" * 10 + "\n\n[diff truncado: 10")


class FakeGitHub:
    def __init__(self):
        self.comments = []
        self.calls = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.calls.append((request.method, request.url.path))
        path = request.url.path
        if path == "/app/installations/42/access_tokens":
            return httpx.Response(201, json={"token": "ghs_installation"})
        assert request.headers["Authorization"] == "Bearer ghs_installation"
        if path == "/repos/acme/api/pulls/7":
            return httpx.Response(200, text="diff --git a/x b/x\n+print('oi')\n")
        if path == "/repos/acme/api/issues/7/comments" and request.method == "GET":
            return httpx.Response(200, json=self.comments)
        if path == "/repos/acme/api/issues/7/comments" and request.method == "POST":
            self.comments.append({"id": 1, "body": json.loads(request.content)["body"]})
            return httpx.Response(201, json=self.comments[-1])
        if path == "/repos/acme/api/issues/comments/1" and request.method == "PATCH":
            self.comments[0]["body"] = json.loads(request.content)["body"]
            return httpx.Response(200, json=self.comments[0])
        return httpx.Response(404)


class FakeLLM:
    def __init__(self):
        self.requests = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    def create(self, **kwargs):
        self.requests.append(kwargs)
        message = SimpleNamespace(content=f"revisão {len(self.requests)}")
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])


def test_review_posts_then_updates_single_comment(monkeypatch, rsa_key):
    monkeypatch.setenv("GITHUB_APP_ID", "123")
    monkeypatch.setenv("GITHUB_APP_PRIVATE_KEY", rsa_key[0])
    github, llm = FakeGitHub(), FakeLLM()
    http = httpx.Client(base_url="https://api.github.test", transport=httpx.MockTransport(github.handler))
    reviewer = gh_app.Reviewer(http=http, llm=llm)

    reviewer.review(pr_payload())
    reviewer.review(pr_payload("synchronize"))

    assert len(github.comments) == 1
    assert gh_app.COMMENT_MARKER in github.comments[0]["body"]
    assert "revisão 2" in github.comments[0]["body"]
    assert llm.requests[0]["model"] == "gpt-5.4-mini"
    assert "print('oi')" in llm.requests[0]["messages"][1]["content"]


def test_webhook(monkeypatch):
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", SECRET)
    received = []
    monkeypatch.setattr(gh_app, "run_review", received.append)
    client = TestClient(gh_app.app)

    def post(event, payload, signature=None):
        body = json.dumps(payload).encode()
        headers = {"X-GitHub-Event": event, "X-Hub-Signature-256": signature or sign(body)}
        return client.post("/webhook", content=body, headers=headers)

    assert post("pull_request", pr_payload(), signature="sha256=bad").status_code == 401
    assert post("issues", {"action": "opened"}).json()["status"] == "ignored"
    assert post("pull_request", pr_payload("closed")).json()["status"] == "ignored"
    assert post("pull_request", pr_payload(draft=True)).json()["reason"] == "draft"
    response = post("pull_request", pr_payload())
    assert response.status_code == 202
    assert received and received[0]["pull_request"]["number"] == 7
