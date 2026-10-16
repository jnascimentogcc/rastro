# Spec Técnica — Rastro (MVP)

> "Rastro" é um nome provisório. Substituir quando houver nome definitivo.

## 1. Visão

O Rastro preserva o **porquê** do código gerado por agentes de IA. Captura a intenção no momento em que o código nasce (as conversas com o agente), extrai as regras de negócio que daí resultam, liga-as ao código e avisa quando uma alteração as pode partir.

Problema que resolve: código gerado por IA funciona mas ninguém o percebe a fundo ("comprehension debt"). A razão de cada regra vive na conversa com o agente e perde-se.

## 2. Objetivo do MVP

Provar, num repositório real, que conseguimos:

1. Ligar uma sessão do Claude Code ao commit que ela produziu.
2. Extrair dessa sessão + diff as regras de negócio, com o porquê.
3. Num pull request, avisar quando a alteração toca numa regra existente, incluindo cópias duplicadas da mesma lógica.

**Critérios de sucesso**

- No repositório de teste (ver secção 12), pelo menos 80% das regras plantadas são extraídas com o porquê correto.
- Na análise de impacto, menos de 1 comentário irrelevante em cada 5 PRs (falsos positivos matam a adoção).
- Tempo desde o push até ao comentário no PR inferior a 2 minutos em repositórios até 50 mil linhas.

## 3. Escopo do MVP

**Dentro**

- Linguagens: **TypeScript e JavaScript** apenas.
- Agente suportado: **Claude Code** apenas.
- Plataforma Git: **GitHub** apenas (GitHub App).
- CLI com git hook `post-commit` que associa sessões a commits.
- Ingestão, análise estática, extração com LLM, grafo em PostgreSQL.
- Análise de impacto com comentário no PR.
- Comando de CLI para listar regras (sem interface web).

**Fora** — ver secção 13.

## 4. Stack

| Camada | Escolha | Motivo |
|---|---|---|
| Linguagem backend | Python 3.12+ | Uma linguagem para CLI, API e workers |
| API | FastAPI + Pydantic v2 | Tipagem, validação e OpenAPI automática |
| Base de dados | PostgreSQL 16 + pgvector | Grafo em tabelas relacionais + pesquisa semântica |
| ORM e migrações | SQLAlchemy 2.0 (async) + Alembic + `pgvector` (pacote Python) | Padrão em Python, suporte a vetores |
| Filas | Procrastinate | Filas em cima do PostgreSQL, sem Redis |
| Análise estática | `tree-sitter` (py-tree-sitter) + `tree-sitter-typescript` | AST rápida e tolerante a erros |
| GitHub | GitHub App com `githubkit` | Webhooks de push e PR, comentários, async |
| LLM | SDK `openai` (modelo configurável via `LLM_MODEL`) | Structured Outputs com schemas Pydantic |
| Embeddings | API de embeddings da OpenAI (modelo via `EMBEDDING_MODEL`) | Mesmo fornecedor e mesma chave que o LLM |
| CLI | Typer, distribuída via PyPI | `uvx rastro init` ou `pipx install rastro` |
| Frontend | Angular (fora do MVP, ver secção 13) | Interface web nas fases seguintes |
| Ferramentas | uv (dependências e workspaces), Ruff, mypy, pytest | Rápido e consistente |

## 5. Arquitetura do MVP

```
 Máquina do programador                      Servidor Rastro
┌────────────────────────┐            ┌─────────────────────────────────┐
│ Claude Code            │            │ API (FastAPI)                   │
│  └ ~/.claude/projects/ │            │  ├ POST /v1/sessions            │
│      *.jsonl           │            │  ├ POST /webhooks/github        │
│ git commit             │            │  └ GET  /v1/repos/:id/rules     │
│  └ hook post-commit ───┼── HTTPS ──▶│                                 │
│      (rastro CLI)      │            │ Filas (Procrastinate)           │
└────────────────────────┘            │  ├ analyze_commit               │
                                      │  ├ resync_anchors               │
 GitHub ── webhooks push/PR ─────────▶│  └ analyze_pr                   │
        ◀── comentário no PR ─────────│                                 │
                                      │ Worker (tarefas)                │
                                      │  ├ static_analysis (tree-sitter)│
                                      │  ├ rule_extraction (LLM)        │
                                      │  └ impact_analysis (LLM)        │
                                      │                                 │
                                      │ PostgreSQL + pgvector (grafo)   │
                                      └─────────────────────────────────┘
```

