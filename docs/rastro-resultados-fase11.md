# Resultados da Fase 11: `rastro health`

> Relatório vivo da [spec da Fase 11](spec-fase-11-rastro-health.md). Esta versão cobre os **passos 11.1 (núcleo), 11.2 (processo e proveniência), 11.3 (uso pelos agentes) e 11.4 (validação ultrapassada pelo código) 11.5 (instantâneos e tendência) e a parte de código do 11.6 (formato Markdown)**, feitos a 21/10/2026. As secções dos passos 11.2 a 11.6 acrescentam-se quando existirem.

## 1. Passo 11.1: núcleo

Feito: `rastro_db.health.build_report`, `GET /v1/repos/{id}/health` e `rastro health` com `--window`, `--zones`, `--queue`, `--exclude`, `--explain` e `--json`. Sem migração: só lê dados que já existiam.

### O que o relatório já mostra

- **Base de conhecimento (estado atual):** regras por estado (R1), com porquê conhecido e por quem foi escrito (R2), confirmadas por uma pessoa (R3), confirmações verificadas pelo GitHub (R3b, só pela origem da revisão), confirmação ultrapassada por uma edição da regra (R4), idade da regra mais antiga por confirmar (R6).
- **Aviso de pipeline (V2):** commits que falharam a análise ou foram saltados por orçamento ou quota na janela, e commits à espera de merge há mais de 14 dias. Abre o relatório porque, enquanto existirem, os outros números podem estar desatualizados.
- **Zonas:** os dois primeiros diretórios dos símbolos ativos (símbolos, símbolos com regra primária, regras, confirmadas, sem porquê). Testes, dependências, `*.d.ts`, `dist`, `build` e código gerado ficam de fora por omissão; `--exclude` acrescenta.
- **Fila de revisão:** regras ativas sem porquê ou sem confirmação válida, por esta ordem: sem porquê, por confirmar, número de chamadores dos símbolos âncora, última alteração. Cada linha diz o motivo e o comando.

Cada número traz numerador e denominador. Sem denominador, aparece `n/a` com a razão; abaixo de 10 casos aparece "small sample". Não há nota única nem nada por pessoa.

### Verificação no `bella-nina` real

Contra a pilha de desenvolvimento, `rastro health` e uma contagem manual em SQL dão o mesmo:

| Figura | `rastro health` | SQL à mão |
|---|---|---|
| Regras ativas / rejeitadas | 27 / 1 | 27 / 1 |
| Com porquê | 7 de 27 (6 da conversa, 1 de pessoa) | 7 / 27 (6 / 1) |
| Confirmadas | 3 de 27 | 3 / 27 |
| ...pelo GitHub | 3 de 3 | 3 |
| Ultrapassadas pela regra | 0 de 3 | 0 |
| Por confirmar / mais antiga | 24 / 2 dias | 24 / 2 dias |

O que o relatório diz do `bella-nina`: **só 26% das regras têm porquê e só 11% foram confirmadas por uma pessoa**; a fila abre com regras de datas e fusos, de intervalos entre atendimentos e de horário de funcionamento, todas sem porquê e com 2 a 4 chamadores. As 3 confirmadas passaram todas pelo GitHub. Não há aviso de pipeline.

### Desempenho

Num repositório sintético de 5 000 regras, 20 000 símbolos (cerca de 50 000 linhas) e 20 000 chamadas, o relatório completo com a fila de 50 responde em **0,31 s**, contra o orçamento de 2 s. Antes de uma correção demorava 18 s: o número de chamadores era uma subconsulta por regra que percorria `symbol_calls` (que não tem índice em `callee_id`) 5 000 vezes. Passou a uma junção agrupada uma só vez.

### Testes

