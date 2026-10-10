#!/usr/bin/env python3
"""
Servidor do Painel de Agentes — só biblioteca padrão do Python.

  python3 tools/agents-panel/server.py
  python3 tools/agents-panel/server.py --config ~/meus-agentes.json
  # abre em http://127.0.0.1:7777

Rotas:
  GET  /                       página do painel
  GET  /api/agents             snapshot de todos os agentes (LEITURA PURA)
  GET  /api/agents/<id>        snapshot de um agente     (LEITURA PURA)
  POST /api/agents/<id>/run    EXECUTA o agente

A separação entre as duas últimas é o ponto mais importante do arquivo. A rota
GET nunca dispara nada; se o botão do painel apontasse para ela, diria
"concluído" sem ter executado coisa alguma.

Segurança: escuta em 127.0.0.1 por padrão. O snapshot é estado operacional —
não carrega token, credencial, corpo de mensagem nem dado pessoal. O mapa dos
seus processos internos é informação sensível; se um dia expuser isto para
fora da máquina, ponha atrás da mesma autenticação do resto do projeto.

Sobre `run.cmd`: a configuração é superfície executável. Quem escreve o
`agents.json` escolhe o que o botão roda, com os privilégios de quem subiu o
servidor. Comandos são sempre uma lista de argumentos executada sem shell, mas
isso protege contra acidente de quoting, não contra uma configuração hostil —
trate `agents.json` com o mesmo cuidado de um script seu.
"""

import argparse
import json
import subprocess
import sys
import threading
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# Sem bytecode: o eclint do projeto varre `tools/` inteiro e não respeita
# .gitignore, então um __pycache__/ criado ao rodar o painel faria
# `npm run eclint` falhar no repositório de quem só quis dar uma olhada.
sys.dont_write_bytecode = True

sys.path.insert(0, str(Path(__file__).resolve().parent))

import probes  # noqa: E402
import snapshot  # noqa: E402
from registry import (  # noqa: E402
  PANEL_DIR,
  STATE_DIR,
  ConfigError,
  is_configured,
  load_agents,
)

STATIC_DIR = PANEL_DIR / 'static'

# De quanto em quanto tempo o agendador local acorda para ver quem está devendo
# rodada. Não é a cadência dos agentes — é só a granularidade do relógio.
SCHEDULER_TICK_SEC = 5.0

# Cadência do agente contínuo (que não declara cadência, só heartbeat).
HEARTBEAT_TICK_SEC = 20.0

# Teto de tempo para um disparo manual, quando a config não disser outro.
DEFAULT_RUN_TIMEOUT_SEC = 120

# Idades iniciais, em minutos, usadas só na primeira subida do modo demo, para
# o painel já nascer com fases visivelmente diferentes umas das outras.
SEED_AGES_MIN = {
  'mirror-index': 0.6,
  'cache-sweeper': 3.9,
  'alias-reconciler': 4.7,
  'docs-toc': 95.0,
  'codeql-scan': 2 * 24 * 60.0,
}

AGENTS = []
AGENTS_BY_ID = {}
MODO = 'demo'

_run_lock = threading.Lock()


