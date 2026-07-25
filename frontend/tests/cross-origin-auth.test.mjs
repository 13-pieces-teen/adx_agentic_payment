import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';
import { fileURLToPath } from 'node:url';
import ts from 'typescript';

function loadTypeScriptModule(path, dependencies = {}) {
  const filePath = path instanceof URL ? fileURLToPath(path) : path;
  const source = readFileSync(filePath, 'utf8');
  const compiled = ts.transpileModule(source, {
    compilerOptions: {
      module: ts.ModuleKind.CommonJS,
      target: ts.ScriptTarget.ES2022,
    },
    fileName: filePath,
  }).outputText;
  const module = { exports: {} };
  const localRequire = (specifier) => {
    if (Object.hasOwn(dependencies, specifier)) return dependencies[specifier];
    throw new Error(`Unexpected test dependency: ${specifier}`);
  };
  Function('require', 'module', 'exports', compiled)(
    localRequire,
    module,
    module.exports,
  );
  return module.exports;
}

function jsonResponse(body, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

test('Hosted credential creation obtains CSRF from the authenticated API session', async () => {
  const calls = [];
  const previousFetch = globalThis.fetch;
  const previousDocument = globalThis.document;

  globalThis.document = undefined;
  globalThis.fetch = async (url, init = {}) => {
    calls.push({ url: String(url), init });
    if (String(url).endsWith('/api/auth/session')) {
      return jsonResponse({
        user: {
          user_id: 'user_123',
          username: 'octo-cat',
          temporary: false,
          auth_provider: 'github',
        },
        csrf_token: 'session-csrf-token',
      });
    }
    if (String(url).endsWith('/api/model-credentials')) {
      assert.equal(
        new Headers(init.headers).get('X-CSRF-Token'),
        'session-csrf-token',
      );
      return jsonResponse({
        credentialId: 'credential_123',
        providerId: 'deepseek',
        status: 'stored',
        fingerprintHint: 'abcd',
        createdAt: '2026-07-25T00:00:00Z',
        updatedAt: '2026-07-25T00:00:00Z',
        schemaVersion: 'arena.model-credential.v1',
      });
    }
    throw new Error(`Unexpected request: ${url}`);
  };

  try {
    const connectorApi = loadTypeScriptModule(
      new URL('../src/lib/connector-api.ts', import.meta.url),
    );
    const hostedAgentApi = loadTypeScriptModule(
      new URL('../src/lib/hosted-agent-api.ts', import.meta.url),
      { '@/lib/connector-api': connectorApi },
    );

    const credential = await hostedAgentApi.createModelCredential(
      { providerId: 'deepseek', apiKey: 'test-provider-key' },
      'hosted-credential-test',
    );

    assert.equal(credential.credentialId, 'credential_123');
    assert.deepEqual(
      calls.map(({ url }) => new URL(url, 'https://www.arena402.com').pathname),
      ['/api/auth/session', '/api/model-credentials'],
    );
  } finally {
    globalThis.fetch = previousFetch;
    globalThis.document = previousDocument;
  }
});

test('Public Connector pairing remains available without an auth session', async () => {
  const calls = [];
  const previousFetch = globalThis.fetch;

  globalThis.fetch = async (url, init = {}) => {
    calls.push({ url: String(url), init });
    if (String(url).endsWith('/api/connectors/pairings')) {
      assert.equal(new Headers(init.headers).get('X-CSRF-Token'), null);
      return jsonResponse(
        {
          pairing_id: 'pairing_123',
          user_code: 'ABCD-EFGH',
          expires_at: '2026-07-25T01:00:00Z',
          status: 'pending',
        },
        201,
      );
    }
    throw new Error(`Unexpected request: ${url}`);
  };

  try {
    const connectorApi = loadTypeScriptModule(
      new URL('../src/lib/connector-api.ts', import.meta.url),
    );
    const pairing = await connectorApi.createPairing({
      device_name: 'Local connector',
    });

    assert.equal(pairing.pairing_id, 'pairing_123');
    assert.deepEqual(
      calls.map(({ url }) => new URL(url, 'https://www.arena402.com').pathname),
      ['/api/connectors/pairings'],
    );
  } finally {
    globalThis.fetch = previousFetch;
  }
});
