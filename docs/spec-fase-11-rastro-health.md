# Spec — Fase 11: `rastro health` (saúde do conhecimento e do processo)

> Rascunho de desenho. Continua o plano da secção 10 de [spec-rastro-mvp.md](spec-rastro-mvp.md), depois da [Fase 9](spec-fase-9-validacao-humana.md) (validação humana) e da [Fase 10](spec-fase-10-consulta-agente.md) (consulta pelo agente, resultados em [rastro-resultados-fase10.md](rastro-resultados-fase10.md)). Fecha o círculo: as fases 9 e 10 produzem dados (quem validou, o que foi consultado) que ainda ninguém lê.
>
> Escrito a partir do estado do repositório em 21/10/2026 (migrações até `0011`, `agent_sessions.consulted` já preenchido e por ler).
>
> **Revisão de 21/10/2026.** Os pressupostos da primeira versão foram confrontados com o código e com a base de desenvolvimento do `bella-beauty`. Quatro definições não funcionavam com os dados reais e foram corrigidas: R3a (a marca de autor nunca é falsa), V1 (daria 0%), a cobertura (perdia sessões com vários commits) e P5 (o veredicto desaparece quando a proposta passa a `applied`). As secções afetadas dizem o que mudou e porquê; a secção 12 regista as decisões sobre as questões que estavam em aberto.

## 1. Porquê

O artigo de Osmani aponta a **lacuna de medição** como o centro do problema: velocidade, DORA, número de PRs e cobertura de testes parecem saudáveis enquanto a compreensão se degrada. O Rastro já guarda os sinais certos, mas só os mostra regra a regra (`rastro rules`) ou PR a PR (o comentário). Falta a vista de conjunto: **quanto do conhecimento do repositório está registado, explicado, confirmado por pessoas e a ser usado pelos agentes**, e o que fazer a seguir.

Dois princípios guiam o desenho:

1. **Honestidade sobre o que se mede.** O Rastro não mede compreensão humana. Mede indícios: há intenção registada? Alguém que não é o autor a confirmou? O agente foi informado antes de editar? Cada número diz o que é, de onde vem e o que não prova.
2. **Um número só vale se levar a uma ação.** Métrica sem ação é decoração. Por isso o relatório inclui uma **fila de revisão** (o que validar primeiro) e as zonas sem intenção registada.

## 2. Critérios de sucesso

- `rastro health` responde em menos de 2 s (p95) num repositório do tamanho do `bella-beauty` e num conjunto sintético de 50 mil linhas e 5 000 regras. Só SQL; **zero chamadas ao LLM** (teste automático).
- Cada número é rastreável: `rastro health --explain` mostra fórmula, fonte e limitações. Cada métrica tem teste com dados feitos à mão.
- Verificação no `bella-beauty` real: os contadores coincidem com a contagem manual por SQL, e agir muda os números (validar 5 regras sobe "validadas" em 5 e encurta a fila).
- Dado em falta aparece como **n/a, com o motivo**, nunca como 0% (D4).
- Depois de dois instantâneos, a tendência aparece (D7).
- Uma pessoa sem contexto consegue, só com o relatório em Markdown, dizer o que valeria a pena fazer a seguir.

## 3. Decisões de desenho

**D1. Sem índice composto.** Nada de "pontuação de saúde 73/100": esconderia o que está mal, os pesos seriam arbitrários e convidaria a manipulação. O relatório mostra poucos indicadores separados, cada um com numerador e denominador ("3 de 27"). Três servem de manchete: regras com porquê conhecido, regras confirmadas por alguém que não foi o autor, ficheiros com regras consultados antes de editados.

**D2. Contagens com denominador; amostras pequenas assinaladas.** Uma percentagem sozinha engana ("100%" de 2 regras). Abaixo de 10 casos o relatório escreve "amostra pequena".

