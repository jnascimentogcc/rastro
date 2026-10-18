# Spec — Fase 9: Validação humana e resumo de regras no PR

> Rascunho de desenho. Continua o plano da secção 10 de [spec-rastro-mvp.md](spec-rastro-mvp.md) (fases 1 a 8 implementadas). Segue o mesmo estilo; a spec do MVP continua a ser a fonte de verdade para tudo o que não é alterado aqui.

## 1. Porquê

O Rastro regista a **intenção** (o que o programador pediu e porquê), mas não sabe se alguém a **compreendeu**. Um revisor que aprova um PR sem ler continua a gerar débito de compreensão, mesmo com a regra bem registada. Hoje também só há aviso depois de a regra existir: as regras de um PR só são extraídas **depois do merge** (commits em branch de feature ficam `awaiting_merge`), por isso o revisor nunca vê, antes de aprovar, o que o PR ensina ao Rastro.

Esta fase fecha esse elo:

1. mostra no PR as regras que ele **propõe** (pré-visualização, sem tocar no grafo);
2. deixa um humano **confirmar, corrigir ou rejeitar** cada uma sem sair do GitHub;
3. guarda quem validou o quê e invalida a validação quando a regra muda;
4. impede que uma regra rejeitada volte a ser criada.

É a base para as fases seguintes (métricas de saúde, consulta pelo agente, criticidade): sem "validada por humano" não há ownership nem medida de compreensão.

## 2. Critérios de sucesso

- Num PR com commits que têm sessão, o comentário passa a listar as regras propostas até 2 minutos depois do push (mesmo orçamento do Fluxo C).
- Confirmar, corrigir e rejeitar funcionam só com ações no GitHub (caixa de seleção e comandos em comentário). A CLI cobre as regras que já estão no grafo.
- Uma regra rejeitada **nunca** é recriada por extrações seguintes (teste automático).
- Uma regra validada que é alterada por uma extração volta a `não validada` e o próximo PR que lhe toca mostra "altera regra validada por @x em dd/mm".
- Nenhuma ação bloqueia merge ou commit. O Rastro continua informativo (ver D5).
- Avaliação: as regras propostas em pré-visualização coincidem com as plantadas em `fixtures/clinica-demo` ao mesmo nível da extração real (>= 80%).

## 3. Decisões de desenho

**D1. Pré-visualização em vez de esperar pelo merge.** Corre-se a extração em modo `preview` sobre os commits do PR que têm sessão: reutiliza `_analyze_changes`, `_ask_model` e `_validate` (todas as proteções "o modelo propõe, o código decide") e **para antes de `_apply`**. O resultado fica em `rule_proposals`, nunca em `rules`. Alternativa rejeitada: validar só depois do merge, porque o revisor já aprovou.

**D2. Identidade do revisor = utilizador GitHub, via eventos do próprio GitHub.** Evita criar um sistema de autenticação. Caixa de seleção marcada no comentário do Rastro = confirmar (evento `issue_comment` `edited`, `sender.login`). Corrigir e rejeitar precisam de texto, por isso usam comandos num comentário novo (`issue_comment` `created`). Só contam utilizadores com permissão de escrita no repositório (verificada na API do GitHub). Alternativa rejeitada: login próprio com OAuth, demasiado cedo sem interface web.

**D3. A validação aplica-se a uma revisão da regra.** `rules.revision` sobe sempre que `statement` ou `rationale` mudam. Uma regra está validada se `validated_revision == revision`. Não há estado `stale` a manter: a invalidação é consequência do número.

**D4. Rejeitar é memória, não apagar.** Uma regra rejeitada fica com `status = 'rejected'`, com embedding e âncoras, e serve de filtro: propostas `create` com similaridade >= 0.92 a uma regra rejeitada nos mesmos símbolos são descartadas. Como todas as consultas existentes filtram `status == 'active'` (impacto, extração, resincronização), a regra rejeitada fica fora de tudo sem mais alterações.

**D5. Informativo, não bloqueante.** Sem `check run` obrigatório nesta fase. Um bloqueio só faz sentido depois de medir falsos positivos e de existir criticidade (fase futura).

