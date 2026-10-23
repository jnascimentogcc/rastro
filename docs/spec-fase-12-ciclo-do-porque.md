# Spec — Fase 12: Ciclo do porquê em falta

> Rascunho de desenho. Continua o plano depois da [Fase 9](spec-fase-9-validacao-humana.md) (validação humana), da [Fase 10](spec-fase-10-consulta-agente.md) (consulta pelo agente) e da [Fase 11](rastro-resultados-fase11.md) (`rastro health`). Escrito a partir do estado do repositório em 21/10/2026 (migrações até `0015`). Revisto no mesmo dia após uma análise do rascunho e **cortado ao núcleo**: a fase implementa só os passos 12.1, 12.1b e 12.2. O seguimento depois do merge, a recolha da conversa e as indicações ao agente ficam na secção 11, com a condição para serem retomados.

## 1. Porquê

A regra de ouro do Rastro é **nunca inventar o porquê**: se a conversa não o explica, `rationale` fica `null`. É a decisão certa, mas tem um custo que o `rastro health` tornou visível: no `bella-beauty` só **7 de 27 regras (26%) têm porquê**, e a fila de revisão abre com regras de datas, intervalos e horários, todas sem porquê. Um agente que consulta essas regras lê "Porquê: não registado". Um revisor que as confirma confirma uma frase sem razão.

Hoje existem canais soltos para suprir a falta: `/rastro why <id> <texto>` no PR e `rastro rule <id> --why` na CLI. Falta o essencial: **um só caminho de escrita**, um **estado explícito** para cada regra sem porquê (incluindo "ninguém sabe"), a **pergunta feita no PR**, onde as propostas já aparecem, e a **medida** de se isso resultou. Este é o núcleo; o resto do ciclo ideal (seguimento, recolha da conversa, agente) só vale a pena com mais utilizadores (secção 11).

## 2. Critérios de sucesso

- Toda a regra sem porquê tem um **estado explícito** visível no `rastro health`: por perguntar, perguntada e à espera, respondida, ou "ninguém sabe" confirmado. Nada fica "em falta" sem se saber porquê.
- Responder custa **uma linha**, no PR ou na CLI.
- **Nunca se insiste**: um pedido por proposta (o da própria pergunta no PR), registado uma só vez mesmo que o comentário seja reescrito (teste automático).
- Um porquê só entra com palavras de uma pessoa. Nenhum caminho de escrita aceita texto composto por um modelo (teste automático).
- No `bella-beauty`, com o dono a responder: a percentagem de regras com porquê **ou** "ninguém sabe" confirmado sobe de forma mensurável, e o relatório mostra de que canal veio cada resposta.
- Nenhuma chamada ao LLM é acrescentada pela fase (teste automático).

## 3. Decisões de desenho

**D1. Perguntar onde já se está.** O PR é o ponto onde as propostas aparecem e onde o autor ainda tem o contexto na cabeça. Esta fase pergunta aí e em mais lado nenhum.

**D2. Só palavras de uma pessoa.** Nenhum canal escreve um porquê que um modelo tenha composto. As origens permitidas nesta fase são duas: comentário no PR de quem tem permissão de escrita, e a CLI do utilizador.

**D3. "Ninguém sabe" é uma resposta legítima.** Às vezes a decisão veio de um cliente, de um colega que saiu ou de um palpite. Registar isso (`/rastro why <id> unknown`, `--why-unknown`) pára a pergunta e distingue um **desconhecido conhecido** de um desconhecido por perguntar. O `health` mostra os dois separados. Se alguém só escrever "unknown" para calar o sistema, fica visível na contagem, nunca escondido. O histórico continua só de acréscimos: "ninguém sabe" grava uma revisão `why_unknown` em `rule_reviews` (quem e quando), e `why_unknown_at/by` na regra são só o estado derivado dela. **Um porquê dado depois ganha**: limpa `why_unknown_at` (a revisão antiga fica no histórico).

