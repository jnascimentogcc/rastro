# Resultados da Fase 10: consulta pré-edição pelo agente

> Relatório vivo da [spec da Fase 10](spec-fase-10-consulta-agente.md). Esta versão cobre só o **passo 10.0 (spike)**, feito em 19/10/2026 com o Claude Code 2.1.299 (a versão embutida na app desktop) no Windows 11. As secções dos passos 10.1 a 10.5 acrescentam-se quando existirem.

## 1. Resumo

| Ponto da spec | Resultado |
|---|---|
| (a) O `additionalContext` de um `PreToolUse` sobre `Read` chega ao modelo **antes** da edição | **Confirmado.** O modelo viu o bloco ao ler o ficheiro e referiu-o depois como "uma nota que apareceu quando li o ficheiro". D2 mantém-se |
| (b) O `deny` com razão faz o modelo repetir a edição | **Confirmado.** O modelo recebeu a razão (apresentada como `PreToolUse:Edit hook error: ...`), mencionou-a e repetiu a edição, que passou. D3 mantém-se |
| (c) Latência | Um hook só com a biblioteca padrão custa **~105–130 ms** (p50) com 100 a 5 000 regras. O `rastro` instalado hoje custa **~1,2 s** só a arrancar. Ver 2 |
| (d) Subagentes | O hook **dispara** nas chamadas dos subagentes e o subagente recebe o contexto. O evento traz `agent_id`. Ver 3 |
| Edições por `Bash` (`sed -i`) | **Não apanhadas**, como a spec previa (o matcher não inclui `Bash`) |
| Forma `command` + `args` da configuração | **Funciona** (testada com o caminho do Python e o script em `args`) |

Ainda sem teste: `SessionStart` com `compact`/`clear` (o apagar do estado "informado"), macOS e Linux, outras máquinas Windows.

## 2. Latência (ponto c)

Medido numa máquina Windows 11 Enterprise, 25 execuções por linha, processo lançado a partir do Python (o PowerShell 5.1 antepõe um BOM ao stdin e estraga o JSON, por isso não serve para esta medição).

| Comando | p50 | p95 |
|---|---|---|
| `python -I -S -c pass` (Python do venv) | 64 ms | 71 ms |
| `python -c pass` (Python do venv) | 102 ms | 128 ms |
| `python -c pass` (Python da Microsoft Store, o que `python` resolve aqui) | 209 ms | 260 ms |
| Hook só com a biblioteca padrão, 100 regras (38 KB) | 110 ms | 204 ms |
| Hook só com a biblioteca padrão, 1 000 regras (390 KB) | 108 ms | 115 ms |
| Hook só com a biblioteca padrão, 5 000 regras (2 MB) | 130 ms | 156 ms |
| `rastro --help` (instalado, via `uv tool`) | 1 222 ms | 1 527 ms |
| `import typer` / `import keyring` / `import httpx` (isolados) | 269 / 417 / 485 ms | |

Conclusões:

1. **O tamanho da cache não conta**: de 100 a 5 000 regras a diferença é de ~20 ms. O custo é o arranque do interpretador.
2. **Não se pode usar o `rastro` atual como comando do hook.** Cada `Read` pagaria ~1,2 s, quase todo em imports (`typer`, `keyring`, `httpx`). O hook precisa de uma entrada própria que só importe a biblioteca padrão e leia a cache; o token só é necessário no `session-start`, que não está no caminho quente.
3. **O orçamento de 150 ms (p95) fica no limite.** Já só o interpretador custa 100 a 130 ms nesta máquina, e o p95 varia entre 115 e 204 ms conforme a corrida. Duas opções: aceitar ~250 ms, ou o processo residente que a secção 11 da spec prevê. Convém também que o instalador aponte para o Python do ambiente da CLI e não para o `python` do PATH (o da Microsoft Store custa o dobro).
4. Uma única máquina, com antivírus corporativo provavelmente a pesar. Falta medir macOS e Linux.

## 3. Comportamento no Claude Code