**D3. Só ao nível da equipa.** Sem classificação por pessoa e sem logins de revisores no relatório. Medir pessoas convida a "marcar caixas para melhorar o número" (lei de Goodhart) e levanta questões de RH. A API devolve agregados. **Equipas pequenas:** numa equipa de três pessoas, um agregado identifica quem foi. As repartições que dependem de quem reviu (P4 e P5) só aparecem com pelo menos **3 revisores distintos** na janela; abaixo disso mostram "n/a (menos de 3 revisores)".

**D4. Desconhecido não é zero.** Sessões antigas sem `consulted`, repositórios sem o hook instalado, regras validadas antes de existirem hashes: aparecem como `n/a` com a razão ("hook não instalado em 4 das 9 sessões"). O mesmo para commits anteriores à existência da ligação commit→PR (V1).

**D5. Tudo em SQL, sem LLM.** Custo nulo e resultado reproduzível. Qualquer métrica que precisasse de modelo fica fora.

**D6. Cobertura de consulta calculada na ingestão, por sessão, não por captura.** As edições de uma sessão estão cifradas em `excerpts`; decifrá-las a cada `rastro health` seria lento e espalharia texto de conversas. Ao receber uma captura, o `POST /v1/sessions` (re)calcula a cobertura **da sessão inteira** (todas as linhas de `agent_sessions` com o mesmo `session_ref`) e grava só caminhos e booleanos numa tabela própria (`session_coverage`, secção 5). Por sessão, e não por captura, porque os `consulted` seguem com a *primeira* captura de uma sessão (Fase 10.4): uma sessão com vários commits teria as capturas seguintes com a lista vazia e a cobertura sairia a 0 sem ser verdade. "Já tinha regras" usa `rules.created_at` anterior à primeira edição do ficheiro (e não o estado de agora), para que um envio atrasado (`rastro flush`, recuperação com `--since`) não conte regras criadas entretanto. Depende de 11.3.

**D7. Instantâneos com versão de definições.** O histórico não se reconstrói (regras mudam, símbolos são reescritos), por isso um trabalho periódico grava os indicadores. Cada instantâneo guarda `definitions_version` e `window_days`; a tendência só compara instantâneos com **ambos iguais**, para que mudar uma fórmula ou a janela não crie um salto falso. Há no máximo um instantâneo por repositório e por dia (coluna `taken_on`).

**D8. Acionável antes de bonito.** A fila de revisão ordena regras por (sem porquê, sem validação, muito ligada ao resto do código) e diz porquê cada uma está lá.

**D9. A cobertura de símbolos não é manchete.** Muito código (utilitários, infraestrutura) não tem regra de negócio, por isso "12% dos símbolos têm regra" não é mau nem bom. Aparece só por zona (diretório), em termos relativos, com exclusões configuráveis.

**D10. Saúde do próprio pipeline à vista.** Se há commits `skipped_budget`, `skipped_quota` ou `analysis_failed`, a base de conhecimento está desatualizada e os outros números enganam. O relatório abre com esse aviso quando existir.

**D11. Estado atual e janela, rotulados.** R1 a R6 descrevem o **estado atual** da base de conhecimento. P, C e V descrevem o que aconteceu **na janela**. O relatório separa as duas coisas com títulos, para ninguém ler "70% com porquê" como "nos últimos 30 dias".

## 4. Métricas (versão 1 das definições)

Janela por omissão: 30 dias (`--window`), aplicada a P, C e V. Todas por repositório.

### 4.1 Base de conhecimento (regras) — estado atual