**D4. Uma só função de escrita.** Todos os canais passam por `record_rationale(regra, texto ou unknown, quem, canal)`: valida a resposta (D5), sobe `revision`, grava a revisão `rationale_added` (ou `why_unknown`), regista a resposta no pedido, se houver, e devolve o resultado. Evita caminhos com regras ligeiramente diferentes.

**D5. Validação mínima da resposta.** Aparar espaços; pelo menos 12 caracteres; sem ser igual à própria regra depois de normalizar; "n/a", "tbd", "porque sim", "não sei" e equivalentes (português e inglês) são reencaminhados para `unknown` ou recusados com uma frase curta. A lista é uma constante pequena no código e **não é exaustiva**: apanha o óbvio, não o resto. O mínimo de 12 caracteres é provisório: antes de o fixar, medir quantas respostas reais curtas e legítimas ("lei 4/2020") seriam recusadas; uma recusa diz sempre que `unknown` é uma resposta válida. Não há juízo de qualidade (nenhum modelo avalia o que uma pessoa escreveu). Limite de 500 caracteres, como hoje.

**D6. Escrever o porquê não confirma sozinho.** Mantém-se o desenho da Fase 11: acrescentar um motivo sobe a revisão e pede nova confirmação. Para reduzir o atrito, `rastro rule <id> --why "..." --confirm` faz as duas coisas, e `/rastro why` no PR continua separado de `/rastro ok`.

**D7. Quem pergunta é texto fixo, não modelo.** A pergunta é um modelo de texto. Esta fase não usa LLM.

**Quando um pedido conta como feito.** Um pedido regista-se **uma vez**, quando a pergunta fica visível pela primeira vez num comentário publicado com sucesso (a chamada ao GitHub devolveu), nunca ao reconstruir ou editar o comentário: reeditar o mesmo comentário não cria pedidos novos. O pedido refere-se a uma **proposta** (a regra ainda não existe antes do merge); ao aplicar a proposta no merge, o pedido passa a apontar para a regra criada. A chave de idempotência é (regra ou proposta, canal, PR). Regras `rejected`, `orphaned` e `superseded` nunca são perguntadas.

## 4. Fluxos

### Fluxo I, no PR

A secção de propostas deixa de dizer só "origem desconhecida" e passa a perguntar:

```
- [ ] **Máximo de 3 marcações ativas por cliente**  <!-- rastro:proposal:b21c... -->
  Porquê: ainda não sei. @autor, sabe porquê?
  `/rastro why b21c <porquê>` · `/rastro why b21c unknown`
```

Mais uma linha no topo da secção quando faltam porquês ("3 das 5 regras propostas não têm razão"). Cada pergunta publicada fica registada como pedido **uma vez** (canal `pr_proposal`, regra acima), referido à proposta até ao merge. Mencionar o autor só usa o login do GitHub que o próprio PR traz (`rule_proposals.pr_author`); sem ele, a pergunta aparece sem menção.

**Ids nos comandos.** Hoje `/rastro why`, `ok`, `reject` e `correct` resolvem o prefixo (4+ caracteres) contra **propostas**. Passam a resolver contra propostas e regras: um prefixo que pertença a uma proposta já aplicada resolve para a regra que ela criou; um prefixo que corresponda a mais de um objeto (duas regras, ou uma proposta e uma regra diferentes) é recusado com uma resposta curta que lista os candidatos, nunca adivinhado. O resto do comportamento dos comandos não muda. Isto também permite responder depois do merge, sobre o id da regra, no mesmo PR.

### Fluxo K, receber a resposta

Todos os canais chamam `record_rationale` (D4):

| Canal | Como |
|---|---|
| Comentário no PR | `/rastro why <id> <texto>` ou `... unknown` (só com permissão de escrita, como hoje) |
| CLI | `rastro rule <id> --why "<texto>" [--confirm]` ou `--why-unknown` |

## 5. Dados novos (migração `0016`)