def run_agent(agent):
  """Executa um agente de verdade. Worker Python (demo) ou comando (config)."""
  with _run_lock:
    if agent.worker is not None:
      try:
        result = agent.worker()
      except Exception as exc:  # noqa: BLE001 - a falha do agente é o dado
        result = {'ok': False, 'metric': 'falhou',
                  'detail': '%s: %s' % (type(exc).__name__, exc)}
      snapshot.write_state(agent, result)
      return result

    cmd = agent.run['cmd']
    timeout = agent.run.get('timeout_s', DEFAULT_RUN_TIMEOUT_SEC)
    cwd = agent.run.get('cwd')
    try:
      proc = subprocess.run(
        cmd,
        cwd=str(Path(cwd).expanduser()) if cwd else None,
        capture_output=True, text=True, timeout=timeout,
        # Nunca shell=True: a lista de argumentos vai direto para execve.
        shell=False,
      )
    except subprocess.TimeoutExpired:
      return {'ok': False, 'metric': 'tempo esgotado',
              'detail': 'O comando passou de %ss e foi interrompido.' % timeout}
    except (OSError, ValueError) as exc:
      return {'ok': False, 'metric': 'não executou',
              'detail': '%s: %s' % (type(exc).__name__, exc)}

    if proc.returncode != 0:
      cauda = (proc.stderr or proc.stdout or '').strip().splitlines()
      return {'ok': False, 'metric': 'saiu com %d' % proc.returncode,
              'detail': cauda[-1][:200] if cauda else 'Sem saída.'}
    # O agente é quem grava a própria prova de vida; o painel não inventa uma.
    # Se o timestamp não mudar depois disto, o problema é do agente — e o
    # painel vai mostrar isso em vez de esconder.
    return {'ok': True, 'metric': 'disparado', 'detail': 'Comando concluído.'}


def seed_initial_state():
  """
  Semeia estados na primeira subida do modo demo.

  Só grava o que ainda não existe: estado real de rodadas anteriores é
  preservado entre reinícios.
  """
  STATE_DIR.mkdir(parents=True, exist_ok=True)
  now = snapshot.utc_now()
  for agent in AGENTS:
    if agent.worker is None or not is_configured(agent):
      continue
    if Path(agent.liveness['arquivo']).exists():
      continue
    age = SEED_AGES_MIN.get(agent.id)
    if age is None:
      continue
    try:
      result = agent.worker()
    except Exception as exc:  # noqa: BLE001
      result = {'ok': False, 'metric': 'falhou', 'detail': str(exc)}
    snapshot.write_state(agent, result, when=now - timedelta(minutes=age))


def scheduler_loop(stop_event):
  """Agendador local: roda os agentes marcados com auto=True quando vencem."""
  while not stop_event.wait(SCHEDULER_TICK_SEC):
    now = snapshot.utc_now()
    for agent in AGENTS:
      if not agent.auto or not is_configured(agent) or agent.worker is None:
        continue
      try:
        vida = probes.run_probe(agent.liveness)
      except probes.ProbeError:
        # Estado ilegível é problema do painel mostrar, não do agendador
        # resolver. Ele tenta de novo na próxima volta.
        vida = None

      due_min = (
        HEARTBEAT_TICK_SEC / 60.0 if agent.every_min is None else agent.every_min)
      if vida is None or vida.last is None:
        run_agent(agent)
      elif (now - vida.last).total_seconds() / 60.0 >= due_min:
        run_agent(agent)