30 testes da API sobre uma base de dados construída à mão (cada figura calculada manualmente: regras com e sem porquê por conversa e por pessoa, confirmadas pelo GitHub, pela CLI e sem revisão, ultrapassadas pela regra, zonas com exclusões, fila com ordem e motivos, commits em cada estado dentro e fora da janela), os casos de `n/a`, amostra pequena, janela fora dos limites, isolamento por token, `explain`, e uma verificação de que o relatório **não chama o modelo**. 12 testes da CLI sobre a formatação, os parâmetros enviados, `--explain`, `--json`, janelas inválidas e erros do servidor.

### Decisões e limites

- **R4** divide pelas regras que alguma vez foram confirmadas (confirmadas e ultrapassadas), e não pelas ativas, porque pergunta "das que alguém confirmou, quantas mudaram depois".
- **R3b** só olha para a origem da revisão que deu a confirmação. Uma regra confirmada sem linha em `rule_reviews` conta como não verificada.
- **A fila e as zonas** são lidas do estado atual; não há tendência até ao passo 11.5.
- **Ficou de fora de propósito** (precisa de dados que os passos seguintes passam a gravar): R3a (quem não é o autor do PR), R5 (código mudou depois de validar), P1 a P5, V1, C1 e C2, e os instantâneos. O estado do `bella-nina` confirma porquê: `reviewer_is_session_author` está nulo em todas as revisões e nenhum commit tem `skipped_no_session`.
- **`--queue N`** é uma contagem (0 esconde a fila) e não um simples interruptor como a spec sugere; mostra por omissão 5.
- **Sem `--format markdown` nem `--history`:** são dos passos 11.5 e 11.6.

## 2. Passo 11.2: processo e proveniência

Feito: migração `0012`, o registo de três factos novos pelos trabalhos que já tratam os PRs, e as figuras R3a, P1 a P5 e V1 no relatório (API e `rastro health`).

### O que passou a ser gravado

| Facto | Onde | Como |
|---|---|---|
| Autor do PR | `rule_proposals.pr_author` | o webhook `pull_request` passa `user.login` ao `analyze_pr` e ao `pull_request_merged`; só preenche propostas que ainda não têm autor |
| PR a que um commit pertence | `commits.merged_pr_number` | gravado nos commits do PR e no commit de merge ou squash; um commit mantém o primeiro PR com que foi registado. A migração preenche os commits que já tinham propostas (3 no `bella-nina`) |
| Revisor é o autor do PR | `rule_reviews.reviewer_is_pr_author` | compara dois logins do GitHub, sem distinguir maiúsculas, quando a revisão é aplicada no merge: verdadeiro, falso, ou nulo se o autor do PR não é conhecido. As revisões da CLI ficam nulas |

A marca antiga `reviewer_is_session_author` continua a existir mas não é usada: compara o nome do autor do commit com um login e nunca é falsa.

### As figuras novas

- **R3a:** das regras confirmadas, quantas foram confirmadas por um login diferente do autor do PR; só conta quando os dois logins se conhecem.
- **P1:** PRs com propostas na janela. **P2 e P3:** propostas integradas na janela que foram, ou não, revistas antes do merge. **P4:** mediana do tempo até à primeira revisão. **P5:** veredictos sobre a extração (confirmada tal como estava, corrigida, rejeitada), lidos de `rule_reviews` porque depois do merge todas as propostas ficam `applied`.
- **V1:** integrações sem sessão registada, por unidade de integração (um PR com os seus commits e o commit de merge, datado pelo mais recente; ou um commit direto com sessão).
- **Privacidade (D3):** P4 e P5 só aparecem com pelo menos 3 revisores distintos na janela; abaixo disso mostram `n/a` e a razão. Os totais que não nomeiam ninguém (P1 a P3) aparecem sempre.

### Verificação no `bella-nina` real

Contra a pilha reconstruída, `rastro health --window 365d` e a contagem manual em SQL dão o mesmo:

