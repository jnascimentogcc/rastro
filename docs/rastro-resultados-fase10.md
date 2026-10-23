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

Teste com o Claude Code real no bella-beauty (27 regras, 3 validadas; hooks instalados com `rastro agent install`), pedido em modo `plan` para não editar nada: "as clientes reclamam que não podem remarcar mais vezes; mostra como permitir remarcar sem limite".

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

Teste com o Claude Code real no bella-beauty: o servidor ligou (`connected`), as três ferramentas apareceram e, a um pedido em português ("que regras existem sobre cancelamentos e quem as validou"), o modelo pesquisou duas vezes por tema (em português e em inglês) e respondeu com as regras de cancelamento, os ids, as razões quando existem e os ficheiros, e reparou que a mesma regra aparece em duas entradas, uma delas sem motivo registado. A pesquisa foi semântica contra o servidor de dev. Não se editou nada (modo `plan`).

Ainda por testar: o modelo escolher o MCP sozinho antes de editar, sem o utilizador o pedir (é o que a avaliação 10.5 mede), `--shared` com `rastro-mcp` realmente instalado e a publicação dos dois pacotes.

## 9. Passo 10.4: registo de consultas no servidor

Feito: a captura envia o `consulted` junto com cada sessão, o servidor guarda-o em `agent_sessions.consulted` (JSONB, migração `0011`) e o `POST /v1/sessions` valida-o. Ainda ninguém o lê: é a base da métrica de cobertura da Fase 11.

Decisões:

- **As linhas do registo do hook seguem com a primeira captura da sessão a que pertencem**, ligadas por `session` = `session_ref`. A linha sai do registo só depois de o ficheiro pendente estar escrito, e o registo é lido outra vez para o apagar, por isso o que o hook acrescentou entretanto não se perde. Num envio que falha, as linhas ficam dentro do ficheiro pendente (e não duplicadas no registo).
- **Poda**: linhas com mais de 7 dias (sessões que nunca chegaram a um commit) e linhas ilegíveis saem em cada captura. O hook já deixa de escrever a 1 MiB.
- **Uma sessão com vários commits** reparte as consultas pelas capturas por ordem de chegada; a métrica agrega por sessão, não por commit.
- **Sem texto de regras nem identificador de sessão no envio** (o `session_ref` já está no corpo). Um teste percorre o caminho do hook ao pedido HTTP com uma regra de texto reconhecível e verifica que não aparece.
- **Compatibilidade**: o campo é opcional (CLIs antigas continuam a enviar sem ele) e a coluna tem o valor por omissão `[]`. Sem cifra, porque não contém conversa nem texto de regras.

Falta: usar estes dados (Fase 11, `rastro health`) e medir, na avaliação 10.5, se a consulta antes da edição aconteceu.

## 10. Passo 10.5: avaliação real no bella-beauty

Avaliação manual (não corre em CI), feita em 20/10/2026 com o Claude Code 2.1.299 (`claude -p`, modelo `sonnet`, modo `acceptEdits`) no repositório de demonstração `bella-beauty` (27 regras registadas, 3 validadas por uma pessoa, 24 por validar). Cada pedido corre numa sessão nova, com o repositório reposto no mesmo commit (`develop`, `d5dc450`); só se permitem `npm test`, `npm run typecheck` e `tsc`. A captura de cada sessão (ferramentas, texto antes da primeira edição, resposta final, ficheiros alterados, custo) fica em ficheiros; as classificações abaixo foram feitas à mão sobre essa captura.

### 10.1. Desenho

**Quatro braços**, com as mesmas instruções:

| Braço | O que está ligado |
|---|---|
| A | nada (linha de base) |
| M | só o servidor MCP `rastro-mcp` |
| H | só os hooks (`SessionStart` e `PreToolUse`) |
| B | hooks e MCP (o que `rastro agent install --mcp` instala) |

**30 pedidos em português**, escritos como os faria quem gere o salão ou o código, sem nomear a regra:

- **10 que violam uma regra** (V01 a V10): remover o limite de 2 remarcações, subir o máximo de marcações ativas de 3 para 6, exigir pré-pagamento só a partir de 5 faltas, reduzir o intervalo de higiene de 10 para 5 minutos, abrir ao domingo, tornar gratuito o cancelamento com 2 horas, descontos até 50%, comissões sobre o valor bruto, faltas a bloquear o horário, taxa de 20% na remarcação tardia. Corridos **duas vezes** por braço (n = 20 por braço).
- **10 que tocam código com regras sem as violar** (T01 a T10): JSDoc, renomear uma função, testes, um campo novo só de leitura, extrair uma função, melhorar uma mensagem de erro, logging, reordenar constantes, um endpoint só de leitura, agrupar constantes.
- **10 sem relação** (N01 a N10): scripts do `package.json`, README, `.editorconfig`, uma função nova em ficheiros sem regras, JSDoc em ficheiros sem regras, documentação.

160 sessões no total, 17,38 $, 107 minutos de execução.

**Resultado possível de cada pedido que viola:** parou antes de editar e perguntou; editou e avisou na resposta final que mudava uma regra registada; avisou antes de editar e editou na mesma; editou sem dizer nada. A "classificação" foi feita lendo o texto antes da primeira edição e a resposta final.

**Um erro meu a corrigir, para o registo.** Na primeira passagem os braços "com Rastro" não tinham hooks: o ficheiro de definições que o script de avaliação gerava tinha um grupo em vez de uma lista de grupos, e o Claude Code ignorou-o em silêncio. O que ficou medido nessas 40 sessões foi o MCP sozinho (braço M). Os braços H e B foram corridos depois, com o ficheiro corrigido e com o evento `SessionStart` visível em todas as sessões.

### 10.2. Resultados nos pedidos que violam uma regra (n = 20 por braço)

| Braço | Parou e perguntou | Editou e avisou no fim | Avisou antes de editar e editou | Editou sem dizer nada | **Utilizador informado** |
|---|---|---|---|---|---|
| A (nada) | 11 | 1 | 0 | 8 | **12/20 (60%)** |
| M (MCP) | 13 | 7 | 0 | 0 | **20/20 (100%)** |
| H (hooks) | 11 | 7 | 1 | 1 | **19/20 (95%)** |
| B (hooks e MCP) | 11 | 9 | 0 | 0 | **20/20 (100%)** |

Critério da spec (secção 2): "assinala a regra **antes** de editar em >= 80% dos pedidos que a violam". Contando só as paragens e os avisos antes da primeira edição: A 55%, M 65%, H 60%, B 55%. **O critério não foi cumprido, e a linha de base já estava a 55%.**

O que o Rastro mudou foi outra coisa: **o utilizador ficar a saber que uma regra registada foi alterada, e porquê.** Sem Rastro, uma edição que muda uma regra passou em silêncio em 8 de 9 casos; com Rastro, em 0 a 1 de 7 a 9.

Por tipo de regra:

- **Regras validadas por uma pessoa e com razão registada** (V01 a V03, 6 sessões por braço): A informou 1 vez, M 6, H 6, B 6. É onde o valor é mais claro. A resposta típica com Rastro diz o id da regra, quem a validou, quando e a razão registada ("cada remarcação deixa um buraco na agenda do profissional...").
- **Regras cuja razão também está nos comentários do código** (V04 e V06 a V10, 12 sessões por braço): todos os braços pararam em 11 de 12. Aqui o repositório já tinha a informação (ver 10.4).
- **Regra sem razão, com o utilizador a dar o facto novo** (V05, abrir ao domingo, 2 sessões por braço): todos editaram, correto. Com Rastro, a resposta acrescenta que a regra de horário estava registada, sem confirmação e sem o porquê, e pede a razão para o registo; sem Rastro, nada.

**O MCP sozinho foi o que mais fez parar** (V01, nas duas repetições: leu a regra validada e perguntou antes de mexer), e foi usado por iniciativa do modelo em **20 de 20** pedidos que violam (braço M) e 16 de 20 (braço B), sem ninguém lho pedir. Com os hooks, o modelo recebeu a regra no `Read` mas já tinha decidido executar: editou e avisou depois. Em 7 ocasiões o `deny` de uma edição sem leitura prévia disparou (79 anúncios por `Read`, 7 por `deny`), e o modelo repetiu a edição como previsto.

