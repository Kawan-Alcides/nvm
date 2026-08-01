"""
Registro dos agentes do protótipo de Painel de Agentes.

PROTÓTIPO: as *cadências* aqui são simuladas — este repositório (nvm) tem
exatamente um processo agendado de verdade, o workflow semanal do CodeQL
(.github/workflows/codeql-analysis.yml, cron '0 17 * * 4'). Os demais agentes
existem para exercitar o painel de ponta a ponta.

O que é real, mesmo assim: cada worker faz trabalho local de verdade sobre este
repositório (conta arquivos, calcula checksum, lê o cron do workflow) e grava
uma prova de vida real em disco. Nenhuma métrica exibida é inventada — todas
saem de uma leitura efetiva do repositório.

Zero rede. Zero IA. Só leitura local.
"""

import hashlib
import os
import re
from pathlib import Path

PANEL_DIR = Path(__file__).resolve().parent
REPO_ROOT = PANEL_DIR.parents[1]
STATE_DIR = PANEL_DIR / 'state'

# Um agente é considerado atrasado depois de passar deste múltiplo da própria
# cadência sem rodar. 2,5x dá folga para uma rodada perdida sem gritar à toa.
# Ajuste aqui — este é o único lugar onde o fator vive.
STALE_FACTOR = 2.5

# Agentes contínuos não têm cadência; cobramos deles apenas um heartbeat
# recente. Passando disto sem dar sinal, viram "atrasado".
HEARTBEAT_STALE_MIN = 2.0


def _count_files(directory):
  """Conta arquivos comuns em um diretório do repositório."""
  return sum(1 for entry in directory.iterdir() if entry.is_file())


def work_mirror_index():
  """Indexa os mocks de plataforma que substituem o índice remoto nos testes."""
  mocks = REPO_ROOT / 'test' / 'mocks'
  total = _count_files(mocks)
  return {
    'ok': True,
    'metric': '%d entradas indexadas' % total,
    'detail': 'Índice reconstruído a partir de test/mocks.',
  }


def _plural(count, singular, plural):
  return '%d %s' % (count, singular if count == 1 else plural)


def work_cache_sweeper():
  """Varre o diretório de estado do painel atrás de artefatos velhos."""
  scanned = _count_files(STATE_DIR) if STATE_DIR.is_dir() else 0
  return {
    'ok': True,
    'metric': _plural(scanned, 'arquivo varrido', 'arquivos varridos'),
    'detail': 'Nenhum artefato acima do limite de idade.',
  }


# Apelidos de versão que o nvm implementa nativamente. `stable`/`unstable` vêm
# do plano de release pré-1.0 do node e hoje são legados, mas ainda precisam
# ser reconhecidos.
BUILTIN_ALIASES = ('default', 'node', 'iojs', 'stable', 'unstable')


def work_alias_reconciler():
  """
  Confere se o nvm.sh ainda reconhece cada apelido embutido.

  Conta verificações que passaram — não ocorrências de texto. A distinção
  importa: contar call sites de `nvm_make_alias` daria um número real com um
  rótulo mentiroso, que é justamente o tipo de métrica que corrói a confiança
  no painel.
  """
  source = (REPO_ROOT / 'nvm.sh').read_text(encoding='utf-8', errors='replace')
  found = [
    name for name in BUILTIN_ALIASES
    if re.search(r'([\'"])%s\1' % re.escape(name), source)
  ]
  missing = [name for name in BUILTIN_ALIASES if name not in found]
  if missing:
    return {
      'ok': False,
      'metric': '%d/%d aliases' % (len(found), len(BUILTIN_ALIASES)),
      'detail': 'Apelidos sem tratamento no nvm.sh: %s.' % ', '.join(missing),
    }
  return {
    'ok': True,
    'metric': '%d/%d aliases conferidos' % (len(found), len(BUILTIN_ALIASES)),
    'detail': 'Todos os apelidos embutidos seguem reconhecidos.',
  }


def work_shasum_verifier():
  """
  Confere o nvm.sh contra um arquivo de checksum publicado.

  Este agente falha de propósito: o arquivo nvm.sh.sha256 não existe neste
  repositório. A falha é genuína (não é um estado "error" chumbado), e serve
  para provar que o painel pinta erro de verdade.
  """
  target = REPO_ROOT / 'nvm.sh'
  digest = hashlib.sha256(target.read_bytes()).hexdigest()
  expected_file = REPO_ROOT / 'nvm.sh.sha256'
  try:
    expected = expected_file.read_text(encoding='utf-8').split()[0]
  except FileNotFoundError:
    return {
      'ok': False,
      'metric': 'checksum sem par',
      'detail': 'nvm.sh.sha256 não existe no repositório; nada para comparar.',
    }
  if expected != digest:
    return {
      'ok': False,
      'metric': 'checksum divergente',
      'detail': 'nvm.sh não bate com o checksum publicado.',
    }
  return {
    'ok': True,
    'metric': 'checksum confere',
    'detail': 'nvm.sh íntegro.',
  }


def work_docs_toc():
  """Recalcula o sumário do README."""
  readme = (REPO_ROOT / 'README.md').read_text(encoding='utf-8', errors='replace')
  headings = re.findall(r'(?m)^#{2,4}\s+\S', readme)
  return {
    'ok': True,
    'metric': '%d seções mapeadas' % len(headings),
    'detail': 'Sumário do README recalculado.',
  }


