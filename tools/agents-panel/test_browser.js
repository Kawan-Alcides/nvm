//
// Testes de navegador do Painel de Agentes.
//
// # com os dois servidores no ar:
// python3 tools/agents-panel/server.py --port 7777 &
// python3 tools/agents-panel/server.py --port 7788 --config SUA_CONFIG.json &
// node tools/agents-panel/test_browser.js
//
// Precisa do Playwright, que NÃO é dependência do projeto — instale por fora
// (`npm i -g playwright` ou `npx playwright`). O test_panel.py cobre a lógica
// sem navegador nenhum; este arquivo cobre o que só existe na tela: a animação
// ser função do dado, a seleção nos dois sentidos, o botão executar de verdade,
// o painel parar quando ninguém está olhando, e o celular continuar legível.
//
// Variáveis: PANEL_DEMO (padrão 7777), PANEL_CONFIG (padrão 7788; vazio pula os
// testes do modo declarativo), PANEL_SHOTS (onde salvar as capturas).

const { chromium } = require('playwright');

const DEMO = process.env.PANEL_DEMO || 'http://127.0.0.1:7777';
const CONFIG = process.env.PANEL_CONFIG === '' ? null
  : (process.env.PANEL_CONFIG || 'http://127.0.0.1:7788');
const SHOTS = process.env.PANEL_SHOTS || null;

let falhas = 0;
function check(nome, passou, extra) {
  if (!passou) falhas++;
  console.log(`${passou ? '✅' : '❌'} ${nome}${extra ? ' — ' + extra : ''}`);
}

async function novaPagina(browser, base, largura) {
  const page = await browser.newPage({ viewport: { width: largura || 1280, height: 950 } });
  const erros = [];
  page.on('console', m => { if (m.type() === 'error') erros.push(m.text()); });
  page.on('pageerror', e => erros.push('pageerror: ' + e.message));
  await page.goto(base, { waitUntil: 'networkidle' });
  await page.waitForSelector('.ap-node', { timeout: 10000 });
  return { page, erros };
}

/* A animação tem de ser função do dado, não enfeite. */
async function testaAnimacao(page, snap) {
  const dash = await page.$$eval('.ap-node .ap-arc',
    els => els.map(e => parseFloat(e.getAttribute('stroke-dasharray').split(' ')[0])));
  check('arcos visivelmente diferentes entre si',
    new Set(dash.map(n => n.toFixed(1))).size >= Math.min(4, snap.agents.length),
    dash.map(n => n.toFixed(1)).join(', '));

  async function amplitude(estado) {
    const sel = `.ap-node[data-state="${estado}"] .ap-hex`;
    if (!(await page.locator(sel).count())) return null;
    const v = [];
    for (let i = 0; i < 12; i++) {
      v.push(await page.$eval(sel, el => parseFloat(
        (el.parentElement.getAttribute('transform') || 'scale(1)')
          .replace(/[^0-9.]/g, '')) || 1));
      await page.waitForTimeout(40);
    }
    return Math.max(...v) - Math.min(...v);
  }
  const err = await amplitude('error');
  const ok = await amplitude('ok');
  if (err !== null && ok !== null) {
    check('nó em erro pulsa mais forte que um saudável', err > ok * 2,
      `erro=${err.toFixed(4)} ok=${ok.toFixed(4)}`);
  }

  await page.waitForTimeout(2500);
  check('pulsos viajando até o núcleo', (await page.locator('.ap-pulse').count()) > 0);
}

/* O snapshot envelhece entre fetches; o arco não pode congelar junto. */
async function testaIdadeViva(page) {
  const ler = () => page.$eval('.ap-node .ap-arc',
    el => parseFloat(el.getAttribute('stroke-dasharray').split(' ')[0]));
  let requisicoes = 0;
  const conta = r => { if (r.url().includes('/api/agents')) requisicoes++; };
  page.on('request', conta);
  const antes = await ler();
  await page.waitForTimeout(4000);
  const depois = await ler();
  page.off('request', conta);
  check('o arco avança entre fetches, sem rede',
    depois > antes && requisicoes === 0,
    `${antes.toFixed(2)} -> ${depois.toFixed(2)} com ${requisicoes} requisições`);
}