| Figura | `rastro health` | SQL à mão |
|---|---|---|
| P1: PRs com propostas | 3 | 3 |
| P2 / P3: integradas, revistas | 4 de 4 / 0 de 4 | 4 aplicadas, 4 revistas |
| P4 e P5 | `n/a` (menos de 3 revisores) | 1 revisor distinto |
| V1 | 0 de 19; 40 commits não atribuídos | 3 PRs, 16 commits diretos com sessão, 40 sem sessão nem PR |
| R3a | `n/a` (nenhuma confirmação com autor do PR conhecido) | as 3 revisões são anteriores a `pr_author` |

O que isto diz da equipa de demonstração: todas as propostas foram revistas antes do merge, mas por **um só revisor**, e a **maior parte do histórico (40 commits) não se consegue atribuir a um PR**, porque é anterior à ligação. O V1 de 0% é por isso uma leitura otimista: o relatório diz-o logo por baixo. R3a e P4/P5 só passam a ter números depois de um próximo ciclo de revisão num PR real (o autor do PR passa a ser gravado e os revisores acumulam-se).

### Decisões e limites

- **Commits diretos sem sessão ficam fora do rácio de V1** (`unattributed_commits`), contra a spec original, que os contava como "sem intenção registada". Na base real não se distinguem de commits de merge anteriores à coluna `merged_pr_number`, e contá-los daria 68% de código "sem intenção" que na maior parte não o é. Quando houver histórico suficiente depois de `0012`, pode voltar a entrar.
- **Uma unidade é datada pelo commit mais recente**, porque os commits do PR são mais antigos do que o merge que os integra; sem isto um squash recente apareceria sozinho, sem as sessões dos commits originais.
- **Falta testar ao vivo** o caminho completo (um PR novo no `bella-nina` com três revisores diferentes), que é o que dá números a R3a, P4 e P5. Os testes automáticos cobrem cada passo: webhook com e sem `user`, `pr_author` só em propostas vazias, marca do revisor verdadeira, falsa e nula, `merged_pr_number` nos commits e no commit de squash, e cada figura sobre dados feitos à mão.

## 3. Passo 11.3: uso pelos agentes

Feito: migração `0013`, o marcador de arranque do hook, a cobertura calculada por sessão quando chega uma captura, e as figuras C1 e C2.

### O que passou a ser gravado

| Facto | Onde | Como |
|---|---|---|
| O hook correu nesta sessão | linha `via: "start"` em `.rastro/consulted.jsonl` | o `session-start` acrescenta-a sempre que corre num repositório com `.rastro` (`startup`, `resume`, `clear` e `compact`), mesmo sem regras nem cache |
| Estado do hook por captura | `agent_sessions.hook_active` | a captura envia `agent_hook = true` se encontra o marcador da sessão; `false` se há hook instalado nas definições (locais ou partilhadas) e o marcador não veio; omite o campo se não há hook instalado |
| Cobertura da sessão | tabela `session_coverage` (uma linha por sessão) | recalculada dentro do `POST /v1/sessions` com todas as capturas da mesma sessão |

### Como se calcula a cobertura

Por **sessão**, e não por captura, porque as consultas seguem com a primeira captura de uma sessão: o 2.º commit da mesma sessão chegaria com a lista vazia e daria 0%. Um ficheiro conta quando foi editado e já tinha uma regra ativa, com âncora ativa, **criada antes da primeira edição** (`rules.created_at`, e não o estado de hoje, para um envio atrasado não contar regras escritas entretanto). Foi consultado quando houve um anúncio (`read` ou `deny`) desse ficheiro não posterior à primeira edição; os caminhos comparam-se sem distinguir maiúsculas nem barras. O estado do hook da sessão é verdadeiro se alguma captura o provou, falso se todas as que disseram algo disseram falso, e desconhecido nos restantes casos (CLI antiga).

### As figuras