### Fluxo A — Commit com sessão

1. Programador trabalha com o Claude Code e faz `git commit`.
2. O hook `post-commit` corre `rastro capture`, que:
   - identifica o SHA, ficheiros alterados e o diff;
   - encontra as sessões do Claude Code deste projeto modificadas desde o commit anterior;
   - extrai delas as mensagens do utilizador e os excertos do assistente relacionados com os ficheiros alterados;
   - remove segredos (ver secção 11);
   - envia para `POST /v1/sessions` com o SHA. Nunca bloqueia nem falha o commit: em caso de erro, guarda localmente em `.rastro/pending/` e reenvia no próximo commit.
3. Quando o push chega via webhook, a API junta sessão e commit pelo SHA e cria o job `analyze_commit`.

### Fluxo B — Análise do commit

1. **Análise estática** dos ficheiros alterados: símbolos (funções, métodos, classes), chamadas entre símbolos, hash normalizado do corpo de cada símbolo.
2. **Extração com LLM**: recebe diff + excertos da sessão + símbolos alterados. Devolve zero ou mais regras (formato na secção 7.5).
3. **Ancoragem**: cada regra é ligada aos símbolos onde está implementada.
4. **Deteção de duplicados**: procura no repositório outros símbolos com lógica semelhante (hash de AST normalizada + similaridade de embeddings) e liga-os à mesma regra como `duplicate`.
5. Gravação no grafo.

### Fluxo C — Pull request

1. Webhook `pull_request` (opened, synchronize) cria o job `analyze_pr`.
2. Determinar os símbolos alterados pelo diff do PR.
3. Recolher regras candidatas:
   - ancoradas diretamente a símbolos alterados;
   - ancoradas a símbolos que chamam ou são chamados pelos alterados (profundidade máxima 2);
   - com duplicados que **não** foram alterados neste PR (risco de inconsistência).
4. Para cada candidata, o LLM avalia: "esta alteração pode violar ou mudar esta regra?" com nível de confiança.
5. Só comenta se houver pelo menos uma regra com confiança acima de `IMPACT_THRESHOLD` (padrão 0.7).
6. Um único comentário por PR, atualizado no lugar a cada novo push (nunca empilhar comentários).

## 6. Modelo de dados