| # | Métrica | Fórmula | Fonte | O que não prova |
|---|---|---|---|---|
| R1 | Regras ativas / órfãs / rejeitadas | contagem por `rules.status` | `rules` | |
| R2 | Com porquê conhecido | `rationale IS NOT NULL` ÷ ativas; separado por `rationale_source` (conversa / humano) | `rules` | Que o porquê esteja certo |
| R3 | Validadas | `validated_revision = revision` ÷ ativas | `rules` | Que quem validou tenha compreendido |
| R3a | ...das quais, por alguém que não foi o autor do PR | a revisão que deu a validação tem `reviewer_is_pr_author = false`. **Só revisões com origem GitHub**: o login do autor do PR vem do evento e o do revisor é verificado | `rule_reviews` | Revisões feitas pela CLI (`reviewer_source = 'cli'`) não são verificadas: contam como validadas em R3 mas entram em R3a como `n/a` |
| R3b | ...das quais, verificadas pelo GitHub | `reviewer_source = 'github'` ÷ validadas | `rule_reviews` | |
| R4 | Validação ultrapassada pela regra | `validated_revision < revision` (foi validada e depois mudou) | `rules` | |
| R5 | Validação ultrapassada pelo código | regra validada cujo código âncora mudou **depois** da validação: o `body_hash` atual do símbolo é diferente do gravado na revisão (ver 5). Cobre também cópias duplicadas que divergiram. Uma âncora cujo símbolo desapareceu não conta aqui (a regra fica órfã, R1) | `rule_reviews.anchor_hashes`, `symbols` | Que a mudança viole a regra; só que ninguém voltou a olhar |
| R6 | Mais antigas sem validar | idade da regra não validada mais antiga; mediana | `rules.created_at` | |

> **Porque mudou R3a.** A coluna `reviewer_is_session_author` só é escrita como verdadeira ou nula, nunca como falsa, e compara o nome do autor do commit (um nome do git) com um login do GitHub. Na base de desenvolvimento está nula nas 6 revisões. A nova marca `reviewer_is_pr_author` compara dois logins do GitHub e é gravada como verdadeira ou falsa nas revisões feitas pelo GitHub.

### 4.2 Processo (PRs e revisão) — na janela

| # | Métrica | Fórmula | Fonte | Cuidado |
|---|---|---|---|---|
| P1 | PRs com propostas | PRs com `rule_proposals` no período | `rule_proposals` | |
| P2 | Revistas antes do merge | propostas aplicadas com `reviewed_at` preenchido ÷ propostas aplicadas | `rule_proposals` | Depois do merge todas as propostas passam a `applied` (na base de desenvolvimento, 4 em 4); o veredicto fica em `reviewed_at` e em `rule_reviews` |
| P3 | Integradas sem revisão | propostas aplicadas com `reviewed_at IS NULL` | `rule_proposals` | O número que mais importa à equipa |
| P4 | Tempo até à primeira revisão | mediana de `reviewed_at − created_at` (D3: só com 3 ou mais revisores distintos) | `rule_proposals` | |
| P5 | Precisão percebida da extração | entre os **veredictos** da janela: confirmadas sem alteração / corrigidas / rejeitadas. Conta linhas de `rule_reviews` com `verdict` em confirmed, corrected ou rejected; `rationale_added` não é um veredicto e fica de fora (D3: só com 3 ou mais revisores distintos) | `rule_reviews` | Serve para melhorar o Rastro, não para avaliar pessoas. Não sai de `rule_proposals.state`, que já é `applied` |

> **Retirado: P6 (indício de confirmação em lote).** O limiar de 10 s era arbitrário, agrupava por revisor (contra D3), numa equipa pequena identificava a pessoa, e convidava a abrandar de propósito. Fica fora da v1 (secção 11).

### 4.3 Uso pelos agentes — na janela

| # | Métrica | Fórmula | Fonte | Cuidado |
|---|---|---|---|---|
| C1 | Cobertura de consulta | por sessão: ficheiros editados que já tinham regras quando a sessão começou e foram anunciados pelo hook antes da primeira edição ÷ ficheiros editados que já tinham regras. Somado sobre as sessões da janela | `session_coverage` | Só entram os ficheiros do commit (a captura só guarda as edições que tocam os ficheiros do commit). Edições por `Bash` sem `Read` contam como não consultadas, o que é a lacuna que se quer ver |
| C2 | Adoção do hook | sessões com `hook_active = true` ÷ sessões com o campo conhecido | `session_coverage` | Separa "não consultou" de "o hook nem correu" |

### 4.4 Proveniência e pipeline — na janela