async function testaSelecao(page) {
  // Começa de um estado conhecido. Clicar num agente já selecionado desmarca —
  // comportamento certo do painel que, sem isto, faz este teste falhar quando
  // alguma checagem anterior deixou algo selecionado.
  const jaMarcado = await page.$$eval('.ap-list button',
    els => els.findIndex(e => e.getAttribute('aria-pressed') === 'true'));
  if (jaMarcado >= 0) {
    await page.locator('.ap-list button').nth(jaMarcado).click();
  }

  await page.locator('.ap-node').first().click();
  check('clique no nó reflete na lista',
    (await page.$eval('.ap-node', e => e.getAttribute('aria-selected'))) === 'true' &&
    (await page.$$eval('.ap-list button',
      els => els.filter(e => e.getAttribute('aria-pressed') === 'true').length)) === 1);

  await page.locator('.ap-list button').nth(1).click();
  check('clique na lista reflete no nó',
    (await page.$$eval('.ap-node',
      els => els.map(e => e.getAttribute('aria-selected'))))[1] === 'true');

  await page.locator('.ap-list button').nth(1).click();
  check('clicar no já selecionado desmarca',
    (await page.$$eval('.ap-node',
      els => els.filter(e => e.getAttribute('aria-selected') === 'true').length)) === 0);
}

/* rAF e polling queimam bateria para ninguém ver quando a aba está escondida. */
async function testaVisibilidade(page) {
  const visibilidade = v => page.evaluate(estado => {
    Object.defineProperty(document, 'visibilityState', { value: estado, configurable: true });
    document.dispatchEvent(new Event('visibilitychange'));
  }, v);
  await visibilidade('hidden');
  await page.waitForTimeout(2500);
  check('animação para com a aba escondida', (await page.locator('.ap-pulse').count()) === 0);
  await visibilidade('visible');
  await page.waitForTimeout(2500);
  check('animação religa ao voltar', (await page.locator('.ap-pulse').count()) > 0);
}

/* O viewBox encolhe o texto junto com o desenho. */
async function testaCelular(page) {
  await page.setViewportSize({ width: 375, height: 800 });
  await page.waitForTimeout(1200);
  const sc = await page.evaluate(() => ({
    sw: document.documentElement.scrollWidth,
    cw: document.documentElement.clientWidth,
  }));
  check('sem rolagem horizontal em 375px', sc.sw <= sc.cw + 1, `${sc.sw} vs ${sc.cw}`);

  const altura = await page.$eval('.ap-node-name', el => el.getBoundingClientRect().height);
  check('rótulo legível no celular (>=9px)', altura >= 9, `${altura.toFixed(1)}px`);

  const sobrepoe = await page.$$eval('.ap-node-name', els => {
    const b = els.map(e => e.getBoundingClientRect());
    let n = 0;
    for (let i = 0; i < b.length; i++) {
      for (let j = i + 1; j < b.length; j++) {
        if (b[i].left < b[j].right && b[j].left < b[i].right &&
            b[i].top < b[j].bottom && b[j].top < b[i].bottom) n++;
      }
    }
    return n;
  });
  check('rótulos não se sobrepõem no celular', sobrepoe === 0, String(sobrepoe));
  await page.setViewportSize({ width: 1280, height: 950 });
}

