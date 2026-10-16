#!/usr/bin/env python3
"""
Fase 1 do Painel de Agentes: varredura de inventário.

Roda na SUA máquina, encontra o que dispara sozinho e devolve um JSON que vira
o `registry.py` do painel. Somente leitura — não escreve, não executa nada que
tenha achado, não faz rede.

  python3 inventory_scan.py ~ ~/.claude ~/.agents ~/.aiox-core ~/EXODIA-LAB
  python3 inventory_scan.py ~/EXODIA-LAB --json > inventario.json

Segredos: os trechos de código exibidos passam por uma redação antes de sair.
Ainda assim, dê uma olhada no resultado antes de colar num chat — o que este
script não reconhecer como segredo, ele não esconde.
"""

import argparse
import json
import os
import plistlib
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

# Diretórios que nunca carregam agente e só fariam a varredura demorar.
SKIP_DIRS = {
  '.git', 'node_modules', '__pycache__', '.venv', 'venv', 'env', '.tox',
  'dist', 'build', '.next', '.nuxt', 'target', 'vendor', '.cache', '.mypy_cache',
  '.pytest_cache', '.ruff_cache', 'Library', '.Trash', '.npm', '.pyenv', '.rbenv',
  '.nvm', 'site-packages', '.terraform', 'coverage', '.gradle', 'Pods',
}

SCAN_SUFFIXES = {
  '.py', '.js', '.mjs', '.cjs', '.ts', '.tsx', '.go', '.rb', '.php', '.sh',
  '.bash', '.zsh', '.yml', '.yaml', '.toml', '.json', '.md',
}

MAX_FILE_BYTES = 400_000
MAX_FILES = 20_000

# (rótulo, regex, dica de cadência)
PATTERNS = [
  ('cron (GitHub Actions)', re.compile(r'^\s*-?\s*cron:\s*[\'"]([^\'"]+)', re.M), 'cron'),
  ('k8s CronJob', re.compile(r'kind:\s*CronJob', re.M), 'cron'),
  ('node-cron', re.compile(r'\bcron\.schedule\s*\(\s*[\'"]([^\'"]+)', re.M), 'cron'),
  ('APScheduler', re.compile(r'\b(?:BackgroundScheduler|BlockingScheduler|AsyncIOScheduler)\b', re.M), '?'),
  ('APScheduler @job', re.compile(r'add_job\s*\(.*?(?:minutes|hours|seconds|days)\s*=\s*(\d+)', re.S), 'intervalo'),
  ('Celery beat', re.compile(r'beat_schedule|CELERYBEAT_SCHEDULE|crontab\s*\(', re.M), 'cron'),
  ('schedule (py)', re.compile(r'schedule\.every\s*\(\s*(\d*)\s*\)\.(\w+)', re.M), 'intervalo'),
  ('Sidekiq-cron', re.compile(r'Sidekiq::Cron|sidekiq_cron', re.M), 'cron'),
  ('setInterval', re.compile(r'setInterval\s*\(\s*[^,]+,\s*([0-9_*\s]+)\)', re.M), 'intervalo'),
  ('loop + sleep (py)', re.compile(r'while\s+True\s*:(?:[^\n]*\n){0,12}?\s*(?:time\.)?sleep\s*\(\s*([0-9.]+)', re.M), 'intervalo'),
  ('loop + sleep (sh)', re.compile(r'while\s+(?:true|:)\s*;?\s*do(?:[^\n]*\n){0,12}?\s*sleep\s+([0-9.]+)', re.M), 'intervalo'),
  ('goroutine ticker', re.compile(r'time\.NewTicker\s*\(\s*([^)]+)\)', re.M), 'intervalo'),
  ('Thread daemon', re.compile(r'Thread\s*\([^)]*daemon\s*=\s*True', re.S), '?'),
  ('watchdog/observer', re.compile(r'\bObserver\(\)|watchdog\.observers', re.M), 'evento'),
  ('launchd (código)', re.compile(r'launchctl\s+(?:load|bootstrap|start)', re.M), 'launchd'),
]

