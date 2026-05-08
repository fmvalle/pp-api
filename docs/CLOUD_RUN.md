# Cloud Run (API)

## Startup probe (Knative / YAML) — erro comum

Se definires `startupProbe` manualmente, **não uses `failureThreshold: 1`** com `periodSeconds` muito alto.

Com **`failureThreshold: 1`**, a **primeira** tentativa de `tcpSocket` na porta (ex. 8000) que falhar (ex. *connection refused* porque o Python ainda está a importar módulos) **mata o arranque de imediato**. O texto do Cloud Run fala em “timeout” mas, na prática, foi **uma única falha**.

Configuração **recomendada** (ajusta `port` = `containerPort` = `PORT`):

```yaml
startupProbe:
  tcpSocket:
    port: 8000
  periodSeconds: 5        # voltar a tentar a cada 5 s
  timeoutSeconds: 5       # tempo máximo por tentativa de ligação TCP
  failureThreshold: 30      # até ~150 s de janela antes de desistir (5 × 30)
```

Ou **remove** o bloco `startupProbe` inteiro e deixa o **Default** do Cloud Run (comportamento mais seguro se não precisares de valores custom).

## Porta

O serviço define **`PORT`**. A imagem usa `docker-entrypoint.sh`, que corre:

`uvicorn … --port "$PORT"` (predefinição **8000** se `PORT` estiver vazio).

Na consola Cloud Run, a **porta do contentor** deve ser a **mesma** que a variável `PORT` (ex.: `8000` e `PORT=8000`, ou `8080` e `PORT=8080`). Se a consola disser “container port 8080” mas `PORT=8000`, o health check falha.

## Arranque lento (timeout na PORT)

O contentor tem de **abrir o socket** dentro do tempo de *startup* da revisão. Com CPU/memória baixas, importar **ReportLab**, **gRPC** (Firebase) e restantes módulos pode ir ao limite do timeout.

- O código **não** importa ReportLab ao carregar a app (só ao gerar PDF).
- O entrypoint **não** volta a importar `app.main` antes do uvicorn (evita **duplicar** o trabalho). Para forçar um teste de import no arranque, define **`ENTRYPOINT_PREFLIGHT=1`** na revisão (só para debug).

Recomendações na revisão Cloud Run: **≥512 MiB** de memória, **1 vCPU** se possível, e **Startup CPU boost** ativado. Se ainda falhar, aumenta o **startup probe timeout** / tempo de arranque na documentação do Cloud Run da tua região.

## Arranque e `/health`

O Firebase Admin **não** bloqueia mais o arranque: inicializa na primeira rota que precisa (login, criação de utilizador com Firebase, etc.). Assim o revision fica **Ready** mesmo que corrijas credenciais depois.

### Confirmar que a revisão Cloud Run tem a **imagem mais recente** da API

Se o erro de login ainda menciona **só** `FIREBASE_CREDENTIALS_PATH` (texto antigo) ou o diagnóstico abaixo não aparece, a revisão em produção **não** foi actualizada com o último código.

```bash
curl -sS 'https://SEU-SERVICO.run.app/health?firebase=1'
```

Resposta esperada **com código actual** (exemplo):

```json
{"status":"ok","firebase":{"firebase_project_id":"parametro-pedagogico","api_supports_env_pem":true,"credential_branch":"FIREBASE_CLIENT_EMAIL_AND_PRIVATE_KEY","has_client_email":true,"has_private_key":true,"private_key_looks_like_pem":true}}
```

- Se receber **apenas** `{"status":"ok"}` **com** `?firebase=1` na URL → a imagem em Cloud Run **ainda não inclui** este endpoint (falta **build + deploy** do repositório `api`).
- Use `credential_branch` e `path_is_file` / `private_key_looks_like_pem` para ver qual ramo o `init_firebase` usa e se o ficheiro PEM existe / está bem formatado (sem expor segredos).

**Postman:** coleções em `pp-bo/postman/` (`PP-API-v1.postman_collection.json`, `PP-API.postman_collection.json`) incluem `GET /health?firebase=1` e a descrição da coleção aponta para `/docs` e importação via `/openapi.json`.

Se `/health` responder mas o login falhar, vê os logs: costuma ser `FIREBASE_CREDENTIALS_JSON`, `FIREBASE_CREDENTIALS_PATH` ou `GOOGLE_APPLICATION_CREDENTIALS` em falta ou inválido.

### Login 401 (“Token inválido ou service account de outro projeto”)

O cliente (Flutter) já emitiu o token para o `FIREBASE_PROJECT_ID` certo; o 401 vem do **Admin SDK na revisão Cloud Run** ao validar o token.

1. **Revisão → Variáveis de ambiente:** `FIREBASE_PROJECT_ID` = o mesmo ID do Firebase do app (ex.: `parametro-pedagogico`), sem espaços nem aspas a mais.
2. **Credencial:** tem de ser o JSON **“Gerar nova chave privada”** em Firebase Console → Definições do projeto → **Contas de serviço** (Admin SDK), não um JSON aleatório de outro projeto GCP.
3. No JSON, o campo **`project_id`** tem de ser **igual** a `FIREBASE_PROJECT_ID` (a API valida isso no arranque do Admin SDK).
4. Se usas `FIREBASE_CREDENTIALS_PATH=/secrets/firebase-admin.json`, o ficheiro tem de **existir no contentor** (secret montado como ficheiro, não pasta vazia). Se o caminho não existir, `verify_id_token` falha com a mensagem genérica acima.
5. **Ordem de precedência** (`app/core/firebase.py`): `FIREBASE_CREDENTIALS_PATH` → `FIREBASE_CREDENTIALS_JSON` → **`FIREBASE_CLIENT_EMAIL` + `FIREBASE_PRIVATE_KEY`** (mesmo padrão que Directus) → `GOOGLE_APPLICATION_CREDENTIALS`. Se `FIREBASE_CREDENTIALS_PATH` aponta para um ficheiro errado ou inexistente, **remove-a** para a API usar JSON ou email+PEM.
6. Nos **logs** da revisão, procura `verify_firebase_id_token falhou` — aí aparece o tipo e a mensagem real da exceção (ex.: ficheiro em falta, `project_id` divergente, token expirado). Em desenvolvimento podes definir `API_DEBUG=true` para o detalhe ir também no corpo HTTP do 401.