- **C1:** ficheiros com regras anunciados antes de editados, somado sobre as sessões da janela cujo estado do hook é conhecido, repartido entre sessões com e sem o hook. As sessões com estado desconhecido aparecem à parte e não contam como zero.
- **C2:** sessões em que o hook correu, sobre as que têm o estado conhecido.
- Sem edições de ficheiros com regras, ou sem nenhuma CLI que diga o estado, o relatório diz-o em vez de mostrar 0%.

### Verificação ponta a ponta no `bella-nina`

Com o Claude Code real: `rastro agent install`, uma sessão a pedir um comentário numa função com regra (`hasReachedRescheduleLimit`), commit (que dispara a captura), e `rastro health`.

- O registo do hook teve o marcador de arranque às 12:44:05 e o anúncio de `src/domain/scheduling.ts` 7 segundos depois, **antes** da edição. O modelo disse, na resposta final, que o Rastro avisou que a função implementa a regra das 2 remarcações validada por uma pessoa e que, como só a documentava, não pediu confirmação.
- Na base de dados: `agent_sessions.hook_active = true`, 2 anúncios guardados e uma linha em `session_coverage` com `edited_ruled = 1`, `consulted_first = 1` e `[{"file": "src/domain/scheduling.ts", "consulted": true}]`.
- `rastro health` mostra "Files with rules announced first 1 of 1 (100%) - small sample" e "Sessions where the hook ran 1 of 1 (100%)". Os dados desta verificação (sessão, captura e linha de cobertura) foram apagados da base de desenvolvimento depois.

### Desvios da spec

- **`session_coverage` tem uma coluna `started_at`** (primeiro excerto da sessão), que a spec não previa: é com ela que se aplica a janela, porque `computed_at` muda de cada vez que se recalcula.
- **C1 exclui as sessões com estado do hook desconhecido**, em vez de as contar como não consultadas, para a cobertura não parecer pior só porque a CLI era antiga (spec D4). A repartição com e sem hook fica visível.
- **O marcador de arranque também conta em `clear` e `compact`**, não só em `startup`: nesses casos o hook voltou a correr, e é prova igualmente válida.

### Limites

- Só entram os ficheiros do commit (a captura só guarda as edições que tocam os ficheiros do commit); uma sessão que editou ficheiros com regras mas só fez commit de outros não os conta.
- Edições por `Bash` sem `Read` contam como não anunciadas, que é a lacuna que a figura quer mostrar.
- Uma amostra de uma sessão não diz nada ainda: a cobertura real só se lê depois de várias sessões com o hook instalado.

### Poluição da base de testes

A execução dos testes da API deixou 12 repositórios `acme/clinic` na base de desenvolvimento quando o `conftest` ainda não limpava `session_coverage` nem `rule_proposals` (falha de limpeza, não do produto). Foram apagados à mão e o `conftest` passou a limpar ambas as tabelas.

## 4. Passo 11.4: a validação ultrapassada pelo código

Feito: migração `0014` (`rule_reviews.anchor_hashes`), o registo do código que a pessoa viu ao confirmar, a figura R5 e a fila de revisão a incluir estas regras.

### O que passou a ser gravado

Quando alguém **confirma ou corrige** uma regra, grava-se `{"caminho::nome qualificado": body_hash}` de cada âncora ativa da regra nesse momento. A chave é o caminho e o nome, e não o id do símbolo, porque o `resync` passa a âncora para um símbolo sucessor com outro id quando o código é renomeado ou movido. Rejeições e motivos acrescentados (`rationale_added`) não gravam nada: não são confirmações.

A spec falava de **três** caminhos de revisão. Na prática são **dois** que escrevem a linha de validação: a aplicação das propostas no merge (`reviewer_source = 'github'`) e a rota da CLI. As caixas e os comandos `/rastro` do GitHub só movem propostas entre estados; é no merge que viram `rule_reviews`, por isso o caminho do GitHub fica coberto pelo do merge.

### R5

Das regras ativas confirmadas, quantas têm hoje, em alguma âncora, um `body_hash` diferente do que a pessoa viu. Detalhes:

- Conta-se a confirmação mais recente da revisão validada: uma regra reconfirmada depois de o código mudar já não conta.
- Uma cópia duplicada que divergiu conta, e um símbolo que desapareceu não (a regra fica órfã e aparece em R1).
- As confirmações sem hashes (as anteriores a esta fase, ou sem âncoras ativas) ficam em "desconhecidas" e fora do denominador; se não houver nenhuma com hashes, o R5 diz `n/a` com a razão.
- Uma regra ultrapassada entra na fila de revisão com o motivo `validation_outdated_by_code`, mesmo tendo razão e confirmação.

### Verificação no `bella-nina` real

Num ciclo completo contra a pilha reconstruída:

1. `rastro rule 995e23 --confirm` (confirmação pela CLI): `rastro health` passou a mostrar "Confirmed by a person 4 of 27" e **"Code changed after confirming 0 of 1"**.
2. Alterei o `body_hash` de um símbolo âncora dessa regra, a simular uma edição do código: o R5 passou a **1 of 1 (100%)**.
3. Repus tudo (hashes, a linha de revisão e a validação da regra); o relatório voltou a "3 of 27" e a R5 `n/a`, porque as 3 confirmações que já existiam são anteriores aos hashes.

As 3 regras confirmadas que o `bella-nina` já tinha ficam portanto em "desconhecidas" para sempre: confirmações antigas não se retro-preenchem, porque não se sabe que código a pessoa viu.

### Decisões e limites

- **Não há preenchimento do histórico** (decisão da spec): usar o código de hoje diria que "nada mudou" a regras cujo código pode ter mudado.
- **Os hashes gravados no merge** são os que a base tem nesse momento. Estão certos porque o `analyze_commit` do commit de merge corre antes da extração (a fila FIFO por repositório garante a ordem); se esse trabalho falhasse, o hash gravado seria o do estado anterior e o R5 acusaria uma mudança que não houve.
- **Qualquer mudança no corpo de uma função âncora conta**, incluindo as que não alteram o comportamento (o `body_hash` ignora comentários, formatação e nomes locais, mas não literais nem globais referenciados). O R5 diz "ninguém voltou a olhar", não "a regra foi violada".
- **Pormenor de implementação:** a coluna é `JSONB(none_as_null=True)`; sem isso o `None` do Python fica como `null` JSON e o filtro `IS NOT NULL` não o apanha (apanhei-o num teste: contava como conhecida uma confirmação sem hashes).

## 5. Passo 11.5: instantâneos e tendência

Feito: migração `0015` (`health_snapshots`), tarefa periódica diária, comparação no relatório (`compared_to`), rota `GET /health/history` e `rastro health --history`.

### O que se grava

Uma linha por repositório e dia (`UNIQUE (repository_id, taken_on)`): `window_days`, `definitions_version`, `head_sha` e `metrics` (as figuras de cada métrica: numerador, denominador, valor, disponibilidade, amostra pequena, detalhe em contagens). **Não há texto de regras**, nem zonas, nem fila, nem nada por pessoa. Uma segunda execução no mesmo dia substitui a primeira. O instantâneo usa sempre a janela de 30 dias.

### A tarefa

`health_snapshots` corre às 03:00 e só **distribui o trabalho**: põe na fila um `health_snapshot` por repositório **ativo**, com `queueing_lock` por repositório (sem o bloqueio de repositório dos outros trabalhos, porque só lê). Se um repositório não se consegue pôr na fila, ou o relatório dele falha, os outros seguem (a falha fica no registo). Não chama o modelo.

### A comparação

`compared_to` é o instantâneo **mais antigo dentro da janela** (anterior a hoje) com a **mesma `definitions_version` e a mesma `window_days`**. Se uma fórmula mudar, ou se se pedir outra janela, não há comparação em vez de uma tendência falsa. A CLI escreve "Changes are against the snapshot of AAAA-MM-DD (N days ago)" e, ao lado de cada figura que mudou, "(was N of M)". `rastro health --history` lista os dias, do mais recente para o mais antigo, com R1-R5 (a rota devolve também P2, P3, C1 e C2) e avisa se a definição das figuras mudou dentro do período.