**D6. A validação humana é uma dimensão separada de `confidence`.** `confidence` continua a ser a confiança do modelo; "validada" é um facto humano. Nunca se mistura num único número.

**D7. Texto humano é legítimo como `rationale`.** Se o revisor escreve o porquê, ele entra com `rationale_source = 'human'`. Isto respeita a regra "nunca inventar o porquê": quem inventa passa a ser uma pessoa identificada, não o modelo.

## 4. Modelo de dados (migração `0008`)

```sql
ALTER TABLE rules
  ADD COLUMN revision            int  NOT NULL DEFAULT 1,
  ADD COLUMN validated_revision  int,                -- NULL = nunca validada
  ADD COLUMN validated_by        text,
  ADD COLUMN validated_at        timestamptz,
  ADD COLUMN rationale_source    text;              -- conversation | human | NULL
-- rules.status passa a aceitar também 'rejected'

CREATE TABLE rule_reviews (                          -- histórico, só se acrescenta
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  rule_id         uuid NOT NULL REFERENCES rules(id) ON DELETE CASCADE,
  rule_revision   int  NOT NULL,
  verdict         text NOT NULL,                     -- confirmed | corrected | rejected | rationale_added
  reviewer        text NOT NULL,                     -- login GitHub ou email do git
  reviewer_source text NOT NULL,                     -- github | cli (cli não é verificado)
  reviewer_is_session_author boolean,
  statement_after text,
  rationale_after text,
  note            text,
  pr_number       int,
  created_at      timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE rule_proposals (                        -- saída do modo preview, por commit
  id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  repository_id    uuid NOT NULL REFERENCES repositories(id),
  pr_number        int  NOT NULL,
  commit_sha       text NOT NULL,                    -- SHA original (não o do merge)
  proposal_key     text NOT NULL,                    -- hash(statement normalizado + símbolos ordenados)
  action           text NOT NULL,                    -- create | update
  existing_rule_id uuid REFERENCES rules(id),
  statement        text NOT NULL,
  rationale        text,
  origin_quote     text,
  symbols          jsonb NOT NULL,                   -- chaves path::qualified.name
  confidence       real NOT NULL,
  state            text NOT NULL DEFAULT 'pending',  -- pending | confirmed | corrected | rejected | applied | superseded
  reviewed_by      text,
  reviewed_at      timestamptz,
  created_at       timestamptz NOT NULL DEFAULT now(),
  UNIQUE (repository_id, commit_sha, proposal_key)
);
CREATE INDEX ON rule_proposals (repository_id, pr_number);
```

Notas:

- `origin_quote` vem das sessões. Tratar como `rule_origins.quote` hoje; decidir na implementação se passa a cifrado (ver questões em aberto).
- Uma proposta `update` mostra o estado anterior da regra, incluindo "validada por @x" quando `validated_revision == revision`.
- Duas propostas de commits diferentes do mesmo PR sobre os mesmos símbolos: a mais recente fica `pending`, a anterior passa a `superseded` (espelha o "último commit ganha" do merge).

## 5. Fluxos

### Fluxo D — Pré-visualização no PR

1. `analyze_pr` (já tem `queueing_lock` por PR) lista os commits do PR com `get_pull_request_commits`.
2. Para cada commit com sessão e **sem propostas já guardadas** para esse SHA, corre `extract_rules(mode="preview")`. Fica em cache por SHA, por isso pushes seguintes só pagam os commits novos.
3. Respeita `LLM_MONTHLY_TOKEN_BUDGET` e o tratamento de quota. Se não puder correr, o comentário diz "pré-visualização de regras indisponível (limite de custo)". Silêncio não pode ser lido como "sem regras".
4. O comentário único (marcador `<!-- rastro:pr-impact -->`) ganha uma segunda secção. Só aparece se houver propostas; o comportamento de "só comentar acima do limiar" mantém-se para a secção de impacto.

Exemplo da nova secção:

