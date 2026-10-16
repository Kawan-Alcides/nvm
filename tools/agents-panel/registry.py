"""
Registro de agentes.

Dois modos:

1. **Declarativo** (o que você vai usar): um `agents.json` descreve cada agente,
onde ele deixa prova de vida e como forçar uma rodada. Agentes podem morar em
árvores completamente diferentes — `~/.claude`, `~/.agents`, um repo, um
diretório de trabalho. Nenhuma edição de Python por agente.

2. **Demo** (fallback quando não há `agents.json`): um conjunto embutido que faz
trabalho local real sobre este repositório, para o painel ter o que mostrar e
para a suíte de validação ter em que rodar.

Zero rede na montagem do snapshot. Zero IA, sempre.
"""

import hashlib
import json
import os
import re
from collections import namedtuple
from pathlib import Path

PANEL_DIR = Path(__file__).resolve().parent
REPO_ROOT = PANEL_DIR.parents[1]
STATE_DIR = PANEL_DIR / 'state'
CONFIG_PATH = Path(os.environ.get('NVM_PANEL_CONFIG') or (PANEL_DIR / 'agents.json'))

# Um agente é considerado atrasado depois de passar deste múltiplo da própria
# cadência sem rodar. 2,5x dá folga para uma rodada perdida sem gritar à toa.
# Ajuste aqui — este é o único lugar onde o fator vive.
STALE_FACTOR = 2.5

# Agentes contínuos não têm cadência; cobramos deles apenas um heartbeat
# recente. Passando disto sem dar sinal, viram "atrasado".
HEARTBEAT_STALE_MIN = 2.0


class Agent(object):
  """Definição estática de um agente. Nada aqui muda em tempo de execução."""

  def __init__(self, id, nome, icon, faz, every_min, liveness,
      run=None, requires_env=None, auto=True, worker=None, origem=None):
    self.id = id
    self.nome = nome
    self.icon = icon
    self.faz = faz
    self.every_min = every_min  # None => contínuo (só heartbeat)
    self.liveness = liveness    # config da sonda (ver probes.py)
    self.run = run              # como o SERVIDOR executa (cmd ou worker)
    self.requires_env = requires_env
    # auto=False => quem agenda este agente é alguém de fora (CI, launchd,
    # cron da máquina). O painel observa, mas não dirige. É exatamente nesses
    # que o painel costuma revelar que algo parou.
    self.auto = auto
    self.worker = worker        # só no modo demo
    self.origem = origem        # de qual árvore ele veio, para exibição

  @property
  def tem_disparo(self):
    return bool(self.worker or (self.run and self.run.get('cmd')))

  @property
  def arquivo(self):
    """Onde o estado mora, para mostrar no painel."""
    alvo = self.liveness.get('arquivo')
    return Path(alvo).name if alvo else self.liveness.get('tipo', '—')


def is_configured(agent):
  """Um agente sem a config de que depende está desligado de propósito."""
  if agent.requires_env is None:
    return True
  return bool(os.environ.get(agent.requires_env))


# --------------------------------------------------------------------------
# Modo declarativo
# --------------------------------------------------------------------------

# `nome` fica de fora de propósito: quando falta, o carregador usa o `id`.
# Tê-lo aqui fazia a validação recusar por falta de nome antes de chegar no
# defeito real da entrada, escondendo as outras checagens.
CAMPOS_OBRIGATORIOS = ('id', 'faz', 'liveness')


class ConfigError(Exception):
  """A configuração está malformada. Falha alto: um painel que ignora metade
  do próprio inventário é pior que um painel que não sobe."""


def _validate(entrada, indice):
  for campo in CAMPOS_OBRIGATORIOS:
    if campo not in entrada:
      raise ConfigError('agente #%d: falta o campo obrigatório %r' % (indice, campo))
  liveness = entrada['liveness']
  if not isinstance(liveness, dict) or 'tipo' not in liveness:
    raise ConfigError('agente %r: `liveness` precisa de um `tipo`' % entrada['id'])

  run = entrada.get('run')
  if run is not None:
    if not isinstance(run, dict) or 'cmd' not in run:
      raise ConfigError('agente %r: `run` precisa de `cmd`' % entrada['id'])
    if not isinstance(run['cmd'], list) or not run['cmd']:
      # Lista de argumentos, nunca string: nada passa por shell, então um
      # caminho com espaço ou um `;` perdido não vira execução extra.
      raise ConfigError('agente %r: `run.cmd` tem de ser uma lista de argumentos'
                        % entrada['id'])


Config = namedtuple('Config', 'agents sistema')

# Como o núcleo da constelação se chama quando a config não diz. "NVM" só faz
# sentido no conjunto demo; num painel dos agentes de outra pessoa, o centro é
# o sistema dela.
SISTEMA_PADRAO = 'AGENTES'


