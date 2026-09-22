"""Mock mínimo da API da Azure OpenAI, para desenvolvimento local e testes de integração.

Implementa chat completions (com e sem streaming) e embeddings nos formatos de URL da
Azure (`/openai/deployments/<deployment>/...?api-version=...`) e da API v1 (`/openai/v1/...`).
Exige o header `api-key` igual a MOCK_AZURE_API_KEY, para provar que a chave chegou
do "vault" até o gateway.
"""

from __future__ import annotations

import json
import os
import time
import uuid

from fastapi import FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

app = FastAPI(title="mock-azure-openai")
EXPECTED_KEY = os.getenv("MOCK_AZURE_API_KEY", "mock-azure-key")
REPLY = "Olá do mock da Azure OpenAI."
PROMPT_TOKENS, COMPLETION_TOKENS = 1000, 500


def _check(api_key: str | None, api_version: str | None, deployment: str | None) -> None:
    if api_key != EXPECTED_KEY:
        raise HTTPException(401, {"error": {"code": "401", "message": "Access denied due to invalid key."}})
    if deployment is not None and not api_version:
        raise HTTPException(400, {"error": {"code": "400", "message": "Missing api-version"}})


def _usage() -> dict:
    return {
        "prompt_tokens": PROMPT_TOKENS,
        "completion_tokens": COMPLETION_TOKENS,
        "total_tokens": PROMPT_TOKENS + COMPLETION_TOKENS,
    }


def _chat(model: str, body: dict):
    completion_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"
    created = int(time.time())
    if not body.get("stream"):
        return {
            "id": completion_id,
            "object": "chat.completion",
            "created": created,
            "model": model,
            "choices": [
                {"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": REPLY}}
            ],
            "usage": _usage(),
        }

    def events():
        base = {"id": completion_id, "object": "chat.completion.chunk", "created": created, "model": model}
        for i, word in enumerate(REPLY.split(" ")):
            delta = {"role": "assistant", "content": word} if i == 0 else {"content": " " + word}
            yield f"data: {json.dumps({**base, 'choices': [{'index': 0, 'delta': delta}]})}\n\n"
        stop = {"index": 0, "delta": {}, "finish_reason": "stop"}
        yield f"data: {json.dumps({**base, 'choices': [stop]})}\n\n"
        if (body.get("stream_options") or {}).get("include_usage"):
            yield f"data: {json.dumps({**base, 'choices': [], 'usage': _usage()})}\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(events(), media_type="text/event-stream")


def _embeddings(model: str, body: dict) -> dict:
    inputs = body.get("input")
    inputs = inputs if isinstance(inputs, list) else [inputs]
    return {
        "object": "list",
        "model": model,
        "data": [{"object": "embedding", "index": i, "embedding": [0.01] * 8} for i in range(len(inputs))],
        "usage": {"prompt_tokens": 10 * len(inputs), "total_tokens": 10 * len(inputs)},
    }


@app.post("/openai/deployments/{deployment}/chat/completions")
async def deployment_chat(
    deployment: str,
    request: Request,
    api_version: str | None = Query(None, alias="api-version"),
    api_key: str | None = Header(None, alias="api-key"),
):
    _check(api_key, api_version, deployment)
    return _chat(deployment, await request.json())


@app.post("/openai/deployments/{deployment}/embeddings")
async def deployment_embeddings(
    deployment: str,
    request: Request,
    api_version: str | None = Query(None, alias="api-version"),
    api_key: str | None = Header(None, alias="api-key"),
):
    _check(api_key, api_version, deployment)
    return _embeddings(deployment, await request.json())


@app.post("/openai/v1/chat/completions")
async def v1_chat(
    request: Request,
    api_key: str | None = Header(None, alias="api-key"),
    authorization: str | None = Header(None),
):
    _check(api_key or (authorization or "").removeprefix("Bearer "), None, None)
    body = await request.json()
    return _chat(body.get("model", "unknown"), body)


@app.get("/health")
async def health():
    return {"status": "ok"}