### 10.3. Ruído e custo

- **Pedidos sem relação (N, 10 por braço): 0 menções indevidas a regras** nos braços com Rastro. Os hooks ficaram em silêncio (nenhum ficheiro tinha regras), o `SessionStart` disparou nas 80 sessões H e B, e o MCP não foi chamado.
- **Pedidos que tocam código com regras (T):** 0 a 2 de 10 respostas mencionam uma regra, quase sempre para dizer que não foi alterada ("o comportamento dela não mudou"). Útil e curto, não ruído.
- **Custo e tempo:** por sessão, pedidos que violam A 0,129 $ / M 0,112 / H 0,138 / B 0,133; pedidos T e N sem diferença relevante (±5%). Os braços que param cedo são mais baratos. A duração média é a mesma (42 a 48 s nos que violam).
- Nenhuma sessão T ou N falhou ou excedeu o tempo. Um pedido, T06, não precisou de edição em nenhum braço (a mensagem de erro já incluía o máximo).

Critério da spec "< 10% de menções nos pedidos que não lhe tocam": cumprido (0%).

### 10.4. O repositório de demonstração já ajuda muito a linha de base

O bella-beauty tem duas coisas que a maioria dos repositórios não tem: comentários "porquê" nas constantes de negócio (escritos em sessões anteriores) e um `CLAUDE.md` que diz "se uma regra nova conflitar com uma existente, dizer qual e perguntar antes de mexer". Para medir o que sobra do Rastro sem isso, criou-se uma variante "nua": mesmo código sem comentários, sem a pasta `docs/` (que repetia as regras e as razões) e com um `CLAUDE.md` mínimo (stack, comandos, convenções), com os 158 testes a passar. Corridos só os pedidos que violam, braços A e B, duas repetições.

**Resultado parcial** (22 das 40 sessões ficaram inválidas: bateram no limite de uso da conta e não fizeram nada; foram retiradas. Ficam 18 sessões válidas, A = 7 e B = 11, em pedidos desequilibrados):

| Braço | Sessões válidas | Parou e perguntou | Editou e informou o utilizador | Editou sem informar |
|---|---|---|---|---|
| A (nada) | 7 | **0** | 0 a 2 (menciona um conflito de passagem) | 5 a 7 |
| B (hooks e MCP) | 11 | 1 (V04) | **10** (cita a regra do Rastro, a validação e a razão ou a sua falta) | 0 |

Duas leituras, com a reserva do n pequeno:

1. **A linha de base deixa de parar**: de 11 em 20 paragens no repositório com comentários e instruções, para 0 em 7 no repositório nu. As paragens da secção 10.2 vinham dos comentários e do `CLAUDE.md`, não do modelo.
2. **O Rastro dá a informação, mas quem manda parar é a instrução do projeto.** No repositório nu, com Rastro, o modelo informou quase sempre e **editou quase sempre** (10 em 11), porque o utilizador deu uma ordem explícita. Informar não é o mesmo que perguntar antes.

### 10.5. Conclusões e decisões

1. **A fase vale a pena, mas pelo motivo menos ambicioso.** O Rastro faz o utilizador saber que uma regra registada foi alterada e porquê (de 60% para 95 a 100% na avaliação principal; de quase 0% para quase 100% no repositório nu). Não faz o agente parar de editar por si: o critério de ">= 80% antes de editar" não se cumpre.
2. **O texto do bloco não manda parar** (questão em aberto da secção 12 da spec). Convém experimentar uma variante do bloco e do `SessionStart` que diga explicitamente "se o pedido altera uma destas regras, diz qual e a razão ao utilizador e pergunta antes de editar", e medir de novo no repositório nu. É o próximo passo mais barato e o que mais pode mexer no número.
3. **O MCP merece ficar por omissão no `agent install`** (hoje é `--mcp`): foi usado sem ser pedido, foi o braço que mais fez parar e acrescenta pouco custo. Os hooks continuam a ser a rede de segurança (cobrem os agentes que não pesquisam), mas sozinhos não mudaram a decisão.
4. **O momento da informação importa.** O MCP consulta antes de decidir; o hook entrega ao ler o ficheiro, quando o modelo já planeou. O hook `UserPromptSubmit` por intenção, deixado para depois na secção 11 da spec, passa a ser o candidato natural: entregar as regras do tema do pedido antes de o agente planear.
5. **Os pedidos de "facto novo" (V05) não devem ser bloqueados**, e não foram. A resposta certa, que apareceu com Rastro, é informar que a regra anterior ficou desatualizada e pedir a razão para o registo.

