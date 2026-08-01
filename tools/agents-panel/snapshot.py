"""
Leitura do estado dos agentes em disco e montagem do snapshot do painel.

Este é o núcleo portátil: é o único arquivo que precisa mudar quando a prova
de vida sair de arquivos JSON e for para um banco. Nada aqui faz rede, nada
chama IA, e nada tem efeito colateral — é leitura pura.
"""

import errno
import json
from datetime import datetime, timezone

from registry import (
  AGENTS,
  HEARTBEAT_STALE_MIN,
  STALE_FACTOR,
  STATE_DIR,
  is_configured,
)

# Ordem de gravidade, do mais grave para o mais inofensivo. Usada para eleger
# o "pior" agente do resumo.
SEVERITY = {
  'error': 4,
  'stale': 3,
  'idle': 2,
  'ok': 1,
  'off': 0,
}


def utc_now():
  return datetime.now(timezone.utc)


def write_state(agent, result, when=None):
  """Grava a prova de vida de um agente. Chamado pelo agendador e pelo botão."""
  when = when or utc_now()
  payload = {
    'last_run': when.isoformat(),
    'ok': bool(result.get('ok', True)),
    'metric': result.get('metric'),
    'detail': result.get('detail'),
  }
  STATE_DIR.mkdir(parents=True, exist_ok=True)
  # Grava em arquivo temporário e move: um leitor concorrente nunca vê JSON
  # pela metade.
  tmp = agent.state_path.with_suffix('.tmp')
  # O newline final não é estética: o eclint do projeto varre `tools/` inteiro
  # e não respeita .gitignore, então o estado gravado em runtime também precisa
  # obedecer ao .editorconfig.
  tmp.write_text(json.dumps(payload, ensure_ascii=False) + '\n', encoding='utf-8')
  tmp.replace(agent.state_path)
  return payload


class StateReadError(Exception):
  """A prova de vida existe mas não pôde ser lida. Isso é erro, não vazio."""


def read_state(agent):
  """
  Lê a prova de vida de um agente.

  Devolve None SÓ quando o arquivo comprovadamente não existe (instalação
  nova). Qualquer outra falha — permissão negada, disco fora, JSON corrompido —
  vira StateReadError e sobe como estado de erro no painel.

  Repare que não há um `if path.exists()` aqui de propósito: aquele teste
  devolve False para permissão negada também, e o painel passaria a mentir
  "nunca rodou" quando na verdade não conseguiu ler.
  """
  try:
    raw = agent.state_path.read_text(encoding='utf-8')
  except FileNotFoundError:
    return None
  except OSError as exc:
    if exc.errno == errno.ENOENT:
      return None
    raise StateReadError('não foi possível ler %s: %s' % (agent.arquivo, exc.strerror))

  try:
    parsed = json.loads(raw)
  except ValueError as exc:
    raise StateReadError('estado corrompido em %s: %s' % (agent.arquivo, exc))

  if not isinstance(parsed, dict):
    raise StateReadError('estado inesperado em %s: não é um objeto' % agent.arquivo)
  return parsed


def _parse_timestamp(value):
  if not value:
    return None
  try:
    parsed = datetime.fromisoformat(value)
  except (TypeError, ValueError):
    raise StateReadError('timestamp ilegível: %r' % (value,))
  if parsed.tzinfo is None:
    parsed = parsed.replace(tzinfo=timezone.utc)
  return parsed


def describe(agent, now=None):
  """Monta a entrada de um agente no snapshot."""
  now = now or utc_now()

  entry = {
    'id': agent.id,
    'nome': agent.nome,
    'icon': agent.icon,
    'faz': agent.faz,
    'every_min': agent.every_min,
    'last': None,
    'age_min': None,
    'next_in_min': None,
    'phase': None,
    'state': 'idle',
    'metric': None,
    'detail': None,
    'run': agent.run,
    'arquivo': agent.arquivo,
  }

  # Agente desligado de propósito não é alarme, e nem se tenta ler o estado
  # dele — a config de que depende simplesmente não existe.
  if not is_configured(agent):
    entry['state'] = 'off'
    entry['detail'] = 'Desligado: %s não está configurada.' % agent.requires_env
    entry['run'] = None
    return entry

  try:
    state = read_state(agent)
  except StateReadError as exc:
    entry['state'] = 'error'
    entry['detail'] = str(exc)
    return entry

  # Ausência de dado nunca é "tudo certo": é "nunca rodou".
  if state is None:
    entry['detail'] = 'Nenhuma rodada registrada até agora.'
    return entry

  entry['metric'] = state.get('metric')
  entry['detail'] = state.get('detail')

  try:
    last = _parse_timestamp(state.get('last_run'))
  except StateReadError as exc:
    entry['state'] = 'error'
    entry['detail'] = str(exc)
    return entry

  if last is None:
    entry['detail'] = state.get('detail') or 'Nenhuma rodada registrada até agora.'
    return entry

  age_min = max(0.0, (now - last).total_seconds() / 60.0)
  entry['last'] = last.isoformat()
  entry['age_min'] = round(age_min, 2)

  if agent.every_min:
    entry['phase'] = round(min(1.0, age_min / agent.every_min), 4)
    entry['next_in_min'] = round(max(0.0, agent.every_min - age_min), 2)

  # A falha registrada pelo próprio agente ganha precedência sobre o relógio:
  # um agente que rodou agora e falhou está em erro, não "em dia".
  if not state.get('ok', True):
    entry['state'] = 'error'
    return entry

  if agent.every_min is None:
    entry['state'] = 'stale' if age_min > HEARTBEAT_STALE_MIN else 'ok'
  else:
    entry['state'] = 'stale' if age_min > agent.every_min * STALE_FACTOR else 'ok'
  return entry


def build(now=None):
  """Monta o snapshot completo, no contrato consumido pelo front."""
  now = now or utc_now()
  agents = [describe(agent, now) for agent in AGENTS]

  counts = {'ok': 0, 'atencao': 0, 'off': 0}
  worst = None
  worst_rank = -1
  for entry in agents:
    state = entry['state']
    if state == 'ok':
      counts['ok'] += 1
    elif state == 'off':
      counts['off'] += 1
    else:
      counts['atencao'] += 1

    rank = SEVERITY.get(state, 0)
    if rank > worst_rank:
      worst_rank = rank
      worst = entry['id']

  return {
    'ok': True,
    'now': now.isoformat(),
    'stale_factor': STALE_FACTOR,
    'agents': agents,
    'resumo': {
      'total': len(agents),
      'ok': counts['ok'],
      'atencao': counts['atencao'],
      'off': counts['off'],
      # Só aponta um "pior" se houver de fato algo fora do trilho.
      'pior': worst if worst_rank >= SEVERITY['idle'] else None,
    },
  }
