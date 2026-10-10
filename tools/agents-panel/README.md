# 🛰 Painel de Agentes

Constelação animada dos processos autônomos de um projeto, onde **a animação é o
dado**: o arco em volta de cada agente é a fração do ciclo já decorrida
(`idade ÷ cadência`), pulsos só nascem de agente saudável, e cor é sempre
semântica.

Só biblioteca padrão do Python + HTML/CSS/JS puro. Zero dependência nova, zero
chamada de IA, zero rede externa na montagem do snapshot.

## Dois modos

**Declarativo** — um `agents.json` descreve os seus agentes. Eles podem morar em
árvores completamente diferentes (`~/.claude`, `~/.agents`, um repo, um diretório
de trabalho); o painel só precisa saber onde cada um deixa prova de vida.
Nenhuma linha de Python por agente.

```sh
cp tools/agents-panel/agents.example.json tools/agents-panel/agents.json
# edite, depois:
python3 tools/agents-panel/server.py
```

**Demo** — sem `agents.json`, sobe um conjunto embutido que faz trabalho local
real sobre este repositório. Serve para ver o painel funcionando e para a suíte
de validação ter em que rodar.

## ⚠️ Sobre o conjunto demo

O nvm tem **um** processo agendado de verdade: a varredura semanal do CodeQL
(`.github/workflows/codeql-analysis.yml`, `cron '0 17 * * 4'`). Todos os outros
workflows são reativos (`push` / `pull_request`), e o `nvm.sh` não tem daemon,
worker nem cache com TTL.

No conjunto demo as **cadências são simuladas**, mas cada agente faz trabalho
local real (conta arquivos, calcula checksum, lê o cron do workflow) e grava
prova de vida real em disco. Nenhum número exibido é inventado.

O agente `VARREDURA CODEQL` aparece como **"nunca rodou"** de propósito: quem o
agenda é o GitHub, e o painel não tem prova local de nenhuma rodada. Semeá-lo
com um timestamp plausível seria exatamente a mentira que o painel existe para
não contar.

## Sondas de prova de vida

Cada agente declara de onde vem o "quando rodou pela última vez". As sondas
estão na ordem de preferência — da melhor fonte para a pior:

| `tipo` | Fonte | Campos |
|:--|:--|:--|
| `json_field` | Campo de timestamp num arquivo de estado | `arquivo`, `campo`, `ok_campo`, `metric_campo`, `detail_campo` |
| `sqlite` | Coluna num banco (consulta somente leitura) | `arquivo`, `consulta`, `parametros` |
| `artifact_field` | Timestamp dentro do artefato produzido | iguais a `json_field` |
| `mtime` | Data de modificação do arquivo de saída | `arquivo` |
| `log_regex` | Última linha casada num log | `arquivo`, `padrao` (com `(?P<ts>…)`), `padrao_erro` |

`campo` aceita caminho pontilhado (`resumo.processados`). Timestamps podem ser
ISO-8601 (com ou sem `Z`), epoch em segundos ou epoch em milissegundos.

Duas advertências que valem repetir, porque estão no código como comentário:
`mtime` mente se outra coisa tocar o arquivo — um `touch`, um backup, um editor
aberto por engano reiniciam o relógio sem o agente ter rodado. E `log_regex` é a
mais frágil de todas: log rotacionado ou com formato alterado faz o agente
parecer morto sem nada ter acontecido com ele.

## Rodando

```sh
python3 tools/agents-panel/server.py
python3 tools/agents-panel/server.py --config ~/meus-agentes.json
# abre http://127.0.0.1:7777
```

Opções: `--config`, `--host`, `--port`, `--no-seed`, `--no-scheduler`.

Configuração inválida **derruba a subida** com saída 2 e diz qual entrada está
errada. Um painel que ignora metade do próprio inventário em silêncio é pior que
um painel que não sobe.

## Arquitetura

| Arquivo | Papel |
|:--|:--|
| `inventory_scan.py` | Fase 1: varre a máquina atrás de processos autônomos. |
| `agents.example.json` | Modelo de configuração, com as cinco sondas. |
| `probes.py` | Sondas de prova de vida. O único lugar que conhece formato de dado. |
| `registry.py` | Carrega a config (ou o conjunto demo) e valida. |
| `snapshot.py` | Máquina de estados e contrato JSON. Leitura pura. |
| `server.py` | Rotas HTTP, execução de disparo e agendador do modo demo. |
| `static/index.html` | A constelação e o painel lateral, em arquivo único. |