```
### Rastro — regras que este PR propõe

- [ ] **Implantes não podem ser marcados à sexta à tarde**  <!-- rastro:proposal:7f3a... -->
  Porquê: o Dr. Mendes precisa de 48h de acompanhamento. (da conversa)
  Nova regra · `agenda.ts::AgendaService.validarMarcacao`
- [ ] **Maiores de 65 anos têm 10% de desconto**  <!-- rastro:proposal:b21c... -->
  Porquê: origem desconhecida. Responda `/rastro why b21c <texto>` se souber.
  Altera regra validada por @ana em 12/10 · `preco.ts::calcularPreco`

Marque a caixa para **confirmar**. Para outras ações, comente:
`/rastro reject <id>` · `/rastro correct <id>: <nova frase>` · `/rastro why <id> <porquê>`
```

Limites para evitar fadiga de revisão: no máximo 5 propostas visíveis, ordenadas por (altera regra validada, confidence); as restantes ficam num bloco `<details>`.

### Fluxo E — Revisão

1. Subscrever `issue_comment` (`created` e `edited`) na GitHub App. Verificar que as permissões atuais (`pull_requests: write`) chegam para receber o evento; se não, acrescentar `issues: read`.
2. O handler valida HMAC (como hoje), ignora eventos cujo `sender` seja a própria App (evita o ciclo "o Rastro edita o seu comentário, que gera um evento") e enfileira `apply_review` com `lock=<repository_id>`.
3. `apply_review`:
   - confirma que o `sender` tem permissão de escrita (novo método `get_collaborator_permission` no protocolo `GitHubClient`, com versão em `FakeGitHub`);
   - caixa assinalada de **desmarcada para marcada** na proposta = `confirmed`; desmarcar retira a confirmação (volta a `pending`);
   - `/rastro reject|correct|why` num comentário novo atua sobre a proposta pelo prefixo do id (mínimo 4 caracteres, ambíguo = responde com erro curto);
   - atualiza `rule_proposals`, e `rule_reviews` quando a proposta já aponta para uma regra existente (`update`);
   - volta a renderizar o comentário: a caixa passa a "confirmada por @user".
4. Idempotente por `delivery_id` (já existe `webhook_deliveries`).

### Fluxo F — Merge

`pull_request_merged` (já existe) passa, por commit, a:

1. procurar propostas para `(repository, sha original)`;
2. se existem: `extract_rules(mode="apply")` salta o modelo e executa `_apply` com as propostas guardadas (texto corrigido tem prioridade; `rejected` cria a regra com `status = 'rejected'` apenas como memória; `pending` aplica-se normalmente como hoje, não validada), depois grava `rule_reviews` e `validated_*` para as `confirmed`/`corrected`;
3. se não existem (commit sem pré-visualização, sessão chegou depois, PR anterior a esta fase): comportamento atual, sem alterações.

Como o `apply` não volta a chamar o modelo, não há problema de "a frase saiu diferente da que o revisor confirmou".

### Fluxo G — Invalidação

Em `_store_rule`, quando um `update` muda `statement` ou `rationale`, `revision += 1`. O caso "restates the same" (ignorado como `unchanged`) não toca na revisão. A regra continua `active`, mas deixa de estar validada.

## 6. API e CLI

API (token por repositório, como hoje):

| Método | Rota | Descrição |
|---|---|---|
| GET | `/v1/repos/:id/rules` | novo campo `validation` (`validated` / `unvalidated` / `rejected`) e filtro `validated=true\|false`; `status` aceita `rejected` |
| GET | `/v1/repos/:id/rules/:ruleId` | passa a incluir `reviews` (mais recente primeiro) |
| POST | `/v1/repos/:id/rules/:ruleId/reviews` | corpo `{verdict, statement?, rationale?, note?, reviewer}`; grava com `reviewer_source = 'cli'` |

CLI:

- `rastro rules --unvalidated` e `--rejected`.
- `rastro rule <prefixo> --confirm | --reject | --correct "<frase>" --why "<porquê>"`. O revisor é o `git config user.email` e fica marcado como **não verificado** (qualquer portador do token podia escrever outro nome).
- `rastro rule` mostra "validada por X em dd/mm" ou "não validada".