```sql
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE repositories (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  github_id       bigint UNIQUE NOT NULL,
  full_name       text NOT NULL,
  installation_id bigint NOT NULL,
  default_branch  text NOT NULL,
  created_at      timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE commits (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  repository_id uuid NOT NULL REFERENCES repositories(id),
  sha           text NOT NULL,
  author        text,
  message       text,
  committed_at  timestamptz,
  analyzed_at   timestamptz,
  UNIQUE (repository_id, sha)
);

CREATE TABLE agent_sessions (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  repository_id uuid NOT NULL REFERENCES repositories(id),
  commit_sha    text NOT NULL,
  agent         text NOT NULL DEFAULT 'claude-code',
  session_ref   text,             -- id da sessão no agente
  excerpts      jsonb NOT NULL,   -- [{role, text, timestamp}]
  captured_at   timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE symbols (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  repository_id uuid NOT NULL REFERENCES repositories(id),
  file_path     text NOT NULL,
  qualified_name text NOT NULL,   -- ex: AgendaService.validarMarcacao
  kind          text NOT NULL,    -- function | method | class
  body_hash     text NOT NULL,    -- hash da AST normalizada
  start_line    int,
  end_line      int,
  embedding     vector(1024),
  last_seen_sha text NOT NULL,
  status        text NOT NULL DEFAULT 'active', -- active | removed
  UNIQUE (repository_id, file_path, qualified_name)
);

CREATE TABLE symbol_calls (
  caller_id uuid NOT NULL REFERENCES symbols(id) ON DELETE CASCADE,
  callee_id uuid NOT NULL REFERENCES symbols(id) ON DELETE CASCADE,
  PRIMARY KEY (caller_id, callee_id)
);

CREATE TABLE rules (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  repository_id uuid NOT NULL REFERENCES repositories(id),
  statement     text NOT NULL,     -- "Implantes não podem ser marcados à sexta à tarde"
  rationale     text,              -- o porquê; NULL = origem desconhecida
  confidence    real NOT NULL,     -- 0..1
  status        text NOT NULL DEFAULT 'active', -- active | orphaned | superseded
  embedding     vector(1024),
  created_at    timestamptz NOT NULL DEFAULT now(),
  updated_at    timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE rule_origins (
  rule_id     uuid NOT NULL REFERENCES rules(id) ON DELETE CASCADE,
  session_id  uuid REFERENCES agent_sessions(id),
  commit_sha  text NOT NULL,
  quote       text,                -- excerto curto do pedido original
  PRIMARY KEY (rule_id, commit_sha)
);

CREATE TABLE rule_anchors (
  rule_id   uuid NOT NULL REFERENCES rules(id) ON DELETE CASCADE,
  symbol_id uuid NOT NULL REFERENCES symbols(id) ON DELETE CASCADE,
  role      text NOT NULL,         -- primary | duplicate
  PRIMARY KEY (rule_id, symbol_id)
);

CREATE TABLE pr_analyses (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  repository_id uuid NOT NULL REFERENCES repositories(id),
  pr_number     int NOT NULL,
  head_sha      text NOT NULL,
  comment_id    bigint,            -- para atualizar no lugar
  findings      jsonb NOT NULL,    -- [{rule_id, risk, confidence, explanation}]
  created_at    timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX ON symbols USING hnsw (embedding vector_cosine_ops);
CREATE INDEX ON rules   USING hnsw (embedding vector_cosine_ops);
```

**Notas:**
- O DDL acima é a referência. A implementação é feita com modelos SQLAlchemy em `packages/db` e migrações Alembic geradas a partir deles.
- A dimensão `vector(1024)` tem de coincidir com a dos embeddings. Os modelos de embeddings da OpenAI permitem fixar a dimensão com o parâmetro `dimensions`; usar `EMBEDDING_DIMENSIONS=1024` e manter o mesmo valor na migração.
- As tabelas do Procrastinate são criadas pela própria biblioteca (`procrastinate schema --apply`), fora do Alembic.

## 7. Componentes

### 7.1 CLI (`packages/cli`)

Comandos:

- `rastro init` — autentica com token do repositório, instala o hook `post-commit` (sem sobrescrever hooks existentes; encadeia), cria `.rastro/config.json`.
- `rastro capture` — chamado pelo hook. Ver Fluxo A. Tempo máximo 2 s; o envio corre num processo separado (`subprocess.Popen` destacado) para não atrasar o commit.
- `rastro rules [--file <path>] [--search <texto>]` — lista regras do repositório via API.
- `rastro flush` — reenvia capturas pendentes.

A CLI usa apenas dependências leves (Typer, httpx, keyring) para arrancar depressa. O hook chama o executável `rastro` instalado; `rastro init` verifica que está no PATH.

