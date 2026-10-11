#!/usr/bin/env python3
"""
Testes do Painel de Agentes.

  python3 tools/agents-panel/test_panel.py

Só a biblioteca padrão, sem rede, sem servidor no ar. Roda em segundos.

O que estes testes protegem não é o desenho — é a honestidade do painel. Quase
toda asserção aqui existe porque a alternativa seria o painel mentir: pintar de
verde o que não conseguiu ler, chamar de "nunca rodou" o que não teve permissão
de abrir, ou dizer "concluído" sobre um comando que falhou.
"""

import json
import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent))

import probes  # noqa: E402
import snapshot  # noqa: E402
import registry  # noqa: E402
from registry import Agent, ConfigError, load_config  # noqa: E402

UTC = timezone.utc


def agente(liveness, every_min=60, requires_env=None, worker=None, run=None):
  return Agent(
    't', 'TESTE', '•', 'faz algo', every_min, liveness,
    run=run, requires_env=requires_env, worker=worker)


class TempDirTest(unittest.TestCase):
  def setUp(self):
    self._tmp = tempfile.TemporaryDirectory()
    self.dir = Path(self._tmp.name)
    self.addCleanup(self._tmp.cleanup)

  def escreve_estado(self, nome, **campos):
    caminho = self.dir / nome
    caminho.write_text(json.dumps(campos), encoding='utf-8')
    return caminho


class TestParseTimestamp(unittest.TestCase):
  """
  O fromisoformat do Python 3.9 — que é o que vem no macOS — só aceita o
  formato exato que o isoformat() produz. Um agente saudável que grave o
  timestamp de outro jeito apareceria como `error`, que é o falso alarme que
  destrói a confiança no painel mais rápido que qualquer outra coisa.
  """

  ACEITOS = [
    ('2026-10-10T14:12:21.6324+00:00', 'fração de 4 dígitos'),
    ('2026-10-10T14:12:21.63240912+00:00', 'fração de 8 dígitos'),
    ('2026-10-10T14:12:21+00', 'fuso sem minutos'),
    ('2026-10-10T14:12:21-0300', 'fuso sem dois-pontos'),
    ('2026-10-10T14:12:21Z', 'sufixo Z'),
    ('2026-10-10t14:12:21z', 'minúsculas'),
    ('2026-10-10 14:12:21', 'espaço no lugar do T'),
    ('2026-10-10T14:12', 'sem segundos'),
    ('2026-10-10T14:12:21,500Z', 'vírgula decimal'),
    ('2026-10-10T14:12:21.500000+00:00', 'ISO canônico'),
  ]

  def test_formatos_reais_sao_aceitos(self):
    for texto, rotulo in self.ACEITOS:
      with self.subTest(rotulo):
        self.assertIsInstance(probes.parse_timestamp(texto), datetime)

  def test_fallback_tolerante_sozinho_cobre_todos_os_formatos(self):
    """
    O teste acima passa por acidente no Python 3.11, onde o fromisoformat
    nativo já aceita tudo isto e o fallback nunca é chamado — teste de mutação
    confirmou que remover o parser tolerante não o quebrava. Como a máquina
    que importa (macOS, 3.9) é justamente onde não dá para rodar isto, o
    fallback precisa ser exercitado direto, sem passar pelo fromisoformat.
    """
    for texto, rotulo in self.ACEITOS:
      with self.subTest(rotulo):
        normalizado = texto[:-1] + '+00:00' if texto[-1] in 'Zz' else texto
        self.assertIsInstance(
          probes._parse_iso_tolerante(normalizado), datetime,
          '%s não seria lido no Python 3.9' % rotulo)

  def test_fallback_tolerante_recusa_lixo(self):
    for ruim in ['ontem', '2026-13-45T00:00:00', '2026-10-10T25:00:00', 'x']:
      with self.subTest(ruim):
        self.assertIsNone(probes._parse_iso_tolerante(ruim))

  def test_funciona_com_fromisoformat_estrito(self):
    """
    Exercita a LIGAÇÃO entre fromisoformat e o fallback, não só o fallback.

    No Python 3.11 o fromisoformat nativo aceita tudo isto, então o `except`
    que chama o fallback é código morto aqui — teste de mutação mostrou que
    apagar aquela chamada não quebrava nada. Substituir fromisoformat por um
    que sempre recusa reproduz o Python 3.9 do macOS e põe o caminho real sob
    teste, em qualquer versão.
    """
    class DatetimeEstrito(datetime):
      @classmethod
      def fromisoformat(cls, s):
        raise ValueError('simulando o rigor do Python 3.9')

    with mock.patch.object(probes, 'datetime', DatetimeEstrito):
      for texto, rotulo in self.ACEITOS:
        with self.subTest(rotulo):
          self.assertIsInstance(probes.parse_timestamp(texto), datetime,
                                '%s quebraria no Python 3.9' % rotulo)
      # E o lixo continua sendo recusado mesmo nesse caminho.
      with self.assertRaises(probes.ProbeError):
        probes.parse_timestamp('ontem')

  def test_fuso_e_respeitado(self):
    t = probes.parse_timestamp('2026-10-10T14:12:21-0300')
    self.assertEqual(t.utcoffset(), timedelta(hours=-3))

  def test_sem_fuso_assume_utc(self):
    # Tratar como hora local daria idades negativas ou infladas conforme a
    # máquina onde o painel roda.
    self.assertEqual(
      probes.parse_timestamp('2026-10-10T14:12:21').utcoffset(), timedelta(0))

  def test_epoch_em_segundos_e_milissegundos(self):
    seg = probes.parse_timestamp(1760105541)
    mili = probes.parse_timestamp(1760105541000)
    self.assertEqual(seg, mili)

  def test_fracao_e_truncada_em_microssegundos(self):
    self.assertEqual(
      probes.parse_timestamp('2026-10-10T14:12:21.63240912Z').microsecond, 632409)

  def test_vazio_nao_e_erro(self):
    # "o agente gravou a chave mas não tem rodada" é `idle`, não falha.
    self.assertIsNone(probes.parse_timestamp(None))
    self.assertIsNone(probes.parse_timestamp(''))

  def test_lixo_e_recusado_em_vez_de_virar_data_qualquer(self):
    ruins = ['ontem', 'None', 'last_run', '2026-13-45T00:00:00Z',
      '2026-10-10T25:00:00Z', [], {}]
    for ruim in ruins:
      with self.subTest(repr(ruim)):
        with self.assertRaises(probes.ProbeError):
          probes.parse_timestamp(ruim)