async function main() {
  const browser = await chromium.launch({
    executablePath: process.env.PLAYWRIGHT_CHROMIUM || undefined,
  });

  // ---------------- modo demo ----------------
  console.log('\n=== modo demo ===');
  {
    const { page, erros } = await novaPagina(browser, DEMO);
    const snap = await page.evaluate(() => fetch('/api/agents').then(r => r.json()));

    check('o painel desenha um nó por agente',
      (await page.locator('.ap-node').count()) === snap.agents.length,
      `${snap.agents.length} agentes`);

    const banner = await page.$eval('#ap-banner',
      e => ({ txt: e.textContent, modo: e.getAttribute('data-modo'), oculto: e.hidden }));
    check('banner identifica o conjunto demo',
      !banner.oculto && banner.modo === 'demo' && /simulada/.test(banner.txt));

    await testaAnimacao(page, snap);
    await testaIdadeViva(page);
    await testaSelecao(page);

    // agente sem disparo não pode ganhar botão
    const semRun = snap.agents.findIndex(a => a.run === null);
    if (semRun >= 0) {
      await page.locator('.ap-list button').nth(semRun).click();
      check('agente sem "run" não ganha botão',
        (await page.locator('.ap-run').count()) === 0, snap.agents[semRun].id);
    }

    // o botão tem de executar de verdade: o timestamp precisa mudar
    const comRun = snap.agents.find(a => a.run && a.id !== 'heartbeat');
    if (comRun) {
      const ler = async () => (await page.evaluate(
        id => fetch('/api/agents').then(r => r.json()).then(
          d => (d.agents.find(a => a.id === id) || {}).last), comRun.id));
      const antes = await ler();
      await page.locator('.ap-list button')
        .nth(snap.agents.findIndex(a => a.id === comRun.id)).click();
      await page.locator('.ap-run').click();
      await page.waitForFunction(
        () => /concluída|Falhou/.test(
          (document.getElementById('ap-run-note') || {}).textContent || ''),
        null, { timeout: 15000 });
      const nota = await page.$eval('#ap-run-note', e => e.textContent);
      const depois = await ler();
      check('o botão executou de verdade (timestamp mudou)',
        antes !== depois && /concluída/.test(nota), `${antes} -> ${depois}`);
    }

    await testaVisibilidade(page);
    if (SHOTS) await page.screenshot({ path: `${SHOTS}/painel-demo.png`, fullPage: true });
    await testaCelular(page);
    check('console do navegador sem erros', erros.length === 0, erros[0] || 'nenhum');
    await page.close();
  }

  // ---------------- modo declarativo ----------------
  if (CONFIG) {
    console.log('\n=== modo declarativo ===');
    const { page, erros } = await novaPagina(browser, CONFIG);
    const snap = await page.evaluate(() => fetch('/api/agents').then(r => r.json()));

    const banner = await page.$eval('#ap-banner',
      e => ({ txt: e.textContent, modo: e.getAttribute('data-modo') }));
    check('banner marcado como config', banner.modo === 'config');
    // O defeito que este teste existe para impedir: o painel anunciar
    // "cadência simulada" sobre os agentes reais de alguém.
    check('banner NÃO descreve agentes reais como demonstração',
      !/simulada|demonstra|CodeQL/i.test(banner.txt), banner.txt.slice(0, 56));

    check('o núcleo não herda o nome do conjunto demo',
      (await page.$eval('.ap-core-label', e => e.textContent)) !== 'NVM',
      await page.$eval('.ap-core-label', e => e.textContent));

    await page.locator('.ap-list button').first().click();
    const rotulos = await page.$$eval('.ap-rows dt', e => e.map(x => x.textContent));
    check('o detalhe diz de qual árvore o agente veio', rotulos.includes('Origem'));
    check('o detalhe diz por qual sonda ele é lido', rotulos.includes('Lido via'));

    await testaAnimacao(page, snap);
    await testaSelecao(page);
    if (SHOTS) await page.screenshot({ path: `${SHOTS}/painel-config.png`, fullPage: true });
    await testaCelular(page);
    check('console do navegador sem erros', erros.length === 0, erros[0] || 'nenhum');
    await page.close();
  }

  await browser.close();
  console.log(`\n${falhas ? falhas + ' FALHA(S)' : 'todas as verificações passaram'}`);
  process.exit(falhas ? 1 : 0);
}

main().catch(e => { console.error('ERRO NA VALIDAÇÃO:', e); process.exit(2); });
