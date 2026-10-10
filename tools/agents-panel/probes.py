"""
Sondas de prova de vida.

Cada sonda sabe extrair "quando rodou pela última vez" de UM tipo de fonte. É o
único lugar do painel que conhece o formato do dado; o resto trabalha com o
resultado normalizado.

As sondas estão na ordem de preferência da Fase 1: campo de timestamp em
arquivo de estado > coluna no banco > timestamp dentro do artefato produzido >
mtime do arquivo de saída > linha de log.

Regra que atravessa todas elas: **ausência comprovada do arquivo devolve vazio;
qualquer outra falha é erro**. Nenhuma sonda pode usar um teste genérico de
existência, que devolve o mesmo para "não existe" e para "permissão negada" e
faria o painel mentir "nunca rodou".
"""

import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from stat import S_ISREG


class ProbeError(Exception):
  """A fonte existe mas não pôde ser lida. Vira estado `error` no painel."""


class Liveness(object):
  """Resultado normalizado de uma sonda."""

  def __init__(self, last=None, ok=True, metric=None, detail=None):
    self.last = last      # datetime com fuso, ou None se nunca rodou
    self.ok = ok          # o agente registrou sucesso?
    self.metric = metric
    self.detail = detail


def parse_timestamp(value, campo='timestamp'):
  """
  Aceita ISO-8601, epoch em segundos e epoch em milissegundos.

  Epoch sem fuso é interpretado como UTC; ISO sem fuso também, porque um
  timestamp ambíguo tratado como hora local daria idades negativas ou infladas
  dependendo de onde o painel roda.
  """
  if value is None or value == '':
    return None

  if isinstance(value, (int, float)):
    seconds = float(value)
    # Heurística de unidade: segundos desde a época cabem em ~10 dígitos até o
    # ano 2286. Acima disso é quase certamente milissegundos.
    if seconds > 1e11:
      seconds /= 1000.0
    try:
      return datetime.fromtimestamp(seconds, timezone.utc)
    except (OverflowError, OSError, ValueError) as exc:
      raise ProbeError('%s fora de faixa: %r (%s)' % (campo, value, exc))

  if not isinstance(value, str):
    raise ProbeError('%s tem tipo inesperado: %r' % (campo, type(value).__name__))

  texto = value.strip()
  # 'Z' é válido em ISO-8601 mas o fromisoformat de Python < 3.11 não aceita.
  if texto.endswith('Z'):
    texto = texto[:-1] + '+00:00'
  try:
    parsed = datetime.fromisoformat(texto)
  except ValueError:
    # Último recurso: string que é só um número.
    try:
      return parse_timestamp(float(texto), campo)
    except (TypeError, ValueError):
      raise ProbeError('%s ilegível: %r' % (campo, value))
  if parsed.tzinfo is None:
    parsed = parsed.replace(tzinfo=timezone.utc)
  return parsed


def _dig(data, caminho):
  """Busca um campo por caminho pontilhado: 'estado.ultima.rodada'."""
  atual = data
  for parte in caminho.split('.'):
    if not isinstance(atual, dict) or parte not in atual:
      return None
    atual = atual[parte]
  return atual


def _read_json(caminho_arquivo, rotulo):
  path = Path(caminho_arquivo).expanduser()
  try:
    raw = path.read_text(encoding='utf-8')
  except FileNotFoundError:
    return None
  except OSError as exc:
    raise ProbeError('não foi possível ler %s: %s' % (rotulo, exc.strerror or exc))
  try:
    data = json.loads(raw)
  except ValueError as exc:
    raise ProbeError('JSON corrompido em %s: %s' % (rotulo, exc))
  if not isinstance(data, dict):
    raise ProbeError('conteúdo inesperado em %s: não é um objeto' % rotulo)
  return data


def probe_json_field(cfg):
  """
  Melhor fonte: o agente grava um arquivo de estado com timestamp.

  Também cobre "timestamp dentro do artefato produzido" — é o mesmo mecanismo,
  só apontando para o arquivo de saída em vez de um arquivo de estado dedicado.
  """
  arquivo = cfg['arquivo']
  rotulo = Path(arquivo).name
  data = _read_json(arquivo, rotulo)
  if data is None:
    return Liveness()

  bruto = _dig(data, cfg.get('campo', 'last_run'))
  last = parse_timestamp(bruto, cfg.get('campo', 'last_run'))

  ok = True
  if cfg.get('ok_campo'):
    valor = _dig(data, cfg['ok_campo'])
    if valor is not None:
      ok = bool(valor)
  if cfg.get('erro_campo') and _dig(data, cfg['erro_campo']):
    ok = False

  metric = _dig(data, cfg['metric_campo']) if cfg.get('metric_campo') else None
  detail = _dig(data, cfg['detail_campo']) if cfg.get('detail_campo') else None
  return Liveness(last, ok,
                  None if metric is None else str(metric),
                  None if detail is None else str(detail))


