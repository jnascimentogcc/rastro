# Spec — Fase 12: Ciclo do porquê em falta

> Rascunho de desenho. Continua o plano depois da [Fase 9](spec-fase-9-validacao-humana.md) (validação humana), da [Fase 10](spec-fase-10-consulta-agente.md) (consulta pelo agente) e da [Fase 11](rastro-resultados-fase11.md) (`rastro health`). Escrito a partir do estado do repositório em 21/10/2026 (migrações até `0015`).

## 1. Porquê

A regra de ouro do Rastro é **nunca inventar o porquê**: se a conversa não o explica, `rationale` fica `null`. É a decisão certa, mas tem um custo que o `rastro health` tornou visível: no `bella-nina` só **7 de 27 regras (26%) têm porquê**, e a fila de revisão abre com regras de datas, intervalos e horários, todas sem porquê. Um agente que consulta essas regras lê "Porquê: não registado". Um revisor que as confirma confirma uma frase sem razão.

Hoje existem canais soltos para suprir a falta: `/rastro why <id> <texto>` no PR, `rastro rule <id> --why` na CLI, a pergunta que o agente faz ao utilizador (Fase 10, secção 11). Falta o **ciclo**: detetar a falta, perguntar no momento barato, à pessoa certa, com um custo de resposta mínimo, registar sem inventar, deixar de insistir e medir se resultou. E há um buraco concreto: a Fase 10 mediu que o agente pergunta, mas **não mediu se a resposta do utilizador chega ao Rastro** (secção 11.5). Hoje só chega se um commit tocar nos símbolos da regra e a extração ligar a resposta a ela.

## 2. Critérios de sucesso

- Toda a regra sem porquê tem um **estado explícito** visível no `rastro health`: por perguntar, perguntada e à espera, respondida, "ninguém sabe" confirmado, ou desistida. Nada fica "em falta" sem se saber porquê.
- Responder custa **uma linha**, em qualquer canal (comentário no PR, CLI, conversa com o agente).
- **Nunca se insiste demais**: no máximo 2 pedidos por regra, 1 comentário de seguimento por PR, 3 regras por comentário, e um limite diário por repositório (teste automático de cada limite).
- Um porquê só entra com palavras de uma pessoa. A recolha a partir da conversa nunca aceita uma citação que não esteja literalmente numa mensagem humana posterior à consulta (teste automático).
- Recolha da conversa: **precisão >= 95%** num conjunto de sessões etiquetado à mão (a recolha mede-se pela precisão; o alcance mede-se mas não bloqueia).
- No `bella-nina`, com o dono a responder aos pedidos: a percentagem de regras com porquê **ou** "ninguém sabe" confirmado sobe de forma mensurável, e o relatório mostra de que canal veio cada resposta.
- O agente passa a perguntar o porquê quando o pedido **cria** comportamento de negócio sem razão, **sem** subir o ruído nos pedidos sem relação medido na Fase 10 (secção 11.3).

## 3. Decisões de desenho

**D1. Perguntar no momento mais barato, por esta ordem.** (1) Quando o código nasce, na conversa com o agente: a pessoa tem o contexto na cabeça. (2) No PR, com as propostas a pedir o porquê. (3) Depois do merge, uma única vez e com ajuda de um lembrete. Cada passo custa mais do que o anterior, por isso o desenho investe sobretudo no primeiro.

**D2. Só palavras de uma pessoa.** Nenhum canal escreve um porquê que um modelo tenha composto. Há quatro origens permitidas: comentário no PR de quem tem permissão de escrita, CLI do utilizador, resposta do utilizador na conversa (recolhida com as proteções da secção 5, passo 12.4) e texto do utilizador reencaminhado pelo agente **com o comando visível para aprovação** (D3).

**D3. O agente pode reencaminhar, mas à vista do utilizador.** A Fase 10 rejeitou uma ferramenta MCP de escrita porque o agente podia atribuir ao utilizador palavras que ele não disse. Aqui evita-se isso sem ferramenta nova: o agente corre `rastro rule <id> --why "<palavras do utilizador>" --relayed` pelo `Bash`, e o utilizador vê e aprova esse comando antes de correr. O Rastro marca a origem como `human_relayed`, separada de `human`, para a diferença ficar à vista no `health`. Nada impede tecnicamente um utilizador de aprovar um comando sem ler; fica documentado, não imposto.

**D4. "Ninguém sabe" é uma resposta legítima.** Às vezes a decisão veio de um cliente, de um colega que saiu ou de um palpite. Registar isso (`/rastro why <id> unknown`, `--why-unknown`) pára os pedidos e distingue um **desconhecido conhecido** de um desconhecido por perguntar. O `health` mostra os dois separados. Se alguém só escrever "unknown" para calar o sistema, fica visível na contagem, nunca escondido.

