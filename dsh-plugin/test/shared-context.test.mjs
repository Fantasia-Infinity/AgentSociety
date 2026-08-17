import { test } from 'node:test'
import assert from 'node:assert/strict'

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