def probe_mtime(cfg):
  """
  Funciona, mas mente se outra coisa tocar o arquivo.

  É a penúltima opção da lista de preferência justamente por isso: um `touch`,
  um backup ou um editor aberto por engano reiniciam o relógio sem que o agente
  tenha rodado. Use só quando não houver timestamp interno.
  """
  path = Path(cfg['arquivo']).expanduser()
  try:
    stat = path.stat()
  except FileNotFoundError:
    return Liveness()
  except OSError as exc:
    raise ProbeError('não foi possível consultar %s: %s' % (path.name, exc.strerror or exc))
  # stat() funciona em diretório, soquete e fifo. Sem este teste, um diretório
  # no lugar do arquivo de saída vira "agente saudável" com a data da pasta
  # como última rodada — o painel pintando verde o que não conseguiu medir.
  if not S_ISREG(stat.st_mode):
    raise ProbeError('%s existe mas não é um arquivo comum' % path.name)
  return Liveness(
    datetime.fromtimestamp(stat.st_mtime, timezone.utc),
    True,
    '%.1f KB' % (stat.st_size / 1024.0),
    'Data de modificação do arquivo — pode ser tocada por outra coisa.',
  )


def probe_sqlite(cfg):
  """Tabela de jobs: SELECT que devolve o timestamp na primeira coluna."""
  path = Path(cfg['arquivo']).expanduser()
  if not path.is_file():
    # Banco ausente é instalação nova; qualquer outro problema cai no except.
    try:
      path.stat()
    except FileNotFoundError:
      return Liveness()
    except OSError as exc:
      raise ProbeError('não foi possível abrir %s: %s' % (path.name, exc.strerror or exc))

  consulta = cfg['consulta']
  try:
    # Somente leitura: o painel nunca escreve no banco de outro processo.
    con = sqlite3.connect('file:%s?mode=ro' % path, uri=True, timeout=5)
    try:
      linha = con.execute(consulta, tuple(cfg.get('parametros', []))).fetchone()
    finally:
      con.close()
  except sqlite3.Error as exc:
    raise ProbeError('consulta falhou em %s: %s' % (path.name, exc))

  if linha is None or linha[0] is None:
    return Liveness()
  last = parse_timestamp(linha[0], 'coluna 1')
  metric = str(linha[1]) if len(linha) > 1 and linha[1] is not None else None
  detail = str(linha[2]) if len(linha) > 2 and linha[2] is not None else None
  return Liveness(last, True, metric, detail)


# Quanto do fim do log ler. Logs crescem sem limite; ler o arquivo inteiro a
# cada snapshot acabaria com a promessa de resposta em milissegundos.
LOG_TAIL_BYTES = 256 * 1024


def probe_log_regex(cfg):
  """
  A fonte mais frágil da lista — use só quando não houver alternativa.

  Um log rotacionado, truncado ou com formato alterado faz o agente parecer
  morto sem que nada tenha acontecido com ele.
  """
  path = Path(cfg['arquivo']).expanduser()
  padrao = re.compile(cfg['padrao'])
  try:
    with path.open('rb') as fh:
      fh.seek(0, 2)
      tamanho = fh.tell()
      fh.seek(max(0, tamanho - LOG_TAIL_BYTES))
      trecho = fh.read().decode('utf-8', errors='replace')
  except FileNotFoundError:
    return Liveness()
  except OSError as exc:
    raise ProbeError('não foi possível ler %s: %s' % (path.name, exc.strerror or exc))

  ultimo = None
  for m in padrao.finditer(trecho):
    ultimo = m
  if ultimo is None:
    return Liveness()

  grupos = ultimo.groupdict()
  bruto = grupos.get('ts') or (ultimo.group(1) if ultimo.groups() else None)
  if bruto is None:
    raise ProbeError('o padrão casou mas não capturou timestamp (use (?P<ts>...))')
  return Liveness(
    parse_timestamp(bruto, 'timestamp do log'),
    not bool(cfg.get('padrao_erro') and re.search(cfg['padrao_erro'], ultimo.group(0))),
    grupos.get('metric'),
    ultimo.group(0)[:160].strip(),
  )


PROBES = {
  'json_field': probe_json_field,
  'artifact_field': probe_json_field,  # mesmo mecanismo, outro arquivo
  'sqlite': probe_sqlite,
  'mtime': probe_mtime,
  'log_regex': probe_log_regex,
}


def run_probe(cfg):
  """Despacha para a sonda declarada em `tipo`."""
  tipo = cfg.get('tipo')
  sonda = PROBES.get(tipo)
  if sonda is None:
    raise ProbeError(
      'sonda desconhecida: %r (conhecidas: %s)'
      % (tipo, ', '.join(sorted(PROBES))))
  return sonda(cfg)