# Sinais de prova de vida já existente.
LIVENESS = re.compile(
  r'[\'"]?(last_run|lastRun|last_run_at|lastRunAt|last_executed|updated_at|'
  r'generated_at|last_success|heartbeat|last_seen|lastSeen)[\'"]?\s*[:=]', re.I)

# Redação de segredos antes de qualquer coisa sair daqui.
SECRET_PATTERNS = [
  re.compile(r'(sk-[A-Za-z0-9_\-]{12,})'),
  re.compile(r'(gh[pousr]_[A-Za-z0-9]{16,})'),
  re.compile(r'(xox[baprs]-[A-Za-z0-9\-]{10,})'),
  re.compile(r'(AKIA[0-9A-Z]{12,})'),
  re.compile(r'(eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,})'),
  re.compile(
    r'((?:api[_-]?key|token|secret|password|passwd|pwd|bearer)'
    r'["\']?\s*[:=]\s*["\']?)([^\s"\',;]{6,})', re.I),
  re.compile(r'\b([A-Za-z0-9+/]{40,}={0,2})\b'),
]


def redact(text):
  out = text
  for pat in SECRET_PATTERNS:
    if pat.groups >= 2:
      out = pat.sub(lambda m: m.group(1) + '«redigido»', out)
    else:
      out = pat.sub('«redigido»', out)
  return out


def snippet(text, start, width=140):
  line_start = text.rfind('\n', 0, start) + 1
  raw = text[line_start:line_start + width].strip()
  return redact(raw)


def line_of(text, index):
  return text.count('\n', 0, index) + 1


def iter_files(roots):
  seen = set()
  count = 0
  # Este arquivo contém, em texto, todos os padrões que procura — sem isto ele
  # se denuncia como se fosse um enxame de agentes.
  try:
    seen.add(Path(__file__).resolve())
  except OSError:
    pass
  for root in roots:
    root = Path(root).expanduser()
    if not root.exists():
      yield ('__missing__', root)
      continue
    if root.is_file():
      yield ('file', root)
      continue
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
      dirnames[:] = [
        d for d in dirnames if d not in SKIP_DIRS and not d.startswith('.venv')]
      for name in filenames:
        path = Path(dirpath) / name
        if path.suffix.lower() not in SCAN_SUFFIXES:
          continue
        try:
          real = path.resolve()
        except OSError:
          continue
        if real in seen:
          continue
        seen.add(real)
        count += 1
        if count > MAX_FILES:
          return
        yield ('file', path)


def scan_file(path):
  """Devolve (achados, tem_prova_de_vida) para um arquivo."""
  try:
    if path.stat().st_size > MAX_FILE_BYTES:
      return [], False
    text = path.read_text(encoding='utf-8', errors='replace')
  except (OSError, ValueError):
    return [], False

  hits = []
  for label, pat, kind in PATTERNS:
    for m in pat.finditer(text):
      hits.append({
        'tipo': label,
        'cadencia_bruta': (m.group(1) if m.groups() and m.group(1) else None),
        'unidade': kind,
        'linha': line_of(text, m.start()),
        'trecho': snippet(text, m.start()),
      })
      if len(hits) >= 12:
        break
  return hits, bool(LIVENESS.search(text))


def scan_crontab():
  try:
    out = subprocess.run(['crontab', '-l'], capture_output=True, text=True, timeout=10)
  except (OSError, subprocess.SubprocessError):
    return []
  if out.returncode != 0 or not out.stdout.strip():
    return []
  entries = []
  for raw in out.stdout.splitlines():
    line = raw.strip()
    if not line or line.startswith('#'):
      continue
    entries.append({'fonte': 'crontab -l', 'entrada': redact(line)})
  return entries


