import { test } from 'node:test'
import assert from 'node:assert/strict'

import { buildSessionDigest, digestEventId } from '../lib/digest.js'
import { HubClient } from '../lib/hub-client.js'

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