### Cloud Run: `FIREBASE_CREDENTIALS_PATH` sozinho não basta

Na consola, **“Variáveis e segredos”** com `FIREBASE_CREDENTIALS_PATH=/secrets/firebase-admin.json` **não grava o JSON em lado nenhum**. O contentor só tem esse ficheiro se:

- montares um **volume** (Secret Manager) nesse caminho, **ou**
- usares outra fonte (ex.: `FIREBASE_CREDENTIALS_JSON` vinda de secret referenciado na variável).

Se o ficheiro **não existir**, o Admin SDK falha ao abrir o caminho e o login devolve o 401 genérico (“service account de outro projeto…”), mesmo com `FIREBASE_PROJECT_ID` correcto.

**Opção recomendada (menos confusão com caminhos):**

1. No **Secret Manager**, cria um secret (ex.: `pp-api-firebase-admin-json`) cuja **versão** é o conteúdo **exacto** do ficheiro JSON gerado no Firebase (Admin SDK → nova chave privada). O campo `"project_id"` dentro do JSON deve ser `parametro-pedagogico`.
2. Na revisão do **pp-api** → variáveis de ambiente:
   - **Remove** `FIREBASE_CREDENTIALS_PATH` (importante: o código tenta o **path primeiro**; se ficar apontando para um ficheiro inexistente, ignora o JSON).
   - Adiciona `FIREBASE_CREDENTIALS_JSON` com valor **“Referência do Secret Manager”** (ou equivalente na UI) apontando para esse secret/versão.
3. Mantém `FIREBASE_PROJECT_ID=parametro-pedagogico`.
4. Implanta nova revisão e testa o login.

**Opção com ficheiro em `/secrets/…`:**

1. Secret Manager com o mesmo JSON.
2. Na revisão → separador do **contentor** → **Volumes** → adicionar volume (tipo Secret) + **montagem** no contentor de modo a existir um **ficheiro** em `/secrets/firebase-admin.json` (o nome do ficheiro montado depende da UI; se o Cloud Run criar outro nome, ajusta `FIREBASE_CREDENTIALS_PATH` para esse caminho **real** dentro do contentor, ou usa a opção com `FIREBASE_CREDENTIALS_JSON` acima).

**URL do serviço:** a região é `us-central1` (com **um** “l”), por exemplo `…us-central1.run.app`. `us-centrall` não é o host correcto.

**Nota:** `FIREBASE_WEB_API_KEY` (chave Web do mesmo projeto Firebase) é usada para **`POST /v1/auth/sign-in`** / **`POST /api/v1/auth/sign-in`** (email+senha → Identity Toolkit). **Não** substitui a service account Admin: `FIREBASE_PROJECT_ID` + JSON / `FIREBASE_CLIENT_EMAIL` + `FIREBASE_PRIVATE_KEY` continuam necessários para **validar** o `id_token` nas rotas que o usam. Sem credencial Admin, o Cloud Run pode cair em `GOOGLE_APPLICATION_CREDENTIALS` por defeito → 401 no login.

## Variáveis obrigatórias (mínimo)

- `DATABASE_URL` (asyncpg), ex.: `postgresql+asyncpg://USER:PASS@HOST:5432/DBNAME`
- `JWT_SECRET` (≥32 caracteres)
- `REFRESH_TOKEN_PEPPER` (≥16)
- Firebase (só necessário para login / rotas Firebase): `FIREBASE_PROJECT_ID` + uma de `FIREBASE_CREDENTIALS_JSON`, `FIREBASE_CREDENTIALS_PATH`, ou `GOOGLE_APPLICATION_CREDENTIALS`

Sem `DATABASE_URL` / `JWT_SECRET` / `REFRESH_TOKEN_PEPPER`, o Pydantic falha **ao importar** `app.main` → o contentor **nunca escuta** na `PORT` e o Cloud Run devolve o erro genérico de timeout.

**Logs:** na revisão falhada, filtra por `pp-api entrypoint`. Se aparecer `FATAL: falha ao importar`, corrige as variáveis acima (nomes **exactos** como no `.env.example`, maiúsculas).

**Nota:** não há ficheiro `.env` dentro da imagem Docker; tudo tem de vir das **variáveis de ambiente / secrets** do serviço Cloud Run.

## Recursos

Com `reportlab` e vários routers, o arranque pode ser pesado. Se ainda falhar por timeout, aumenta **CPU** da revisão (ex. 1 vCPU) e **memória** (≥512 MiB) e/ou **startup CPU boost** no Cloud Run.

## Timeout

Se o Postgres estiver longe ou lento, aumenta **CPU startup** / **request timeout** na revisão, ou usa **Cloud SQL connector** + instância próxima da região do serviço.
