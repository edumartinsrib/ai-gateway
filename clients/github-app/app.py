"""GitHub App de revisão de Pull Requests que consome o AI Gateway.

Fluxo: webhook `pull_request` (opened/synchronize/reopened/ready_for_review) -> valida a
assinatura HMAC -> responde 202 na hora -> em background busca o diff com um token de
instalação, pede a revisão ao gateway (API compatível com OpenAI, virtual key do time
`github-app`) e publica/atualiza um único comentário no PR.

Variáveis de ambiente:
  GITHUB_APP_ID, GITHUB_APP_PRIVATE_KEY (PEM), GITHUB_WEBHOOK_SECRET
  GATEWAY_URL (ex.: http://ai-gateway.ai-gateway.svc:4000), GATEWAY_API_KEY (virtual key)
  GATEWAY_MODEL (padrão gpt-5.4-mini), MAX_DIFF_CHARS (padrão 60000)
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import time

import httpx
import jwt
import openai
from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Request

log = logging.getLogger("github-app")
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(levelname)s %(name)s %(message)s")

GITHUB_API = os.getenv("GITHUB_API_URL", "https://api.github.com")
USER_AGENT = "ai-gateway-github-app/1.0"
COMMENT_MARKER = "<!-- ai-gateway-review -->"
HANDLED_ACTIONS = {"opened", "synchronize", "reopened", "ready_for_review"}
SYSTEM_PROMPT = (
    "Você é um revisor de código sênior. Revise o diff do Pull Request e responda em português, "
    "em Markdown, com no máximo 10 itens objetivos: bugs, riscos de segurança, problemas de "
    "desempenho e sugestões concretas (cite arquivo e trecho). Se não houver nada relevante, diga isso."
)

app = FastAPI(title="ai-gateway-github-app")


def verify_signature(secret: str, body: bytes, signature: str | None) -> bool:
    if not signature or not signature.startswith("sha256="):
        return False
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


def app_jwt(app_id: str, private_key: str, now: int | None = None) -> str:
    now = int(time.time()) if now is None else now
    # iat 60s no passado tolera diferença de relógio; o GitHub aceita no máximo 10 min.
    return jwt.encode({"iat": now - 60, "exp": now + 540, "iss": app_id}, private_key, algorithm="RS256")


def truncate_diff(diff: str, limit: int) -> str:
    if len(diff) <= limit:
        return diff
    return diff[:limit] + f"\n\n[diff truncado: {len(diff) - limit} caracteres omitidos]"


class Reviewer:
    def __init__(self, http: httpx.Client | None = None, llm: openai.OpenAI | None = None) -> None:
        self.app_id = os.environ["GITHUB_APP_ID"]
        self.private_key = os.environ["GITHUB_APP_PRIVATE_KEY"]
        self.model = os.getenv("GATEWAY_MODEL", "gpt-5.4-mini")
        self.max_diff_chars = int(os.getenv("MAX_DIFF_CHARS", "60000"))
        self.http = http or httpx.Client(base_url=GITHUB_API, timeout=30, headers={"User-Agent": USER_AGENT})
        self.llm = llm or openai.OpenAI(
            base_url=os.environ["GATEWAY_URL"].rstrip("/") + "/v1",
            api_key=os.environ["GATEWAY_API_KEY"],
            default_headers={"User-Agent": USER_AGENT},
            timeout=300,
        )

    def installation_token(self, installation_id: int) -> str:
        response = self.http.post(
            f"/app/installations/{installation_id}/access_tokens",
            headers={
                "Authorization": f"Bearer {app_jwt(self.app_id, self.private_key)}",
                "Accept": "application/vnd.github+json",
            },
        )
        response.raise_for_status()
        return response.json()["token"]

    def review(self, payload: dict) -> None:
        pr = payload["pull_request"]
        repo = payload["repository"]["full_name"]
        number = pr["number"]
        token = self.installation_token(payload["installation"]["id"])
        auth = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}

        diff = self.http.get(
            f"/repos/{repo}/pulls/{number}", headers={**auth, "Accept": "application/vnd.github.diff"}
        )
        diff.raise_for_status()

        try:
            completion = self.llm.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {
                        "role": "user",
                        "content": f"# {pr['title']}\n\n{pr.get('body') or ''}\n\n```diff\n"
                        f"{truncate_diff(diff.text, self.max_diff_chars)}\n```",
                    },
                ],
                max_tokens=2000,
            )
            review = completion.choices[0].message.content or "(sem conteúdo)"
        except openai.RateLimitError as exc:
            # 429 do gateway: rate limit ou orçamento do time github-app esgotado.
            log.warning("Gateway recusou a revisão de %s#%s: %s", repo, number, exc)
            review = "Revisão automática indisponível: limite de uso ou orçamento de IA do time esgotado."

        self.upsert_comment(
            repo, number, auth, f"{COMMENT_MARKER}\n### Revisão automática (AI Gateway)\n\n{review}"
        )
        log.info("Revisão publicada em %s#%s", repo, number)

    def upsert_comment(self, repo: str, number: int, auth: dict, body: str) -> None:
        comments = self.http.get(
            f"/repos/{repo}/issues/{number}/comments", headers=auth, params={"per_page": 100}
        )
        comments.raise_for_status()
        existing = next((c for c in comments.json() if COMMENT_MARKER in (c.get("body") or "")), None)
        if existing:
            self.http.patch(
                f"/repos/{repo}/issues/comments/{existing['id']}", headers=auth, json={"body": body}
            ).raise_for_status()
        else:
            self.http.post(
                f"/repos/{repo}/issues/{number}/comments", headers=auth, json={"body": body}
            ).raise_for_status()


_reviewer: Reviewer | None = None


def get_reviewer() -> Reviewer:
    global _reviewer
    if _reviewer is None:
        _reviewer = Reviewer()
    return _reviewer


def run_review(payload: dict) -> None:
    try:
        get_reviewer().review(payload)
    except Exception:
        log.exception("Falha ao revisar PR")


@app.post("/webhook", status_code=202)
async def webhook(
    request: Request,
    background: BackgroundTasks,
    x_github_event: str = Header(...),
    x_hub_signature_256: str | None = Header(None),
):
    body = await request.body()
    if not verify_signature(os.environ["GITHUB_WEBHOOK_SECRET"], body, x_hub_signature_256):
        raise HTTPException(401, "assinatura inválida")

    payload = await request.json()
    if x_github_event != "pull_request" or payload.get("action") not in HANDLED_ACTIONS:
        return {"status": "ignored"}
    if payload["pull_request"].get("draft"):
        return {"status": "ignored", "reason": "draft"}

    background.add_task(run_review, payload)
    return {"status": "accepted"}


@app.get("/healthz")
async def healthz():
    return {"status": "ok"}
