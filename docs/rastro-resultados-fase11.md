# Resultados da Fase 11: `rastro health`

> Relatório vivo da [spec da Fase 11](spec-fase-11-rastro-health.md). Esta versão cobre os **passos 11.1 (núcleo) e 11.2 (processo e proveniência)**, feitos a 21/10/2026. As secções dos passos 11.2 a 11.6 acrescentam-se quando existirem.

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