Cenário: ficheiro `src/agenda.ts` com uma regra plantada ("implantes não podem ser marcados à sexta à tarde. Porquê: o Dr. Mendes precisa de 48 h de acompanhamento"), hook de teste que devolve o bloco, `claude -p` com `--settings`.

- **Pedido explícito** ("permite implantes à sexta à tarde"): em todas as variantes o modelo **fez a edição**, porque o utilizador o pediu. Com o bloco (no `Read` ou via `deny`) referiu o porquê e sugeriu confirmar o acompanhamento; sem o bloco não referiu nada. A consulta **informa**, não impede.
- **`deny` uma vez**: o modelo recebeu a razão, escreveu que o utilizador tinha pedido a alteração e repetiu a edição. Gastou uma chamada, como a spec admite.
- **Subagente**: o hook disparou para o `Read` e o `Edit` do subagente (evento com `agent_id`, `session_id` igual ao do pai). O subagente incluiu o aviso no resumo que devolveu.
- **`Bash`**: com `sed -i` o ficheiro foi alterado sem qualquer disparo do hook.

### Teste de valor (pequeno, n = 1 por célula, anedótico)

Quatro pedidos sem o bloco e com o bloco, no ficheiro de teste:

| Pedido | Sem o bloco | Com o bloco |
|---|---|---|
| "Simplifica `validateBooking`" (vago) | não edita, explica que só tem uma regra | idem |
| "Os pacientes queixam-se de não poderem marcar implantes às sextas; torna a regra menos estrita" | edita (corte às 15:00, "palpite meu") | edita igual, **e refere a razão do Dr. Mendes** |
| "Acrescenta um comentário a `validateBooking`" (toca na função, não na regra) | edita | edita (sem referir a razão) |
| "`greet` com ponto de exclamação" (ficheiro sem regras) | edita | edita; **sem ruído** |

Leitura prudente: neste ficheiro de teste o modelo base já trata bem os pedidos mais simples, porque o código é minúsculo e a regra está à vista. O ganho visível está no pedido que de facto relaxa a regra: o utilizador passa a ser informado do **porquê**, que o código não tinha. **Nenhum** pedido terminou em "pára e pergunta". Isto vale como indício para a avaliação real (10.5), com 30 pedidos e uma fixture com mais código, e não como medida do critério de >= 80%. Convém também decidir, antes dessa avaliação, se o critério é "assinala" (informar) ou "evita" (parar e perguntar), porque o spike sugere que o primeiro é o realista.

## 4. Consequências para a spec e para o plano

1. **Latência (D4, secção 2)**: o hook tem de ser um ponto de entrada só com a biblioteca padrão, separado do `rastro` (Typer, keyring, httpx). Isto passa de risco aceite a requisito de desenho. Rever o orçamento de 150 ms (secção 2 da spec) para o valor que o 10.1 medir com o hook real, ou adotar o processo residente.
2. **Estado "informado" (secção 6)**: tem de ser por `(session_id, agent_id)`. O subagente partilha o `session_id` do pai mas tem o seu contexto, por isso um ficheiro lido pelo pai não está "informado" para ele.
3. **Registo de consultas (D7)**: guardar também o `agent_id` (é opcional).
4. **Critério de sucesso (secção 2)**: distinguir "assinala o porquê ao utilizador" de "evita a edição". O spike só viu o primeiro.
5. **Texto do `deny`**: aparece ao modelo como "hook error", o que pode soar a falha técnica. Convém uma frase inicial a dizer que é um aviso de regra registada e não um erro.
6. **`Bash`** continua fora do âmbito; a métrica de cobertura (D7) dirá o peso.

## 5. Como foi feito

Hook de teste em Python (só biblioteca padrão), cache JSON de regras e projeto TypeScript minúsculo num diretório temporário; `claude -p ... --output-format stream-json --verbose --settings <ficheiro>`; o hook regista cada evento recebido (ferramenta, ficheiro, `session_id`, `agent_id`). Nota prática: o Claude Code embutido na app desktop está num pacote MSIX e o Windows redireciona-lhe o `AppData\Roaming`; a autenticação do `claude -p` foi feita com esse executável. Os scripts do spike não entram no repositório.