| # | Métrica | Fórmula | Fonte |
|---|---|---|---|
| V1 | Código sem intenção registada | **unidades de integração** no ramo principal sem nenhuma sessão ÷ todas as unidades. Unidade = um PR integrado (todos os commits do PR, mais o commit de merge ou squash) ou um commit direto. Uma unidade tem intenção registada se algum dos seus commits tem linha em `agent_sessions` | `commits.merged_pr_number`, `agent_sessions` |
| V2 | Pipeline com falhas | contagens de `analysis_failed`, `skipped_budget`, `skipped_quota`, `awaiting_merge` há mais de N dias | `commits` |

> **Porque mudou V1.** A primeira definição contava commits com `analysis_status = 'skipped_no_session'`. Na base de desenvolvimento há 0 desses; os 40 commits do ramo principal sem sessão têm o estado vazio (commits de merge ou squash, histórico anterior ao Rastro, trabalho feito sem agente). E um squash cria um commit novo sem sessão mesmo quando os commits do PR têm. Por isso a unidade é o PR e não o commit. Commits anteriores à coluna `merged_pr_number` são `n/a` (D4) a menos que se preencham a partir de `pr_analyses`.

V1 não é "código gerado por IA sem registo": código escrito à mão também não tem sessão e é legítimo. É **código sem intenção registada**, e é assim que se rotula.

### 4.5 Zonas e fila

- **Zonas** (diretórios a profundidade 2, configurável): símbolos ativos, % com âncora primária, regras, % validadas, regras sem porquê. Exclusões por omissão: testes, `node_modules`, `*.d.ts`, código gerado; `--exclude` acrescenta globs.
- **Fila de revisão** (por omissão 5): regras ativas ordenadas por (sem porquê, não validada, nº de chamadores dos símbolos âncora, última alteração). Cada linha diz o motivo ("sem porquê; 7 chamadores") e como agir (`rastro rule <id> --confirm`). Uma regra com validação ultrapassada (R4 ou R5) entra na fila com esse motivo.

## 5. Dados novos (migrações `0012` a `0015`, uma por passo)

```sql
-- 0012 (passo 11.2): processo e proveniência
ALTER TABLE rule_proposals ADD COLUMN pr_author text;          -- login GitHub do autor do PR, do evento
ALTER TABLE rule_reviews   ADD COLUMN reviewer_is_pr_author boolean;  -- verdadeiro/falso nas revisões GitHub; NULL na CLI
ALTER TABLE commits        ADD COLUMN merged_pr_number int;     -- PR de que o commit faz parte (inclui o commit de merge/squash)

-- 0013 (passo 11.3): uso pelos agentes
ALTER TABLE agent_sessions ADD COLUMN hook_active boolean;      -- NULL = CLI antiga que não o envia
CREATE TABLE session_coverage (
  id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  repository_id  uuid NOT NULL REFERENCES repositories(id),
  session_ref    text NOT NULL,
  computed_at    timestamptz NOT NULL DEFAULT now(),
  hook_active    boolean,                  -- NULL = desconhecido
  edited_ruled   int  NOT NULL,            -- ficheiros editados que já tinham regras
  consulted_first int NOT NULL,            -- ...e foram anunciados antes da primeira edição
  files          jsonb NOT NULL,           -- [{"file":"src/a.ts","consulted":true}]; só caminhos e booleanos
  UNIQUE (repository_id, session_ref)
);

-- 0014 (passo 11.4): validação ultrapassada pelo código
ALTER TABLE rule_reviews ADD COLUMN anchor_hashes jsonb;        -- {"<caminho>::<nome qualificado>":"<body_hash>"} ao confirmar/corrigir

-- 0015 (passo 11.5): instantâneos
CREATE TABLE health_snapshots (
  id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  repository_id       uuid NOT NULL REFERENCES repositories(id),
  taken_at            timestamptz NOT NULL DEFAULT now(),
  taken_on            date NOT NULL,                  -- dia UTC; garante um instantâneo por dia
  window_days         int  NOT NULL,
  definitions_version int  NOT NULL,
  head_sha            text,
  metrics             jsonb NOT NULL,                 -- números de manchete + contagens; sem texto de regras
  UNIQUE (repository_id, taken_on)
);
```