```sql
CREATE TABLE why_requests (                  -- registo de cada pergunta e da sua resposta
  id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  repository_id  uuid NOT NULL REFERENCES repositories(id),
  rule_id        uuid REFERENCES rules(id) ON DELETE CASCADE,   -- NULL até a proposta ser aplicada
  proposal_id    uuid REFERENCES rule_proposals(id) ON DELETE SET NULL,
  channel        text NOT NULL,              -- pr_proposal (nesta fase)
  pr_number      int,
  asked_at       timestamptz NOT NULL DEFAULT now(),
  answered_at    timestamptz,
  answer_kind    text,                       -- reason | unknown | rejected
  answer_channel text                        -- pr | cli
);
CREATE INDEX ON why_requests (repository_id, rule_id);
-- um pedido por (regra ou proposta, canal, PR): reconstruir o comentário não cria outro
CREATE UNIQUE INDEX ON why_requests (repository_id, COALESCE(rule_id, proposal_id), channel, COALESCE(pr_number, 0));

ALTER TABLE rules
  ADD COLUMN why_unknown_at timestamptz,     -- estado derivado da revisão `why_unknown`
  ADD COLUMN why_unknown_by text;
-- rule_reviews ganha o veredicto `why_unknown`. rules.rationale_source mantém os valores atuais.
```

A confirmação de que `rule_proposals.id` é uuid faz-se ao escrever a migração (assumido aqui, não verificado).

O estado de cada regra deriva destes campos e do registo:

| Estado | Condição |
|---|---|
| com porquê | `rationale IS NOT NULL` |
| ninguém sabe | `rationale IS NULL` e `why_unknown_at IS NOT NULL` |
| perguntada, à espera | um pedido, sem resposta |
| por perguntar | sem pedidos |

O estado "desistida" (dois pedidos sem resposta) só existe com o seguimento da secção 11. O texto das regras aparece nos comentários como já aparece nas propostas: num repositório privado só o vê quem já pode ler o PR.

## 6. Interface

CLI:

```
rastro rule <id> --why "<texto>" [--confirm]
rastro rule <id> --why-unknown
rastro rules --without-why [--asked | --unasked | --unknown]
```

`rastro health` acrescenta as figuras abaixo **sem mudar `DEFINITIONS_VERSION`**: nenhuma fórmula existente (R1 a R6, P, C, V) muda, só se juntam figuras novas, por isso a comparação com o histórico dos instantâneos continua. Se alguma fórmula antiga mudar, aí sobe a versão, como na Fase 11. O R2 passa a separar a origem do porquê (`human`, `model`), para ficar pronto a separar outras origens se vierem a existir (secção 11):

- **W1** regras sem porquê por estado (por perguntar, à espera, "ninguém sabe")
- **W2** tempo mediano até à resposta
- **W3** respostas por canal (PR, CLI)
- **W4** pedidos por semana (prova de que não há spam)

A fila de revisão passa a pôr os "ninguém sabe" depois das regras ainda perguntáveis.

## 7. Segurança e privacidade

- O texto da resposta humana é dado, nunca instrução (como na Fase 9). Limitado a 500 caracteres e escapado ao reapresentar.
- A pergunta herda o comportamento das propostas nos repositórios públicos (mostrada, sem interruptor, decisão do dono do projeto).
- Mencionar o autor de um PR só usa o login do GitHub que o próprio PR traz.
- Nenhum texto de conversa passa a ser enviado ao servidor: esta fase só trata de texto que uma pessoa escreve de propósito.

## 8. Testes e avaliação

**Automáticos**
- `record_rationale`: validação (curto, igual à regra, "n/a" e equivalentes), `unknown` (grava a revisão `why_unknown`), um porquê posterior a um `unknown` ganha e limpa o estado, invalidação da confirmação, `--confirm`, idempotência, um só caminho para todos os canais.
- Pedidos: reconstruir ou editar o comentário várias vezes cria um só pedido; o pedido de uma proposta passa para a regra ao aplicar no merge; regras `rejected`/`orphaned`/`superseded` nunca são perguntadas; regra com "ninguém sabe" nunca é perguntada.
- Comandos: por id de proposta, por id de regra, por prefixo de proposta já aplicada, e prefixo ambíguo (recusado com lista).
- `health`: W1 a W4, separação das origens do R2, fila com "ninguém sabe" depois das perguntáveis; isolamento por repositório; nenhuma chamada ao LLM em toda a fase.