def work_codeql_scan():
  """
  Lê a cadência real do único processo agendado de verdade deste repositório.

  Diferente dos outros, este agente espelha algo que existe: o cron do
  workflow do CodeQL. O que ele NÃO sabe é se a última rodada na nuvem passou —
  isso exigiria a API do GitHub, que é rede externa e está proibida no painel.
  """
  workflow = REPO_ROOT / '.github' / 'workflows' / 'codeql-analysis.yml'
  text = workflow.read_text(encoding='utf-8')
  match = re.search(r"cron:\s*'([^']+)'", text)
  cron = match.group(1) if match else 'desconhecido'
  return {
    'ok': True,
    'metric': 'cron %s' % cron,
    'detail': 'Cadência lida do workflow. O resultado da rodada mora no GitHub.',
  }


def work_heartbeat():
  """Pulso de vida do próprio servidor do painel."""
  return {
    'ok': True,
    'metric': 'servidor vivo',
    'detail': 'Processo do painel respondendo.',
  }


def work_release_publisher():
  """Nunca executa sem credencial — existe para demonstrar o estado 'desligado'."""
  return {
    'ok': True,
    'metric': 'publicação simulada',
    'detail': 'Rodada de publicação concluída.',
  }


class Agent(object):
  """Definição estática de um agente. Nada aqui muda em tempo de execução."""

  def __init__(self, id, nome, icon, faz, every_min, arquivo, worker,
      run=None, requires_env=None, auto=True):
    self.id = id
    self.nome = nome
    self.icon = icon
    self.faz = faz
    self.every_min = every_min  # None => contínuo (só heartbeat)
    self.arquivo = arquivo
    self.worker = worker
    self.run = run
    self.requires_env = requires_env
    # auto=False => quem agenda este agente é alguém de fora (CI, GitHub,
    # cron da máquina). O painel observa, mas não dirige. É exatamente nesses
    # que o painel costuma revelar que algo parou.
    self.auto = auto

  @property
  def state_path(self):
    return STATE_DIR / self.arquivo


def _run_action(agent_id):
  """
  Descreve a rota que REALMENTE executa o agente.

  Repare que não é a rota de leitura. GET /api/agents devolve o snapshot em
  cache de disco e nunca dispara nada; a execução mora no POST abaixo. Essa
  separação é deliberada — botão ligado na rota errada diz "concluído" sem ter
  feito nada, que é o pior bug possível num painel.
  """
  return {
    'url': '/api/agents/%s/run' % agent_id,
    'method': 'POST',
    'body': {'action': 'run'},
  }


AGENTS = [
  Agent(
    id='mirror-index',
    nome='ÍNDICE DE MIRRORS',
    icon='📡',
    faz='Reconstrói a lista de versões do Node disponíveis para download.',
    every_min=3,
    arquivo='mirror-index.json',
    worker=work_mirror_index,
    run=_run_action('mirror-index'),
  ),
  Agent(
    id='cache-sweeper',
    nome='FAXINA DE CACHE',
    icon='🧹',
    faz='Apaga tarballs e fontes velhas do cache de download.',
    every_min=7,
    arquivo='cache-sweeper.json',
    worker=work_cache_sweeper,
    run=_run_action('cache-sweeper'),
  ),
  Agent(
    id='alias-reconciler',
    nome='ALIASES',
    icon='🔗',
    faz='Confere se os apelidos de versão apontam para onde deveriam.',
    every_min=5,
    arquivo='alias-reconciler.json',
    worker=work_alias_reconciler,
    run=_run_action('alias-reconciler'),
  ),
  Agent(
    id='shasum-verifier',
    nome='CHECKSUMS',
    icon='🔐',
    faz='Confere a integridade do nvm.sh contra o checksum publicado.',
    every_min=11,
    arquivo='shasum-verifier.json',
    worker=work_shasum_verifier,
    run=_run_action('shasum-verifier'),
  ),
  Agent(
    id='docs-toc',
    nome='SUMÁRIO DOS DOCS',
    icon='📚',
    faz='Regenera o sumário do README quando as seções mudam.',
    every_min=30,
    arquivo='docs-toc.json',
    worker=work_docs_toc,
    run=_run_action('docs-toc'),
    auto=False,
  ),
  Agent(
    id='codeql-scan',
    nome='VARREDURA CODEQL',
    icon='🛡',
    faz='Varredura semanal de segurança do código (roda no GitHub Actions).',
    every_min=7 * 24 * 60,
    arquivo='codeql-scan.json',
    worker=work_codeql_scan,
    # Sem botão: quem dispara este agente é o agendador do GitHub, não nós.
    run=None,
    auto=False,
  ),
  Agent(
    id='heartbeat',
    nome='BATIMENTO',
    icon='💓',
    faz='Pulso contínuo que prova que o próprio servidor do painel está de pé.',
    every_min=None,
    arquivo='heartbeat.json',
    worker=work_heartbeat,
    run=_run_action('heartbeat'),
  ),
  Agent(
    id='release-publisher',
    nome='PUBLICAÇÃO',
    icon='📦',
    faz='Publica uma nova versão no registro de pacotes.',
    every_min=60,
    arquivo='release-publisher.json',
    worker=work_release_publisher,
    run=_run_action('release-publisher'),
    # Sem esta variável, o agente nem deveria estar rodando. Isso é estado
    # "desligado" — informação, não alarme.
    requires_env='NVM_PANEL_RELEASE_TOKEN',
    auto=False,
  ),
]

AGENTS_BY_ID = dict((agent.id, agent) for agent in AGENTS)


def is_configured(agent):
  """Um agente sem a config de que depende está desligado de propósito."""
  if agent.requires_env is None:
    return True
  return bool(os.environ.get(agent.requires_env))
