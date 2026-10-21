# Resultados da Fase 11: `rastro health`

> Relatório vivo da [spec da Fase 11](spec-fase-11-rastro-health.md). Esta versão cobre o **passo 11.1 (núcleo)**, feito a 21/10/2026. As secções dos passos 11.2 a 11.6 acrescentam-se quando existirem.

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
