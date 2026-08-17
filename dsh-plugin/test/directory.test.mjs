import { test } from 'node:test'
import assert from 'node:assert/strict'
import { mkdtempSync, readFileSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'

import {
  buildLocalRow,
  loadMirror,
  mergeInvocation,
  saveMirror,
  mirrorPath,
} from '../lib/directory.js'

test('mirror round-trip preserves rows and seq', () => {
  const dir = mkdtempSync(join(tmpdir(), 'dsh-mirror-'))
  try {
    const path = mirrorPath(dir)
    saveMirror(path, {
      seq: 42,
      updated_at: 1,
      rows: {
        'session-1': {
          session_id: 'session-1',
          title: 'T',
          workspace: '/w',
          status: 'idle',
          last_active_at: 2,
          session_mode: 'per_task',
          tool_policy: 'full',
          invocations: [],
        },
      },
    })
    const loaded = loadMirror(path)
    assert.equal(loaded.seq, 42)
    assert.equal(loaded.rows['session-1'].title, 'T')
  } finally {
    rmSync(dir, { recursive: true, force: true })
  }
})

test('loadMirror tolerates missing and corrupt files', () => {
  const dir = mkdtempSync(join(tmpdir(), 'dsh-mirror-'))
  try {
    assert.deepEqual(loadMirror(join(dir, 'missing.json')), {
      seq: 0,
      updated_at: 0,
      rows: {},
      consensus: { seq: 0, entries: [] },
    })
    const path = join(dir, 'bad.json')
    saveMirror(path, { seq: 1, updated_at: 1, rows: { 's1': { junk: true } } })
    const loaded = loadMirror(path)
    assert.equal(loaded.seq, 1)
    assert.deepEqual(loaded.rows, {})
  } finally {
    rmSync(dir, { recursive: true, force: true })
  }
})

test('mergeInvocation appends, dedupes by run and caps history', () => {
  const invocation = (runId, status, at) => ({
    task_id: `task_${runId}`,
    run_id: runId,
    objective: `objective ${runId}`,
    status,
    at,
  })
  const merged = mergeInvocation({
    row: undefined,
    sessionId: 'session-1',
    workspace: '/w',
    title: 'First',
    sessionMode: 'continuous',
    toolPolicy: 'full',
    invocation: invocation('run-1', 'completed', 100),
  })
  assert.equal(merged.status, 'done')
  assert.equal(merged.invocations.length, 1)
  assert.equal(merged.session_mode, 'continuous')

  const again = mergeInvocation({
    row: merged,
    sessionId: 'session-1',
    workspace: '/w',
    title: undefined,
    sessionMode: 'continuous',
    toolPolicy: 'full',
    invocation: invocation('run-1', 'failed', 200),
  })
  // Same run: replaced, not duplicated.
  assert.equal(again.invocations.length, 1)
  assert.equal(again.invocations[0].status, 'failed')
  assert.equal(again.status, 'failed')

  let row = again
  for (let index = 0; index < 15; index += 1) {
    row = mergeInvocation({
      row,
      sessionId: 'session-1',
      workspace: '/w',
      title: undefined,
      sessionMode: 'continuous',
      toolPolicy: 'full',
      invocation: invocation(`run-${100 + index}`, 'completed', 300 + index),
    })
  }
  assert.ok(row.invocations.length <= 10, 'history is capped')
  // Most recent first.
  assert.equal(row.invocations[0].run_id, 'run-114')
})

test('buildLocalRow produces a depth-0/1 row', () => {
  const row = buildLocalRow({
    sessionId: 'session-1',
    title: 'My session',
    workspace: '/repo',
    lastActiveAt: 5,
    sessionMode: 'per_task',
    toolPolicy: 'read_only',
  })
  assert.equal(row.session_id, 'session-1')
  assert.equal(row.title, 'My session')
  assert.equal(row.status, 'idle')
  assert.deepEqual(row.invocations, [])
})

test('HubClient directory methods shape requests', async () => {
  let captured
  const client = new (await import('../lib/hub-client.js')).HubClient(
    'http://hub',
    'token',
    async (url, init) => {
      captured = { url, init }
      return {
        ok: true,
        status: 200,
        json: async () => ({ row: { session_id: 's1' }, rows: [], event: {} }),
      }
    },
  )
  await client.upsertDirectoryRow({
    session_id: 's1',
    row: { title: 'T' },
    principal_id: 'p1',
    actor_id: 'a1',
    node_id: 'n1',
  })
  assert.ok(captured.url.includes('/v1/hub/directory/s1'))
  const body = JSON.parse(captured.init.body)
  assert.equal(body.actor_id, 'a1')
  assert.equal(body.title, 'T')

  await client.listDirectory({ after_seq: 5, limit: 20 })
  assert.ok(captured.url.includes('after_seq=5'))

  await client.getDirectoryRow('s1', 2)
  assert.ok(captured.url.includes('/v1/hub/directory/s1?depth=2'))
})

test('prompt sections respect the 4KB budget and ranking', async () => {
  const { buildSharedContextSections } = await import('../lib/directory.js')
  const rows = {}
  for (let index = 0; index < 40; index += 1) {
    rows[`session-${index}`] = {
      session_id: `session-${index}`,
      actor_id: `actor-${index % 3}`,
      title: `Title ${index} `.repeat(10),
      workspace: '/w',
      status: index === 1 ? 'working' : 'idle',
      last_active_at: index === 1 ? 999 : 1000 - index,
      session_mode: 'per_task',
      tool_policy: 'full',
      invocations: [],
    }
  }
  const mirror = {
    seq: 100,
    updated_at: 1,
    rows,
    consensus: {
      seq: 50,
      entries: Array.from({ length: 30 }, (_, index) => ({
        seq: index,
        kind: 'digest',
        session_id: `session-${index}`,
        summary: `Summary ${index} `.repeat(20),
      })),
    },
  }
  const sections = buildSharedContextSections(mirror)
  const total = sections.reduce((sum, s) => sum + s.text.length, 0)
  assert.ok(total <= 4000, `total ${total} exceeds budget`)
  const indexText = sections.find((s) => s.name === 'agent-society:directory-index').text
  assert.ok(indexText.includes('| working'), 'working row is ranked first')
  assert.ok(
    indexText.indexOf('session-1 |') < indexText.indexOf('session-0 |'),
    'working row appears before idle rows',
  )
})

test('empty mirror yields no sections', async () => {
  const { buildSharedContextSections } = await import('../lib/directory.js')
  const sections = buildSharedContextSections({
    seq: 0,
    updated_at: 0,
    rows: {},
    consensus: { seq: 0, entries: [] },
  })
  assert.deepEqual(sections, [])
})