**Avaliação real**
- No `bella-beauty`, com o dono a responder a pedidos reais (os do PR e `rastro rules --without-why`): ler W1 a W4. Em particular, quantas das 20 regras sem porquê passam a ter razão ou "ninguém sabe", e quantas respostas teriam sido recusadas pelo mínimo de D5.
- Escrever `docs/rastro-resultados-fase12.md` com o resultado e com a decisão sobre retomar ou não a secção 11.

## 9. Plano de implementação

Cada passo termina com testes a passar e entrega valor sozinho.

**12.1 Escrita única.** `record_rationale`, validação (D5), `unknown` com a revisão `why_unknown`, migração `0016` (`why_requests`, colunas novas), CLI (`--why`, `--why-unknown`, `--confirm`), rota da API com os veredictos novos, filtro `--without-why`. Já fecha o buraco de ter vários caminhos.

**12.1b Medida inicial.** Logo a seguir à 12.1: W1 (estados), W3 (canais) e a separação das origens no R2 no `rastro health`, para que o passo seguinte já tenha onde se medir.

**12.2 PR.** Pergunta explícita nas propostas, `unknown`, registo dos pedidos (uma vez por pergunta publicada), comandos por id de proposta ou de regra; acrescenta W2 e W4. Termina com a avaliação real no `bella-beauty` e `docs/rastro-resultados-fase12.md`.

Ordem: 12.1 primeiro porque dá a base única a todos os canais; 12.1b logo depois para haver medida cedo; 12.2 por fim, porque é o que muda o que as pessoas veem.

## 10. Fora de âmbito

- **Sugestões a partir do que já está escrito:** descrição do PR, mensagem de commit, issues e tickets. São palavras humanas e muitas vezes explicam o porquê. Seriam **oferecidas** à pessoa e nunca aplicadas sozinhas. Exige decidir que fontes se leem e como se cita.
- Perguntar por e-mail ou Slack.
- Encaminhar a pergunta ao dono do código (CODEOWNERS ou `git blame`) quando o autor já não está. Commits diretos no ramo principal ficam só na fila do `health`: `commits.author` é um nome, não um login do GitHub (como a Fase 9 já notou).
- Avaliar a qualidade do porquê escrito (hoje só se valida o mínimo, D5). Uma marca de "motivo fraco" feita por quem revê só se justifica se o `health` mostrar que o R2 está a ser manipulado.
- Interface web para responder em lote.

## 11. Adiado, com a condição para retomar

O desenho completo existia no rascunho; foi cortado por falta de evidência (um repositório, um utilizador, sem equipa a responder). Fica aqui o que era e quando faz sentido voltar.

**Condição geral:** retomar quando houver pelo menos uma segunda pessoa a usar o Rastro num repositório e os números de `docs/rastro-resultados-fase12.md` mostrarem que as perguntas no PR são respondidas. Se o dono nem responde às perguntas do PR, mais comentários não vão ajudar.

### 12.3 Seguimento depois do merge
Um trabalho periódico (com `queueing_lock`) procura regras ativas criadas sem porquê, com o primeiro pedido feito há 3 dias ou mais, sem resposta e com menos de 2 pedidos. Junta-as num único comentário por PR de origem (`<!-- rastro:why-followup -->`, atualizado no lugar), menciona o autor e lista até 3 regras pela ordem da fila. O PR de origem é o que **criou** a regra (`commits.merged_pr_number`); regra sem PR não tem seguimento. Após o 2.º pedido sem resposta a regra fica "desistida" (novo estado do W1). Limites: 2 pedidos por regra, 1 comentário por PR, 3 regras por comentário, `WHY_FOLLOWUPS_PER_DAY` = 10, desligável por repositório (`repositories.why_followups boolean default true`). O pedido do seguimento é o 2.º e último; o canal passa a incluir `pr_followup`. Valores todos por afinar com o primeiro repositório com mais de um utilizador. Testes: limites, comentário atualizado no lugar, comentário apagado volta a ser publicado, PR sem regras em falta passa a "tudo respondido", duas execuções não duplicam.
**Retomar quando:** houver uma equipa e pedidos do PR sem resposta que justifiquem um lembrete.