def load_config(path=None):
  """Lê `agents.json`. Devolve None se não existir — aí vale o modo demo."""
  path = Path(path or CONFIG_PATH)
  try:
    raw = path.read_text(encoding='utf-8')
  except FileNotFoundError:
    return None
  except OSError as exc:
    raise ConfigError('não foi possível ler %s: %s' % (path, exc.strerror or exc))

  try:
    data = json.loads(raw)
  except ValueError as exc:
    raise ConfigError('%s não é JSON válido: %s' % (path, exc))

  entradas = data.get('agents') if isinstance(data, dict) else data
  if not isinstance(entradas, list):
    raise ConfigError('%s: esperava uma lista em `agents`' % path)

  agentes = []
  vistos = set()
  for i, entrada in enumerate(entradas):
    if not isinstance(entrada, dict):
      raise ConfigError('agente #%d: esperava um objeto' % i)
    _validate(entrada, i)
    if entrada['id'] in vistos:
      raise ConfigError('id repetido: %r' % entrada['id'])
    vistos.add(entrada['id'])
    agentes.append(Agent(
      id=entrada['id'],
      nome=entrada.get('nome') or entrada['id'],
      icon=entrada.get('icon') or '•',
      faz=entrada['faz'],
      every_min=entrada.get('every_min'),
      liveness=entrada['liveness'],
      run=entrada.get('run'),
      requires_env=entrada.get('requires_env'),
      # Agente declarado em config é observado, não dirigido: quem o agenda é
      # o cron/launchd/CI da máquina. O painel só olha.
      auto=False,
      origem=entrada.get('origem'),
    ))
  sistema = SISTEMA_PADRAO
  if isinstance(data, dict) and data.get('sistema'):
    # Vai para dentro do SVG como texto; um nome quilométrico estoura o núcleo.
    sistema = str(data['sistema'])[:14]
  return Config(agentes, sistema)


# --------------------------------------------------------------------------
# Modo demo
# --------------------------------------------------------------------------

def _demo_liveness(arquivo):
  return {
    'tipo': 'json_field',
    'arquivo': str(STATE_DIR / arquivo),
    'campo': 'last_run',
    'ok_campo': 'ok',
    'metric_campo': 'metric',
    'detail_campo': 'detail',
  }


def _count_files(directory):
  return sum(1 for entry in directory.iterdir() if entry.is_file())


def _plural(count, singular, plural):
  return '%d %s' % (count, singular if count == 1 else plural)


def work_mirror_index():
  """Indexa os mocks de plataforma que substituem o índice remoto nos testes."""
  total = _count_files(REPO_ROOT / 'test' / 'mocks')
  return {'ok': True, 'metric': '%d entradas indexadas' % total,
          'detail': 'Índice reconstruído a partir de test/mocks.'}


def work_cache_sweeper():
  """Varre o diretório de estado do painel atrás de artefatos velhos."""
  scanned = _count_files(STATE_DIR) if STATE_DIR.is_dir() else 0
  return {'ok': True,
          'metric': _plural(scanned, 'arquivo varrido', 'arquivos varridos'),
          'detail': 'Nenhum artefato acima do limite de idade.'}


# Apelidos de versão que o nvm implementa nativamente. `stable`/`unstable` vêm
# do plano de release pré-1.0 do node e hoje são legados, mas ainda precisam
# ser reconhecidos.
BUILTIN_ALIASES = ('default', 'node', 'iojs', 'stable', 'unstable')


def work_alias_reconciler():
  """
  Confere se o nvm.sh ainda reconhece cada apelido embutido.

  Conta verificações que passaram — não ocorrências de texto. A distinção
  importa: contar call sites de `nvm_make_alias` daria um número real com um
  rótulo mentiroso, que é o tipo de métrica que corrói a confiança no painel.
  """
  source = (REPO_ROOT / 'nvm.sh').read_text(encoding='utf-8', errors='replace')
  found = [
    n for n in BUILTIN_ALIASES if re.search(r'([\'"])%s\1' % re.escape(n), source)]
  missing = [n for n in BUILTIN_ALIASES if n not in found]
  if missing:
    return {'ok': False, 'metric': '%d/%d aliases' % (len(found), len(BUILTIN_ALIASES)),
            'detail': 'Apelidos sem tratamento no nvm.sh: %s.' % ', '.join(missing)}
  return {'ok': True,
          'metric': '%d/%d aliases conferidos' % (len(found), len(BUILTIN_ALIASES)),
          'detail': 'Todos os apelidos embutidos seguem reconhecidos.'}