class TestSondas(TempDirTest):
  def test_json_field_com_caminho_pontilhado(self):
    self.escreve_estado('e.json', last_run='2026-10-10T14:00:00Z', ok=True,
                        resumo={'n': '36 processados', 'nota': 'fila vazia'})
    v = probes.run_probe({'tipo': 'json_field', 'arquivo': str(self.dir / 'e.json'),
                          'campo': 'last_run', 'ok_campo': 'ok',
                          'metric_campo': 'resumo.n', 'detail_campo': 'resumo.nota'})
    self.assertEqual(v.metric, '36 processados')
    self.assertEqual(v.detail, 'fila vazia')
    self.assertTrue(v.ok)

  def test_json_field_respeita_ok_false(self):
    self.escreve_estado('e.json', last_run='2026-10-10T14:00:00Z', ok=False)
    v = probes.run_probe({'tipo': 'json_field', 'arquivo': str(self.dir / 'e.json'),
                          'campo': 'last_run', 'ok_campo': 'ok'})
    self.assertFalse(v.ok)

  def test_sqlite_somente_leitura(self):
    db = self.dir / 'j.db'
    con = sqlite3.connect(str(db))
    con.execute('CREATE TABLE jobs (nome TEXT, quando TEXT, n INT)')
    con.execute('INSERT INTO jobs VALUES (?,?,?)', ('w', '2026-10-10T14:00:00Z', 7))
    con.commit()
    con.close()
    v = probes.run_probe({'tipo': 'sqlite', 'arquivo': str(db),
                          'consulta': 'SELECT quando, n FROM jobs WHERE nome = ?',
                          'parametros': ['w']})
    self.assertEqual(v.metric, '7')

  def test_sqlite_nao_pode_escrever(self):
    db = self.dir / 'j.db'
    con = sqlite3.connect(str(db))
    con.execute('CREATE TABLE jobs (quando TEXT)')
    con.commit()
    con.close()
    # O painel observa processos de outras pessoas; escrever no banco delas
    # seria um efeito colateral que ninguém pediu.
    with self.assertRaises(probes.ProbeError):
      probes.run_probe({'tipo': 'sqlite', 'arquivo': str(db),
                        'consulta': "INSERT INTO jobs VALUES ('x')"})

  def test_mtime(self):
    alvo = self.dir / 'saida.bin'
    alvo.write_bytes(b'x' * 2048)
    os.utime(alvo, (1760105541, 1760105541))
    v = probes.run_probe({'tipo': 'mtime', 'arquivo': str(alvo)})
    self.assertEqual(v.last, datetime.fromtimestamp(1760105541, UTC))

  def test_mtime_recusa_diretorio(self):
    # stat() funciona em diretório. Sem este teste, uma pasta no lugar do
    # arquivo de saída vira "agente saudável" com a data da pasta como última
    # rodada — o painel pintando verde o que nunca mediu.
    (self.dir / 'pasta').mkdir()
    with self.assertRaises(probes.ProbeError):
      probes.run_probe({'tipo': 'mtime', 'arquivo': str(self.dir / 'pasta')})

  def test_log_regex_pega_a_ultima_ocorrencia(self):
    log = self.dir / 'b.log'
    log.write_text(
      '2026-10-10T10:00:00Z heartbeat\n'
      '2026-10-10T14:00:00Z heartbeat\n', encoding='utf-8')
    v = probes.run_probe({'tipo': 'log_regex', 'arquivo': str(log),
                          'padrao': r'(?P<ts>\S+)\s+heartbeat'})
    self.assertEqual(v.last.hour, 14)

  def test_log_regex_sem_grupo_de_timestamp_e_erro(self):
    log = self.dir / 'b.log'
    log.write_text('qualquer coisa heartbeat\n', encoding='utf-8')
    with self.assertRaises(probes.ProbeError):
      probes.run_probe({'tipo': 'log_regex', 'arquivo': str(log),
                        'padrao': 'heartbeat'})

  def test_sonda_desconhecida_e_erro(self):
    with self.assertRaises(probes.ProbeError):
      probes.run_probe({'tipo': 'telepatia', 'arquivo': 'x'})


