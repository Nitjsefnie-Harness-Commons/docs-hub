// Loads the real public/index.html and public/app.js in jsdom, with
// /api/whoami and /api/list stubbed, and checks the index route renders.
import assert from 'node:assert/strict';
import { test } from 'node:test';
import { fileURLToPath } from 'node:url';
import { JSDOM, requestInterceptor } from 'jsdom';

const INDEX = new URL('../public/index.html', import.meta.url);

const API = {
  '/api/whoami': { ok: true, principal: 'human', user_id: 'reader' },
  '/api/list': {
    ok: true,
    docs: [{
      slug: 'plans/q4',
      title: 'Q4 plan',
      tags: ['plan'],
      project: 'hub',
      updated_at: '2026-10-01T12:00:00+00:00',
      latest_version: 2,
      posted_by: 'agent-a',
      byte_size: 2048,
      public: false,
      expires_at: null,
      format: 'html',
    }],
  },
};

function stubFetch(url) {
  const body = API[url];
  return Promise.resolve(body
    ? new Response(JSON.stringify(body), { headers: { 'content-type': 'application/json' } })
    : new Response('{}', { status: 404 }));
}

async function loadPage(hash) {
  const dom = await JSDOM.fromFile(fileURLToPath(INDEX), {
    url: INDEX.href + hash,
    runScripts: 'dangerously',
    // jsdom reads file: URLs before any interceptor runs, so this answers only
    // network loads, such as the font @import in styles.css, with empty CSS.
    resources: {
      interceptors: [requestInterceptor(() => new Response('', { headers: { 'content-type': 'text/css' } }))],
    },
    beforeParse(window) {
      window.fetch = stubFetch;
    },
  });
  const app = dom.window.document.getElementById('app');
  const deadline = Date.now() + 5000;
  while (!app.firstElementChild && Date.now() < deadline) {
    await new Promise((resolve) => setTimeout(resolve, 10));
  }
  return dom;
}

for (const hash of ['', '#/']) {
  test(`the empty hash ${JSON.stringify(hash)} renders the document index`, async (t) => {
    const dom = await loadPage(hash);
    t.after(() => dom.window.close());
    const { document } = dom.window;
    assert.equal(document.title, 'docs.nitjsefni.eu');
    assert.equal(document.querySelector('#app .notfound'), null);
    const link = document.querySelector('#app a.t[href="#/d/plans/q4"]');
    assert.ok(link, 'the index lists the stubbed document');
    assert.equal(link.textContent, 'Q4 plan');
  });
}
