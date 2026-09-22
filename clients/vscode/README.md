# Consumindo o AI Gateway no VS Code

O gateway expõe uma API compatível com a OpenAI em `https://<seu-host>/v1`. Cada pessoa usa
a **própria virtual key**, vinculada ao seu usuário e time — é ela que carrega orçamento,
limites e modelos permitidos.

## 1. Obtenha sua chave

Um administrador gera a chave (a master key nunca sai do cluster):

```bash
kubectl -n ai-gateway port-forward svc/ai-gateway 4000:4000 &
export LITELLM_MASTER_KEY=...   # do vault
gatewayctl key create --team platform --user alice --alias alice-vscode --client vscode
```

A chave (`sk-...`) é exibida uma única vez. `--client vscode` faz o custo aparecer como
"vscode" no dashboard.

## 2a. GitHub Copilot Chat (Custom Endpoint)

1. Paleta de comandos → **Chat: Manage Language Models** → **Add Models** → **Custom Endpoint**.
2. Nome do grupo: `AI Gateway`; API type: **Chat Completions**; informe a chave.
3. No `chatLanguageModels.json` aberto pelo VS Code, use os modelos de
   [`chatLanguageModels.json`](chatLanguageModels.json) trocando `ai-gateway.example.com`
   pelo host do gateway.
4. Escolha "GPT-5.4 (AI Gateway)" no seletor de modelos do chat.

> Em planos Copilot Business/Enterprise, o administrador da organização precisa permitir
> BYOK (Bring Your Own Key) nas políticas do Copilot.

## 2b. Continue

Copie [`continue-config.yaml`](continue-config.yaml) para `~/.continue/config.yaml`, troque o
host e crie `~/.continue/.env` com `AI_GATEWAY_KEY=sk-...`. O modelo de embeddings habilita a
indexação do código (`@codebase`).

## 2c. Cline / Roo Code e outras extensões

Provider **OpenAI Compatible**: Base URL `https://<seu-host>/v1`, API key = sua virtual key,
Model ID = `gpt-5.4` ou `gpt-5.4-mini`.

## Verificando

```bash
curl https://<seu-host>/v1/models -H "Authorization: Bearer sk-..."
```

Erros comuns:

| HTTP | Significado |
|------|-------------|
| 401 | Chave inválida, expirada ou revogada |
| 403 | Modelo fora dos permitidos para o seu time, ou chave sem time |
| 429 `budget_exceeded` | Orçamento da chave, do usuário ou do time esgotado (reinicia no próximo período) |
| 429 (sem budget) | Limite de requisições/tokens por minuto |
