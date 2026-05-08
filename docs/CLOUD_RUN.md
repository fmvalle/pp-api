# Cloud Run (API)

## Porta

O serviço define **`PORT`**. A imagem usa `docker-entrypoint.sh`, que corre:

`uvicorn … --port "$PORT"` (predefinição **8000** se `PORT` estiver vazio).

Na consola Cloud Run, a **porta do contentor** deve ser a **mesma** que a variável `PORT` (ex.: `8000` e `PORT=8000`, ou `8080` e `PORT=8080`). Se a consola disser “container port 8080” mas `PORT=8000`, o health check falha.

## Arranque e `/health`

O Firebase Admin **não** bloqueia mais o arranque: inicializa na primeira rota que precisa (login, criação de utilizador com Firebase, etc.). Assim o revision fica **Ready** mesmo que corrijas credenciais depois.

Se `/health` responder mas o login falhar, vê os logs: costuma ser `FIREBASE_CREDENTIALS_JSON`, `FIREBASE_CREDENTIALS_PATH` ou `GOOGLE_APPLICATION_CREDENTIALS` em falta ou inválido.

## Variáveis obrigatórias (mínimo)

- `DATABASE_URL` (asyncpg)
- `JWT_SECRET` (≥32 caracteres)
- `REFRESH_TOKEN_PEPPER` (≥16)
- Firebase: `FIREBASE_PROJECT_ID` + uma de: `FIREBASE_CREDENTIALS_JSON` (JSON escapado / Secret), `FIREBASE_CREDENTIALS_PATH` (ficheiro montado), ou `GOOGLE_APPLICATION_CREDENTIALS`

Sem `DATABASE_URL` válido, o processo **nem arranca** (Pydantic ao importar `settings`).

## Timeout

Se o Postgres estiver longe ou lento, aumenta **CPU startup** / **request timeout** na revisão, ou usa **Cloud SQL connector** + instância próxima da região do serviço.
