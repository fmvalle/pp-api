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

Se `/health` responder mas o login falhar, vê os logs: costuma ser `FIREBASE_CREDENTIALS_JSON`, `FIREBASE_CREDENTIALS_PATH` ou `GOOGLE_APPLICATION_CREDENTIALS` em falta ou inválido.

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