Notas:

- `session_coverage` guarda caminhos de ficheiros (já presentes nos excertos) e booleanos; nunca texto de regras nem de conversas. Não cifrada, como `consulted`. É recalculada a cada captura da mesma sessão (D6).
- `anchor_hashes` tem como chave `caminho::nome qualificado` (e não o id do símbolo, que muda quando o `resync` move a âncora para um sucessor). É gravado pelos três caminhos de revisão (caixa e comandos no GitHub, rota da CLI, aplicação no merge). Regras validadas antes desta fase ficam com R5 em `n/a`.
- O `POST /v1/sessions` aceita um campo opcional `agent_hook: bool`. A CLI não o tira do instalador: tira-o do próprio hook, que no `session-start` acrescenta uma linha de "estou vivo" ao registo `consulted.jsonl` (`via: "start"`); a captura envia `agent_hook = true` se a sessão tem essa linha, `false` se há hook instalado mas a linha falta, e omite o campo se não sabe. O esquema de `consulted` passa a aceitar `via: "start"` com `file` vazio, e o cálculo de cobertura ignora essas linhas. CLIs antigas continuam a funcionar.
- `pr_author` e `merged_pr_number` são gravados pelos trabalhos que já tratam o PR (`analyze_pr`, `pull_request_merged`, `apply_review`), sem chamadas novas ao GitHub além das que já fazem.
- Sem backfill de `session_coverage` para sessões antigas (ficam sem linha, `n/a`). Fica como opção um comando de administração que decifra e recalcula.

## 6. Cálculo e API

Módulo `rastro_db.health` (ao lado de `budget`): funções que recebem a sessão da base de dados e a janela e devolvem um `HealthReport` (dataclass). Usado pela API e pelo trabalho de instantâneos, por isso os dois nunca divergem.

| Método | Rota | Descrição |
|---|---|---|
| GET | `/v1/repos/:id/health` | `window` (dias, 7 a 365), `exclude` (globs, repetível), `zones`, `queue`; inclui `compared_to` com o instantâneo mais antigo da janela com a mesma `definitions_version` e a mesma `window_days`, se existir |
| GET | `/v1/repos/:id/health/history` | instantâneos (só manchetes), mais recentes primeiro |

Mesma autenticação e isolamento por repositório que as rotas de regras. Sem rota de escrita: os instantâneos só os cria o trabalho.

Trabalho `health_snapshot` (Procrastinate, `@app.periodic`, diário), para repositórios `active`, com `queueing_lock` por repositório. Falha de um repositório não pára os outros. Repetir no mesmo dia é inofensivo (`UNIQUE (repository_id, taken_on)`).

## 7. CLI

```
rastro health [--window 30d] [--zones] [--queue] [--exclude GLOB] [--json]
              [--format markdown] [--explain] [--history]
```

Exemplo (dados ilustrativos):

```
Rastro — saúde de acme/clinica  (regras de d27aa39; processo e uso dos últimos 30 dias)

! 2 commits ficaram sem análise por limite de custo: os números abaixo podem estar desatualizados.

Conhecimento registado (estado atual)
  Regras ativas                      27     (1 órfã, 2 rejeitadas)
  Com porquê conhecido               19 de 27   (70%)   6 da conversa, 13 escritas por pessoas
  Confirmadas por uma pessoa          3 de 27   (11%)   2 por alguém que não foi o autor do PR
  Validação ultrapassada pelo código  1 de 3             [rastro rule 7f3a...]

Processo (últimos 30 dias)
  Propostas de PRs                   14   integradas sem revisão: 6 (43%)   mediana até 1.ª revisão: 5 h
  Confirmadas / corrigidas / rejeitadas   5 / 2 / 1

Agentes (últimos 30 dias)
  Ficheiros com regras consultados antes de editar   9 de 15 (60%)
  Sessões com o hook a correr                        7 de 11 (n/a em 3 sessões antigas)

Código sem intenção registada (últimos 30 dias)
  Integrações sem sessão             12 de 41 (29%)

Fila de revisão (o que validar primeiro)
  1. 2a7851  Máximo de 3 marcações ativas        sem porquê; 7 chamadores
  2. ...
```