def work_shasum_verifier():
  """
  Confere o nvm.sh contra um arquivo de checksum publicado.

  Este agente falha de propósito: o arquivo nvm.sh.sha256 não existe neste
  repositório. A falha é genuína (não é um estado "error" chumbado), e serve
  para provar que o painel pinta erro de verdade.
  """
  digest = hashlib.sha256((REPO_ROOT / 'nvm.sh').read_bytes()).hexdigest()
  try:
    expected = (REPO_ROOT / 'nvm.sh.sha256').read_text(encoding='utf-8').split()[0]
  except FileNotFoundError:
    return {'ok': False, 'metric': 'checksum sem par',
            'detail': 'nvm.sh.sha256 não existe no repositório; nada para comparar.'}
  if expected != digest:
    return {'ok': False, 'metric': 'checksum divergente',
            'detail': 'nvm.sh não bate com o checksum publicado.'}
  return {'ok': True, 'metric': 'checksum confere', 'detail': 'nvm.sh íntegro.'}


def work_docs_toc():
  """Recalcula o sumário do README."""
  readme = (REPO_ROOT / 'README.md').read_text(encoding='utf-8', errors='replace')
  headings = re.findall(r'(?m)^#{2,4}\s+\S', readme)
  return {'ok': True, 'metric': '%d seções mapeadas' % len(headings),
          'detail': 'Sumário do README recalculado.'}


def work_codeql_scan():
  """
  Lê a cadência real do único processo agendado de verdade deste repositório.

  O que ele NÃO sabe é se a última rodada na nuvem passou — isso exigiria a API
  do GitHub, que é rede externa e está proibida na montagem do snapshot.
  """
  text = (REPO_ROOT / '.github' / 'workflows' / 'codeql-analysis.yml').read_text(encoding='utf-8')
  match = re.search(r"cron:\s*'([^']+)'", text)
  return {'ok': True, 'metric': 'cron %s' % (match.group(1) if match else 'desconhecido'),
          'detail': 'Cadência lida do workflow. O resultado da rodada mora no GitHub.'}


def work_heartbeat():
  """Pulso de vida do próprio servidor do painel."""
  return {'ok': True, 'metric': 'servidor vivo',
          'detail': 'Processo do painel respondendo.'}


def work_release_publisher():
  """Nunca executa sem credencial — existe para o estado 'desligado'."""
  return {'ok': True, 'metric': 'publicação simulada',
          'detail': 'Rodada de publicação concluída.'}


def demo_agents():
  """Conjunto embutido. Cadências simuladas, trabalho e métricas reais."""
  return [
    Agent('mirror-index', 'ÍNDICE DE MIRRORS', '📡',
          'Reconstrói a lista de versões do Node disponíveis para download.',
          3, _demo_liveness('mirror-index.json'), worker=work_mirror_index),
    Agent('cache-sweeper', 'FAXINA DE CACHE', '🧹',
          'Apaga tarballs e fontes velhas do cache de download.',
          7, _demo_liveness('cache-sweeper.json'), worker=work_cache_sweeper),
    Agent('alias-reconciler', 'ALIASES', '🔗',
          'Confere se os apelidos de versão apontam para onde deveriam.',
          5, _demo_liveness('alias-reconciler.json'), worker=work_alias_reconciler),
    Agent('shasum-verifier', 'CHECKSUMS', '🔐',
          'Confere a integridade do nvm.sh contra o checksum publicado.',
          11, _demo_liveness('shasum-verifier.json'), worker=work_shasum_verifier),
    Agent('docs-toc', 'SUMÁRIO DOS DOCS', '📚',
          'Regenera o sumário do README quando as seções mudam.',
          30, _demo_liveness('docs-toc.json'), worker=work_docs_toc, auto=False),
    Agent('codeql-scan', 'VARREDURA CODEQL', '🛡',
          'Varredura semanal de segurança do código (roda no GitHub Actions).',
          7 * 24 * 60, _demo_liveness('codeql-scan.json'),
          # Sem disparo: quem agenda é o GitHub, não nós.
          worker=None, auto=False),
    Agent('heartbeat', 'BATIMENTO', '💓',
          'Pulso contínuo que prova que o próprio servidor do painel está de pé.',
          None, _demo_liveness('heartbeat.json'), worker=work_heartbeat),
    Agent('release-publisher', 'PUBLICAÇÃO', '📦',
          'Publica uma nova versão no registro de pacotes.',
          60, _demo_liveness('release-publisher.json'),
          worker=work_release_publisher, auto=False,
          requires_env='NVM_PANEL_RELEASE_TOKEN'),
  ]


# --------------------------------------------------------------------------

def load_agents(path=None):
  """Config declarativa quando existir; conjunto demo caso contrário."""
  cfg = load_config(path)
  if cfg is None:
    return demo_agents(), 'demo', 'NVM'
  if not cfg.agents:
    raise ConfigError('a configuração não declara nenhum agente')
  return cfg.agents, 'config', cfg.sistema