def scan_launchd():
  """macOS: agentes e daemons do launchd, que é o cron de verdade do sistema."""
  found = []
  roots = [
    Path.home() / 'Library' / 'LaunchAgents',
    Path('/Library/LaunchAgents'),
    Path('/Library/LaunchDaemons'),
  ]
  for root in roots:
    if not root.is_dir():
      continue
    try:
      plists = sorted(root.glob('*.plist'))
    except OSError:
      continue
    for plist in plists:
      try:
        with plist.open('rb') as fh:
          data = plistlib.load(fh)
      except Exception:
        continue
      interval = data.get('StartInterval')
      calendar = data.get('StartCalendarInterval')
      if interval is None and calendar is None:
        continue  # sem agendamento: não é agente periódico
      found.append({
        'fonte': str(plist),
        'label': data.get('Label'),
        'every_min': (interval / 60.0) if isinstance(interval, (int, float)) else None,
        'calendario': calendar,
        'programa': redact(' '.join(data.get('ProgramArguments', []))[:200])
                    if data.get('ProgramArguments') else data.get('Program'),
        'roda_ao_carregar': bool(data.get('RunAtLoad')),
      })
  return found


def scan_systemd_timers():
  try:
    out = subprocess.run(
      ['systemctl', 'list-timers', '--all', '--no-pager', '--no-legend'],
      capture_output=True, text=True, timeout=10)
  except (OSError, subprocess.SubprocessError):
    return []
  if out.returncode != 0:
    return []
  return [{'entrada': redact(l.strip())} for l in out.stdout.splitlines() if l.strip()]


def main():
  ap = argparse.ArgumentParser(description='Inventário de processos autônomos')
  ap.add_argument('paths', nargs='*', default=[str(Path.home())],
                  help='diretórios a varrer')
  ap.add_argument('--json', action='store_true', help='só o JSON, sem resumo')
  args = ap.parse_args()

  arquivos = []
  ausentes = []
  com_prova = []

  for kind, path in iter_files(args.paths):
    if kind == '__missing__':
      ausentes.append(str(path))
      continue
    hits, has_liveness = scan_file(path)
    if has_liveness:
      com_prova.append(str(path))
    if hits:
      arquivos.append({
        'arquivo': str(path),
        'prova_de_vida': has_liveness,
        'achados': hits,
      })

  relatorio = {
    'gerado_em': datetime.now(timezone.utc).isoformat(),
    'plataforma': sys.platform,
    'raizes': [str(Path(p).expanduser()) for p in args.paths],
    'raizes_ausentes': ausentes,
    'crontab': scan_crontab(),
    'launchd': scan_launchd(),
    'systemd_timers': scan_systemd_timers(),
    'arquivos': arquivos,
    'arquivos_com_prova_de_vida': com_prova[:200],
  }

  if args.json:
    print(json.dumps(relatorio, ensure_ascii=False, indent=2))
    return

  print(json.dumps(relatorio, ensure_ascii=False, indent=2))
  print('\n' + '=' * 68, file=sys.stderr)
  print('RESUMO', file=sys.stderr)
  print('=' * 68, file=sys.stderr)
  if ausentes:
    print('raízes que não existem: %s' % ', '.join(ausentes), file=sys.stderr)
  print('entradas de crontab .....: %d' % len(relatorio['crontab']), file=sys.stderr)
  print('agendamentos launchd ....: %d' % len(relatorio['launchd']), file=sys.stderr)
  print('timers systemd ..........: %d' % len(relatorio['systemd_timers']), file=sys.stderr)
  print('arquivos com sinal ......: %d' % len(arquivos), file=sys.stderr)
  print('com prova de vida .......: %d' % len(com_prova), file=sys.stderr)
  contagem = {}
  for entry in arquivos:
    for hit in entry['achados']:
      contagem[hit['tipo']] = contagem.get(hit['tipo'], 0) + 1
  for tipo, n in sorted(contagem.items(), key=lambda kv: -kv[1]):
    print('  %-24s %d' % (tipo, n), file=sys.stderr)
  print('\nConfira o JSON antes de compartilhar: a redação de segredos cobre os '
        'formatos conhecidos, não tudo.', file=sys.stderr)


if __name__ == '__main__':
  main()
