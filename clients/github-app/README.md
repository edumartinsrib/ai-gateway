# GitHub App de revisão de PR via AI Gateway

Exemplo de consumidor do gateway: a cada PR aberto ou atualizado, o app busca o diff, pede
uma revisão ao modelo pelo gateway e publica (ou atualiza) **um único** comentário no PR.

O app usa uma virtual key de serviço do time `github-app` (orçamento próprio, só
`gpt-5.4-mini`). Quando o orçamento acaba, o gateway responde 429 e o app publica um aviso
em vez da revisão.

## 1. Crie o GitHub App

Em *Settings → Developer settings → GitHub Apps → New GitHub App*:

- **Webhook URL**: `https://github-app.<seu-dominio>/webhook`
- **Webhook secret**: um valor aleatório (`openssl rand -hex 32`)
- **Permissões de repositório**: *Pull requests: Read & write*, *Contents: Read*
- **Eventos**: *Pull request*

Gere a chave privada (`.pem`) e anote o **App ID**. Instale o app nos repositórios desejados.

## 2. Gere a virtual key e guarde tudo no vault

```bash
gatewayctl key create --team github-app --alias github-app-prod --client github-app
vault kv put secret/github-app/gateway api_key=sk-...
vault kv put secret/github-app/app private_key=@app.private-key.pem webhook_secret=...
```

## 3. Publique

```bash
docker build -t ghcr.io/<org>/ai-gateway-github-app:0.1.0 clients/github-app
# ajuste GITHUB_APP_ID, host e servidor do vault em deploy/
kubectl apply -k clients/github-app/deploy
```

O namespace `github-app` tem o label `ai-gateway/client=true`, liberado pela NetworkPolicy
do gateway; o app fala com `http://ai-gateway.ai-gateway.svc:4000` sem sair do cluster.

## Desenvolvimento local

```bash
pip install -r requirements.lock
export GITHUB_APP_ID=... GITHUB_APP_PRIVATE_KEY="$(cat app.pem)" GITHUB_WEBHOOK_SECRET=...
export GATEWAY_URL=http://localhost:4000 GATEWAY_API_KEY=sk-...
uvicorn app:app --port 8080
# encaminhe os webhooks com: gh webhook forward --events=pull_request --url=http://localhost:8080/webhook ...
```
