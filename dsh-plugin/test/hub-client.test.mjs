import { test } from 'node:test'
import assert from 'node:assert/strict'

import { HubClient } from '../lib/hub-client.js'

function jsonResponse(payload) {
  return {
    ok: true,
    status: 200,
    json: async () => payload,
  }
}

function sseResponse(chunks) {
  const body = new ReadableStream({
    start(controller) {
      for (const chunk of chunks) {
        controller.enqueue(new TextEncoder().encode(chunk))
      }
      controller.close()
    },
  })
  return { ok: true, status: 200, body }
}

test('updateTask sends partial_result when provided', async () => {
  let captured
  const client = new HubClient('http://hub', 'token', async (_url, init) => {
    captured = JSON.parse(init.body)
    return jsonResponse({})
  })
  await client.updateTask('task_1', {
    run_id: 'run_1',
    lease_token: 'lease-1',
    status: 'working',
    result: {},
    partial_result: { phase: 'tool', toolCount: 2 },
  })
  assert.equal(captured.status, 'working')
  assert.deepEqual(captured.partial_result, { phase: 'tool', toolCount: 2 })
  assert.equal(captured.run_id, 'run_1')
})

test('updateTask omits partial_result when absent', async () => {
  let captured
  const client = new HubClient('http://hub', 'token', async (_url, init) => {
    captured = JSON.parse(init.body)
    return jsonResponse({})
  })
  await client.updateTask('task_1', {
    run_id: 'run_1',
    lease_token: 'lease-1',
    status: 'completed',
    result: { text: 'done' },
  })
  assert.equal(captured.partial_result, undefined)
  assert.equal(captured.status, 'completed')
})

test('claimTask passes wait_seconds through', async () => {
  let captured
  const client = new HubClient('http://hub', 'token', async (_url, init) => {
    captured = JSON.parse(init.body)
    return jsonResponse({ claim: null })
  })
  const claim = await client.claimTask({
    actor_id: 'actor-a',
    node_id: 'node-a',
    wait_seconds: 25,
    lease_seconds: 300,
  })
  assert.equal(claim, null)
  assert.equal(captured.wait_seconds, 25)
  assert.equal(captured.lease_seconds, 300)
  assert.equal(captured.node_id, 'node-a')
})

test('subscribeEvents parses SSE blocks and boundary-split chunks', async () => {
  const client = new HubClient('http://hub', 'token', async (url, init) => {
    assert.equal(init.headers.Authorization, 'Bearer token')
    assert.ok(url.includes('/v1/hub/events?node_id=node-a'))
    return sseResponse([
      'retry: 30000\n\nevent: connected\ndata: {"node_id":"node-a"}\n\n',
      'event: control/new\nda',
      'ta: {"task_id":"task_1","control_id":"c1","kind":"steer"}\n\n',
      ': keep-alive\n\n',
      'event: task/cancelled\ndata: {"task_id":"task_1","reason":"x"}\n\n',
    ])
  })
  const events = []
  await client.subscribeEvents('node-a', (event) => events.push(event))
  assert.deepEqual(events, [
    { name: 'connected', data: { node_id: 'node-a' } },
    { name: 'control/new', data: { task_id: 'task_1', control_id: 'c1', kind: 'steer' } },
    { name: 'task/cancelled', data: { task_id: 'task_1', reason: 'x' } },
  ])
})

test('subscribeEvents rejects on non-ok status', async () => {
  const client = new HubClient('http://hub', 'token', async () => ({
    ok: false,
    status: 403,
  }))
  await assert.rejects(
    client.subscribeEvents('node-a', () => {}),
    /403/,
  )
})

test('subscribeEvents aborts on signal', async () => {
  const client = new HubClient('http://hub', 'token', async (_url, init) => {
    assert.ok(init.signal !== undefined)
    return sseResponse(['event: connected\ndata: {}\n\n'])
  })
  const controller = new AbortController()
  await client.subscribeEvents('node-a', () => {}, {
    signal: controller.signal,
  })
  controller.abort()
})