## 7. Segurança e privacidade

- Autorização de comandos: permissão de escrita no repositório, verificada na API do GitHub a cada ação. Comentários de quem não tem escrita são ignorados em silêncio.
- O texto do revisor é dado, nunca instrução: não vai para nenhum prompt. Limitar `correct`/`why` a 500 caracteres e escapar Markdown ao reapresentar.
- O comentário no PR é visível a quem pode ler o repositório. Mostra `rationale`, nunca `origin_quote`. **Em repositórios públicos, o porquê vem de conversas privadas**: a pré-visualização fica desligada por omissão nesses repositórios, com opção explícita para ligar (`PREVIEW_PUBLIC_REPOS`).
- `reviewer_is_session_author`: guardado para a fase de métricas distinguir confirmações independentes de auto-confirmações. Nesta fase auto-confirmar é permitido.

## 8. Testes

- `FakeGitHub`: eventos `issue_comment`, permissões de colaborador, edição do comentário do Rastro.
- Unitários: renderização das duas secções (pt e en); parsing de caixas e comandos; prefixos ambíguos; limite de 5 propostas; bot a editar o seu comentário não gera ação.
- Integração: preview não escreve em `rules`; confirmar, depois merge, regra `validated`; corrigir, depois merge, texto corrigido em vigor; rejeitar, depois outro commit semelhante, regra **não** recriada; squash merge mantém as revisões (SHA original); extração posterior que muda a frase invalida a validação; orçamento/quota esgotados mostram o aviso; utilizador sem escrita é ignorado.
- Avaliação: acrescentar à `fixtures/clinica-demo` um PR com duas regras plantadas e uma proposta que deve ser rejeitada; critério: propostas = regras plantadas (>= 80%).

## 9. Plano de implementação

Cada passo termina com testes a passar e entrega valor sozinho.

**9.1 Modelo, API e CLI** (sem GitHub): migração `0008`, `revision`/`validated_*`, `rule_reviews`, estado `rejected`, rota de revisão, comandos da CLI, supressão de regras rejeitadas em `_validate`, invalidação por `revision`. Já permite validar regras existentes no grafo.

**9.2 Pré-visualização**: modo `preview` de `extract_rules`, `rule_proposals`, secção nova do comentário (só leitura, sem ações), tratamento de orçamento/quota e de repositórios públicos.

**9.3 Revisão no GitHub**: subscrição `issue_comment`, `get_collaborator_permission`, job `apply_review`, caixas e comandos, re-renderização.

**9.4 Merge**: modo `apply` em `pull_request_merged`, gravação das revisões, "altera regra validada por @x" no PR seguinte. Avaliação com a fixture.

## 10. Fases seguintes (esboço)

- **Fase 10, `rastro health`**: % de símbolos com regra, % de regras com porquê, % validadas (separando GitHub de CLI e independentes de auto-confirmadas), órfãs, duplicados inconsistentes, "matéria escura" (código sem regra nem sessão) e tendência.
- **Fase 11, consulta pelo agente**: servidor MCP (ou hook `PreToolUse`) para o Claude Code consultar as regras do ficheiro antes de editar e registar o porquê em falta.
- Depois: criticidade das regras e `check run` bloqueante; regra para teste.

## 11. Questões em aberto

- **Eventos de caixa de seleção**: confirmar com uma App real que editar uma caixa no comentário de um bot gera `issue_comment` `edited` com o `sender` correto e que quem tem escrita o pode fazer. Se não, a confirmação passa também a comando (`/rastro ok <id>`) e a caixa fica só como atalho. Não consegui validar isto sem uma App instalada.
- Cifrar `origin_quote` e o texto das propostas como `excerpts`?
- Política para auto-confirmação: permitir, mas contar à parte (proposto), ou exigir outro revisor?
- Limite de 5 propostas visíveis: é o valor certo para evitar fadiga? Medir no primeiro repositório real.
- Preview em PRs com muitos commits: custo por PR. Medir com `llm_calls` e decidir se há teto por PR.