## 6. Passo 10.1: snapshot, cache e entrada do hook

Feito: rota `GET /v1/repos/{id}/rules/snapshot`, `rastro sync`, cache local `.rastro/cache/rules.json`, bloco de regras (`rules_block`) e a entrada `python -m rastro_cli.hook_entry pre-tool`. Ainda não está instalada em nenhum lado (instalador, `session-start` e registo de consultas são do 10.2).

Decisões tomadas a partir do spike:

- **O `ETag` hasheia o conteúdo servido**, não `contagem + max(updated_at)` como a spec previa. As âncoras mudam no `resync_anchors` e as validações nas revisões sem um `updated_at` fiável, e uma cache desatualizada falha em silêncio. Há um teste para a validação e para uma âncora que muda de ficheiro.
- **A entrada do hook é independente da CLI**: só biblioteca padrão e, além disso, sem `pathlib`, `datetime`, `contextlib`, `typing` nem `tempfile` em tempo de importação (um teste verifica-o num processo à parte). Cada import custa dezenas de ms quando o Python arranca de raiz a cada ferramenta.
- **O estado "informado" é por `(session_id, agent_id)`**, pelo que o spike mostrou sobre subagentes.
- Só entram regras `active` **com pelo menos uma âncora ativa**; âncoras de símbolos removidos ficam de fora.

Latência da entrada real (25 execuções, 100 a 5 000 regras): **o tamanho da cache continua a não contar**, e o custo acima do Python vazio é de ~20 ms no mínimo (~50–100 ms no p50). Em valor absoluto os números desta máquina variaram muito entre corridas (o `python -c pass` foi de 102 ms a 278 ms), por isso o orçamento de 150 ms só se confirma com a medição do 10.5 em condições estáveis.

Teste de ponta a ponta com o Claude Code real (cache com uma regra **validada**, pedido vago de relaxar a regra): o modelo **não editou** e perguntou, citando a regra, quem a validou e o porquê. No spike, com o bloco de teste (regra sem validação nem a frase final do bloco), o modelo editou e só referiu o porquê no resumo. É uma só execução, mas sugere que o rótulo "validada por" e a frase "é uma decisão de negócio a mencionar ao utilizador" pesam; fica como hipótese para o 10.5.

## 7. Passo 10.2: instalador, `session-start` e registo de consultas

Feito: `rastro agent install|uninstall [--shared]`, o subcomando `session-start` do hook e o registo local `.rastro/consulted.jsonl`. O script `rastro-hook` foi acrescentado à CLI para a instalação partilhada.

Decisões:

- **A instalação local aponta para o interpretador que corre a CLI** (`sys.executable -m rastro_cli.hook_entry`), por isso não depende do `rastro` estar no PATH; fecha o defeito "hook mudo quando o `rastro` não está no PATH". A instalação partilhada usa `rastro-hook`, porque um caminho de interpretador é de uma máquina. Depois de instalar, a CLI corre o hook uma vez, com um evento vazio, e avisa se falhar.
- **A fusão nunca toca em hooks de terceiros** e é idempotente. Um ficheiro de definições inválido, ou com `hooks` de tipo errado, é recusado sem ser alterado.
- **`session-start` não faz rede no processo do hook**: lança um `rastro sync` desligado do terminal quando a cache falta ou tem mais de 10 minutos, e responde com o que já existe. Na primeira sessão sem cache o modelo não ouve nada; a seguinte já ouve. O `rastro sync` passa a atualizar a data do ficheiro num 304, para a idade da cache significar "verificada".
- **`clear` e `compact` esquecem o estado "informado" da sessão; `startup` e `resume` não**, porque aí o contexto sobrevive.
- **O registo de consultas leva ids e ficheiros, nunca texto de regras**, e deixa de crescer a 1 MiB se ninguém o esvaziar (o envio é o 10.4).