class TestAusenciaVersusFalha(TempDirTest):
  """
  A distinção que sustenta o painel inteiro: só o arquivo comprovadamente
  ausente é vazio. Qualquer outra falha é erro. Nenhuma sonda pode usar um
  teste genérico de existência, que devolveria o mesmo para "não existe" e para
  "permissão negada".
  """

  def test_arquivo_ausente_e_vazio_em_todas_as_sondas(self):
    faltando = str(self.dir / 'nao-existe')
    casos = [
      {'tipo': 'json_field', 'arquivo': faltando, 'campo': 'last_run'},
      {'tipo': 'mtime', 'arquivo': faltando},
      {'tipo': 'log_regex', 'arquivo': faltando, 'padrao': '(?P<ts>x)'},
      {'tipo': 'sqlite', 'arquivo': faltando, 'consulta': 'SELECT 1'},
    ]
    for cfg in casos:
      with self.subTest(cfg['tipo']):
        self.assertIsNone(probes.run_probe(cfg).last)

  def test_falha_de_leitura_nunca_vira_vazio(self):
    ruim = self.dir / 'ruim.json'
    ruim.write_text('{"last_run": "2026-10', encoding='utf-8')
    lista = self.dir / 'lista.json'
    lista.write_text('[1,2,3]', encoding='utf-8')
    (self.dir / 'dir.json').mkdir()
    casos = {
      'json truncado': str(ruim),
      'json não-objeto': str(lista),
      'diretório no lugar do arquivo': str(self.dir / 'dir.json'),
    }
    for rotulo, caminho in casos.items():
      with self.subTest(rotulo):
        with self.assertRaises(probes.ProbeError):
          probes.run_probe({'tipo': 'json_field', 'arquivo': caminho,
                            'campo': 'last_run'})