class Handler(BaseHTTPRequestHandler):
  server_version = 'AgentsPanel/2.0'

  def log_message(self, fmt, *args):
    sys.stderr.write('[painel] %s - %s\n' % (self.address_string(), fmt % args))

  def _send_json(self, payload, status=200):
    body = json.dumps(payload, ensure_ascii=False).encode('utf-8')
    self.send_response(status)
    self.send_header('Content-Type', 'application/json; charset=utf-8')
    self.send_header('Content-Length', str(len(body)))
    self.send_header('Cache-Control', 'no-store')
    self.end_headers()
    self.wfile.write(body)

  def _send_file(self, path, content_type):
    try:
      body = path.read_bytes()
    except FileNotFoundError:
      self._send_json({'ok': False, 'erro': 'arquivo não encontrado'}, status=404)
      return
    self.send_response(200)
    self.send_header('Content-Type', content_type)
    self.send_header('Content-Length', str(len(body)))
    self.send_header('Cache-Control', 'no-store')
    self.end_headers()
    self.wfile.write(body)

  def do_GET(self):
    route = self.path.split('?', 1)[0].rstrip('/') or '/'

    if route in ('/', '/index.html'):
      self._send_file(STATIC_DIR / 'index.html', 'text/html; charset=utf-8')
      return

    if route == '/api/agents':
      # LEITURA PURA. Nenhum agente é executado aqui, em nenhuma hipótese.
      self._send_json(snapshot.build(AGENTS, MODO))
      return

    if route.startswith('/api/agents/'):
      rest = route[len('/api/agents/'):]
      if rest.endswith('/run'):
        self._send_json({
          'ok': False,
          'erro': 'esta rota executa o agente e só aceita POST',
        }, status=405)
        return
      agent = AGENTS_BY_ID.get(rest)
      if agent is None:
        self._send_json({'ok': False, 'erro': 'agente desconhecido'}, status=404)
        return
      # Também leitura pura — devolve o que a sonda lê, nada mais.
      self._send_json({
        'ok': True,
        'now': snapshot.utc_now().isoformat(),
        'agent': snapshot.describe(agent),
      })
      return

    self._send_json({'ok': False, 'erro': 'rota desconhecida'}, status=404)

  def do_POST(self):
    route = self.path.split('?', 1)[0].rstrip('/')

    if not (route.startswith('/api/agents/') and route.endswith('/run')):
      self._send_json({'ok': False, 'erro': 'rota desconhecida'}, status=404)
      return

    agent = AGENTS_BY_ID.get(route[len('/api/agents/'):-len('/run')])
    if agent is None:
      self._send_json({'ok': False, 'erro': 'agente desconhecido'}, status=404)
      return

    if not agent.tem_disparo:
      self._send_json({
        'ok': False,
        'erro': 'este agente não pode ser forçado a partir do painel',
      }, status=409)
      return

    if not is_configured(agent):
      self._send_json({
        'ok': False,
        'erro': 'agente desligado: %s não está configurada' % agent.requires_env,
      }, status=409)
      return

    # Consome o corpo para não deixar a conexão suja, mas não depende dele.
    length = int(self.headers.get('Content-Length') or 0)
    if length:
      self.rfile.read(length)

    resultado = run_agent(agent)
    ok = bool(resultado.get('ok', True))
    self._send_json({
      'ok': ok,
      'erro': None if ok else resultado.get('detail'),
      'agent': snapshot.describe(agent),
    }, status=200 if ok else 500)


def main():
  global AGENTS, AGENTS_BY_ID, MODO

  parser = argparse.ArgumentParser(description='Painel de Agentes')
  parser.add_argument('--config', help='caminho do agents.json')
  parser.add_argument('--host', default='127.0.0.1',
                      help='endereço de escuta (padrão: 127.0.0.1)')
  parser.add_argument('--port', type=int, default=7777, help='porta (padrão: 7777)')
  parser.add_argument('--no-seed', action='store_true',
                      help='não semeia estados iniciais (modo demo)')
  parser.add_argument('--no-scheduler', action='store_true',
                      help='não sobe o agendador local')
  args = parser.parse_args()

  try:
    AGENTS, MODO = load_agents(args.config)
  except ConfigError as exc:
    # Falha alto: um painel que ignora metade do próprio inventário é pior que
    # um painel que não sobe.
    sys.stderr.write('configuração inválida: %s\n' % exc)
    return 2

  AGENTS_BY_ID = dict((a.id, a) for a in AGENTS)
  sys.stderr.write('modo: %s — %d agentes\n' % (MODO, len(AGENTS)))

  if MODO == 'demo' and not args.no_seed:
    seed_initial_state()

  stop_event = threading.Event()
  if not args.no_scheduler:
    threading.Thread(
      target=scheduler_loop, args=(stop_event,),
      daemon=True, name='agents-scheduler').start()

  httpd = ThreadingHTTPServer((args.host, args.port), Handler)
  sys.stderr.write(
    'Painel de Agentes em http://%s:%d  (Ctrl+C para sair)\n'
    % (args.host, args.port))
  try:
    httpd.serve_forever()
  except KeyboardInterrupt:
    sys.stderr.write('\nencerrando…\n')
  finally:
    stop_event.set()
    httpd.server_close()
  return 0


if __name__ == '__main__':
  sys.exit(main())
