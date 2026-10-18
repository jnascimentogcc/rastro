# Resultados do teste do Rastro (Salão Demo)

Registo dos cenários de PR (P1 a P7) do `salao-demo-roteiro.md`, executados em 18 Oct 2026. Servidor do Rastro em
`localhost:8000`, GitHub App de desenvolvimento no repositório. Os números das PRs são deste repositório.

## Como correu (contexto que afeta os resultados)

1. **Token rejeitado no início.** Os uploads das sessões falharam com HTTP 401 desde o primeiro commit; o Rastro só
   recebeu sessões depois de o token ser renovado (`rastro init`) e das capturas pendentes serem reenviadas
   (`rastro flush`). O primeiro P1 (#15) foi feito nesse estado e não conta.
2. **As razões foram propostas pelo assistente, não ditas pelo utilizador.** Nas sessões S1 a S8 as regras e os porquês
   vieram do roteiro, propostos no chat pelo Claude Code; o utilizador respondeu "use os valores do roteiro". O Rastro
   guardou essas regras com `Why: unknown origin`. Só passaram a ter porquê as regras cujo prompt (regra + razão) foi
   colado pelo utilizador numa sessão que alterou comportamento.
3. **Sessões de fixação do porquê** (PRs #16, #17, #18, #19, #24, #26, #29): repetiram S1, S5, S8, S2 e S6 com a razão
   escrita pelo utilizador. As alterações de só comentário (#17) e de só refatoração (#18) não geraram regra; as que
   mudaram comportamento (#19 `discountCapped`, #24, #26, #29) sim.

## Resultados por cenário

| Cenário | PR | Esperado | Resultado |
|---|---|---|---|
| P1 (1.ª) | #15 | aviso | sem comentário; Rastro sem token e sem regras, não conta |
| P1 (2.ª) | #31 | aviso | **aviso correto**: regra do cancelamento, porquê fiel, risco exato, sem erros |
| P2 | #21 | sem aviso | sem comentário, **correto** |
| P3 | #20 | aviso | **aviso correto, parcial**: só a regra do `discountCapped`; não citou o teto original nem as comissões (S6) e os pontos (S7) que dependem dele |
| P4 (1.ª) | #28 | aviso de duplicado | sem comentário, **falso negativo**; nessa altura o Rastro não tinha regra de comissões |
| P4 (2.ª) | #30 | aviso de duplicado | sem comentário, **falso negativo**, mesmo com a regra de comissões (S6) registada |
| P5 (1.ª) | #23 | aviso | sem comentário, **falso negativo**; regra ancorada só em `server.ts` e sem porquê |
| P5 (2.ª) | #25 | aviso | **aviso correto**, com porquê; uma frase errada sobre "lógica duplicada que não mudou" |
| P6 | #22 | sem aviso | sem comentário, **correto**; a regra seguiu o ficheiro para `src/domain/billing/pricing.ts` |
| P7 | #27 | aviso | **aviso correto** mas sem o porquê sanitário; 4 regras fragmentadas; frase errada sobre duplicação |

Contagem: 4 avisos em 11 PRs de cenário, todos relevantes (nenhum comentário irrelevante). Falsos negativos: P4 (2x) e
P5 (1.ª). Falsos positivos em PRs sem aviso esperado: 0 (P2, P6).

## Critérios do roteiro

- **Menos de um comentário irrelevante em cada cinco PRs:** cumprido. Nenhum comentário foi irrelevante, embora dois
  (P5 2.ª e P7) tenham trazido uma afirmação errada (ver abaixo).
- **Pelo menos 80% das regras corretas:** não medido. Das 25 regras extraídas, só têm porquê as cujo prompt o
  utilizador escreveu; as restantes têm `unknown origin`. Falta rever a lista completa (`rastro rules --status all -n 100`)
  contra o código.
- **Tempo do push ao comentário e custo (`llm_calls`):** não registados nesta sessão. A preencher à mão.

## O que se observou

1. **Só há porquê se o utilizador o disser no chat**, e só aparece como regra quando o commit muda comportamento.
   Refatorações e comentários não geraram regra (hipótese, consistente com #17 e #18).
2. **O aviso depende de a PR mexer no código onde a regra está ancorada.** O P5 falhou na 1.ª tentativa porque a regra do
   pré-pagamento estava em `server.ts`, e a PR mudou a constante em `cancellation.ts`. Os P3, P5 (2.ª), P7 e P1 (2.ª)
   avisaram porque as PRs alteravam o código das regras.
3. **A deteção de duplicados falhou.** No P4 o `reports.ts` novo copia o cálculo de `commission.ts` sobre o valor antes
   do desconto, contra a regra S6 (depois do desconto). Em duas tentativas, sem comentário. Hipótese: o aviso não dispara
   quando a cópia está num ficheiro novo e a PR não toca no ficheiro onde a regra está ancorada. Por confirmar nos logs
   do servidor.
4. **Afirma duplicados que não existem.** No P5 (2.ª) e no P7 diz que "a mesma lógica existe em `server.ts` /
   `scheduling.ts` e não foi alterada", mas `server.ts` só chama as funções do domínio e `scheduling.ts` foi alterado
   pela PR.
5. **Regras fragmentadas e duplicadas.** A mesma regra do intervalo de 10 minutos aparece em várias formulações, e no
   P7 o comentário citou as cópias sem porquê e deixou de fora a que tinha a razão sanitária.
6. **A regra segue o código quando o ficheiro muda de pasta** (P6): reancorada em `billing/pricing.ts` sem perder o
   porquê.
7. **As regras extraídas ficam com o valor do momento** (por exemplo, o teto de 30%) e só se atualizam num commit
   posterior; um aviso pode citar um valor já alterado.

## Pontos a melhorar no Rastro

- Ancorar a regra no ficheiro onde a constante ou a função vive, e não só onde é usada.
- Fundir formulações duplicadas da mesma regra e preferir, nos avisos, a que tem o porquê.
- Rever a afirmação "a mesma lógica existe noutro ficheiro e não foi alterada": verificar se o ficheiro foi alterado ou
  se só chama a função.
- Verificar a deteção de código copiado para ficheiros novos (P4).
- Registar tempo push→comentário e custo por PR, para fechar os critérios do roteiro.

## PRs de teste

As PRs dos cenários que contradizem regras (#20, #23, #25, #27, #28, #30, #31) não devem ser integradas: o `develop`
mantém as regras originais (24 h, 30%, 3 faltas, 10 minutos, comissão depois do desconto). A #15 (P1 original) foi
integrada e revertida pela #16. As refatorações (#21 P2, #22 P6) não mudam comportamento.