class TestMaquinaDeEstados(TempDirTest):
  def descreve(self, idade_min=None, ok=True, every_min=60, **kw):
    agora = datetime(2026, 10, 10, 12, 0, tzinfo=UTC)
    campos = {'ok': ok}
    if idade_min is not None:
      campos['last_run'] = (agora - timedelta(minutes=idade_min)).isoformat()
    self.escreve_estado('e.json', **campos)
    liveness = {'tipo': 'json_field', 'arquivo': str(self.dir / 'e.json'),
                'campo': 'last_run', 'ok_campo': 'ok'}
    return snapshot.describe(agente(liveness, every_min=every_min, **kw), agora)

  def test_recente_e_ok(self):
    self.assertEqual(self.descreve(idade_min=10)['state'], 'ok')

  def test_limite_do_atraso(self):
    # Exatamente em cima do limite ainda é ok; um passo além é atraso.
    limite = 60 * registry.STALE_FACTOR
    self.assertEqual(self.descreve(idade_min=limite)['state'], 'ok')
    self.assertEqual(self.descreve(idade_min=limite + 0.1)['state'], 'stale')

  def test_fator_de_atraso_fica_numa_faixa_util(self):
    """
    O teste acima deriva o limite da própria constante, então ele confere o
    mecanismo e nunca o valor — teste de mutação mostrou que trocar 2,5 por 99
    passava despercebido, e com 99 nenhum agente jamais ficaria atrasado. O
    fator é para ser ajustável, então não se trava o número exato; trava-se a
    faixa em que ele ainda significa alguma coisa.
    """
    self.assertGreater(
      registry.STALE_FACTOR, 1.0,
      'fator <= 1 acusa atraso no instante em que a rodada vence')
    self.assertLessEqual(
      registry.STALE_FACTOR, 5.0,
      'fator alto demais faz o painel nunca acusar nada')

  def test_agente_muito_atrasado_e_stale_seja_qual_for_o_fator(self):
    # Independente de ajuste: um agente de cadência horária parado há um dia
    # está atrasado, ponto.
    self.assertEqual(self.descreve(idade_min=24 * 60, every_min=60)['state'], 'stale')

  def test_sem_timestamp_e_idle_e_nunca_ok(self):
    e = self.descreve(idade_min=None)
    self.assertEqual(e['state'], 'idle')
    self.assertIsNone(e['last'])

  def test_falha_do_agente_vence_o_relogio(self):
    # Rodou há 1 minuto, mas falhou: está em erro, não "em dia".
    self.assertEqual(self.descreve(idade_min=1, ok=False)['state'], 'error')

  def test_desligado_nao_e_alarme(self):
    e = self.descreve(idade_min=9999, requires_env='VAR_QUE_NAO_EXISTE_12345')
    self.assertEqual(e['state'], 'off')
    self.assertIsNone(e['run'])

  def test_desligado_nem_chega_a_sondar(self):
    # Estado corrompido não pode transformar um agente desligado em erro: se a
    # config não existe, o agente nem deveria estar rodando.
    (self.dir / 'e.json').write_text('{{{ lixo', encoding='utf-8')
    liveness = {'tipo': 'json_field', 'arquivo': str(self.dir / 'e.json'),
                'campo': 'last_run'}
    a = agente(liveness, requires_env='VAR_QUE_NAO_EXISTE_12345')
    self.assertEqual(snapshot.describe(a)['state'], 'off')

  def test_fase_e_proxima_rodada(self):
    e = self.descreve(idade_min=15, every_min=60)
    self.assertAlmostEqual(e['phase'], 0.25, places=3)
    self.assertAlmostEqual(e['next_in_min'], 45.0, places=1)

  def test_fase_satura_em_um(self):
    self.assertEqual(self.descreve(idade_min=9999, every_min=60)['phase'], 1.0)
    self.assertEqual(self.descreve(idade_min=9999, every_min=60)['next_in_min'], 0.0)

  def test_continuo_nao_tem_fase(self):
    e = self.descreve(idade_min=0.1, every_min=None)
    self.assertIsNone(e['phase'])
    self.assertIsNone(e['next_in_min'])
    self.assertEqual(e['state'], 'ok')

  def test_continuo_sem_pulso_fica_stale(self):
    idade = registry.HEARTBEAT_STALE_MIN + 1
    self.assertEqual(self.descreve(idade_min=idade, every_min=None)['state'], 'stale')


class TestContrato(TempDirTest):
  def test_sem_disparo_nao_expoe_run(self):
    liveness = {'tipo': 'mtime', 'arquivo': str(self.dir / 'x')}
    self.assertIsNone(snapshot.describe(agente(liveness))['run'])

  def test_com_disparo_expoe_rota_do_painel_e_nao_o_comando(self):
    liveness = {'tipo': 'mtime', 'arquivo': str(self.dir / 'x')}
    a = agente(liveness, run={'cmd': ['echo', 'segredo-interno']})
    run = snapshot.describe(a)['run']
    self.assertEqual(run['method'], 'POST')
    self.assertTrue(run['url'].endswith('/run'))
    # O comando real nunca sai do servidor.
    self.assertNotIn('segredo-interno', json.dumps(run))

  def test_resumo_separa_desligado_de_atencao(self):
    liveness = {'tipo': 'mtime', 'arquivo': str(self.dir / 'x')}
    d = snapshot.build([
      agente(liveness, requires_env='VAR_QUE_NAO_EXISTE_12345'),
      Agent('b', 'B', '•', 'faz', 60, liveness),
    ])
    self.assertEqual(d['resumo']['off'], 1)
    self.assertEqual(d['resumo']['atencao'], 1)  # o idle, não o desligado