**D5. Limites contra a fadiga, por desenho.** Cada regra é perguntada no máximo 2 vezes (a primeira e um lembrete 3 dias depois, valores a afinar). Um só comentário de seguimento por PR, atualizado no lugar, com no máximo 3 regras ordenadas pela fila do `health`. Limite diário por repositório. Desligável por repositório. Sem notificações por outro meio (e-mail, Slack): só o que o GitHub já faz ao mencionar o autor.

**D6. Uma só função de escrita.** Todos os canais passam por `record_rationale(regra, texto ou unknown, quem, canal)`: valida a resposta (D7), sobe `revision`, grava a revisão `rationale_added`, regista a resposta no pedido e devolve o resultado. Evita cinco caminhos com regras ligeiramente diferentes.

**D7. Validação mínima da resposta.** Aparar espaços; pelo menos 12 caracteres; sem ser igual à própria regra depois de normalizar; "n/a", "tbd", "porque sim", "não sei" e equivalentes são reencaminhados para `unknown` ou recusados com uma frase curta. Não há juízo de qualidade (nenhum modelo avalia o que uma pessoa escreveu). Limite de 500 caracteres, como hoje.

**D8. Quem pergunta é texto fixo, não modelo.** Os pedidos são modelos de texto. O único uso de LLM da fase é a recolha da conversa (12.4), com proteções.

**D9. Escrever o porquê não confirma sozinho.** Mantém-se o desenho da Fase 11: acrescentar um motivo sobe a revisão e pede nova confirmação. Para reduzir o atrito, `rastro rule <id> --why "..." --confirm` faz as duas coisas, e `/rastro why` no PR continua separado de `/rastro ok`.

## 4. Fluxos

### Fluxo H, ao nascer (conversa com o agente)

O bloco de regras já diz "Porquê: não registado". Passa a haver duas indicações do próprio Rastro, fora da etiqueta de dados (como na Fase 10):

- **Ao tocar** numa regra sem porquê, e só se o pedido a muda ou contradiz (a pergunta de confirmação que o agente já faz): pedir também a razão, numa frase.
- **Ao criar** comportamento de negócio (um limite, preço, prazo, permissão) sem que o pedido diga porquê: perguntar o porquê numa frase antes de implementar. É o ponto mais barato do ciclo, porque a razão ainda está na cabeça de quem pede, e a resposta fica na conversa que a captura já envia.

Esta segunda indicação vai na nota do `SessionStart` e nas instruções do servidor MCP (o hook só dispara em ficheiros que já têm regras, por isso não cobre a criação). O risco conhecido é o ruído (a Fase 10 viu 40% de menções desnecessárias com um texto mais insistente): a indicação tem de ser medida contra os pedidos sem relação antes de ficar.

### Fluxo I, no PR

A secção de propostas deixa de dizer só "origem desconhecida" e passa a perguntar:

```
- [ ] **Máximo de 3 marcações ativas por cliente**  <!-- rastro:proposal:b21c... -->
  Porquê: ainda não sei. @autor, sabe porquê?
  `/rastro why b21c <porquê>` · `/rastro why b21c unknown`
```

Mais uma linha no topo da secção quando faltam porquês ("3 das 5 regras propostas não têm razão"). Cada pergunta mostrada fica registada como pedido (canal `pr_proposal`).

### Fluxo J, seguimento depois do merge (uma vez)

Um trabalho periódico procura regras criadas sem porquê, com o primeiro pedido feito há 3 dias ou mais, sem resposta e com menos de 2 pedidos. Para cada PR de origem junta-os num **único** comentário (marcador `<!-- rastro:why-followup -->`, atualizado no lugar), menciona o autor do PR e lista até 3 regras pela ordem da fila. Os comandos funcionam sobre o **id da regra**, não só da proposta (hoje só atuam sobre propostas). Depois do segundo pedido a regra passa a `desistida`: deixa de ser perguntada e aparece assim no `health`.

Quando o PR não existe (commit direto no ramo principal), não há onde comentar: a regra fica só na fila do `rastro health` e no bloco que o agente recebe.

### Fluxo K, receber a resposta

Todos os canais chamam `record_rationale` (D6):

| Canal | Como |
|---|---|
| Comentário no PR ou no seguimento | `/rastro why <id> <texto>` ou `... unknown` (só com permissão de escrita, como hoje) |
| CLI | `rastro rule <id> --why "<texto>" [--confirm]` ou `--why-unknown` |
| Agente | `--relayed` com o comando à vista (D3) |
| Conversa | recolha na captura (secção 5, 12.4) |

### Fluxo L, recolha a partir da conversa