### Rotas

| Rota | O que faz |
|:--|:--|
| `GET /api/agents` | Snapshot de todos os agentes. **Nunca executa nada.** |
| `GET /api/agents/<id>` | Snapshot de um agente. **Nunca executa nada.** |
| `POST /api/agents/<id>/run` | **Executa** o agente. |

A separação é deliberada e é o erro mais caro que este tipo de painel comete: um
botão "rodar agora" ligado numa rota que só lê cache responde "concluído" sem ter
feito nada. Por isso o `run` do contrato é um objeto `{url, method, body}`
apontando para a rota do próprio painel — agente que não pode ser forçado recebe
`run: null` e não ganha botão.

### Máquina de estados

Calculada no servidor, nunca no front.

| Estado | Significado |
|:--|:--|
| `ok` | Rodou dentro do esperado. |
| `stale` | Passou de `STALE_FACTOR` (2,5×) da cadência sem rodar. |
| `error` | O agente registrou falha, ou a sonda não conseguiu ler a fonte. |
| `idle` | Nunca rodou. **Não é `ok`.** |
| `off` | A config de que depende não existe. Informação, não alarme. |

O fator 2,5× mora numa constante nomeada em `registry.py`.

## Levantando o inventário (Fase 1)

O painel só vale o que o inventário vale. `inventory_scan.py` faz essa varredura
e é somente leitura — não escreve, não executa o que encontra, não faz rede:

```sh
python3 tools/agents-panel/inventory_scan.py ~/meu-projeto ~/.agents
python3 tools/agents-panel/inventory_scan.py ~ --json > inventario.json
```

Ele procura crontab, agendamentos do launchd (macOS) e timers do systemd
(Linux), `schedule:` de GitHub Actions, CronJobs de k8s, node-cron, APScheduler,
Celery beat, Sidekiq-cron, `setInterval`, laços `while true` com `sleep`, tickers
de goroutine e threads daemon. Também marca quais arquivos já têm prova de vida
(`last_run`, `updated_at`, `heartbeat` e afins) — os que não têm são os que
precisam ganhar isso antes de entrar no painel.

Os trechos exibidos passam por redação de segredos (chaves `sk-`, `ghp_`,
`xox*`, `AKIA`, JWT, e pares `token=`/`password=`). A cobertura é dos formatos
conhecidos, não de tudo: **revise o JSON antes de compartilhar**.

## Portando para outro projeto

1. Rode `inventory_scan.py` e confira o resultado contra o que você já sabia.
2. Para cada agente, escolha a melhor sonda disponível na tabela acima.
3. Agente sem prova de vida nenhuma: faça ele gravar um `last_run` ISO no fim de cada rodada. São três linhas, e é o que torna o painel possível.
4. Escreva o `agents.json` a partir do `agents.example.json`.
5. Troque o bloco `:root` do `index.html` pelas variáveis de tema do projeto. Nenhuma cor está escrita direto no desenho.

Não é preciso editar Python para adicionar agente — só para ensinar uma sonda
nova ao `probes.py`.

## Cuidados

- **`run.cmd` é superfície executável.** O que estiver no `agents.json` roda com os privilégios de quem subiu o servidor. Comandos são sempre uma lista de argumentos executada sem shell, o que protege contra acidente de quoting, não contra uma configuração hostil. Trate o arquivo como um script seu.
- **Não vaze segredo.** O snapshot é estado operacional: nada de token, senha, corpo de mensagem ou dado pessoal. "36 e-mails triados" pode; o assunto dos e-mails não.
- **Escuta em `127.0.0.1`** por padrão. O mapa dos seus processos internos é informação sensível; expondo para fora, ponha atrás da mesma autenticação do resto do projeto.
- **Ausência de dado não é "tudo certo".** Sem timestamp, o estado é `idle`. Falha de leitura vira `error`, nunca estado vazio — todas as sondas distinguem o arquivo comprovadamente ausente de qualquer outro erro, em vez de usar um teste genérico de existência que engoliria permissão negada e mentiria "nunca rodou".
