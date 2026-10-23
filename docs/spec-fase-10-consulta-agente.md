# Spec — Fase 10: Consulta pré-edição pelo agente (hooks e MCP)

> Rascunho de desenho. Continua o plano da secção 10 de [spec-rastro-mvp.md](spec-rastro-mvp.md) e depois da [Fase 9](spec-fase-9-validacao-humana.md) (validação humana, concluída e testada em [rastro-resultados-fase9.md](rastro-resultados-fase9.md)). Na Fase 9 esta fase aparecia como "Fase 11"; passa a 10, e as métricas de saúde (`rastro health`) passam a 11.
>
> Os comportamentos do Claude Code citados abaixo vêm da documentação oficial (https://code.claude.com/docs/en/hooks.md e https://code.claude.com/docs/en/mcp.md), lida em 19/10/2026. Os pontos marcados **[spike]** têm de ser confirmados na prática antes de construir (secção 9, passo 10.0).

## 1. Porquê

Hoje o Rastro avisa **tarde**: o comentário no PR chega depois de o agente já ter escrito o código. O agente, que é quem mais precisa de saber "esta função implementa uma regra de negócio, por esta razão", não consulta nada. `rastro rules` e `rastro rule` existem, mas são para humanos.

Esta fase passa o Rastro de **deteção** para **prevenção**: antes de o agente editar um ficheiro, recebe as regras ancoradas a esse ficheiro, o porquê de cada uma, quem as validou e onde existe lógica duplicada. Se o pedido do utilizador conflitar com uma regra, o agente tem a informação para o dizer antes de escrever, e não o revisor depois.

Isto também reduz o débito de compreensão no sentido do artigo de Osmani: o agente deixa de mexer em código cuja intenção desconhece, e o humano é informado no momento em que a decisão ainda é barata.

## 2. Critérios de sucesso

- Ao ler ou editar um ficheiro com regras, o agente recebe-as **antes** da primeira edição, sem ter de decidir chamar uma ferramenta (consulta determinística).
- O agente pode pesquisar regras por tema ("que regras existem sobre remarcações?") e abrir o detalhe de uma regra (consulta a pedido, via MCP).
- A consulta **nunca** bloqueia nem faz falhar o trabalho do agente: sem rede, sem token ou com a API em baixo, o hook sai em silêncio.
- Latência acrescentada por Read/Edit: orçamento de **150 ms no percentil 95** com cache quente (a medir no passo 10.0; ver D4).
- Avaliação real (secção 8): com a consulta ligada, o agente assinala a regra antes de editar em >= 80% dos pedidos que a violam (linha de base sem consulta medida na mesma fixture) e em < 10% dos pedidos que não lhe tocam.

## 3. Decisões de desenho

**D1. Dois mecanismos complementares, não um.**

| | Hook `PreToolUse` | Servidor MCP |
|---|---|---|
| Quem decide consultar | O Claude Code (determinístico) | O modelo (pode esquecer-se ou nem tentar) |
| Para quê | "Estas são as regras deste ficheiro" no momento certo | Pesquisa por tema, detalhe, histórico |
| Funciona noutros agentes | Não (só Claude Code) | Sim (MCP é aberto) |

Só o MCP deixaria a prevenção dependente de o modelo se lembrar de perguntar, que é exatamente o comportamento que falha sob pressão. Só o hook não permitiria pesquisa. Primeiro o hook (maior valor), depois o MCP.

**D2. Entregar as regras no `Read`, não só no `Edit`.** A documentação diz que o `additionalContext` de um `PreToolUse` é inserido junto do resultado da ferramenta, ou seja, **depois** de ela correr. Num `Edit` isso seria tarde demais para a primeira edição. Mas o `Edit` e o `Write` sobre ficheiro existente exigem um `Read` antes ("File has not been read yet"), por isso o contexto anexado ao `Read` chega ao modelo junto com o conteúdo do ficheiro e antes de qualquer alteração. **[spike]** confirmar esta ordem na versão instalada.

**D3. Rede de segurança "informar uma vez" para os casos sem `Read`.** Se um `Edit`/`Write`/`MultiEdit` chega a um ficheiro com regras que ainda não foi informado nesta sessão (contexto compactado, ou edição sem `Read` registado), o hook responde `permissionDecision: "deny"` com as regras na razão, e marca o ficheiro como informado. A repetição da edição passa. Custo: uma chamada desperdiçada, só nesses casos raros. Edições por `Bash` (`sed -i`) não são apanhadas; a métrica de cobertura (D7) mostra quanto isso pesa.

**D4. Hook local com cache, não chamada de rede por ficheiro.** O hook corre em cada `Read`/`Edit`. Uma chamada HTTP a cada um somaria latência e dependência da rede. Em vez disso, a CLI mantém uma **cache local** de todas as regras do repositório (`.rastro/cache/rules.json`, permissões 0600, fora do git como o resto de `.rastro/`), sincronizada com `ETag`. O hook só lê a cache. Alternativa rejeitada: hook do tipo `http`, que evita arrancar Python mas exige o token numa variável de ambiente e uma ida à rede por ficheiro. Risco aceite: o arranque do processo Python no Windows pode estourar o orçamento de latência; se o spike o mostrar, reduz-se o âmbito (secção 11).

**D5. A consulta é só de leitura; não há escrita do agente no Rastro.** Tentação: ferramenta MCP para o agente registar o porquê em falta. Rejeitada: o agente poderia atribuir ao utilizador palavras que ele não disse, o que quebra a regra "nunca inventar o porquê". O ciclo já fecha sozinho: se o agente perguntar "porque existe esta regra?" e o utilizador responder, essa resposta é um pedido humano da sessão e a captura existente (Fase 3) e a extração (Fase 5) já a aproveitam. Para responder sem tocar em código existe `rastro rule <id> --why`. Resultado: todas as ferramentas MCP são marcadas `readOnlyHint` e podem entrar numa lista de permissões sem risco.

**D6. As regras entram no contexto do agente como dado delimitado, não como instrução.** O texto das regras vem de conversas, do LLM e de revisores humanos, e passa a ser lido por um agente com poder de escrita: é uma superfície de injeção de prompt. Medidas: enviar só `statement`, `rationale`, estado de validação e símbolo (nunca `origin_quote`); remover caracteres de controlo e marcas que imitem tags de sistema; limitar tamanho por regra (o `statement` tem uma frase; o `rationale` é cortado a 300 caracteres); delimitar o bloco e rotulá-lo como "dados registados"; redigir em afirmações factuais, como recomenda a documentação, e não em ordens. Só há regras extraídas de código já integrado no ramo principal, e quem pode escrever nelas já pode alterar o `CLAUDE.md` do repositório; o risco é do mesmo nível, mas deixa de ser invisível.

**D7. Registar o que foi consultado, para medir cobertura.** O hook escreve localmente (`file`, `rule_ids`, `via`, hora) e a captura seguinte envia isso junto com a sessão. Permite à Fase 11 calcular "dos ficheiros com regras editados nesta sessão, quantos foram consultados antes". Sem isto não se sabe se a prevenção funciona.

**D8. Instalação opt-in e local por omissão.** Hooks num ficheiro partilhado correm comandos nas máquinas de toda a equipa. Por omissão `rastro agent install` escreve em `.claude/settings.local.json` (não vai para o git); `--shared` escreve em `.claude/settings.json` e `.mcp.json` para a equipa, com aviso. Funde com o que já existe (nunca sobrescreve hooks de outros), como o `post-commit` já faz.

**D9. Só Claude Code nesta fase, mas com o núcleo separado do agente.** Hook e captura são para Claude Code. O código do hook divide-se em duas camadas: um núcleo que não conhece nenhum agente (normalização de caminhos, cache, ordenação, formatação do bloco, estado "informado") e um adaptador fino que traduz o JSON de entrada e saída do Claude Code. Assim, Codex e Cursor serão novos adaptadores e não uma reescrita. O servidor MCP já é independente do agente.

## 4. Arquitetura

```
 Claude Code (máquina do programador)
 ├─ Read/Edit/Write ──► hook PreToolUse ──► rastro hook pre-tool ──► lê .rastro/cache/rules.json
 │                                          └─ devolve additionalContext (Read) ou deny-uma-vez (Edit)
 ├─ início de sessão ─► hook SessionStart ─► rastro hook session-start ─► sincroniza cache (ETag)
 └─ modelo ──────────► MCP stdio ──► rastro-mcp ──► API  GET /v1/repos/:id/rules[?q=]  e  /rules/:ruleId

 Servidor Rastro
 └─ API  GET /v1/repos/:id/rules/snapshot   (novo, com ETag)
```

Em tempo de sessão o servidor só serve a sincronização da cache e as ferramentas MCP; não há chamadas ao LLM novas (a pesquisa semântica já existe e respeita o orçamento de tokens).

## 5. API (novo)

`GET /v1/repos/:id/rules/snapshot` — token por repositório, como hoje.

- Devolve só as regras `active` (órfãs e rejeitadas não aconselham ninguém), em formato compacto:

```json
{
  "head_sha": "d27aa39...",                       // ramo principal a que as regras se referem
  "generated_at": "2026-10-19T16:00:00Z",
  "truncated": false,
  "rules": [{
    "id": "uuid", "statement": "...", "rationale": "..." ,
    "rationale_source": "conversation|human|null",
    "validation": "validated|unvalidated", "validated_by": "ana", "validated_at": "...",
    "revision": 2,
    "anchors": [{"file_path": "src/agenda.ts", "qualified_name": "AgendaService.validarMarcacao", "role": "primary"}]
  }]
}
```

- `ETag` = hash de (contagem, `max(updated_at)` das regras, `head_sha`); `If-None-Match` devolve `304`. Limite de 5 000 regras (`truncated: true` acima disso).
- As âncoras vão por **caminho e nome qualificado**, sem números de linha: as linhas descrevem o ramo principal e a máquina do programador pode estar noutro ramo.
- As âncoras `duplicate` entram, para o agente ser avisado também quando edita a cópia.

Não há outras rotas novas: as ferramentas MCP reutilizam `GET /rules`, `GET /rules/:id` e a pesquisa `q`.

## 6. Hook (`rastro hook`)

Subcomandos internos da CLI, chamados pelo Claude Code com o JSON do evento em stdin.

Configuração instalada:

```json
{
  "hooks": {
    "SessionStart": [{ "hooks": [{ "type": "command", "command": "rastro", "args": ["hook", "session-start"], "timeout": 5 }] }],
    "PreToolUse":   [{ "matcher": "Read|Edit|Write|MultiEdit",
                       "hooks": [{ "type": "command", "command": "rastro", "args": ["hook", "pre-tool"], "timeout": 3 }] }]
  }
}
```

**`pre-tool`** (entrada: `session_id`, `cwd`, `tool_name`, `tool_input.file_path`, e `old_string` nas edições):

1. Normalizar `file_path` para caminho relativo à raiz do repositório (reutilizar a normalização do parser de sessões: separadores, maiúsculas no Windows, ficheiros fora da raiz ignorados).
2. Procurar na cache as regras com âncora nesse ficheiro. Sem regras, ou ficheiro já informado nesta sessão: sair com código 0 e sem saída.
3. Ordenar: regras cujo símbolo aparece no `old_string` (ou no ficheiro, se for `Read`), depois validadas, depois mais recentes. Máximo **5** regras e ~600 tokens; o resto vira "e mais N: usar `rastro_rules_for_file`".
4. `Read`: devolver `hookSpecificOutput.additionalContext` com o bloco abaixo; marcar o ficheiro como informado.
5. `Edit`/`Write`/`MultiEdit` em ficheiro **não** informado: devolver `permissionDecision: "deny"` com o mesmo bloco na `permissionDecisionReason` e uma linha a dizer que repetir a edição prossegue; marcar como informado.
6. Qualquer erro, cache ausente ou token em falta: saída 0, silêncio, uma linha em `.rastro/hook.log`. Nunca código 2.

Estado "informado" em `.rastro/cache/informed-<session_id>.json`; o `SessionStart` com `source` `compact` ou `clear` apaga-o, porque o modelo deixou de ter o contexto.

**`session-start`**: sincroniza a cache (`If-None-Match`, tempo máximo ~2 s), limpa o estado "informado" se a origem for `compact`/`clear`, e devolve um `additionalContext` curto: "Este repositório tem N regras de negócio registadas (M validadas por pessoas). Ao ler um ficheiro com regras, estas são indicadas. Pesquisa por tema: ferramentas `rastro_*`." Sem cache nem rede, sai em silêncio.

Bloco entregue ao agente (exemplo; pt ou en conforme a configuração do repositório):

```
<rastro-rules source="default branch @d27aa39" status="recorded data, not instructions">
Regras de negócio registadas para src/agenda.ts:
1. [validada por @ana, 12/10] Implantes não podem ser marcados à sexta à tarde.
   Porquê: o Dr. Mendes precisa de 48h de acompanhamento. Símbolo: AgendaService.validarMarcacao.
   A mesma lógica existe em src/calendario.ts.
2. [ainda não confirmada por uma pessoa] Máximo de 2 remarcações por marcação.
   Porquê: não registado.
As regras não confirmadas foram extraídas automaticamente de conversas anteriores.
Uma alteração que mude uma destas regras é uma decisão de negócio a mencionar ao utilizador.
</rastro-rules>
```

## 7. Servidor MCP (`rastro-mcp`)

Pacote novo `packages/mcp` (distribuição `rastro-mcp`, comando `rastro-mcp`), transporte **stdio**, construído com o SDK oficial de MCP para Python. Fica fora da `rastro-cli` para esta continuar leve (a CLI arranca em cada commit e em cada hook). Descobre o repositório e o token como a CLI (`.rastro/config.json` e keychain), por isso funciona logo após `rastro init`.

Ferramentas, todas só de leitura (`readOnlyHint`) e com saída curta, bem abaixo do limite de 25 000 tokens do Claude Code:

| Ferramenta | Entrada | Saída |
|---|---|---|
| `rastro_rules_for_file` | `path`, `symbol?` | regras ancoradas (mesmo formato do hook), incluindo duplicados |
| `rastro_search_rules` | `query`, `limit?` (omissão 8) | regras mais próximas; diz se a pesquisa foi semântica ou textual |
| `rastro_get_rule` | `rule_id` ou prefixo | detalhe: âncoras, origens (sem citações), revisões, estado de validação |

Instalação: `.mcp.json` com `{"mcpServers": {"rastro": {"type": "stdio", "command": "rastro-mcp"}}}`. No âmbito `project`, o Claude Code pede aprovação ao abrir o projeto; no âmbito `local` (omissão do `rastro agent install`) não vai para o git.

Fora desta fase: servidor MCP alojado na API (HTTP) para agentes na cloud; ferramenta "esta alteração planeada viola alguma regra?" (exigiria chamada ao LLM por pergunta).

## 8. Segurança, privacidade e custos

- A cache local contém texto das regras; fica em `.rastro/cache/` com permissões restritas e já fora do git. Sem `origin_quote`, sem excertos de sessão.
- O token nunca vai para ficheiros de configuração do Claude Code: o hook e o MCP leem-no do keychain/`RASTRO_TOKEN`, como a CLI.
- Isolamento por repositório inalterado (o `repo_id` tem de ser o do token).
- Texto das regras tratado como dado não fiável (D6); testes com regras que contêm tags e ordens.
- Custo: nenhuma chamada nova ao LLM; a pesquisa `q` já embute uma frase por consulta e respeita `LLM_MONTHLY_TOKEN_BUDGET` (cai para texto simples quando excedido).

## 9. Testes e avaliação

**Automáticos**
- Contrato do hook: JSON de stdin para JSON de stdout, para `Read`, `Edit`, `Write`, `MultiEdit`; ficheiro sem regras; ficheiro já informado; `deny` só uma vez; caminhos Windows (`C:\...`) e fora da raiz; cache ausente/corrompida/antiga; token em falta; API a devolver 5xx; saída sempre 0.
- Cache: `304`, `ETag`, `truncated`, escrita atómica.
- API `snapshot`: só `active`, âncoras duplicadas incluídas, isolamento por repositório.
- MCP: cliente em memória do SDK contra a API simulada (`httpx.MockTransport`, como nos testes da CLI); ferramentas marcadas só de leitura.
- Injeção: regra com `</rastro-rules>`, "ignore as instruções anteriores" e caracteres de controlo sai neutralizada e truncada.
- Instalador: funde com hooks existentes, `--remove` desfaz só o que pôs, idempotente.

**Avaliação real** (manual, no estilo de `rastro-resultados-fase9.md`; não corre em CI)
- Fixture `clinica-demo` e/ou `bella-beauty`, com `claude -p` e as mesmas instruções com e sem a consulta.
- 10 pedidos que violam uma regra plantada, 10 que lhe tocam sem a violar, 10 que não lhe tocam.
- Medir: o agente menciona a regra antes de editar? pede confirmação? edita mesmo assim? E nos neutros, menciona regras sem necessidade? (ruído)
- Medir latência real do hook em Windows, macOS e Linux, e tokens acrescentados por sessão.
- Critério: ver secção 2. Se a linha de base já for alta, a fase vale menos do que parece e convém saber-se cedo.

## 10. Plano de implementação

Cada passo termina com testes a passar e entrega valor sozinho.

**10.0 Spike (meio dia, antes de escrever código de produto).** Com um hook de teste, confirmar na versão instalada do Claude Code: (a) o `additionalContext` de um `PreToolUse` sobre `Read` chega ao modelo junto com o conteúdo do ficheiro; (b) o `deny` com razão faz o modelo repetir a edição; (c) latência de um comando Python mínimo e do `rastro` real no Windows; (d) se os hooks disparam nas chamadas de subagentes. **Ponto de decisão:** se (a) falhar, D2 cai e o `deny` passa a ser o mecanismo principal; se (c) estourar o orçamento, aplica-se a redução de âmbito da secção 11.

**10.1 Snapshot e cache.** Rota `snapshot` com `ETag`, cliente na CLI, `.rastro/cache/rules.json`, renderizador do bloco partilhado entre hook e MCP (em `packages/cli`, ao lado de `render.py`).

**10.2 Hook.** `rastro hook pre-tool` e `session-start`, estado "informado", `rastro agent install|uninstall` (local por omissão, `--shared`), registo local das consultas.

**10.3 MCP.** Pacote `packages/mcp` com as três ferramentas, instalação em `.mcp.json`, documentação no README.

**10.4 Registo de consultas.** Campo `consulted` no corpo de `POST /v1/sessions` (coluna JSON em `agent_sessions`, migração `0011`), enviado pela captura; sem tabela nova. Base da métrica de cobertura da Fase 11.

**10.5 Avaliação real.** Executar a secção 9, escrever `docs/rastro-resultados-fase10.md` e ajustar limites (nº de regras, tamanho, redação do bloco) com base nos números.

## 11. Fora de âmbito e fases seguintes

- **Fase 11, `rastro health`**: inclui a cobertura de consulta (D7), além das métricas já esboçadas na Fase 9.
- Servidor MCP HTTP alojado; outros agentes além do Claude Code (o MCP já serve, o hook não).
- Injeção por intenção: um hook `UserPromptSubmit` que procura regras pelo texto do pedido do utilizador e as entrega antes de o agente planear. Mais poderoso, mas gasta uma consulta semântica por mensagem; avaliar depois dos números da 10.5.
- Edições por `Bash` e por subagentes: dependem do que o spike mostrar.
- Se o orçamento de latência for ultrapassado no Windows: reduzir o âmbito do hook a `Edit|Write|MultiEdit` com `deny` uma vez (um só disparo por ficheiro e sessão) ou passar o hook a um pequeno processo residente local.

## 11.1 Compatibilidade com outros agentes (apurado em 19/10/2026, não implementado)

Fontes: https://cursor.com/docs/agent/hooks e https://learn.chatgpt.com/docs/hooks. Confirmar de novo antes de começar cada um, porque mudam depressa.

- **MCP**: o mesmo `rastro-mcp` serve Claude Code, Cursor e Codex; muda só o ficheiro onde se regista (`.mcp.json`, `mcp.json` do Cursor, `config.toml` do Codex). Sem hook, a consulta depende de o modelo a pedir.
- **Cursor**: tem hooks, mas `beforeReadFile` só permite ou nega, sem injetar contexto. O contexto (`additional_context`) só se injeta em `sessionStart` e depois de uma ferramenta (`postToolUse`). A entrega no `Read` (D2) não se transpõe tal como está: avaliar `postToolUse` sobre leitura de ficheiros, ou `preToolUse` com `agent_message` (equivalente ao D3).
- **Codex**: `PreToolUse` aceita `additionalContext` e bloqueio, mas só cobre shell, `apply_patch` e ferramentas MCP; leituras de ficheiro não são cobertas (o Codex lê normalmente por shell). Restam o bloqueio uma vez no `apply_patch` e contexto em `SessionStart`/`UserPromptSubmit`. Hooks têm de ser aprovados pelo utilizador (`/hooks`) e o contexto tem um limite por omissão de ~2 500 tokens.
- **Alternativa barata sem hook**: instrução em `AGENTS.md` (ou nas regras do Cursor) a pedir `rastro_rules_for_file` antes de editar. Mais fraca, porque depende do modelo.
- **Captura**: só há parser do Claude Code. Cursor e Codex poderiam consumir regras já criadas, mas as suas sessões não geram regras até existirem novos módulos em `session_parsers` (spec do MVP, secção 13).

## 12. Questões em aberto

- O texto do bloco deve limitar-se a informar ou dizer ao agente para parar e perguntar quando houver conflito? A documentação aconselha afirmações factuais; a avaliação (secção 9) decide se isso basta.
- Regras ainda não confirmadas por uma pessoa devem ser entregues ao agente? Proposta: sim, claramente rotuladas, porque o valor de saber que existe uma regra suspeita é maior do que o risco de uma regra errada, que o rótulo mitiga. Medir se o agente as trata como verdade.
- A cache deve incluir regras de PRs abertos (as propostas da Fase 9)? Proposta: não; só ramo principal, para o agente não ser guiado por regras que ninguém aprovou.
- Equipas com vários ramos de longa duração: as regras descrevem o ramo principal e podem não valer na branch local. O bloco já indica `@sha`; basta?