Teste com o Claude Code real no bella-nina (27 regras, 3 validadas; hooks instalados com `rastro agent install`), pedido em modo `plan` para não editar nada: "as clientes reclamam que não podem remarcar mais vezes; mostra como permitir remarcar sem limite".

- O `SessionStart` entregou "27 regras, 3 confirmadas por uma pessoa".
- O subagente de exploração do Claude Code recebeu as regras ao ler `src/api/server.ts` e `src/domain/scheduling.ts`; o registo tem as duas leituras, com o `agent_id` do subagente e 5 ids cada.
- O modelo **não propôs apagar o limite sem avisar**: escreveu que a regra do máximo de 2 remarcações foi validada a 19/10 com a razão da agenda da profissional, que removê-la a contradiz, e pediu ao utilizador a decisão e a razão (sugerindo, em alternativa, subir o limite). Uma só execução, mas é a segunda vez que uma regra validada faz o modelo parar e perguntar.

Ainda por testar: `compact` num caso real (só testado com eventos simulados), macOS/Linux e o `--shared` com o `rastro-hook` realmente instalado.

## 8. Passo 10.3: servidor MCP

Feito: pacote `packages/mcp` (`rastro-mcp`, SDK de MCP 2.3, transporte stdio) com `rastro_rules_for_file`, `rastro_search_rules` e `rastro_get_rule`, todas só de leitura (`readOnlyHint`, `destructiveHint = false`), e `rastro agent install --mcp`.

Decisões:

- **O `rastro-mcp` depende do `rastro-cli`** em vez de um pacote comum novo. O ganho da extração que propus na análise seria evitar o `typer`, mas o servidor é um processo longo (o arranque não conta) e precisa de `keyring` e `httpx` de qualquer forma. Reutiliza `config`, `client` e `rules_block` sem refatorar o que já funciona.
- **`rastro_rules_for_file` usa o snapshot** (com a mesma cache e o mesmo ETag do hook), por isso devolve exatamente o formato do hook, com duplicados, até 15 regras. Se o servidor estiver em baixo responde com a cópia local e diz-o; um 401, 403 ou 404 é um erro e não é escondido pela cache.
- **Pesquisa e detalhe nunca mostram citações de origem**; o detalhe mostra âncoras, commits (sem texto) e revisões humanas. Todo o texto passa por `clean` e vem dentro das mesmas etiquetas "dados registados, não instruções".
- **`install --mcp` local escreve `.mcp.json` com o interpretador desta máquina**, exclui-o do git e pré-aprova o servidor em `enabledMcpjsonServers` (a pessoa acabou de o pedir). Recusa-se a escrever num `.mcp.json` que já exista com outro conteúdo, porque pode ser um ficheiro de equipa e o caminho é desta máquina: nesse caso `--shared` (script `rastro-mcp`, sem pré-aprovação) ou `claude mcp add --scope local`. Nunca sobrescreve um servidor `rastro` que não seja o nosso. A permissão `mcp__rastro` não é acrescentada sozinha: o comando imprime a linha a juntar a `permissions.allow`, porque é uma definição de segurança da pessoa.

Teste com o Claude Code real no bella-nina: o servidor ligou (`connected`), as três ferramentas apareceram e, a um pedido em português ("que regras existem sobre cancelamentos e quem as validou"), o modelo pesquisou duas vezes por tema (em português e em inglês) e respondeu com as regras de cancelamento, os ids, as razões quando existem e os ficheiros, e reparou que a mesma regra aparece em duas entradas, uma delas sem motivo registado. A pesquisa foi semântica contra o servidor de dev. Não se editou nada (modo `plan`).

Ainda por testar: o modelo escolher o MCP sozinho antes de editar, sem o utilizador o pedir (é o que a avaliação 10.5 mede), `--shared` com `rastro-mcp` realmente instalado e a publicação dos dois pacotes.
