import { test } from 'node:test'
import assert from 'node:assert/strict'

import { hashToolSchemas, recordAgentSocietyMetrics } from '../lib/context-metrics.js'

test('context metrics persist counters without prompt content', async () => {
  const { mkdtempSync, readFileSync, rmSync } = await import('node:fs')
  const { join } = await import('node:path')
  const { tmpdir } = await import('node:os')
  const dshHome = mkdtempSync(join(tmpdir(), 'dsh-context-metrics-'))
  try {
    recordAgentSocietyMetrics(dshHome, {
      context: { kind: 'initial', bytes: 123, sessionId: 'session-1' },
      usage: { inputTokens: 7, cacheReadTokens: 11, cacheWriteTokens: 2 },
      digestModelCall: true,
    })
    const metrics = JSON.parse(
      readFileSync(join(dshHome, 'agent-society-context-metrics.json'), 'utf8'),
    )
    assert.equal(metrics.contextInitialBytes, 123)
    assert.equal(metrics.inputTokens, 20)
    assert.equal(metrics.uncachedInputTokens, 7)
    assert.equal(metrics.cacheReadTokens, 11)
    assert.equal(metrics.digestModelCalls, 1)
    assert.equal(metrics.contextUpdatesPerSession['session-1'].initial, 1)
    assert.equal(Object.prototype.hasOwnProperty.call(metrics, 'prompt'), false)
    assert.equal(Object.prototype.hasOwnProperty.call(metrics, 'summary'), false)
  } finally {
    rmSync(dshHome, { recursive: true, force: true })
  }
})

test('tool schema hashes ignore object insertion order', () => {
  const first = [{
    name: 'hub_tool',
    description: 'tool',
    parameters: { type: 'object', properties: { z: { type: 'string' }, a: { type: 'string' } } },
  }]
  const second = [{
    parameters: { properties: { a: { type: 'string' }, z: { type: 'string' } }, type: 'object' },
    description: 'tool',
    name: 'hub_tool',
  }]
  assert.equal(hashToolSchemas(first), hashToolSchemas(second))
})