`--explain` junta a definição e as limitações de cada linha. `--format markdown` produz o relatório para não técnicos (secção 13 da spec do MVP), com as explicações incluídas. `--history` mostra a tendência das manchetes entre instantâneos da mesma versão de definições e da mesma janela.

Fora do âmbito de propósito: `--fail-under` ou qualquer bloqueio em CI. Um limiar fixo sobre estas métricas é o caminho mais curto para as manipular.

## 8. Segurança e privacidade

- O relatório contém contagens, caminhos de ficheiros e, na fila, o texto das regras: pede o token do repositório, como as rotas de regras. Os instantâneos não guardam texto de regras.
- Sem logins nem nomes de revisores no relatório (D3), e sem repartições por revisor quando há menos de 3 revisores distintos. `pr_author` e `reviewer_is_pr_author` guardam-se na base de dados para o cálculo mas nunca saem da API.
- `session_coverage` e `anchor_hashes` não contêm conversa. O `hook_active` é um booleano.

## 9. Testes e avaliação

**Automáticos**
- Uma base de dados construída à mão por métrica, com resultado calculado manualmente: regras com e sem porquê (conversa e humano), validadas pelo autor do PR, por outro revisor GitHub e pela CLI, ultrapassadas pela regra e pelo código (hash diferente, cópia duplicada divergente, âncora movida pelo `resync` sem mudar o corpo), propostas integradas com e sem revisão, ficheiros editados com e sem consulta, PRs com e sem sessão (incluindo squash), commits com cada estado.
- **Cobertura por sessão:** uma sessão com dois commits, em que as consultas viajaram só com a primeira captura, dá a cobertura certa; um envio atrasado não conta regras criadas depois da primeira edição; recalcular não duplica.
- **V1:** um PR com sessões e o seu commit de squash contam como uma unidade com intenção; um commit direto sem sessão conta como sem intenção; commits sem `merged_pr_number` ficam `n/a`.
- **R3a:** uma revisão GitHub do autor do PR e uma de outro revisor dão contagens diferentes; uma revisão da CLI dá `n/a`.
- `n/a` em vez de 0%: sessões sem `consulted`, `hook_active` nulo, regras sem `anchor_hashes`, repositório vazio.
- Amostra pequena (menos de 10) assinalada; menos de 3 revisores distintos suprime P4 e P5.
- Nenhuma chamada ao LLM durante `health` e durante o instantâneo (o `FakeProvider` falha se for chamado).
- Isolamento: o token de outro repositório não vê nada.
- Instantâneos: um por dia, comparação só entre versões de definições e janelas iguais, falha de um repositório não pára os outros.
- Relatório de referência (golden) em texto e em Markdown, para apanhar alterações acidentais de formato.
- Desempenho: o conjunto sintético de 5 000 regras e 50 mil linhas dentro do orçamento.

**Avaliação real** (estilo de `rastro-resultados-fase10.md`)
- No `bella-beauty`: comparar cada contador com SQL escrito à mão.
- Teste de ação: validar 5 regras da fila e confirmar que o número sobe em 5, a fila encurta e R3a distingue autor de outro revisor.
- Teste de ultrapassagem pelo código: alterar o corpo de uma função âncora de uma regra validada e ver R5 passar a 1.
- Teste de leitura: dar o relatório em Markdown a alguém que não conhece o projeto e perguntar o que faria a seguir. Se a resposta não for uma das linhas da fila ou das zonas, o relatório falhou.
- Pergunta que ficou da Fase 10 (valeria a pena perguntar só nas regras validadas? ver secção 12 de `rastro-resultados-fase10.md`): **o `bella-beauty` só tem 3 regras validadas, pouco para responder**. Só se responde depois de validar uma parte substancial das regras ou noutro repositório; até lá, o relatório regista quantas regras validadas existem como pré-condição.

