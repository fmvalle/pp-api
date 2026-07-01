# Chatbot híbrido (Avaliador)

Sistema em **duas camadas** para economia de tokens:

1. **Local (0 tokens)** — `rapidfuzz` + intenções em `bot_intents` + consultas SQL (`bot_data`)
2. **LLM dinâmica** — configuração ativa em `bot_settings` (Grok, OpenAI, Anthropic, Gemini, Vertex)

## Migration

```bash
cd ../api && make db-migrate
# ou aplique pp-bo/migrations/013_bot_hybrid_chat.sql e 014_bot_providers_gemini_vertex.sql
```

## Configuração LLM (fonte primária: `bot_settings`)

A chave e o provedor ficam na tabela **`bot_settings`** (admin em `/admin/bot`).
Apenas **uma** linha pode ter `is_active = true`.

```sql
SELECT provider, model_name, base_url, is_active,
       left(api_key, 8) || '…' AS api_key_preview
FROM bot_settings
WHERE is_active = true;
```

Para **Groq**, use:

| Campo | Valor |
|-------|-------|
| `provider` | `groq` |
| `model_name` | `llama-3.3-70b-versatile` (ou outro modelo ativo no Groq) |
| `base_url` | `https://api.groq.com/openai/v1` |
| `api_key` | `gsk_…` |
| `is_active` | `true` |

> Chaves `gsk_` com `provider=grok` ou URL `api.x.ai` são corrigidas em runtime e pela migration **018**.

Fallback `.env` (`GROQ_API_KEY`, `BOT_LLM_SOURCE=env`) só para dev local **sem** linha ativa no banco.

### Autenticação (401 Missing bearer token)

`/v1/chat/*` exige **login** — header `Authorization: Bearer <access_token>`.

Abrir `http://localhost:8001/v1/chat/status` no navegador **sem token** sempre retorna **401** (isso não testa a Groq).

**Como testar corretamente:**

1. **Pela UI** — entre em `/teacher/assistant` ou `/admin/assistant` logado.
2. **Pelo admin** — `GET /v1/admin/bot/stats` (platform admin) inclui `llm_reachable` e `llm_error`.
3. **curl** — após login, use o `access_token` da sessão:

```bash
curl -s http://127.0.0.1:8001/v1/chat/status \
  -H "Authorization: Bearer SEU_ACCESS_TOKEN"
```

`GET /v1/chat/status` retorna `config_source: db|env`, `llm_reachable` e `llm_error` (ping real na API).

## Configurar Groq via `.env` (opcional, dev)

```env
GROQ_API_KEY=gsk_...
BOT_LLM_SOURCE=env
GROQ_MODEL=llama-3.3-70b-versatile
```

Opção B — **Admin** (`/admin/bot`):

```sql
UPDATE bot_settings
SET
  provider = 'groq',
  model_name = 'llama-3.3-70b-versatile',
  api_key = 'gsk-...',
  base_url = 'https://api.groq.com/openai/v1',
  is_active = true,
  updated_at = now()
WHERE id = (SELECT id FROM bot_settings LIMIT 1);
```

> **Groq ≠ Grok:** Groq usa `api.groq.com`; Grok (xAI) usa `api.x.ai`. Painéis e chaves são diferentes.

## Configurar Grok (xAI)

```sql
UPDATE bot_settings
SET
  provider = 'grok',
  model_name = 'grok-2-latest',
  api_key = 'xai-...',
  base_url = 'https://api.x.ai/v1',
  is_active = true,
  updated_at = now()
WHERE id = (SELECT id FROM bot_settings LIMIT 1);
```

Apenas **uma** linha pode ter `is_active = true`.

### Gemini (Google AI Studio)

```sql
UPDATE bot_settings
SET
  provider = 'gemini',
  model_name = 'gemini-2.0-flash',
  api_key = 'AIza...',
  base_url = 'https://generativelanguage.googleapis.com/v1beta/openai',
  is_active = true,
  updated_at = now()
WHERE id = (SELECT id FROM bot_settings LIMIT 1);
```