O registo `consulted` de cada sessão já diz que regras o agente anunciou, e quando. Ao receber uma sessão que anunciou regras **sem porquê**, o servidor corre um modo novo de extração, **só de recolha**: dá ao modelo as regras anunciadas sem porquê e as mensagens humanas **posteriores** ao anúncio, e pergunta se o utilizador explica o porquê de alguma. É o padrão "o modelo propõe, o código decide":

- a resposta tem de citar o id da regra e uma frase que apareça **literalmente** numa mensagem humana (origem `human`) posterior ao anúncio;
- passa a mesma verificação de palavras distintivas da extração normal, com confiança mínima mais alta (0,7 em vez de 0,5);
- entra com `rationale_source = 'conversation'`; sobe a revisão e invalida a confirmação, como qualquer alteração de porquê;
- um modelo indisponível, sem orçamento ou com falha transitória segue as regras da extração normal (`skipped_quota`, `skipped_budget`, retentativa).

O risco principal é atribuir à regra errada o motivo que o utilizador deu para outra coisa: o conjunto de avaliação da secção 8 mede exatamente isso.

## 5. Dados novos (migração `0016`)

```sql
CREATE TABLE why_requests (                  -- registo de cada pergunta e da sua resposta
  id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  repository_id  uuid NOT NULL REFERENCES repositories(id),
  rule_id        uuid NOT NULL REFERENCES rules(id) ON DELETE CASCADE,
  channel        text NOT NULL,              -- pr_proposal | pr_followup
  pr_number      int,
  asked_at       timestamptz NOT NULL DEFAULT now(),
  answered_at    timestamptz,
  answer_kind    text,                       -- reason | unknown | rejected | harvested
  answer_channel text                        -- pr | cli | relayed | conversation
);
CREATE INDEX ON why_requests (repository_id, rule_id);

ALTER TABLE rules
  ADD COLUMN why_unknown_at timestamptz,     -- alguém confirmou que ninguém sabe
  ADD COLUMN why_unknown_by text;
-- rules.rationale_source passa a aceitar também 'human_relayed'

ALTER TABLE repositories
  ADD COLUMN why_followups boolean NOT NULL DEFAULT true;
```

O estado de cada regra deriva destes campos e do registo:

| Estado | Condição |
|---|---|
| com porquê | `rationale IS NOT NULL` |
| ninguém sabe | `rationale IS NULL` e `why_unknown_at IS NOT NULL` |
| perguntada, à espera | pedidos > 0, sem resposta, menos de 2 |
| desistida | 2 pedidos sem resposta |
| por perguntar | sem pedidos |

Os limites de insistência leem o registo, por isso sobrevivem a reinícios e a reprocessamentos.

## 6. Interface

CLI:

```
rastro rule <id> --why "<texto>" [--confirm] [--relayed]
rastro rule <id> --why-unknown
rastro rules --without-why [--asked | --unasked | --unknown | --gave-up]
```

`rastro health` (definições versão 2, a comparação com o histórico recomeça, como na Fase 11):

- **W1** regras sem porquê por estado (por perguntar, à espera, desistidas, "ninguém sabe")
- **W2** tempo mediano até à resposta
- **W3** respostas por canal (PR, CLI, reencaminhada pelo agente, recolhida da conversa)
- **W4** pedidos por semana (prova de que não há spam)
- **W5** das regras criadas na janela, quantas nasceram com porquê e quantas ganharam depois

A fila de revisão passa a pôr os "ninguém sabe" depois das regras ainda perguntáveis, e as desistidas no fim.

## 7. Segurança e privacidade

- Os comentários de seguimento mostram o texto da regra, como as propostas. Herdam a mesma regra dos repositórios públicos (pré-visualização desligada por omissão): sem ela, também não há seguimento.
- `--relayed` e a recolha não enviam texto de conversa a mais do que o que a extração já envia. A recolha usa só mensagens humanas posteriores ao anúncio, com o mesmo corte de tamanho e redação de segredos.
- O texto da resposta humana é dado, nunca instrução (como na Fase 9). Limitado a 500 caracteres e escapado ao reapresentar.
- Mencionar o autor de um PR só usa o login do GitHub que o próprio PR traz.

## 8. Testes e avaliação

**Automáticos**
- `record_rationale`: validação (curto, igual à regra, "n/a" e equivalentes), `unknown`, invalidação da confirmação, `--confirm`, idempotência, um só caminho para todos os canais.
- Limites: no máximo 2 pedidos por regra, 1 comentário de seguimento por PR, 3 regras por comentário, limite diário, repositório com seguimentos desligados, regra com "ninguém sabe" nunca perguntada.
- Seguimento: atualizado no lugar, comentário apagado volta a ser publicado, PR já sem regras em falta passa a "tudo respondido", comandos por id da regra com prefixo ambíguo.
- Recolha: aceita só citação literal humana posterior ao anúncio; recusa citação inventada, citação de antes do anúncio, regra que não foi anunciada, confiança baixa; modelo em falta não escreve nada; modelo falso (`FakeProvider`) com respostas gravadas.
- Nenhuma chamada ao LLM fora da recolha.
- Isolamento por repositório em tudo.