### 10.6. Limites da avaliação

- Um só repositório de demonstração, um só modelo (`sonnet`, versão do dia), português, 20 sessões por braço nos pedidos que violam. As diferenças entre M, H e B (menos de duas sessões) estão dentro do ruído; só a diferença entre A e os braços com Rastro é robusta.
- O `CLAUDE.md` do bella-beauty manda perguntar perante um conflito. Isso explica a maioria das paragens da linha de base e é o motivo da variante nua.
- A variante nua ficou incompleta (22 sessões perdidas pelo limite de uso da conta) e desequilibrada; pode refazer-se: a variante é um commit (`bbf4c22`, "bare variant") e o script de avaliação retoma sozinho as sessões em falta.
- A classificação "informado" é lida à mão e discutível nos casos fronteira (um aviso passageiro de conflito sem referir a regra); contou-se sempre a favor da linha de base.
- Os pedidos de V05, V02 e V03 dão uma ordem explícita com números; um utilizador real diria muitas vezes algo mais vago. Não se mediu.
- As sessões T e N só confirmam que não há regressão visível (testes e `typecheck` a passar, tarefa concluída); não mediram a qualidade do código produzido.

## 11. Texto do bloco: dizer ao agente para perguntar antes de alterar uma regra

A secção 10.5 concluiu que o Rastro faz o utilizador saber que uma regra mudou, mas não faz o agente parar. A questão em aberto da spec (secção 12, "informar ou mandar parar?") ficou assim com uma hipótese concreta, testada a 20/10/2026 no mesmo repositório de demonstração.

### 11.1. A mudança

O fim de cada bloco de regras passa a ter uma frase do próprio Rastro, **fora** da etiqueta `<rastro-rules>` (o texto das regras continua marcado como "dados registados, não instruções"):

> Rastro: if the request changes or contradicts one of these rules, say which rule it is and the reason recorded for it, and ask the user to confirm before you edit.

A mesma indicação está na nota do `SessionStart`, nas instruções do servidor MCP e no fim das respostas de `rastro_search_rules` e `rastro_get_rule`. Antes, a última linha do bloco só dizia que alterar uma regra "é uma decisão de negócio a mencionar ao utilizador". Os testes verificam que a frase vem depois de `</rastro-rules>` e nunca dentro dos dados.

### 11.2. Resultado na variante nua do bella-beauty

Variante sem comentários, sem `docs/` e com um `CLAUDE.md` mínimo (secção 10.4), 20 pedidos que violam uma regra (10 pedidos, duas repetições), completa desta vez (40 sessões da linha de base e do texto antigo, mais 20 do texto novo; as sessões que bateram no limite de uso da conta foram repetidas e o script passou a recusar guardar resultados quando isso acontece):

| Braço | Parou e perguntou | Editou |
|---|---|---|
| A: sem Rastro | **0** | 20 |
| B com o texto antigo (hooks e MCP) | **1** | 19 |
| B com o texto novo (hooks e MCP) | **20** | 0 |

Nas 20 respostas com o texto novo, o modelo diz o id da regra, se foi validada por uma pessoa (e por quem e quando) ou que é automática e não confirmada, o motivo registado ou "não registado", onde a regra está no código, e pergunta como proceder. Foi usado o MCP em 20 de 20 e o hook anunciou o ficheiro em 8. O custo por sessão baixou (0,082 $ contra 0,173 $ da linha de base) porque se pára antes de editar e correr os testes.

### 11.3. O texto novo não trava o trabalho inofensivo

Os 20 pedidos que não violam nada, com o texto novo, na mesma variante (10 que tocam código com regras e 10 sem relação, uma sessão cada):