Compatibilidade: Windows, macOS e Linux. Em Windows o hook é executado pelo Git for Windows (bash), por isso o script do hook deve ser um shell script POSIX simples que chama `rastro capture`, e a CLI deve lidar com caminhos com `\` e `/`.

### 7.2 Parser de sessões do Claude Code

Formato verificado em 16/10/2026 com três sessões reais (Claude Code `2.1.209` e `2.1.295`, entrypoint `claude-desktop`, Windows), incluindo uma com 67 `Edit` e 3 `Write`. Isolar todo o parsing em `session_parsers/claude_code.py`, guardar a `version` em cada captura e falhar de forma suave (registar e ignorar a linha) perante tipos ou campos desconhecidos.

**Localização dos ficheiros**

- Diretório base: `~/.claude/projects/` em macOS e Linux, `%USERPROFILE%\.claude\projects\` em Windows. Respeitar `CLAUDE_CONFIG_DIR` se estiver definida.
- Um ficheiro `<sessionId>.jsonl` por sessão, dentro de uma subpasta por projeto.
- Não depender do algoritmo que codifica o nome da subpasta. Para encontrar as sessões de um repositório: ler a primeira linha com campo `cwd` de cada ficheiro modificado recentemente e comparar com a raiz do repositório (normalizar separadores, maiúsculas em Windows e aceitar subpastas da raiz).

**Estrutura de cada linha**

Cada linha é um objeto JSON independente. Campos comuns às linhas de conversa:

| Campo | Uso no Rastro |
|---|---|
| `type` | Filtrar tipos (ver abaixo) |
| `sessionId` | `agent_sessions.session_ref` |
| `uuid`, `parentUuid` | Reconstruir a ordem da conversa (árvore, não apenas ordem das linhas) |
| `timestamp` | ISO 8601 UTC; filtrar mensagens posteriores ao commit anterior |
| `cwd` | Associar a sessão ao repositório |
| `gitBranch` | Confirmar que a sessão corresponde ao branch do commit |
| `version` | Versão do Claude Code, guardada com a captura |
| `isSidechain` | `true` = subagente; tratar à parte (ver abaixo) |
| `message` | Conteúdo, no formato da API de Messages (`role`, `content`) |

**Tipos de linha**

| `type` | Tratamento |
|---|---|
| `user` | Ver regras abaixo; pode ser pedido humano **ou** resultado de ferramenta |
| `assistant` | Texto e chamadas de ferramentas do agente |
| `attachment` | Ignorar (listas de ferramentas, skills, instruções MCP) |
| `queue-operation` | Ignorar (duplica o pedido do utilizador) |
| `ai-title`, `custom-title` | Opcional: título da sessão para mostrar ao utilizador |
| `last-prompt` | Ignorar |
| `system`, `file-history-snapshot`, `agent-name`, `atis-latch`, `cost-state`, `mode` | Ignorar |
| qualquer outro tipo | Ignorar e registar (falhar de forma suave) |

**Regras para mensagens `user`**

- **Pedido humano** (a fonte principal da intenção): `message.content` é uma string, ou uma lista de blocos `text`, e o campo `origin.kind` é `"human"` quando existe.
- **Resultado de ferramenta**: `message.content` contém blocos `tool_result` e a linha tem `toolUseResult` e `sourceToolAssistantUUID`. **Não** é intenção; usar só para saber se a ação foi executada.
- O pedido humano pode ter blocos `image` além de `text` (ex.: `[image, text]`); usar só os blocos `text`.
- **Ação recusada**: qualquer valor em `toolDenialKind` (observado: `"permission-rule"`) ou `tool_result` com `is_error: true`. A edição associada não chegou ao código; descartar.
- **Mensagens meta**: linhas com `isMeta: true` (ex.: `turnCompanion: true` com o texto `[Image: source: ...]`) não são intenção; ignorar.
- **Mensagens de sistema** em blocos `text` que começam por `[Request interrupted` ou equivalentes: ignorar.

**Regras para mensagens `assistant`**

- Uma única resposta da API aparece repartida por **várias linhas**, uma por bloco de conteúdo, todas com o mesmo `message.id`. Agrupar por `message.id` antes de processar.
- Blocos `thinking`: ignorar (normalmente vêm vazios, só com assinatura).
- Blocos `text`: explicações do agente; guardar apenas os que mencionam ficheiros do diff.
- Blocos `tool_use`: `name` + `input`. Interessam sobretudo as ferramentas de edição (`Edit`, `MultiEdit`, `Write`, `NotebookEdit`), cujo `input.file_path` liga a conversa aos ficheiros do commit. Comandos `Bash` também podem alterar ficheiros; no MVP, guardar o `input.command` como contexto mas não o usar para ligação.
- Ignorar `message.usage`, `requestId` e restantes metadados técnicos.

**Ligação a ficheiros do commit**

1. Para cada `tool_use` de edição cujo `tool_result` correspondente (mesmo `tool_use_id`) não tenha `is_error: true`, normalizar `input.file_path` para caminho relativo à raiz do repositório. Os caminhos são absolutos no formato do sistema (ex.: `C:\Users\...` em Windows).
2. Se o caminho estiver fora da raiz do repositório (ex.: `~/.claude/plans/*.md`), ignorar a edição. Se estiver no diff do commit, a edição pertence a este commit.
3. O pedido humano mais próximo **antes** dessa edição na árvore (`parentUuid`) é a intenção associada. Guardar também os pedidos humanos anteriores da mesma sessão como contexto, até ao limite de tamanho.

**Subagentes (`isSidechain: true`)**

No MVP, incluir as edições feitas por subagentes, mas atribuir a intenção ao pedido humano da conversa principal que lançou o subagente.

**O que nunca enviar ao servidor**

- O conteúdo de `tool_result` e `toolUseResult` (pode conter ficheiros inteiros, variáveis de ambiente ou saídas com segredos). Em `Edit`, o `toolUseResult` traz `originalFile`, `oldString`, `newString` e `structuredPatch`; em `Write`, traz `content`.
- Se `old_string`, `new_string` ou `content` de uma edição forem enviados como contexto, passam primeiro pela deteção de segredos (secção 11).
- Blocos `thinking` e assinaturas.
- Linhas `attachment`.

**Limites**

- `MAX_EXCERPT_CHARS` (padrão 40 000) por captura. Se exceder, priorizar: pedido humano associado a cada edição, depois restantes pedidos humanos, depois textos do assistente.

**Estrutura das ferramentas de edição (validada)**

- `Edit`: `input` = `file_path`, `old_string`, `new_string`, `replace_all`. `Write`: `input` = `file_path`, `content`.
- `tool_result` de sucesso tem `is_error` ausente; em erro tem `is_error: true` e `toolUseResult` é uma string.

**Pendente de validar**

Antes da Fase 3, recolher amostras com subagentes (`isSidechain: true`, sempre `false` nas amostras atuais), `MultiEdit` e `NotebookEdit`.

### 7.3 GitHub App

- Permissões: `contents: read`, `pull_requests: write`, `metadata: read`.
- Eventos: `push`, `pull_request`, `installation`.
- Validar sempre a assinatura do webhook (HMAC SHA-256 com `hmac.compare_digest`).
- O endpoint de webhook responde de imediato (202) e enfileira o trabalho; nunca processa dentro do pedido.
- Na instalação: job `initial_index` que corre a análise estática a todo o repositório (sem extração LLM, para controlar custos).

### 7.4 Análise estática (`packages/code_analysis` + tarefa `static_analysis`)

- py-tree-sitter com as gramáticas TypeScript e TSX do pacote `tree-sitter-typescript`.
- O código analisado continua a ser TypeScript/JavaScript (a linguagem dos repositórios dos clientes); o analisador é escrito em Python.
- Extrair símbolos: funções nomeadas, arrow functions atribuídas a constantes, métodos de classe, classes.
- `qualified_name`: `Classe.metodo` ou `nomeDaFuncao`; para exports default usar o nome do ficheiro.
- `body_hash`: hash da AST com identificadores locais renomeados por posição e literais preservados. Serve para detetar duplicados exatos e seguir símbolos renomeados.
- Grafo de chamadas: resolução por nome dentro do repositório (aproximada, sem type checker no MVP).

### 7.5 Extração de regras com LLM (tarefa `rule_extraction`)

Entrada: diff do commit, excertos da sessão, lista de símbolos alterados, regras já existentes ancoradas a esses símbolos.

Saída obrigatória via **Structured Outputs** da OpenAI, passando o modelo Pydantic como formato de resposta (método `parse` do SDK), para que a resposta seja sempre JSON válido conforme este schema:

```json
{
  "rules": [
    {
      "action": "create | update | none",
      "existing_rule_id": "uuid ou null",
      "statement": "Regra em linguagem de negócio, uma frase",
      "rationale": "Porquê, segundo a conversa, ou null se não houver",
      "origin_quote": "Excerto curto do pedido do utilizador",
      "symbols": ["AgendaService.validarMarcacao"],
      "confidence": 0.0
    }
  ]
}
```

Notas sobre o schema em modo estrito:

- Todos os campos são obrigatórios; campos opcionais são declarados como `str | None` (o modelo devolve `null`).
- `action` e `risk` são `Literal[...]` no Pydantic, o que gera `enum` no JSON Schema.
- Se a resposta vier com `refusal` ou falhar a validação, registar e repetir uma vez; à segunda falha, marcar o commit como `analysis_failed` sem bloquear a fila.

Regras do prompt:

- Regra de negócio = restrição ou comportamento que um gestor reconheceria (preços, prazos, permissões, limites, validações de domínio). **Não** são regras: escolhas técnicas, formatação, refatorações, tratamento genérico de erros.
- Nunca inventar o porquê. Se a conversa não explica, `rationale = null`.
- Preferir `update` a criar uma regra nova quando já existe uma equivalente.
- Descartar resultados com `confidence < 0.5`.

### 7.5.1 Camada de LLM (`packages/llm`)

- Interface `LLMProvider` com dois métodos: `extract(prompt, schema: type[BaseModel]) -> BaseModel` e `embed(texts: list[str]) -> list[list[float]]`.
- Implementação no MVP: `OpenAIProvider`, usando o cliente assíncrono `AsyncOpenAI`.
- O resto do código nunca importa o SDK diretamente; usa sempre a interface. Isto permite trocar ou testar outros fornecedores (Anthropic, Azure OpenAI, modelos locais) sem mexer nos workers.
- Variáveis de ambiente: `OPENAI_API_KEY`, `LLM_MODEL`, `LLM_MODEL_IMPACT` (opcional, para usar um modelo diferente na análise de impacto), `EMBEDDING_MODEL`, `EMBEDDING_DIMENSIONS`.
- Cada chamada regista modelo, tokens de entrada e saída e custo estimado (ver secção 11).
- Testes usam um `FakeProvider` com respostas gravadas, para correrem sem chave nem custos.

### 7.6 Ressincronização de âncoras (tarefa `resync_anchors`)

Corre a cada push no branch principal:

1. Para cada símbolo ancorado cujo ficheiro mudou, procurar por `qualified_name`.
2. Se não existir, procurar por `body_hash` igual (símbolo renomeado ou movido).
3. Se não existir, procurar por similaridade de embedding acima de 0.9.
4. Se nada for encontrado: símbolo `removed`; regras sem nenhuma âncora ativa passam a `orphaned`.

### 7.7 Análise de impacto (tarefa `impact_analysis`)

- Ver Fluxo C.
- Prompt de avaliação recebe: regra (statement + rationale), código atual dos símbolos, diff do PR.
- Saída: `{ risk: "breaks | changes | inconsistent_duplicate | none", confidence, explanation }`.
- Formato do comentário (Markdown, em português, com tom neutro):

```
### Rastro — regras afetadas por este PR

**Implantes não podem ser marcados à sexta à tarde**
Porquê: o Dr. Mendes precisa de 48h de acompanhamento e não trabalha ao fim de semana.
Risco: esta alteração ativa marcações ao sábado, o que muda as condições desta regra.
Atenção: a mesma lógica existe em `calendario.ts` e não foi alterada.
Origem: pedido de 12/03, commit a1b2c3d.
```

## 8. API

| Método | Rota | Descrição |
|---|---|---|
| POST | `/v1/sessions` | Recebe captura da CLI (token por repositório) |
| POST | `/webhooks/github` | Webhooks da GitHub App |
| GET | `/v1/repos/:id/rules` | Lista regras; filtros `file`, `status`, `q` (pesquisa semântica) |
| GET | `/v1/repos/:id/rules/:ruleId` | Regra com origens e âncoras |
| GET | `/health` | Estado |

Autenticação no MVP: token por repositório gerado na instalação, enviado como `Authorization: Bearer`.

## 9. Estrutura do repositório

```
rastro/
├── pyproject.toml            # workspace uv
├── apps/
│   ├── api/                  # FastAPI: rotas, webhooks, enfileiramento
│   │   └── src/rastro_api/
│   ├── worker/               # Procrastinate: tarefas
│   │   └── src/rastro_worker/
│   │       ├── static_analysis.py
│   │       ├── rule_extraction.py
│   │       ├── resync_anchors.py
│   │       └── impact_analysis.py
│   └── web/                  # Angular (fora do MVP, pasta reservada)
├── packages/
│   ├── cli/                  # rastro CLI (Typer, publicada no PyPI)
│   ├── session_parsers/      # claude_code.py (um módulo por agente)
│   ├── code_analysis/        # tree-sitter, hashing, grafo de chamadas
│   ├── llm/                  # interface LLMProvider, OpenAIProvider, prompts, schemas Pydantic
│   └── db/                   # modelos SQLAlchemy + migrações Alembic
├── fixtures/
│   └── clinica-demo/         # repositório TypeScript de teste com regras plantadas
├── docker-compose.yml        # postgres+pgvector, api, worker
└── docs/
```

Monorepo com uv workspaces. Cada pacote tem o seu `pyproject.toml`; os schemas de entrada e saída do LLM (secção 7.5 e 7.7) são modelos Pydantic, reutilizados na validação das respostas.

## 10. Plano de implementação

Implementar por esta ordem. Cada fase termina com testes a passar.

**Fase 1 — Fundações**
- Monorepo uv, modelos SQLAlchemy, migração Alembic da secção 6, docker-compose com PostgreSQL + pgvector.
- API FastAPI com `/health` e worker Procrastinate a correr uma tarefa de teste.
- Ruff, mypy e pytest configurados; CI a correr os três.

**Fase 2 — Análise estática**
- `code_analysis`: extração de símbolos, `body_hash`, grafo de chamadas.
- Testes com ficheiros de exemplo, incluindo renomeações e duplicados.

**Fase 3 — Captura**
- Parser de sessões do Claude Code.
- CLI `init`, `capture`, `flush`.
- `POST /v1/sessions`.

**Fase 4 — GitHub App**
- Webhooks, validação de assinatura, `initial_index`, junção sessão + commit pelo SHA.

**Fase 5 — Extração de regras**
- Tarefa `rule_extraction`, ancoragem, deteção de duplicados.
- Avaliar contra `fixtures/clinica-demo` (critério: 80%).

**Fase 6 — Ressincronização**
- Tarefa `resync_anchors` com os quatro passos da secção 7.6.

**Fase 7 — Análise de impacto**
- Tarefa `impact_analysis`, comentário atualizado no lugar.
- Avaliar taxa de falsos positivos.

**Fase 8 — CLI de consulta**
- `rastro rules` com filtros e pesquisa.

## 11. Segurança e privacidade (mínimo no MVP)

- Remover segredos antes de enviar qualquer captura: padrões de API keys, tokens, strings de ligação, ficheiros `.env`. Usar uma biblioteca de deteção de segredos e testes próprios.
- O conteúdo das sessões é encriptado em repouso (encriptação ao nível da coluna para `excerpts`).
- Nunca guardar o código completo do repositório; apenas metadados dos símbolos e excertos necessários.
- Registar todas as chamadas ao LLM (modelo, tokens, custo) para controlo.
- `.rastro/config.json` não guarda o token em texto simples; usar o keychain do sistema através da biblioteca `keyring`.

## 12. Testes e repositório de demonstração

Criar `fixtures/clinica-demo`: pequena app TypeScript de marcações com histórico de commits e sessões simuladas do Claude Code. Plantar pelo menos 10 regras, incluindo:

- implantes não podem ser marcados à sexta à tarde (com porquê na conversa);
- desconto para maiores de 65 anos (sem porquê na conversa → `rationale` deve ser null);
- a mesma regra duplicada em dois ficheiros;
- uma regra que depois é alterada num commit posterior (deve dar `update`, não regra nova);
- alterações puramente técnicas que **não** devem gerar regras.

Incluir 5 PRs de teste: 2 que devem gerar aviso, 3 que não devem. Estes PRs são a métrica de falsos positivos.

## 13. Fora do MVP — evolução futura

### Fontes e integrações
- Outros agentes: Cursor, Codex, GitHub Copilot, Windsurf, Aider (um módulo por agente em `session_parsers`).
- GitLab e Bitbucket.
- Servidor MCP próprio: o agente consulta as regras antes de alterar código e regista novas regras enquanto trabalha.
- Extensão para VS Code / JetBrains: mostrar a regra e o porquê ao passar o rato sobre o código.
- Ingestão de outras fontes de intenção: issues, tickets (Jira, Linear), descrições de PRs, comentários de revisão, Slack.
- Captura no servidor em vez da máquina do programador, para agentes que corram na cloud.

### Linguagens e análise
- Python, Java, C#, Go, PHP (gramáticas tree-sitter adicionais).
- Resolução de chamadas com type checker (TypeScript compiler API, LSP) para um grafo mais preciso.
- Análise entre repositórios e microserviços (regras que atravessam serviços via APIs).
- Extração de regras de código sem sessão associada (código antigo ou escrito à mão), com `rationale = null` e marcação "origem desconhecida".
- Deteção de regras contraditórias entre si.

### Produto e interface
- Interface web em **Angular** (standalone components, Angular Material) consumindo a API FastAPI: pesquisa de regras, vista por ficheiro, histórico de cada regra, edição e validação manual. Gerar o cliente TypeScript a partir do OpenAPI do FastAPI.
- Chat em linguagem natural sobre o sistema ("que regras existem sobre marcações?").
- Relatório de saúde para não técnicos: regras explicadas vs. sem origem, duplicados, zonas sem dono, evolução no tempo.
- Validação humana de regras (confirmar, corrigir, rejeitar), usada para melhorar a extração.
- Notificações (email, Slack) quando uma regra fica órfã ou é alterada.
- Exportação de documentação de regras (PDF, Markdown) para passagem de testemunho entre equipas.
- Modo "auditoria": relatório pontual para due diligence em compra de empresas.

### Empresa e confiança
- Versão self-hosted / on-premise e opção de usar o LLM do próprio cliente.
- Outros fornecedores de LLM através da interface `LLMProvider`: Anthropic, Azure OpenAI (frequente em clientes empresariais), modelos locais.
- Autenticação com SSO, multi-organização, papéis e permissões.
- Logs de auditoria de acesso.
- Políticas de retenção e eliminação de dados (RGPD).
- Certificações de segurança (ISO 27001, SOC 2) quando houver clientes empresariais.

### Negócio
- Faturação por repositório ou por programador ativo.
- Plano gratuito para projetos open source.
- Métricas de "comprehension debt" para comparar equipas e acompanhar evolução.

### Ideias a explorar
- Ligação ao registo de decisões (decisões de arquitetura, não só regras de negócio).
- Vigilância de pressupostos: avisar quando a razão de uma regra deixa de ser verdade (ex.: "o Dr. Mendes saiu da clínica").
- Geração automática de testes a partir das regras extraídas.

## 14. Questões em aberto

- Como tratar sessões longas que tocam em muitos commits? (MVP: associar à sessão os excertos relevantes para os ficheiros de cada commit.)
- Que modelos usar para extração e para análise de impacto? (Testar 2 ou 3 modelos da OpenAI contra o repositório de demonstração e escolher pelo equilíbrio entre qualidade e custo.)
- Commits sem sessão (feitos à mão): ignorar no MVP ou extrair regras só do diff?
- Política de custos de LLM por repositório (limite mensal de tokens).
