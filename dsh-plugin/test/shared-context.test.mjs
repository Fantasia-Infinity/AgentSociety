import { test } from 'node:test'
import assert from 'node:assert/strict'
import { join } from 'node:path'

import { buildSessionDigest, digestEventId } from '../lib/digest.js'
import { HubClient } from '../lib/hub-client.js'
import {
  consensusPromptLines,
  buildSharedContextSections,
} from '../lib/directory.js'

const INPUT = {
  principalId: 'principal-a',
  sessionId: 'session-1',
  actorId: 'actor-a',
  nodeId: 'node-a',
  taskId: 'task_1',
  runId: 'run_1',
  title: 'Run the suite',
  workspace: '/workspace/repo',
  objective: 'Run the suite and report',
  status: 'completed',
  resultText: 'all green',
  toolCount: 4,
  messageCount: 6,
  createdAt: 1_700_000_000_000,
}

test('digest is deterministic and idempotent per task run', () => {
  const first = buildSessionDigest(INPUT)
  const second = buildSessionDigest(INPUT)
  assert.equal(first.event_id, second.event_id)
  assert.equal(first.event_id, digestEventId(INPUT))
  assert.equal(first.event_id.length, 64)
  assert.equal(first.scope, 'consensus')
  assert.equal(first.kind, 'digest')
  assert.equal(first.ttl_hours, 720)
  // A different run produces a different event id.
  const other = buildSessionDigest({ ...INPUT, runId: 'run_2' })
  assert.notEqual(other.event_id, first.event_id)
})

test('digest bounds the result text', () => {
  const digest = buildSessionDigest({
    ...INPUT,
    resultText: 'x'.repeat(5_000),
  })
  assert.ok(digest.payload.result.length <= 1_000)
})

test('appendSharedEvent posts scope/kind/payload and returns the event', async () => {
  let captured
  const client = new HubClient('http://hub', 'token', async (_url, init) => {
    captured = JSON.parse(init.body)
    return {
      ok: true,
      status: 201,
      json: async () => ({ event: { seq: 7, event_id: captured.event_id } }),
    }
  })
  const event = await client.appendSharedEvent({
    scope: 'consensus',
    kind: 'digest',
    payload: { session_id: 's1' },
    principal_id: 'principal-a',
    session_id: 's1',
    actor_id: 'actor-a',
    node_id: 'node-a',
    event_id: 'evt-1',
    ttl_hours: 720,
  })
  assert.equal(event.seq, 7)
  assert.equal(captured.scope, 'consensus')
  assert.equal(captured.event_id, 'evt-1')
  assert.equal(captured.principal_id, 'principal-a')
})

test('listSharedEvents builds an after_seq query', async () => {
  let capturedUrl
  const client = new HubClient('http://hub', 'token', async (url) => {
    capturedUrl = url
    return { ok: true, status: 200, json: async () => ({ events: [] }) }
  })
  const events = await client.listSharedEvents({
    after_seq: 12,
    kind: 'digest',
    limit: 50,
  })
  assert.deepEqual(events, [])
  assert.ok(capturedUrl.includes('/v1/hub/contexts?'))
  assert.ok(capturedUrl.includes('after_seq=12'))
  assert.ok(capturedUrl.includes('kind=digest'))
  assert.ok(capturedUrl.includes('limit=50'))
})

test('HubClient question methods shape requests', async () => {
  const { HubClient } = await import('../lib/hub-client.js')
  let captured
  const client = new HubClient('http://hub', 'token', async (url, init) => {
    captured = { url, init }
    return {
      ok: true,
      status: 200,
      json: async () => ({
        question: { question_id: 'q1', lease_token: 'lt', answer_text: 'a', status: 'answered' },
        questions: [],
      }),
    }
  })
  await client.createQuestion({
    target_actor_id: 'actor-b',
    message: 'hi',
    require: 'status',
  })
  assert.ok(captured.url.includes('/v1/hub/questions'))
  assert.equal(JSON.parse(captured.init.body).target_actor_id, 'actor-b')

  await client.claimQuestions({ actor_id: 'actor-a', node_id: 'node-a' })
  assert.ok(captured.url.includes('/v1/hub/questions/claim'))

  await client.answerQuestion('q1', { lease_token: 'lt', answer_text: 'ok' })
  assert.ok(captured.url.includes('/v1/hub/questions/q1/answer'))

  await client.getQuestion('q1')
  assert.ok(captured.url.endsWith('/v1/hub/questions/q1'))
})