### Decisões e limites

- **Sem preenchimento do histórico:** os instantâneos começam no dia em que a tarefa corre pela primeira vez; não se reconstrói o passado.
- **Só o trabalhador escreve** instantâneos; o relatório e a rota de histórico não escrevem nada (há um teste).
- **A comparação é com o mais antigo da janela**, não com o de ontem: lê-se "o que mudou nos últimos 30 dias", que é a pergunta do relatório.
- Ainda não há dias suficientes de instantâneos no `bella-nina` para uma tendência com sentido; a verificação real fica para o passo 11.6.

## 6. Passo 11.6 (parte de código): formato Markdown e relatórios de referência

- `rastro health --format markdown` (com `--zones` e `--queue`) imprime o relatório em tabelas, para colar num PR ou numa wiki. Um formato desconhecido é recusado antes de perguntar ao servidor. O texto das regras vem do servidor, por isso o `|` e as quebras de linha são neutralizados dentro das células.
- Dois relatórios de referência (`packages/cli/tests/golden/health.txt` e `health.md`) apanham alterações acidentais de formato; regeneram-se de propósito com `RASTRO_UPDATE_GOLDEN=1`.
- No `bella-nina` real o relatório Markdown sai completo (27 regras ativas, 3 confirmadas, fila com 8 regras). Nota: no Windows é preciso `PYTHONIOENCODING=utf-8` para redirecionar a saída para um ficheiro sem estragar os acentos (já era assim com o formato de texto).

### Ainda por fazer (avaliação real, precisa de pessoas ou de dados novos)

- Teste de leitura: dar o relatório Markdown a alguém que não conhece o projeto e perguntar o que faria a seguir.
- Pergunta da Fase 10 (perguntar só nas regras validadas): precisa de mais regras validadas do que as 3 que existem.
- Ajuste dos limites (amostra pequena, janela, tamanho da fila) com base nessas leituras.

### Teste de ação no `bella-nina` (feito)

Confirmei pela CLI as 5 primeiras regras da fila (`995e23`, `ce19f7`, `3fa603`, `17cba1`, `f877e9`, nota "phase 11.6 action test"). Antes e depois, com `rastro health --json`:

| Figura | Antes | Depois |
|---|---|---|
| Confirmed by a person (R3) | 3 de 27 | **8 de 27** (subiu 5) |
| ...verified by GitHub (R3b) | 3 de 3 | 3 de 8 (as 5 novas são da CLI, "not verified") |
| ...by someone other than the PR author (R3a) | n/a | n/a (revisões da CLI não têm autor do PR conhecido) |
| Code changed after confirming (R5) | n/a | **0 de 5** (as 5 gravaram os hashes) |
| Queue | 5 primeiras sem motivo nem confirmação | passaram a ser outras 5 (`7887bf`, `e4cf0d`, `86bede`, ...) |

Leituras:

- **A fila não encurta quando se confirma uma regra sem motivo**: as 5 regras confirmadas continuam na fila com a razão `no_rationale`, só passam para depois das não confirmadas. É o desenho da spec (a fila pede motivo *e* confirmação), mas quem confirma 5 regras e vê o 5 a voltar à lista pode estranhar. Candidato a ajuste no passo de limites: mostrar a razão "só falta o motivo" de forma distinta, ou propor `--why` no comando da regra.
- O R3a só distingue autor e outro revisor com revisões do GitHub em que o autor do PR é conhecido; pela CLI fica `n/a`, como previsto. Continua por verificar num ciclo de PR real.
- Os dados ficam na base de desenvolvimento (são confirmações reais do utilizador sobre regras reais).