Usa o endpoint OpenAI-compatible do Google (`/v1beta/openai/chat/completions`).

### Vertex AI (Google Cloud)

```sql
UPDATE bot_settings
SET
  provider = 'vertex',
  model_name = 'gemini-2.0-flash-001',
  api_key = '{"type":"service_account",...}',
  base_url = 'projects/SEU_PROJECT/locations/us-central1',
  is_active = true,
  updated_at = now()
WHERE id = (SELECT id FROM bot_settings LIMIT 1);
```

- `base_url`: caminho `projects/PROJECT_ID/locations/REGION`
- `api_key`: JSON da service account GCP (com permissão Vertex AI) ou token OAuth Bearer

Fallback legado: variáveis `ASSISTANT_OPENAI_*` no `.env` se não houver config ativa no banco.

## Administração (platform admin)

CRUD via API e UI em **`/admin/bot`** (pp-app).

| Método | Rota | Descrição |
|--------|------|-----------|
| GET | `/v1/admin/bot/stats` | KPIs (conversas, local vs LLM) |
| GET/POST | `/v1/admin/bot/settings` | Configurações LLM |
| PATCH/DELETE | `/v1/admin/bot/settings/{id}` | Editar / remover |
| POST | `/v1/admin/bot/settings/{id}/activate` | Ativar (desativa as demais) |
| GET/POST | `/v1/admin/bot/intents` | Intenções locais |
| PATCH/DELETE | `/v1/admin/bot/intents/{id}` | Editar / remover |

A `api_key` nunca é retornada por completo — apenas `api_key_masked`.

## Endpoints (chat)

| Método | Rota | Descrição |
|--------|------|-----------|
| POST | `/v1/chat` | Mensagem (híbrido) |
| POST | `/api/chat` | Alias compatível |
| GET | `/v1/chat/status` | Status LLM + intenções |
| GET | `/v1/chat/conversations` | Histórico |
| GET | `/v1/chat/conversations/{id}/messages` | Mensagens |

Rotas legadas: `/v1/teacher/assistant/*` (mesmo motor).

## Fluxo POST /v1/chat

```
mensagem
  → SQL (fatos exatos: nota, média, data) — professor
  → navegação local (score ≥ 92, não interpretativa)
  → LLM + data_pack JSON
  → stub orientativo (se LLM off ou erro)
```

### Diagnóstico: LLM não aparece no painel do provedor

1. Confirme **provedor correto** (Groq vs Grok) em `/v1/chat/status` → `active_provider`.
2. Veja `bot_messages.source`: se só `local`/`local_data`/`stub`, a LLM não foi acionada.
3. Modelo inválido (ex.: `grok-2-latest` descontinuado) → API 400, fallback silencioso para stub.
4. Use `BOT_LLM_SOURCE=env` + `GROQ_API_KEY` ou corrija config em `/admin/bot`.
5. Perguntas interpretativas (“Como interpretar o relatório?”) agora vão para LLM; navegação pura continua local.

### Assistente contextual (relatórios)

Nos relatórios de avaliação, o Avaliador aparece como **botão flutuante** (canto inferior direito) e abre um **painel lateral** (400px) sem bloquear a página.

O frontend envia `page_context` no POST `/v1/chat` com resumo do relatório aberto (componentes, questões críticas, estatísticas).

Páginas iniciais:
- `/teacher/reports/pedagogical` — relatório pedagógico
- `/teacher/reports/:scheduleId` — relatório por agendamento

Para perguntas abertas (“data da avaliação”, “primeira prova aplicada”, etc.), use **híbrido em 3 camadas**:

| Camada | O quê | Por quê |
|--------|-------|---------|
| **1. SQL determinístico** | Handlers em `bot_data.py` para fatos com resposta única (nota, média, data, primeira/próxima prova) | Zero tokens, resposta confiável |
| **2. Context pack JSON** | `load_teacher_data_pack()` → `data_pack` no contexto do professor | Dá à LLM uma “planilha” estruturada para interpretar variações de pergunta |
| **3. LLM** | Só quando não há match local; prompt proíbe inventar números/datas | Cobre NL que regex não antecipa |

O `data_pack` inclui:

- `schedules[]` — título, turma, `start_date`, status, alunos concluídos
- `schedule_facts` — `first_applied`, `last_applied`, `next_upcoming`
- `assessments[]` — pendências e concluídos por caderno
- **`pedagogical_reports[]`** (Fase 2, com turma selecionada) — até 2 avaliações recentes com:
  - `components[]` — acurácia, variação em p.p., ação (`intervir` / `orientar` / `desafiar`)
  - `pedagogical_reading` — leitura determinística + componentes prioritários
  - `critical_questions[]` — questões com menor % de acerto da turma + habilidade BNCC

Sem turma selecionada, `pedagogical_reports` fica vazio — peça ao professor para escolher a turma no chat.

Professores recebem o pack em `load_teacher_context()`; a LLM usa `data_pack.schedules` e `schedule_facts` (ver `bot_llm.py`).

**Regra:** perguntas que **parecem** ser sobre dados da plataforma mas o SQL não resolve → `data_unavailable` (não chute via LLM). Perguntas pedagógicas ou interpretativas → LLM com o pack.

**Evitar:** só regex infinito **ou** só LLM sem contexto estruturado.

## Intenções locais (seed)

Coluna **`audiences`** (`text[]`): `teacher`, `platform_admin`, `school_admin`, `student`, `all`.

Migrations:
- **`015_bot_intents_audiences.sql`** — coluna + constraint
- **`016_bot_intents_enrichment.sql`** — ~43 intenções com audiência por perfil

```bash
cd ../api && make db-migrate
```

Reaplicar `016` é seguro (upsert por `intent_key`).

### Quem acessa o chat

| Perfil | Rota UI | Intenções carregadas |
|--------|---------|----------------------|
| Professor | `/teacher/assistant` | `teacher` + `all` |
| Platform admin | `/admin/assistant` | `platform_admin` + `all` |

Consultas SQL de turma (notas/médias) permanecem **somente professor**.

Perguntas de dados reconhecidas (camada `local_data`, sem inventar resposta):

| Pergunta (exemplos) | Handler |
|---------------------|---------|
| Menor/maior **nota** em um caderno | `data_lowest_score` / `data_highest_score` |
| Menor/maior **média de acerto** por **aluno** | `data_lowest_student_average` / `data_highest_student_average` |
| Média da **turma** / componente | `data_classroom_average` |
| Quantos alunos / pendências | `data_classroom_metrics` |
| Data / primeira / próxima / última avaliação | `data_schedule_date`, `data_first_schedule`, `data_next_schedule`, `data_last_schedule` |

Se a pergunta parece ser sobre **dados** mas o SQL não resolve, o bot responde **`data_unavailable`** — não escala para LLM/stub com chute.

Adicione intenções customizadas:

```sql
INSERT INTO bot_intents (intent_key, title, patterns, response_template, min_score)
VALUES (
  'doc_turmas',
  'Turmas',
  '["minhas turmas","listar turmas"]'::jsonb,
  'Acesse /teacher/classrooms …',
  85
);
```

## CORS

Já configurado via `settings.cors_origins_list` em `app/main.py` (middleware CORSMiddleware).

## Frontend

Componente `AssistantChat` em pp-app:

- Histórico lateral (conversas persistidas)
- Badge: Resposta local / IA · grok / Orientativo
- Indicador “pensando…” + scroll automático

## Testes

```bash
cd api
pip install -r requirements.txt
pytest tests/test_bot_local.py -q
```