### 12.4 Recolha da conversa
Ao receber uma sessão que anunciou regras **sem porquê** (o registo `consulted`), um modo novo de extração, só de recolha, dá ao modelo essas regras e as mensagens humanas **posteriores** ao anúncio. A resposta tem de citar o id da regra e uma frase **literal** de uma mensagem humana; passa a verificação de palavras distintivas com confiança mínima de 0,7. O resultado é um **candidato** (`why_candidates`: `rule_id`, `session_ref`, `quote`, `confidence`, `state shadow|offered|accepted|rejected`, único por regra, sessão e citação), **nunca** um porquê escrito na regra. Duas fases: **sombra** (nada é mostrado, mede-se precisão e alcance, só se avança com precisão >= 95% medida também em candidatos reais) e **oferecida** (mostrado no PR com `/rastro why <id> --from-conversation` e na CLI com `--accept-conversation`; só quando uma pessoa aceita é que `record_rationale` escreve com `rationale_source = 'conversation'`, com a pessoa que aceitou como autora; recusado não volta a ser oferecido). O R2 passaria a separar `conversation`. Modelo indisponível, sem orçamento ou com falha transitória segue as regras da extração normal. Avaliação: sessões do `bella-beauty` e da fixture `clinica-demo` com o porquê dado depois do anúncio, com o porquê dado para **outra** coisa, e sem porquê, etiquetadas à mão.
**Retomar quando:** houver sessões suficientes com motivos dados depois do anúncio (medir antes, à mão, quantas há no `bella-beauty`) e o custo do modelo for aceitável.

### 12.5 O agente
Duas indicações do próprio Rastro, fora da etiqueta de dados: **ao tocar** numa regra sem porquê, e só se o pedido a muda ou contradiz, pedir também a razão numa frase; **ao criar** comportamento de negócio (limite, preço, prazo, permissão) sem que o pedido diga porquê, perguntar o porquê numa frase antes de implementar. A segunda vai na nota do `SessionStart` e nas instruções do MCP (o hook só dispara em ficheiros que já têm regras). O agente pode reencaminhar a resposta com `rastro rule <id> --why "<palavras do utilizador>" --relayed` pelo `Bash`, com o comando visível para aprovação (sem ferramenta MCP de escrita, que a Fase 10 rejeitou); a origem fica `human_relayed`, separada de `human` no `health`. **Critério fixado antes de medir:** o ruído nos pedidos sem relação não pode subir mais de 3 pontos percentuais sobre a linha de base da Fase 10; se subir, "ao criar" fica opcional (`rastro agent install --ask-why`) e só "ao tocar" é o padrão. Avaliação: a bateria da Fase 10 mais pedidos que **criam** regras novas; medir se pergunta quando o pedido não diz o porquê, se deixa de perguntar quando diz, e se confirma `human_relayed` no relatório.
**Retomar quando:** for aceitável gastar tempo numa avaliação real do agente. É o passo com mais ganho por esforço desta lista (só texto), mas o ruído tem de ser medido antes de ficar.

### Outras figuras do `health`
**W5** (das regras criadas na janela, quantas nasceram com porquê e quantas ganharam depois) e **W3** com mais canais (reencaminhada, conversa) voltam com os passos acima.

## 12. Questões em aberto

- **Valores dos limites** (12 caracteres de D5): palpites. Ajustar com respostas reais do `bella-beauty`.
- **Confirmar ao escrever o porquê:** `--confirm` é um atalho opcional. Alguém que escreve a razão e a vê como "por confirmar" pode estranhar; ponderar tornar `--confirm` o comportamento por omissão na CLI (mas não no PR).
- **Um "porquê" fraco** ("o cliente pediu") sobe a percentagem sem acrescentar conhecimento. Com uma só origem humana a separação do R2 não ajuda ainda; reavaliar com `human_relayed` e `conversation` se a secção 11 for retomada.