test('extractAnswer pulls the marked answer or falls back to full text', async () => {
  const { extractAnswer, ANSWER_MARKER } = await import('../lib/answer.js')
  assert.equal(
    extractAnswer(`Some preamble\n${ANSWER_MARKER} the answer is 42`),
    'the answer is 42',
  )
  assert.equal(extractAnswer('plain answer text'), 'plain answer text')
  const long = extractAnswer('x'.repeat(20_000))
  assert.ok(long.length <= 8000)
})

test('consensusPromptLines dedups identical (session, summary) pairs', () => {
  const mirror = {
    seq: 1,
    updated_at: 1,
    rows: {},
    consensus: {
      seq: 9,
      entries: [
        { seq: 9, kind: 'digest', session_id: 'dup-s', summary: 'same-digest' },
        { seq: 8, kind: 'digest', session_id: 'dup-s', summary: 'same-digest' },
        { seq: 7, kind: 'digest', session_id: 'other', summary: 'unique-1' },
        { seq: 6, kind: 'digest', session_id: 'dup-s', summary: 'same-digest' },
      ],
    },
  }
  const lines = consensusPromptLines(mirror)
  assert.equal(lines.filter(l => l.includes('same-digest')).length, 1)
  assert.equal(lines.length, 2)
})

test('consensusPromptLines excludes the current session digests', () => {
  const mirror = {
    seq: 1,
    updated_at: 1,
    rows: {},
    consensus: {
      seq: 5,
      entries: [
        { seq: 5, kind: 'digest', session_id: 'mine', summary: 'mine-latest' },
        { seq: 4, kind: 'digest', session_id: 'other', summary: 'other-1' },
        { seq: 3, kind: 'digest', session_id: 'mine', summary: 'mine-old' },
        { seq: 2, kind: 'decision', session_id: 'other', summary: 'decision-1' },
      ],
    },
  }
  const mine = consensusPromptLines(mirror, 'mine')
  assert.deepEqual(mine, [
    '- [digest] other: other-1',
    '- [decision] other: decision-1',
  ])
  // Without the filter every entry is injected.
  const all = consensusPromptLines(mirror)
  assert.equal(all.length, 4)
})

test('buildSharedContextSections excludes the current session row', () => {
  const mirror = {
    seq: 1,
    updated_at: 1,
    consensus: { seq: 1, entries: [] },
    rows: {
      'mine': { session_id: 'mine', title: 'T', workspace: '/w', status: 'idle', last_active_at: 3, session_mode: 'per_task', tool_policy: 'full', invocations: [] },
      'other': { session_id: 'other', title: 'O', workspace: '/o', status: 'idle', last_active_at: 2, session_mode: 'continuous', tool_policy: 'full', invocations: [] },
    },
  }
  const sections = buildSharedContextSections(mirror, 'mine')
  const dir = sections.find(s => s.name === 'agent-society:directory-index')
  assert.ok(dir)
  assert.ok(dir.text.includes('other'))
  assert.ok(!dir.text.includes('mine |'))
})