**Avaliação real** (estilo das fases 10 e 11)
- **Recolha:** sessões do `bella-nina` e da fixture `clinica-demo` com o porquê dado depois do anúncio, com o porquê dado para **outra** coisa, e sem porquê. Etiquetadas à mão. Precisão >= 95% (secção 2). Medir alcance.
- **Perguntar ao nascer:** a mesma bateria da Fase 10 (pedidos que violam, que tocam e sem relação) mais pedidos que **criam** regras novas. Medir: pergunta o porquê quando o pedido não o diz? Deixa de perguntar quando o diz? O ruído nos pedidos sem relação mantém-se perto de zero?
- **Reencaminhamento:** o agente propõe o comando, o utilizador aprova, a razão fica na regra. Confirmar `human_relayed` no relatório.
- **Ciclo completo no `bella-nina`:** correr PR, seguimento e respostas reais e ler W1 a W5. Em particular, quantas das 20 regras sem porquê passam a ter razão ou "ninguém sabe".
- Resposta a uma questão que a Fase 11 deixou aberta: com mais regras com porquê, volta a valer a pena a variante "perguntar só nas regras validadas"?

## 9. Plano de implementação

Cada passo termina com testes a passar e entrega valor sozinho.

**12.1 Escrita única.** `record_rationale`, validação (D7), `unknown`, migração `0016`, CLI (`--why`, `--why-unknown`, `--confirm`, `--relayed`), rota da API com o veredicto novo, filtro `--without-why`. Já fecha o buraco de ter cinco caminhos.

**12.2 PR.** Pergunta explícita nas propostas, `unknown`, registo dos pedidos, comandos por id de regra.

**12.3 Seguimento.** Trabalho periódico, limites, comentário no lugar, desligar por repositório.

**12.4 Recolha da conversa.** Modo de extração só de recolha, proteções, conjunto de avaliação etiquetado.

**12.5 O agente.** Indicações "ao criar" e "ao tocar", reencaminhamento com `--relayed`, avaliação com a bateria da Fase 10.

**12.6 Medida e avaliação real.** W1 a W5 em `rastro health` (definições v2), ciclo completo no `bella-nina`, `docs/rastro-resultados-fase12.md`.

Ordem: 12.1 primeiro porque dá a base única a todos os canais. 12.4 e 12.5 são os que mais prometem e os mais arriscados (precisão, ruído), por isso vão depois de haver onde medir (12.6 pode começar cedo, só com W1 a W4).

## 10. Fora de âmbito e fases seguintes

- **Sugestões a partir do que já está escrito:** descrição do PR, mensagem de commit, issues e tickets. São palavras humanas e muitas vezes explicam o porquê. Seriam **oferecidas** à pessoa ("a descrição do PR diz: «...». Usar como razão? `/rastro why <id> --from-pr`") e nunca aplicadas sozinhas. Fica para depois: exige decidir que fontes se leem e como se cita.
- Perguntar por e-mail ou Slack.
- Encaminhar a pergunta ao dono do código (CODEOWNERS ou `git blame`) quando o autor já não está.
- Avaliar a qualidade do porquê escrito (hoje só se valida o mínimo, D7).
- Interface web para responder em lote.

## 11. Questões em aberto

- **Quem perguntar quando não há PR.** O autor do PR é claro. Para commits diretos no ramo principal, o Rastro só tem `commits.author`: confirmar o que guarda e se dá para o ligar a um login do GitHub. Se não, ficam só na fila do `health`.
- **Valores dos limites** (2 pedidos, 3 dias, 3 regras, limite diário, 12 caracteres): palpites. Ajustar com o primeiro repositório com mais de um utilizador.
- **Um "porquê" fraco** ("o cliente pediu") sobe a percentagem sem acrescentar conhecimento. Como a medida de R2 pode ser manipulada, aceitar que o `health` separe `human`, `human_relayed` e `conversation`, ou pedir uma marca de "motivo fraco" feita por quem revê?
- **"Ao criar" no agente:** a indicação mais útil é também a mais arriscada para o ruído. Se a avaliação mostrar mais do que alguns pontos percentuais de ruído, ficar só com "ao tocar" e deixar "ao criar" opcional (`rastro agent install --ask-why`).
- **Confirmar ao escrever o porquê:** `--confirm` é um atalho opcional. Alguém que escreve a razão e a vê como "por confirmar" pode estranhar; ponderar tornar `--confirm` o comportamento por omissão na CLI (mas não no PR).