## 10. Plano de implementação

Cada passo termina com testes a passar e entrega valor sozinho.

**11.1 Núcleo.** `rastro_db.health` com R1 a R3, R3b parcial (só `reviewer_source`), R4, R6, V2, zonas e fila (só dados já existentes, sem migração); `GET /health`; `rastro health`, `--json`, `--explain`, `--zones`, `--queue`; testes de definições. V1 e R3a ficam para o 11.2 porque dependem de dados novos.

**11.2 Processo e proveniência.** Migração `0012` (`pr_author`, `reviewer_is_pr_author`, `merged_pr_number`), gravados pelos trabalhos que já tratam o PR; P1 a P5, V1 e R3a completos; D3 aplicado (mínimo de 3 revisores). Preencher `merged_pr_number` a partir de `pr_analyses` para o histórico quando possível.

**11.3 Uso pelos agentes.** Migração `0013` (`agent_sessions.hook_active`, `session_coverage`), o "estou vivo" do `session-start` e `via: "start"` na captura, cálculo da cobertura por sessão no `POST /v1/sessions`, C1 e C2.

**11.4 Ultrapassagem pelo código.** Migração `0014` (`anchor_hashes`) gravado nos três caminhos de revisão e R5; R3b completo.

**11.5 Instantâneos e tendência.** Migração `0015`, trabalho periódico, `compared_to`, `--history`.

**11.6 Relatório e avaliação real.** `--format markdown`, avaliação da secção 9, `docs/rastro-resultados-fase11.md` e ajuste de limites (amostra pequena, janela, tamanho da fila).

Ordem: 11.1 primeiro porque já dá valor sem tocar em esquema nenhum; 11.2 e 11.3 trazem o que a Fase 9 e a 10 prometeram medir; 11.4 só depois, por exigir mexer em três caminhos de escrita.

## 11. Fora de âmbito e fases seguintes

- Interface web (Angular) com gráficos; o `--json` e a API já servem de base.
- Alertas (Slack, e-mail) quando um indicador piora.
- Pontos quentes por alteração frequente: exigem guardar os ficheiros de cada commit (tabela nova), que hoje não existe.
- Comparação entre equipas ou repositórios, indicada na secção 13 da spec do MVP. Fica de fora até haver forma de a tornar à prova de manipulação.
- Bloqueio em CI por limiar (ver secção 7).
- Criticidade das regras (dinheiro, permissões, dados pessoais): permitiria pesar a fila e as métricas; fase própria.
- Indício de confirmação em lote (o antigo P6): retirado por contrariar D3 e por o limiar ser arbitrário. Só voltaria com uma definição que não identificasse pessoas.

## 12. Questões em aberto e decisões

**Decididas na revisão de 21/10/2026**

- **Detetar `hook_active`:** vem do próprio hook, que regista um "estou vivo" no início da sessão (secção 5). O estado do instalador não chega: não diz se o hook correu naquela sessão.
- **P6 (confirmação em lote):** retirado da v1 (secção 4.2).
- **Retenção dos instantâneos:** guardar todos. Um por repositório e por dia são cerca de 365 linhas pequenas por ano.
- **Equipas pequenas:** repartições por revisor só com 3 ou mais revisores distintos (D3).

**Ainda em aberto**

- **Limites:** "amostra pequena" a menos de 10, janela de 30 dias e profundidade 2 das zonas são palpites; ajustar com dados reais. No `bella-beauty` quase tudo na janela vai ser "amostra pequena" (4 propostas, 6 revisões).
- **Fila de revisão:** o peso dado ao número de chamadores como proxy de importância é uma hipótese. No `bella-beauty` 24 das 27 regras estão por validar, por isso a fila será dominada pelo critério "sem porquê"; comparar com a escolha de uma pessoa quando houver mais regras validadas.
- **Backfill de `merged_pr_number`:** preencher o histórico a partir de `pr_analyses` cobre só os PRs analisados; saber se vale a pena um preenchimento mais completo (listar os PRs integrados no GitHub) ou aceitar `n/a` para o passado.