test('prompt-guard injects once and skips unchanged snapshots', async () => {
  const { apply } = await import('../lib/prompt-guard.js')
  const { saveMirror } = await import('../lib/directory.js')
  const { mkdtempSync, rmSync, mkdirSync } = await import('node:fs')
  const { tmpdir } = await import('node:os')
  const dir = mkdtempSync(join(tmpdir(), 'dsh-pg2-'))
  const mirrorFile = join(dir, 'agent-society-directory.json')
  const orig = process.env.DSH_HOME
  const sections = []
  let handler
  const ctx = {
    on: (_evt, fn) => { handler = fn },
    get: (name) => name === 'systemPrompt'
      ? { section: (input) => sections.push(input) }
      : undefined,
    logger: { warn: () => {} },
  }
  try {
    mkdirSync(dir, { recursive: true })
    process.env.DSH_HOME = dir
    saveMirror(mirrorFile, {
      seq: 1, updated_at: 1, rows: {},
      consensus: { seq: 1, entries: [
        { seq: 1, kind: 'digest', session_id: 'other', summary: 's1' },
      ] },
    })
    apply(ctx)
    // Static workflow section registered once.
    assert.ok(sections.some(s => s.name === 'agent-society:workflow'))
    const assembly = { sections: [], contexts: [], tools: [], variables: {} }
    const _context = { agent: { session: { id: 'mine' } } }
    const next = async () => assembly
    const first = await handler(assembly, _context, next)
    assert.equal(first.contexts.length, 1, 'first assembly injects')
    // Same snapshot again -> no injection.
    const second = await handler(assembly, _context, next)
    assert.equal(second.contexts.length, 0, 'unchanged snapshot is skipped')
    // Mirror changes -> injects again.
    saveMirror(mirrorFile, {
      seq: 2, updated_at: 2, rows: {},
      consensus: { seq: 2, entries: [
        { seq: 2, kind: 'digest', session_id: 'other', summary: 's2' },
      ] },
    })
    const third = await handler(assembly, _context, next)
    assert.equal(third.contexts.length, 1, 'changed snapshot injects')
    assert.ok(third.contexts[0].text.includes('s2'))
  } finally {
    if (orig === undefined) delete process.env.DSH_HOME
    else process.env.DSH_HOME = orig
    rmSync(dir, { recursive: true, force: true })
  }
})

test('prompt-guard appends shared context as a runtime context, not a section', async () => {
  const { apply } = await import('../lib/prompt-guard.js')
  const { saveMirror } = await import('../lib/directory.js')
  const { mkdtempSync, writeFileSync, rmSync, mkdirSync } = await import('node:fs')
  const { tmpdir } = await import('node:os')
  const dir = mkdtempSync(join(tmpdir(), 'dsh-pg-'))
  const mirrorFile = join(dir, 'agent-society-directory.json')
  const origMirrorPath = process.env.DSH_HOME
  try {
    mkdirSync(dir, { recursive: true })
    process.env.DSH_HOME = dir
    saveMirror(mirrorFile, {
      seq: 1, updated_at: 1, rows: {},
      consensus: { seq: 1, entries: [
        { seq: 1, kind: 'digest', session_id: 'other', summary: 'summary with {{braces}} inside' },
      ] },
    })
    let handler
    const ctx = {
      on: (_evt, fn) => { handler = fn },
      get: () => undefined,
      logger: { warn: () => {} },
    }
    apply(ctx)
    const assembly = { sections: [], contexts: [], tools: [], variables: {} }
    const _context = { agent: { session: { id: 'mine' } } }
    const next = async () => assembly
    const out = await handler(assembly, _context, next)
    assert.ok(out.contexts.length === 1, 'one runtime context appended')
    assert.equal(out.contexts[0].name, 'agent-society:shared-context')
    assert.ok(out.contexts[0].text.includes('共享共识上下文'))
    // {{ braces neutralized so renderContextSections interpolation cannot throw
    assert.ok(!out.contexts[0].text.includes('{{braces}}'))
    assert.equal(out.sections.length, 0, 'no system sections touched')
  } finally {
    if (origMirrorPath === undefined) delete process.env.DSH_HOME
    else process.env.DSH_HOME = origMirrorPath
    rmSync(dir, { recursive: true, force: true })
  }
})