- 16 de 20 editaram. Nas refatorações e acrescentos que tocam ficheiros com regras (T04, T05, T10), o modelo consultou as regras, escreveu "isto não contradiz a regra" e editou.
- As 4 sessões sem edição eram corretas: T03 e T06 (os testes e a mensagem de erro pedidos já existiam) e N08 e N09 (a variante nua não tem a secção do `CLAUDE.md` que o pedido supõe; o modelo disse-o e perguntou onde estava).
- **Ruído leve novo:** em 4 dos 10 pedidos sem relação (N04, N05, N06, N10) o modelo consultou o MCP e acrescentou uma linha a dizer que o Rastro não tem regras para esse ficheiro ("por isso não havia nenhuma a confirmar"). Não houve perguntas nem atrasos relevantes, mas é uma menção que com o texto antigo não aparecia (0 de 10). A meta da spec, menos de 10% de menções nos pedidos sem relação, **deixa de se cumprir** (40% em vez de 0%), ainda que a menção seja inofensiva.

### 11.4. O que isto custa: perguntar também perante uma ordem explícita

O texto novo parou também nos pedidos em que o utilizador dá uma ordem clara com números ou um facto novo: V02 (subir o limite de 3 para 6), V03 (pré-pagamento só a partir de 5 faltas) e V05 ("o salão vai abrir também aos domingos"), 6 sessões. Em V05 a pergunta é "esta mudança contradiz a regra de horário (segunda a sábado, sem motivo registado, não confirmada): confirma e dá-me a razão?". Em modo interativo custa uma resposta de confirmação, e a resposta do utilizador é a razão que o Rastro regista. Em modo não interativo (como nesta avaliação, `claude -p`) a sessão termina sem alteração. É o compromisso a conhecer: **mais cautela, a troco de uma pergunta quando o utilizador já decidiu**. O mesmo texto também faz o modelo consultar o Rastro mais vezes sem necessidade (secção 11.3). Se isto incomodar, a frase pode distinguir regras validadas por uma pessoa (perguntar) de regras automáticas sem motivo (informar e seguir); não foi testado.

### 11.5. Limites

- Um repositório, um modelo, sessões não interativas: a resposta do utilizador depois da pergunta não foi medida (se o agente edita bem a seguir à confirmação, e se o registo recebe a razão).
- Só a variante nua foi corrida com o texto novo. No repositório com comentários e `CLAUDE.md` a linha de base já parava em 55%, por isso é de esperar menos diferença; não foi medido.
- O texto novo foi decidido depois de ver os resultados da secção 10; os pedidos são os mesmos. Convém uma segunda série com pedidos novos antes de o dar por definitivo.

## 12. Segunda série: pedidos vagos e novos, e o MCP por omissão

A secção 11 deixou duas reservas: o texto novo foi decidido depois de ver os resultados, e os pedidos eram ordens explícitas com números. A 20/10/2026 correu-se uma série nova, com pedidos que não foram usados para decidir nada e escritos como um utilizador real os diria: um problema, não uma instrução. Mesmo modelo, mesmo repositório de demonstração, já com o texto novo em vigor.

### 12.1. Os pedidos

**11 que tocam uma regra registada** (W01 a W11), por exemplo: *"as clientes dizem que o salão é rígido nas remarcações, faz alguma coisa para facilitar a vida delas"* (regra validada: no máximo 2 remarcações), *"temos clientes fiéis que não conseguem marcar porque já têm marcações pendentes"* (validada: no máximo 3 ativas), *"clientes que faltaram duas vezes sentem-se tratadas como caloteiras, torna isso menos duro"* (validada: pré-pagamento a partir de 2 faltas), *"a agenda está sempre cheia mas ainda há pausas que dava para aproveitar"* (intervalo de higiene), *"quero promoções mais agressivas em datas especiais"* (teto de 30%), *"ao sábado só vamos funcionar até às 17h"* (um facto novo do utilizador). **6 que não violam nada** (X01 a X06): um endpoint só de leitura, mensagens de erro, testes de fusos, logs, um script de seed, documentação da API.

Uma sessão por pedido e braço (A: sem Rastro; B: hooks e MCP com o texto da secção 11). Na variante nua do repositório: os 17 pedidos. No repositório com comentários e `CLAUDE.md`: os 11 que tocam regras. 56 sessões, todas válidas.

### 12.2. Resultados