class TestValidacaoDeConfig(TempDirTest):
  def carrega(self, texto):
    caminho = self.dir / 'agents.json'
    caminho.write_text(texto, encoding='utf-8')
    return load_config(caminho)

  CASOS_RUINS = {
    'falta faz': '{"agents":[{"id":"a","liveness":{"tipo":"mtime","arquivo":"/x"}}]}',
    'falta liveness': '{"agents":[{"id":"a","faz":"x"}]}',
    'liveness sem tipo': '{"agents":[{"id":"a","faz":"x","liveness":{"arquivo":"/x"}}]}',
    'id repetido': ('{"agents":[{"id":"a","faz":"x","liveness":{"tipo":"mtime","arquivo":"/x"}},'
                    '{"id":"a","faz":"y","liveness":{"tipo":"mtime","arquivo":"/y"}}]}'),
    'cmd como string': ('{"agents":[{"id":"a","faz":"x","liveness":{"tipo":"mtime","arquivo":"/x"},'
                        '"run":{"cmd":"rm -rf /"}}]}'),
    'cmd vazio': ('{"agents":[{"id":"a","faz":"x","liveness":{"tipo":"mtime","arquivo":"/x"},'
                  '"run":{"cmd":[]}}]}'),
    'run sem cmd': ('{"agents":[{"id":"a","faz":"x","liveness":{"tipo":"mtime","arquivo":"/x"},'
                    '"run":{"url":"/x"}}]}'),
    'json inválido': '{"agents": [',
    'agents não é lista': '{"agents": {"a": 1}}',
    'entrada não é objeto': '{"agents": ["a"]}',
  }

  def test_config_ruim_falha_alto(self):
    # Um painel que ignora metade do próprio inventário em silêncio é pior que
    # um painel que não sobe.
    for rotulo, texto in self.CASOS_RUINS.items():
      with self.subTest(rotulo):
        with self.assertRaises(ConfigError):
          self.carrega(texto)

  def test_config_ausente_cai_no_modo_demo(self):
    self.assertIsNone(load_config(self.dir / 'nao-existe.json'))
    agentes, modo = registry.load_agents(self.dir / 'nao-existe.json')
    self.assertEqual(modo, 'demo')
    self.assertTrue(agentes)

  def test_nome_ausente_usa_o_id(self):
    a = self.carrega(
      '{"agents":[{"id":"sem-nome","faz":"x",'
      '"liveness":{"tipo":"mtime","arquivo":"/x"}}]}')
    self.assertEqual(a[0].nome, 'sem-nome')

  def test_agente_de_config_nao_e_dirigido_pelo_agendador(self):
    # Quem agenda os agentes declarados é o cron/launchd/CI da máquina. O
    # painel observa; não assume o comando de processos que não criou.
    a = self.carrega(
      '{"agents":[{"id":"a","faz":"x",'
      '"liveness":{"tipo":"mtime","arquivo":"/x"}}]}')
    self.assertFalse(a[0].auto)


class TestConjuntoDemo(unittest.TestCase):
  def test_demo_monta_e_todos_os_estados_sao_validos(self):
    agentes, modo = registry.load_agents(Path('/caminho/que/nao/existe.json'))
    self.assertEqual(modo, 'demo')
    d = snapshot.build(agentes, modo)
    validos = set(snapshot.SEVERITY)
    for entrada in d['agents']:
      self.assertIn(entrada['state'], validos)
    self.assertEqual(d['resumo']['total'], len(agentes))

  def test_codeql_nao_pode_ser_disparado_daqui(self):
    # Quem o agenda é o GitHub. Semeá-lo com um timestamp plausível seria
    # inventar uma rodada que nunca aconteceu.
    agentes, _ = registry.load_agents(Path('/caminho/que/nao/existe.json'))
    codeql = [a for a in agentes if a.id == 'codeql-scan'][0]
    self.assertFalse(codeql.tem_disparo)
    self.assertIsNone(snapshot.describe(codeql)['run'])


if __name__ == '__main__':
  unittest.main(verbosity=2)
