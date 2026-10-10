"""
Monta o snapshot do painel a partir das sondas de prova de vida.

Leitura pura: nenhuma função aqui escreve, executa agente ou faz rede — exceto
`write_state`, que existe só para os agentes demo registrarem a própria rodada.
"""

import json
from datetime import datetime, timezone
from pathlib import Path

import probes
from registry import HEARTBEAT_STALE_MIN, STALE_FACTOR, is_configured

# Ordem de gravidade, do mais grave para o mais inofensivo. Elege o "pior"
# agente do resumo.
SEVERITY = {'error': 4, 'stale': 3, 'idle': 2, 'ok': 1, 'off': 0}


def utc_now():
  return datetime.now(timezone.utc)


def write_state(agent, result, when=None):
  """Grava a prova de vida de um agente demo. Não é usado no modo declarativo —
  lá quem grava é o próprio agente, que é o ponto."""
  when = when or utc_now()
  payload = {
    'last_run': when.isoformat(),
    'ok': bool(result.get('ok', True)),
    'metric': result.get('metric'),
    'detail': result.get('detail'),
  }
  path = Path(agent.liveness['arquivo'])
  path.parent.mkdir(parents=True, exist_ok=True)
  # Grava em temporário e move: um leitor concorrente nunca vê JSON pela metade.
  tmp = path.with_suffix('.tmp')
  # O newline final não é estética: o eclint do projeto varre `tools/` inteiro
  # e não respeita .gitignore, então o estado gravado em runtime também precisa
  # obedecer ao .editorconfig.
  tmp.write_text(json.dumps(payload, ensure_ascii=False) + '\n', encoding='utf-8')
  tmp.replace(path)
  return payload


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
    # O front só precisa saber que existe um disparo e onde bater. O comando
    # real nunca sai do servidor.
    'run': ({
      'url': '/api/agents/%s/run' % agent.id,
      'method': 'POST',
      'body': {'action': 'run'},
    } if agent.tem_disparo else None),
    'arquivo': agent.arquivo,
    'origem': agent.origem,
    'fonte': agent.liveness.get('tipo'),
  }

  # Agente desligado de propósito não é alarme, e nem se tenta sondar: a config
  # de que depende simplesmente não existe.
  if not is_configured(agent):
    entry['state'] = 'off'
    entry['detail'] = 'Desligado: %s não está configurada.' % agent.requires_env
    entry['run'] = None
    return entry

  try:
    vida = probes.run_probe(agent.liveness)
  except probes.ProbeError as exc:
    entry['state'] = 'error'
    entry['detail'] = str(exc)
    return entry

  entry['metric'] = vida.metric
  entry['detail'] = vida.detail

  # Ausência de dado nunca é "tudo certo": é "nunca rodou".
  if vida.last is None:
    entry['detail'] = vida.detail or 'Nenhuma rodada registrada até agora.'
    return entry

  age_min = max(0.0, (now - vida.last).total_seconds() / 60.0)
  entry['last'] = vida.last.isoformat()
  entry['age_min'] = round(age_min, 2)

  if agent.every_min:
    entry['phase'] = round(min(1.0, age_min / agent.every_min), 4)
    entry['next_in_min'] = round(max(0.0, agent.every_min - age_min), 2)

  # A falha registrada pelo próprio agente tem precedência sobre o relógio: um
  # agente que rodou agora e falhou está em erro, não "em dia".
  if not vida.ok:
    entry['state'] = 'error'
    return entry

  if agent.every_min is None:
    entry['state'] = 'stale' if age_min > HEARTBEAT_STALE_MIN else 'ok'
  else:
    entry['state'] = 'stale' if age_min > agent.every_min * STALE_FACTOR else 'ok'
  return entry


def build(agents, modo='demo', now=None):
  """Monta o snapshot completo, no contrato consumido pelo front."""
  now = now or utc_now()
  entradas = [describe(a, now) for a in agents]

  counts = {'ok': 0, 'atencao': 0, 'off': 0}
  worst, worst_rank = None, -1
  for entry in entradas:
    state = entry['state']
    if state == 'ok':
      counts['ok'] += 1
    elif state == 'off':
      counts['off'] += 1
    else:
      counts['atencao'] += 1
    rank = SEVERITY.get(state, 0)
    if rank > worst_rank:
      worst_rank, worst = rank, entry['id']

  return {
    'ok': True,
    'now': now.isoformat(),
    'modo': modo,
    'stale_factor': STALE_FACTOR,
    'agents': entradas,
    'resumo': {
      'total': len(entradas),
      'ok': counts['ok'],
      'atencao': counts['atencao'],
      'off': counts['off'],
      # Só aponta um "pior" se houver de fato algo fora do trilho.
      'pior': worst if worst_rank >= SEVERITY['idle'] else None,
    },
  }
