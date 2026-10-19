# Resultados do teste real da Fase 9 (validação humana)

Teste de ponta a ponta da Fase 9 ([spec](spec-fase-9-validacao-humana.md)) contra uma GitHub App real,
executado em 19 de outubro de 2026 no repositório de demonstração `jnascimentogcc/bella-nina`. É o
primeiro teste da fase com uma App e um modelo reais: até aqui só tinha corrido contra simulações.
Este relatório avalia; não deve servir, por si só, para alterar comportamento sem nova análise.

## 1. Cenário

| | |
|---|---|
| Código testado | `develop` no commit `d6edb63` (Fases 9.1 a 9.4 mergeadas) |
| Ambiente | Rastro local em Docker (API, worker, Postgres), webhooks via smee, GitHub App de desenvolvimento |
| Modelo | o configurado no `.env` de dev (extração, impacto e embeddings de 1024 dimensões) |
| Repositório | `bella-nina` (TypeScript, gestão de salão), 25 regras de testes anteriores já na base |
| Testador | um só (dono do repositório, acesso de administrador) |
| Pedido A (PR #34) | regra **nova**: máximo de 2 remarcações por marcação. Integrado por **squash** |
| Pedido B (PR #35) | **altera** uma regra existente: pré-pagamento a partir de 2 faltas consecutivas (antes 3). Integrado por **merge commit** |

Em cada pedido, a regra e o porquê foram escritos pelo testador no chat com o Claude Code, como exige o
Rastro (o porquê só é guardado se vier das palavras do utilizador).

## 2. O que foi validado

| Funcionalidade | Pedido | Resultado |
|---|---|---|
| Evento `issue_comment` chega ao Rastro | pré-requisito | **ok**. A App já o enviava; a API respondeu 202 e ignorou os comentários normais |
| Pré-visualização das regras no comentário da PR (antes do merge) | A e B | **ok**, cerca de 1 minuto depois do push (`analyze_pr` com 22,6 s no B) |
| Nada entra em `rules` antes do merge | A e B | **ok** (25 regras antes, 25 até ao merge) |
| Caixa de seleção confirma a proposta | A | **ok**. Chega como `issue_comment` `edited`, com o utilizador certo e o corpo anterior. Marcar, desmarcar e voltar a marcar geraram 3 jobs `apply_review`, todos com sucesso |
| `/rastro correct <id>: <texto>` | B | **ok**. Proposta `corrected`, texto substituído, porquê mantido |
| Aviso de impacto da regra alterada (cenário de mudança de constante) | B | **ok**: 10 regras avaliadas, 1 reportada, a certa (`9dbfa3`) |
| Aplicação no merge **sem nova extração** | A e B | **ok**. No merge só houve embeddings; nenhuma chamada de extração ao modelo |
| Regra nova validada por quem a confirmou | A | **ok**. Regra `65a7dc`: `validated_revision = revision = 1`, `validated_by = jnascimentogcc`, entrada em `rule_reviews` com origem `github` |
| Regra alterada validada na revisão nova | B | **ok**. Regra `9dbfa3`: texto corrigido, `revision = 2`, `validated_revision = 2`; entrada `corrected` com o texto novo |
| Validação antiga anulada por uma alteração | B | **ok**. A confirmação pela CLI (revisão 1) deixou de valer quando o texto mudou, como a spec previa |
| Squash **e** merge commit | A e B | **ok**, pelos SHAs originais dos commits |

A pergunta que a spec deixava em aberto (**uma caixa marcada num comentário de um bot chega ao Rastro?**)
fica respondida: sim, com `sender` e `changes.body.from` corretos.

## 3. Números

| Métrica | Valor |
|---|---|
| Push → comentário com propostas | ~1 min |
| `analyze_pr` (impacto + pré-visualização) | 22,6 s |
| Extração por commit no merge | 3,4 s (só embeddings, sem chamada de extração) |
| Custo das 3 h do teste | extração: 2 chamadas, 14 279 tokens de entrada, 496 de saída; impacto: 20 chamadas, 55 710 / 1 875; embeddings: 8 chamadas, 852 tokens |
| Erros no worker | nenhum |

Os critérios de sucesso de latência (comentário em menos de 2 minutos) foram cumpridos nesta amostra. É
uma amostra pequena, com um só repositório e poucos commits por PR.

## 4. Defeitos e fragilidades encontrados

### 4.1. A captura ignora edições feitas por comandos de shell (grave)

O Claude Code alterou os ficheiros do pedido B com `sed -i` através da ferramenta Bash. O parser só
reconhece edições das ferramentas `Edit`, `Write` e `MultiEdit`, por isso a sessão existia e estava na
janela de tempo, mas "não tinha nenhuma edição" aos ficheiros do commit. A captura **saiu em silêncio**: nada
enviado, nenhum ficheiro em `pending`, nenhuma linha no `capture.log`. Aconteceu duas vezes seguidas. O
pedido A foi capturado sem problemas (não analisei como o Claude Code editou nesse caso). A solução
alternativa foi pedir explicitamente a ferramenta de edição de ficheiros no pedido B, e a terceira
tentativa foi capturada (106 excertos). Qualquer fluxo em que o agente edite por shell perde a conversa.

Correção proposta (por iniciar): reconhecer, de forma conservadora, comandos Bash que escrevem em ficheiros
do commit, e registar no `capture.log` quando uma sessão existe mas não rende nada.

### 4.2. A janela da captura pode excluir a conversa

A janela de uma sessão começa na data do committer do commit **anterior**. Num fluxo com squash merge, o
commit anterior é o squash criado pelo GitHub, mais recente do que a conversa que o programador teve
enquanto a PR estava aberta. Com a conversa do pedido B feita antes das 12:00:34 (hora do squash do A),
a janela (12:00:34 a 12:07:34) ficou vazia. Sugestão: começar a janela no último commit do próprio
programador, ou no último já capturado.

### 4.3. O hook esconde as falhas

`rastro capture >/dev/null 2>&1 || true` é correto para nunca estragar um commit, mas não deixa rasto de
que nada foi capturado. Cada diagnóstico exigiu ler o código e correr scripts à mão. Falta uma linha de
registo por commit (capturado, sem sessão, sem atividade, erro) e, idealmente, um aviso quando o comando
`rastro` não está no `PATH` do ambiente que fez o commit.

### 4.4. A CLI instalada por cópia fica desatualizada

A CLI do testador (instalada com `uv tool install` a partir de uma cópia) ficou na versão anterior à 9.1,
sem `--confirm`, `--reject`, `--correct` e `--why`. Resolve-se com instalação editável:
`uv tool install --editable ./packages/cli --with-editable ./packages/session_parsers --reinstall`.

### 4.5. Qualidade da extração (observações, não defeitos de código)

- **Pedido B:** a proposta trouxe mais do que foi pedido. Uma função auxiliar que apareceu no código
  (`noShowsUntilPrePayment`) foi promovida a regra de negócio ("...e cada marcação ou registo de falta deve
  indicar quantas faltas consecutivas faltam..."). A correção foi feita no GitHub com `/rastro correct`,
  que é o uso previsto, mas mostra a tendência do modelo para tratar detalhe de implementação como regra.
- **Pedido A:** o texto da regra ficou longo e com detalhe técnico ("contando apenas as remarcações
  efetuadas com sucesso e recusando novas tentativas após atingir esse limite com uma mensagem clara de
  erro").
- O campo "Why" repete o início das palavras do utilizador ("Isto porque cada remarcação..."): fiel, mas
  pouco limpo.

### 4.6. Comportamentos a conhecer

- A validação feita pela CLI **depois** da pré-visualização não atualiza o comentário da PR, por isso o
  aviso "Changes a rule validated by…" só aparece se a validação for anterior à pré-visualização.
- Um revisor que se valida a si próprio como autor do commit só é identificado quando o login coincide
  exatamente com `commits.author`; caso contrário fica desconhecido (por desenho, para não adivinhar).

## 5. Por testar

1. `/rastro reject <id>` e, depois, que a mesma regra **não volta** a ser proposta (supressão por regra
   rejeitada).
2. `/rastro why <id> <porquê>` (porquê escrito por uma pessoa e marcado como tal).
3. A resposta curta a um id errado (`/rastro ok zzzzzz`), a ids ambíguos e a comandos mal escritos.
4. O aviso "Changes a rule validated by…" na PR (validar a regra antes da pré-visualização).
5. Que **quem não tem acesso de escrita** é ignorado: só testado com testes automáticos, porque o testador
   é administrador.
6. Mais de 5 propostas na mesma PR (bloco recolhível), vários commits que alteram o mesmo símbolo
   (a última proposta substitui as anteriores), force-push de commits, comentário apagado.
7. Comentários em português (`IMPACT_COMMENT_LANGUAGE=pt`): o teste correu com os textos em inglês.
8. Repositório público e produção (Hetzner): nada disto foi exercitado. O PROD ainda corre código anterior;
   o deploy aplica as migrações 0008 a 0010, e a App `rastro-prod` precisa de subscrever o evento
   *Issue comment*.
9. Mais de uma pessoa a rever a mesma proposta, e carga real (PRs com muitos commits e custo por PR).

## 6. Conclusão

O fluxo completo da Fase 9 funciona contra uma App e um modelo reais: a pré-visualização aparece antes do
merge, a caixa e os comandos de revisão chegam com a identidade correta, e no merge o que o revisor viu é
exatamente o que fica, sem nova chamada de extração, com a validação atribuída ao utilizador do GitHub.
Os dois caminhos de merge testados (squash e merge commit) funcionaram.

O principal risco encontrado **não está na Fase 9, mas na captura**: se a conversa não é ligada ao commit,
não há propostas, e hoje isso falha em silêncio. Recomenda-se, por esta ordem:

1. corrigir a captura de edições por shell e tornar visível quando nada é capturado (4.1 a 4.3);
2. fechar os testes por fazer da secção 5 (rejeição, `why`, erros de comando, aviso de regra validada);
3. só então o deploy em produção (migrações 0008 a 0010 e subscrição do evento na `rastro-prod`);
4. rever o prompt de extração para não promover detalhe de implementação a regra (4.5).