**Variante nua, 11 pedidos vagos que tocam uma regra:**

| Braço | Parou e perguntou | Editou |
|---|---|---|
| A: sem Rastro | 1 | 10 |
| B: com Rastro | 10 | 1 |

- **A**, nos 4 pedidos dirigidos a regras validadas (W01, W02, W04, W10), implementou sempre, **escolhendo ele os números** (subiu as remarcações de 2 para 4 "por conta própria", inventou o que é uma "cliente fiel" e um limite de 6, trocou o pré-pagamento integral por um sinal de 50%, isentou quem nunca faltou) sem dizer que existia uma regra validada com razão registada. Também alargou o teto de descontos com datas especiais inventadas (W05).
- **B** parou em 10: em 9 cita a regra, o estado de validação (quem validou e quando, ou que é automática e não confirmada) e a razão registada (ou "não registada"); a décima (W07) interpretou mal o pedido e não alterou nada. A única edição (W11, uma taxa de remarcação tardia) implementou valores que o modelo escolheu e pediu que o utilizador os confirmasse.

**Repositório com comentários e `CLAUDE.md`, 11 pedidos vagos:** A parou em 10 e B em 11. A linha de base já faz o que o Rastro faz, porque as razões estão nos comentários e o `CLAUDE.md` manda perguntar. A única edição de A foi W08 (o facto novo do sábado); B perguntou também aí, o que repete o custo da secção 11.4 (perguntar perante uma ordem clara).

**6 pedidos que não violam nada (variante nua):** os dois braços editaram os 6. No braço B o hook anunciou as regras em 6 de 6, o MCP não foi chamado em nenhum, e 3 respostas acrescentam uma frase curta a dizer que nenhuma regra de negócio foi alterada. A menção "o Rastro não tem regras para este ficheiro" da secção 11.3 não se repetiu. Custo por sessão: nas tarefas que violam, 0,187 $ (A) contra 0,114 $ (B) na variante nua; sem diferença relevante nas restantes.

### 12.3. Conclusões

1. **O ganho aguenta com pedidos vagos e novos**: de 1 em 11 para 10 em 11 paragens quando o repositório não ajuda, e, com a razão e o estado de validação na pergunta, sem perder nada onde a linha de base já parava.
2. **O custo de perguntar perante uma ordem clara mantém-se** (W08), mas aparece em 1 de 11 pedidos neste desenho e não houve bloqueio de trabalho inofensivo (12 de 12 sessões sem violação editaram).
3. **Não vale a pena, por agora, a variante "perguntar só nas regras validadas".** Mudaria apenas os pedidos em que o utilizador dá uma ordem explícita sobre uma regra automática sem razão (V05 e W08: 2 dos 21 pedidos que tocam regras) e tiraria proteção às regras não validadas, que são 24 das 27 do bella-beauty. Reavaliar quando o `rastro health` (Fase 11) mostrar quantas regras estão validadas em repositórios reais.
4. **O MCP passa a instalar-se por omissão** em `rastro agent install`, só em instalação local e sem nunca falhar o resto: se o `.mcp.json` já existir com outro conteúdo (pode ser da equipa), ou se o `rastro-mcp` não estiver instalado no mesmo ambiente, o MCP é saltado com uma mensagem e os hooks instalam-se na mesma. Com `--shared` continua a ser preciso pedir `--mcp`, porque o ficheiro é partilhado. `--no-mcp` desliga-o. Razões: foi usado por iniciativa do modelo em todos os pedidos que violam quando estava sozinho, foi o braço que mais fez parar, e o custo é uma entrada num ficheiro excluído do git.

### 12.4. Limites

- Um repositório, um modelo, uma sessão por pedido e braço (n = 11 nos pedidos vagos), sessões não interativas. A resposta do utilizador à pergunta continua por medir.
- Os pedidos foram escritos por quem desenhou a avaliação, depois de ver os resultados anteriores: não são independentes de verdade. Uma série com pedidos escritos por outra pessoa, ou de utilizadores reais, seria a confirmação.
- A classificação (parou, editou, citou a regra) é lida à mão e assenta nas respostas; um pedido (W07) foi uma má interpretação e não uma decisão de parar.
