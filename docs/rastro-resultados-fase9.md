# Resultados do teste real da Fase 9 (validação humana)

Teste de ponta a ponta da Fase 9 ([spec](spec-fase-9-validacao-humana.md)) contra uma GitHub App real,
executado em 19 de outubro de 2026 no repositório de demonstração `jnascimentogcc/bella-nina`. É o
primeiro teste da fase com uma App e um modelo reais: até aqui só tinha corrido contra simulações.
Este relatório avalia; não deve servir, por si só, para alterar comportamento sem nova análise.

## 1. Cenário

| | |
|---|---|
| Código testado | `develop` no commit `d6edb63` (Fases 9.1 a 9.4 mergeadas). Durante o teste entraram as correções #29 (captura de edições por shell), #30 (um teste) e #31 (versão do tree-sitter) |
| Ambiente | Rastro local em Docker (API, worker, Postgres), webhooks via smee, GitHub App de desenvolvimento |
| Modelo | o configurado no `.env` de dev (extração, impacto e embeddings de 1024 dimensões) |
| Repositório | `bella-nina` (TypeScript, gestão de salão), 25 regras de testes anteriores já na base |
| Testador | um só (dono do repositório, acesso de administrador) |
| Pedido A (PR #34) | regra **nova**: máximo de 2 remarcações por marcação. Integrado por **squash** |
| Pedido B (PR #35) | **altera** uma regra existente: pré-pagamento a partir de 2 faltas consecutivas (antes 3). **Merge commit** |
| Pedido C (PR #36) | **duas regras novas** num só commit, editadas **por shell**: máximo de 90 dias de antecedência (com razão) e máximo de 3 marcações ativas (**sem** razão). **Merge commit** |
| Pedido D (PR #37) | repetir a regra dos 90 dias, já rejeitada, mudando só o nome da constante e a mensagem de erro. Serviu só para o teste |
| Pedido E (PR #38) | repetição do passo 5 depois da correção #33: aplicar o limite dos 90 dias também à remarcação, para o modelo propor a regra reformulada |

Em cada pedido, a regra e o porquê foram escritos pelo testador no chat com o Claude Code, como exige o
Rastro (o porquê só é guardado se vier das palavras do utilizador).

## 2. O que foi validado

| Funcionalidade | Pedido | Resultado |
|---|---|---|
| Evento `issue_comment` chega ao Rastro | pré-requisito | **ok**. A API respondeu 202 e ignorou os comentários normais |
| Pré-visualização das regras no comentário da PR (antes do merge) | A, B, C | **ok**, cerca de 1 minuto depois do push |
| Nada entra em `rules` antes do merge | A, B, C | **ok** |
| Caixa de seleção confirma a proposta | A, C | **ok**. Chega como `issue_comment` `edited`, com o utilizador certo e o corpo anterior. No A, marcar, desmarcar e voltar a marcar geraram 3 jobs `apply_review` |
| `/rastro correct <id>: <texto>` | B | **ok**. Proposta `corrected`, texto substituído, porquê mantido |
| `/rastro why <id> <razão>` | C | **ok**. A regra sem razão ("origin unknown") passou a ter a razão escrita pelo testador, marcada como humana (`rationale_source = human`, `rationale_by`) |
| `/rastro reject <id>` | C | **ok**. Proposta `rejected`, riscada no comentário |
| Resposta a um id errado (`/rastro ok zzzzzz`) | C | **ok**. O bot respondeu na PR: "no proposal of this PR has an id starting with `zzzzzz`" |
| Aviso de impacto de uma regra alterada | B | **ok**: 10 regras avaliadas, 1 reportada, a certa. No C, 10 avaliadas e 0 reportadas, sem contradição |
| Aplicação no merge **sem nova extração** | A, B, C | **ok**. Em cada merge só houve embeddings; nenhuma chamada de extração ao modelo |
| Regra confirmada fica validada por quem a confirmou | A, C | **ok**, com `validated_by` o login do GitHub e entrada `confirmed` em `rule_reviews`, origem `github` |
| Razão escrita por uma pessoa, atribuída | C | **ok**. Regra `ec6a3f`: `rationale_source = human`, validada, com entradas `confirmed` e `rationale_added` |
| Regra rejeitada guardada como memória | C | **ok**. Regra `2a7851`: `status = rejected`, com embedding e 2 âncoras, fora da lista de regras ativas; entrada `rejected` em `rule_reviews` |
| Texto corrigido no merge | B | **ok**. `revision = 2`, `validated_revision = 2`; a validação antiga pela CLI (revisão 1) deixou de valer |
| Squash **e** merge commit | A / B, C | **ok**, pelos SHAs originais dos commits |
| Captura de uma sessão editada **por shell** (`sed -i`) | C | **ok** depois da correção #29 (108 excertos). Antes, falhava em silêncio (ver 4.1) |
| A regra rejeitada **não volta** a ser proposta | D, E | D foi **inconclusivo** (o modelo não propôs nada); **ok no pedido E** depois da correção #33 (ver 4.7) |

A pergunta que a spec deixava em aberto (**uma caixa marcada num comentário de um bot chega ao Rastro?**)
fica respondida: sim, com `sender` e `changes.body.from` corretos.

Estado final da base: 28 regras (27 ativas, 1 rejeitada); 5 revisões do GitHub e 1 da CLI em `rule_reviews`;
todas as propostas em `applied`.

## 3. Números

| Métrica | Valor |
|---|---|
| Push → comentário com propostas | ~1 min |
| `analyze_pr` (impacto + pré-visualização), pedido B | 22,6 s |
| Extração por commit no merge | poucos segundos, só embeddings, sem chamada de extração |
| Custo do dia (4 PRs com os respetivos merges e revisões) | extração: 4 chamadas, 32 575 tokens de entrada, 802 de saída; impacto: 40 chamadas, 118 679 / 3 508; embeddings: 13 chamadas, 1 751 tokens |

O critério de latência da spec (comentário em menos de 2 minutos) foi cumprido nesta amostra, que é
pequena: um só repositório e poucos commits por PR.

## 4. Defeitos e fragilidades encontrados

### 4.1. A captura ignorava edições feitas por comandos de shell (grave, corrigido)

O Claude Code alterou os ficheiros do pedido B com `sed -i` através da ferramenta Bash. O parser só
reconhecia edições das ferramentas `Edit`, `Write` e `MultiEdit`, por isso a sessão existia e estava na
janela de tempo, mas "não tinha nenhuma edição" aos ficheiros do commit. A captura **saía em silêncio**:
nada enviado, nenhum ficheiro em `pending`, nenhuma linha no `capture.log`. Aconteceu duas vezes seguidas.

**Corrigido na PR #29**: comandos Bash que escrevem nos ficheiros do commit (`sed -i`, `tee`,
redirecionamentos, `mv`, `cp`, `git apply`, `python`/`node` a escrever) passam a contar, o texto do comando
não é guardado, e o `capture.log` diz agora porque não capturou nada quando a sessão existe mas não rende.
**Confirmado num caso real** no pedido C (edições por shell, 108 excertos capturados). É uma heurística
conservadora: caminhos calculados, globs e variáveis continuam por detetar.

### 4.2. A janela da captura pode excluir a conversa (corrigido em #34)

A janela de uma sessão começa na data do committer do commit **anterior**. Num fluxo com squash merge, o
commit anterior é o squash criado pelo GitHub, mais recente do que a conversa que o programador teve
enquanto a PR estava aberta. No pedido B, com a conversa feita antes das 12:00:34 (hora do squash do A),
a janela ficou vazia. Sugestão: começar a janela no último commit do próprio programador, ou no último já
capturado.

### 4.3. O hook esconde as falhas (parcialmente tratado)

`rastro capture >/dev/null 2>&1 || true` é correto para nunca estragar um commit, mas esconde falhas. A #29
acrescenta uma linha no `capture.log` quando a sessão existe e não rende nada. Continua por tratar o caso em
que o comando `rastro` não está no `PATH` do ambiente que fez o commit (nada é registado).

### 4.4. A CLI instalada por cópia fica desatualizada; a editável segue a branch da pasta

A CLI do testador (instalada com `uv tool install` a partir de uma cópia) ficou na versão anterior à 9.1,
sem `--confirm`, `--reject`, `--correct` e `--why`. A instalação **editável** resolve, mas a CLI passa a
ler o código da pasta do Rastro **na branch que lá estiver**: durante o teste a pasta ficou numa branch
anterior ao merge da #29 e a captura voltou a falhar até a pasta voltar ao `develop`.

### 4.5. `tree-sitter 0.26.0` crashava o worker (grave, corrigido; o PROD está exposto até ao deploy)

Na PR #36 o worker **morreu em silêncio** a meio do `analyze_pr` (sem traceback, reiniciado pelo Docker,
2 reinícios) e deixou o job em `doing`. Como os jobs de um repositório são serializados, esse job órfão
**bloqueou todos os outros do repositório**. Reproduzido fora do Docker: `analyze_source` sobre um ficheiro
de 10 KB (`src/api/server.ts`) terminava com violação de acesso (`Segmentation fault`) em 7 de 8 execuções;
o mesmo ficheiro com o nosso código inalterado corre 6 de 6 vezes sem problema com `tree-sitter` 0.25.2,
0.24.0 e 0.23.2. Uma primeira hipótese (a árvore ou o `Parser` serem libertados cedo) foi testada e
refutada; a causa raiz dentro da biblioteca não foi encontrada.

**Corrigido na PR #31**: versão limitada a `<0.26` (o `uv.lock` passou de 0.26.0 para 0.25.2) e um teste que
falha se alguém voltar a subir sem verificar. Com a correção, 10 de 10 execuções limpas. **O PROD foi
construído com um `uv.lock` que tinha 0.26.0**, por isso tem o mesmo defeito: o deploy desta correção deve
preceder qualquer uso do PROD em repositórios reais.

### 4.6. Um worker que morre deixa o repositório bloqueado (corrigido em #35)

Independentemente da causa, um job que fica em `doing` porque o worker morreu **não é recuperado**: bloqueia
o repositório até alguém o marcar como falhado à mão (feito aqui com um `UPDATE` na tabela da fila). Falta
recuperação de jobs órfãos (por exemplo, o mecanismo de heartbeat do Procrastinate).

### 4.7. A supressão de regras rejeitadas falhava nos dois sentidos (corrigido em #33, confirmado)

O pedido D (mudar só o nome da constante e a mensagem de erro da regra rejeitada) **não gerou nenhuma
proposta**: o modelo tratou-o como refactor (uma chamada de extração com 42 tokens de saída e nenhuma chamada
de embedding depois, o que mostra que o código de supressão nem chegou a correr). O resultado é o certo,
mas **não prova a supressão**, que só está testada com a frase exatamente igual.

Por isso medi, com o modelo de embeddings real, a similaridade entre a regra rejeitada ("90 dias") e
variantes. O Rastro suprime a partir de 0,92:

| Variante | Similaridade | Hoje | Devia |
|---|---|---|---|
| a frase rejeitada, igual | 1,000 | suprimida | suprimida |
| a mesma regra, dita pelo utilizador | 0,904 | passa | suprimida |
| a mesma regra, mais curta | 0,779 | passa | suprimida |
| a mesma regra, para remarcações | 0,772 | passa | suprimida |
| **outro limite (60 dias)** | **0,960** | **suprimida** | **passar** (é outra regra) |
| outra regra (3 marcações ativas) | 0,570 | passa | passar |

Falha em dois sentidos: a regra rejeitada **voltaria** quando o modelo a reformula (o caso normal), e quem
rejeitou "90 dias" **nunca veria proposto** "60 dias". Um só limiar de embedding mede o estilo da frase e
quase ignora números. A correção (#33) baixa o limiar para 0,72 e exige que os números coincidam, mantendo
a condição de ser nos mesmos símbolos. Com ela, das variantes acima só "a mesma regra" (todas) é suprimida;
"60 dias" (0,960), "3 marcações ativas" (0,571), "cancelar com 90 dias" (0,680, margem de só 0,04),
"pontos de fidelidade 90 dias" (0,468) e "24 horas" (0,826) passam. Limite declarado: números escritos
por extenso ("noventa dias", 0,966) não são comparados e não são suprimidos.

**Confirmação com o modelo real (pedido E, PR #38).** Como a regra já estava no código, pediu-se que o
limite valesse também para a remarcação. O modelo propôs a regra reformulada ("Ao remarcar, a nova data não
pode estar a mais de 90 dias a partir de hoje, ...") e o worker descartou-a, com o registo "repeats
rejected rule 2a785168...: dropped". A PR ficou sem proposta e sem comentário. Isto fecha o passo 5: a
regra rejeitada não volta quando é reformulada, com os mesmos números e símbolos em comum.

### 4.8. Qualidade da extração (observações, não defeitos de código)

- **Pedido B:** a proposta trouxe mais do que foi pedido. Uma função auxiliar que apareceu no código
  (`noShowsUntilPrePayment`) foi promovida a regra de negócio. A correção foi feita no GitHub com
  `/rastro correct`, que é o uso previsto, mas mostra a tendência do modelo para tratar detalhe de
  implementação como regra.
- **Pedidos A e C:** os textos das regras saem longos e com detalhe técnico (por exemplo, "contando apenas as
  remarcações efetuadas com sucesso e recusando novas tentativas após atingir esse limite com uma mensagem
  clara de erro"), mais próximos de um comentário de código do que de uma regra de negócio.
- O campo "Why" repete o início das palavras do utilizador ("Isto porque cada remarcação..."): fiel, mas
  pouco limpo.
- O cenário de mudança de uma constante (P5 do relatório anterior) avisou corretamente no pedido B; é uma
  amostra de uma.

### 4.9. Comportamentos a conhecer

- A validação feita pela CLI **depois** da pré-visualização não atualiza o comentário da PR, por isso o
  aviso "Changes a rule validated by…" só aparece se a validação for anterior à pré-visualização.
- Um revisor só é identificado como autor da sessão quando o login coincide exatamente com `commits.author`;
  caso contrário fica desconhecido (por desenho, para não adivinhar).
- O Claude Code do repositório de teste chegou a afirmar que um commit "ainda não tinha sido enviado" quando
  já estava mergeado (estava numa branch antiga, sem `git fetch`): convém não seguir as sugestões de
  reescrever commits sem confirmar no GitHub, porque um SHA novo perde a sessão capturada.

## 5. Por testar

1. ~~A regra rejeitada não voltar com um modelo real~~: feito (pedido E, 4.7).
2. O aviso "Changes a rule validated by…" na PR (validar a regra antes da pré-visualização).
3. Que **quem não tem acesso de escrita** é ignorado: só testado com testes automáticos, porque o testador
   é administrador.
4. Mais de 5 propostas na mesma PR (bloco recolhível), vários commits que alteram o mesmo símbolo (a última
   proposta substitui as anteriores), force-push de commits, comentário apagado, ids ambíguos.
5. Comentários em português (`IMPACT_COMMENT_LANGUAGE=pt`): o teste correu com os textos em inglês.
6. Repositório público e produção (Hetzner): nada disto foi exercitado. O PROD ainda corre código anterior;
   o deploy aplica as migrações 0008 a 0010, traz a correção do tree-sitter, e a App `rastro-prod` precisa de
   subscrever o evento *Issue comment*.
7. Mais de uma pessoa a rever a mesma proposta, e carga real (PRs com muitos commits e custo por PR).
8. Recuperação de um worker que morre a meio (4.6).

## 6. Conclusão

O fluxo completo da Fase 9 funciona contra uma App e um modelo reais: a pré-visualização aparece antes do
merge, a caixa e os comandos de revisão (`correct`, `why`, `reject`, caixa) chegam com a identidade correta, e
no merge o que o revisor viu é exatamente o que fica, sem nova chamada de extração, com a validação
atribuída ao utilizador do GitHub e a razão humana marcada como tal. Os dois caminhos de merge (squash e
merge commit) funcionaram.

O teste encontrou, porém, cinco defeitos, dois deles graves e já corrigidos: a captura que ignorava edições
por shell (#29) e o crash do `tree-sitter 0.26.0` (#31), ambos fora do desenho da Fase 9. A janela da captura (4.2, #34), a recuperação de jobs órfãos (4.6, #35) e a supressão de regras
rejeitadas (4.7, #33) foram corrigidas depois do teste; a última foi confirmada com o modelo real. Recomenda-se, por esta ordem:

1. fazer o **deploy em produção só depois** da #31 (o PROD tem o crash do tree-sitter) e das migrações 0008
   a 0010, com o evento *Issue comment* subscrito na `rastro-prod`;
2. ~~corrigir a supressão de regras rejeitadas (4.7) e repetir o passo 5~~: feito (#33 e pedido E);
3. recuperar jobs órfãos (4.6) e tornar a janela da captura mais robusta (4.2);
4. fechar os testes por fazer da secção 5;
5. rever o prompt de extração para não promover detalhe de implementação a regra (4.8).
