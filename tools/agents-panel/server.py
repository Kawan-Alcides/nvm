#!/usr/bin/env python3
"""
Servidor do Painel de Agentes — só biblioteca padrão do Python.

  python3 tools/agents-panel/server.py
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
"""

import argparse
import json
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

import snapshot  # noqa: E402
from registry import (  # noqa: E402
  AGENTS,
  AGENTS_BY_ID,
  PANEL_DIR,
  STATE_DIR,
  is_configured,
)

STATIC_DIR = PANEL_DIR / 'static'

# De quanto em quanto tempo o agendador local acorda para ver quem está devendo
# rodada. Não é a cadência dos agentes — é só a granularidade do relógio.
SCHEDULER_TICK_SEC = 5.0

# Cadência do agente contínuo (que não tem cadência declarada, só heartbeat).
HEARTBEAT_TICK_SEC = 20.0

# Idades iniciais, em minutos, usadas só na primeira subida, para que o painel
# já nasça com fases visivelmente diferentes umas das outras.
SEED_AGES_MIN = {
  'mirror-index': 0.6,
  'cache-sweeper': 3.9,
  'alias-reconciler': 4.7,
  'docs-toc': 95.0,
  'codeql-scan': 2 * 24 * 60.0,
}

_run_lock = threading.Lock()


def run_agent(agent):
  """Executa um agente de verdade e grava a prova de vida."""
  with _run_lock:
    try:
      result = agent.worker()
    except Exception as exc:  # noqa: BLE001 - a falha do agente é o dado
      result = {
        'ok': False,
        'metric': 'falhou',
        'detail': '%s: %s' % (type(exc).__name__, exc),
      }
    return snapshot.write_state(agent, result)


def seed_initial_state():
  """
  Semeia estados na primeira subida, para o painel não nascer todo vazio.

  Só grava o que ainda não existe: estado real de rodadas anteriores é
  preservado entre reinícios.
  """
  STATE_DIR.mkdir(parents=True, exist_ok=True)
  now = snapshot.utc_now()
  for agent in AGENTS:
    if agent.state_path.exists() or not is_configured(agent):
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
      if not agent.auto or not is_configured(agent):
        continue
      try:
        state = snapshot.read_state(agent)
      except snapshot.StateReadError:
        # Estado ilegível é problema do painel mostrar, não do agendador
        # resolver. Ele tenta rodar de novo na próxima volta.
        state = None

      due_min = (
        HEARTBEAT_TICK_SEC / 60.0 if agent.every_min is None else agent.every_min
      )
      if state is None or not state.get('last_run'):
        run_agent(agent)
        continue
      try:
        last = snapshot._parse_timestamp(state['last_run'])
      except snapshot.StateReadError:
        run_agent(agent)
        continue
      if (now - last).total_seconds() / 60.0 >= due_min:
        run_agent(agent)


class Handler(BaseHTTPRequestHandler):
  server_version = 'AgentsPanel/1.0'

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
      self._send_json(snapshot.build())
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
      # Também leitura pura — devolve o que está em disco, nada mais.
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

    agent_id = route[len('/api/agents/'):-len('/run')]
    agent = AGENTS_BY_ID.get(agent_id)
    if agent is None:
      self._send_json({'ok': False, 'erro': 'agente desconhecido'}, status=404)
      return

    if agent.run is None:
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

    written = run_agent(agent)
    status = 200 if written.get('ok', True) else 500
    self._send_json({
      'ok': bool(written.get('ok', True)),
      'agent': snapshot.describe(agent),
    }, status=status)


def main():
  parser = argparse.ArgumentParser(description='Painel de Agentes (protótipo)')
  parser.add_argument('--host', default='127.0.0.1',
                      help='endereço de escuta (padrão: 127.0.0.1)')
  parser.add_argument('--port', type=int, default=7777,
                      help='porta (padrão: 7777)')
  parser.add_argument('--no-seed', action='store_true',
                      help='não semeia estados iniciais')
  parser.add_argument('--no-scheduler', action='store_true',
                      help='não sobe o agendador local')
  args = parser.parse_args()

  if not args.no_seed:
    seed_initial_state()

  stop_event = threading.Event()
  if not args.no_scheduler:
    thread = threading.Thread(target=scheduler_loop, args=(stop_event,),
                              daemon=True, name='agents-scheduler')
    thread.start()

  httpd = ThreadingHTTPServer((args.host, args.port), Handler)
  sys.stderr.write(
    'Painel de Agentes em http://%s:%d  (Ctrl+C para sair)\n' % (args.host, args.port)
  )
  try:
    httpd.serve_forever()
  except KeyboardInterrupt:
    sys.stderr.write('\nencerrando…\n')
  finally:
    stop_event.set()
    httpd.server_close()


if __name__ == '__main__':
  main()
