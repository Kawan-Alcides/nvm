# 🛰 Painel de Agentes

Constelação animada dos processos autônomos de um projeto, onde **a animação é o
dado**: o arco em volta de cada agente é a fração do ciclo já decorrida
(`idade ÷ cadência`), pulsos só nascem de agente saudável, e cor é sempre
semântica.

Só biblioteca padrão do Python + HTML/CSS/JS puro. Zero dependência nova, zero
chamada de IA, zero rede externa.

## ⚠️ Isto é um protótipo

O nvm tem **um** processo agendado de verdade: a varredura semanal do CodeQL
(`.github/workflows/codeql-analysis.yml`, `cron '0 17 * * 4'`). Todos os outros
workflows do repositório são reativos (`push` / `pull_request`), e o `nvm.sh`
não tem daemon, worker nem cache com TTL.

Um painel precisa de pelo menos dois processos autônomos para ter o que mostrar,
então este aqui roda com um conjunto de agentes de demonstração. O que é real e
o que não é:

| | Real | Simulado |
|:--|:--|:--|
| Cadências | só o `cron` do CodeQL, lido do workflow | as demais |
| Trabalho executado | sim — cada worker lê este repositório de verdade | — |
| Métricas exibidas | sim — todas saem de uma leitura efetiva | — |
| Prova de vida | sim — arquivos JSON gravados em disco | — |

Nenhum número no painel é inventado. O que é fictício é a *periodicidade*.

## Rodando

```sh
python3 tools/agents-panel/server.py
# abre http://127.0.0.1:7777
```

Opções: `--host`, `--port`, `--no-seed` (não semeia estados iniciais),
`--no-scheduler` (não sobe o agendador local).

## Arquitetura

| Arquivo | Papel |
|:--|:--|
| `inventory_scan.py` | Fase 1: varre a máquina atrás de processos autônomos. |
| `registry.py` | Declara os agentes e o trabalho de cada um. É o inventário. |
| `snapshot.py` | Lê o estado em disco e monta o contrato JSON. Leitura pura. |
| `server.py` | Rotas HTTP e agendador local. |
| `static/index.html` | A constelação e o painel lateral, em arquivo único. |

### Rotas

| Rota | O que faz |
|:--|:--|
| `GET /api/agents` | Snapshot de todos os agentes. **Nunca executa nada.** |
| `GET /api/agents/<id>` | Snapshot de um agente. **Nunca executa nada.** |
| `POST /api/agents/<id>/run` | **Executa** o agente. |

A separação é deliberada e é o erro mais caro que este tipo de painel comete: um
botão "rodar agora" ligado numa rota que só lê cache responde "concluído" sem ter
feito nada, e isso destrói a confiança no painel inteiro. Por isso o campo `run`
do contrato é um objeto `{url, method, body}` e não uma string — agente que não
pode ser forçado recebe `run: null` e simplesmente não ganha botão.

### Máquina de estados

Calculada no servidor, nunca no front.

| Estado | Significado |
|:--|:--|
| `ok` | Rodou dentro do esperado. |
| `stale` | Passou de `STALE_FACTOR` (2,5×) da cadência sem rodar. |
| `error` | O agente registrou falha, ou o estado dele não pôde ser lido. |
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
2. Reescreva `registry.py` com os seus agentes reais.
3. Ajuste `read_state()` em `snapshot.py` se a prova de vida não for arquivo JSON — é o único ponto que sabe de onde o dado vem.
4. Troque o bloco `:root` do `index.html` pelas variáveis de tema do projeto. Nenhuma cor está escrita direto no desenho.
5. Monte as rotas no seu framework, mantendo leitura e execução separadas.

Se algum agente seu não deixa prova de vida nenhuma, adicione antes de mais
nada: gravar um `last_run` ISO no fim de cada rodada é mudança de três linhas e
é o que torna o painel possível.

## Cuidados

- **Não vaze segredo.** O snapshot é estado operacional: nada de token, senha,
  corpo de mensagem ou dado pessoal. "36 e-mails triados" pode; o assunto dos
  e-mails não.
- **Escuta em `127.0.0.1`** por padrão. O mapa dos seus processos internos é
  informação sensível; expondo para fora, ponha atrás da mesma autenticação do
  resto do projeto.
- **Ausência de dado não é "tudo certo".** Sem timestamp, o estado é
  `idle`. Falha de leitura vira `error`, nunca estado vazio — por isso
  `read_state()` distingue `FileNotFoundError` de qualquer outro erro em vez de
  usar um teste genérico de existência, que engoliria permissão negada e mentiria
  "nunca rodou".
