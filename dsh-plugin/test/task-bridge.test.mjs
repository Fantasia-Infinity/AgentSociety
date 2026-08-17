import { test } from 'node:test'
import assert from 'node:assert/strict'

import { apply, name } from '../lib/task-bridge.js'

const TASK = {
  task_id: 'task_test_1',
  principal_id: 'human-tester',
  delegator_actor_id: 'human-tester',
  assignee_actor_id: null,
  objective: 'Do the thing',
  required_capabilities: [],
  input: { note: 'x' },
  status: 'submitted',
  result: {},
  error: null,
}

const RUN = {
  run_id: 'run_abc123',
  task_id: 'task_test_1',
  principal_id: 'human-tester',
  actor_id: 'agent-society-testhost',
  node_id: 'testhost',
  status: 'active',
  result: {},
}

const LEASE = 'lease_xyz'

function jsonResponse(body, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

/** Minimal Cordis-like context for the plugin's apply(). */
function mockCtx({ agentStatus = 'idle' } = {}) {
  const polls = []
  const warns = []
  const infos = []
  const agent = {
    status: agentStatus,
    session: {
      id: 'ui-session-1',
      events: [
        {
          type: 'user/message',
          data: { message: { content: [{ type: 'text', text: 'hi' }] } },
        },
        {
          type: 'assistant/message',
          data: { message: { content: [{ type: 'text', text: 'hello back' }] } },
        },
      ],
    },
    followup(message) {
      this.session.events.push({
        type: 'user/message',
        data: { message: { content: [{ type: 'text', text: 'TASK' }] } },
      })
      this.session.events.push({
        type: 'assistant/message',
        data: {
          message: {
            content: [{ type: 'text', text: 'TASK RESULT: done' }],
          },
        },
      })
      this.status = 'idle'
    },
    async whenIdle() {},
  }
  const agents = { list: () => [agent] }
  const ctx = {
    agents,
    get(service) {
      if (service === 'agents') return agents
      return undefined
    },
    setInterval(fn) {
      polls.push(fn)
      return { dispose: () => {} }
    },
    effect() {},
    logger: {
      warn: (msg) => warns.push(msg),
      info: (msg) => infos.push(msg),
    },
  }
  return { ctx, polls, warns, infos, agent }
}

/** HubClient with a fake fetch; records every request body by path. */
function mockHub(claim = { task: TASK, run: RUN, lease_token: LEASE }) {
  const calls = []
  const fetchImpl = async (url, init) => {
    const path = new URL(url).pathname
    const body = init.body ? JSON.parse(init.body) : undefined
    calls.push({ path, body })
    if (path === '/v1/hub/tasks/claim') {
      return jsonResponse({ claim })
    }
    if (path === '/v1/hub/contexts/append') {
      return jsonResponse({ event: { seq: 7, event_id: 'evt' } })
    }
    return jsonResponse({ ok: true })
  }
  return { calls, fetchImpl }
}

test('module surface is exported', () => {
  assert.equal(name, 'agent-society-task-bridge')
  assert.equal(typeof apply, 'function')
})

test('stays idle without hub credentials', () => {
  const { ctx, warns } = mockCtx()
  apply(ctx, {})
  assert.ok(
    warns.some((w) => w.includes('credentials are required')),
    `expected credentials warning, got ${warns.join(' | ')}`,
  )
})

test('claims and executes a task in the idle UI session', async () => {
  const oldUrl = process.env.AGENT_SOCIETY_HUB_URL
  const oldToken = process.env.AGENT_SOCIETY_HUB_TOKEN
  const oldFetch = globalThis.fetch
  process.env.AGENT_SOCIETY_HUB_URL = 'http://hub.test'
  process.env.AGENT_SOCIETY_HUB_TOKEN = 'token'
  try {
    const { ctx, polls, infos, agent } = mockCtx()
    const hub = mockHub()
    globalThis.fetch = hub.fetchImpl
    apply(ctx, {
      hubUrl: 'http://hub.test',
      hubTokenEnv: 'AGENT_SOCIETY_HUB_TOKEN',
      pollSeconds: 30,
      actorId: 'agent-society-testhost',
      nodeId: 'testhost',
    })
    // apply fires one poll immediately and registers a timer.
    assert.ok(polls.length >= 1)
    // Drive the timer poll(s) to completion.
    for (const poll of polls) await poll()
    await new Promise((r) => setTimeout(r, 10))

    const claims = hub.calls.filter((c) => c.path === '/v1/hub/tasks/claim')
    assert.equal(claims.length, 1)
    assert.equal(claims[0].body.actor_id, 'agent-society-testhost')

    const taskUpdates = hub.calls.filter((c) => c.path.endsWith('/updates') && c.body?.run_id === RUN.run_id && c.body?.lease_token === LEASE)
    const statuses = taskUpdates.map((c) => c.body.status)
    assert.ok(statuses.includes('working'), `expected working update, got ${statuses}`)
    assert.ok(statuses.includes('completed'), `expected completed update, got ${statuses}`)

    const completed = taskUpdates.find((c) => c.body.status === 'completed')
    assert.equal(completed.body.result.text, 'TASK RESULT: done')
    assert.equal(completed.body.result.dsh_session_id, 'ui-session-1')
    assert.equal(completed.body.result.dsh_tool_policy, 'full')

    // The digest was appended to shared memory.
    const appends = hub.calls.filter((c) => c.path === '/v1/hub/contexts/append')
    assert.equal(appends.length, 1)
    assert.equal(appends[0].body.scope, 'consensus')
    assert.equal(appends[0].body.kind, 'digest')
    assert.equal(appends[0].body.payload.task_id, 'task_test_1')
    assert.equal(appends[0].body.payload.status, 'completed')
    assert.equal(appends[0].body.payload.result, 'TASK RESULT: done')

    assert.ok(
      infos.some((i) => i.includes('completed task_test_1 in session ui-session-1')),
      `expected completion log, got ${infos.join(' | ')}`,
    )
    assert.ok(
      agent.session.events.some((e) =>
        JSON.stringify(e).includes('TASK RESULT: done'),
      ),
      'task execution landed in the session history',
    )
  } finally {
    globalThis.fetch = oldFetch
    if (oldUrl === undefined) delete process.env.AGENT_SOCIETY_HUB_URL
    else process.env.AGENT_SOCIETY_HUB_URL = oldUrl
    if (oldToken === undefined) delete process.env.AGENT_SOCIETY_HUB_TOKEN
    else process.env.AGENT_SOCIETY_HUB_TOKEN = oldToken
  }
})

test('does not claim while the session agent is busy', async () => {
  const oldUrl = process.env.AGENT_SOCIETY_HUB_URL
  const oldToken = process.env.AGENT_SOCIETY_HUB_TOKEN
  const oldFetch = globalThis.fetch
  process.env.AGENT_SOCIETY_HUB_URL = 'http://hub.test'
  process.env.AGENT_SOCIETY_HUB_TOKEN = 'token'
  try {
    const { ctx, polls } = mockCtx({ agentStatus: 'running' })
    const hub = mockHub()
    globalThis.fetch = hub.fetchImpl
    apply(ctx, { hubUrl: 'http://hub.test', pollSeconds: 30 })
    for (const poll of polls) await poll()
    await new Promise((r) => setTimeout(r, 10))
    const claims = hub.calls.filter((c) => c.path === '/v1/hub/tasks/claim')
    assert.equal(claims.length, 0, 'busy session must not claim tasks')
  } finally {
    globalThis.fetch = oldFetch
    if (oldUrl === undefined) delete process.env.AGENT_SOCIETY_HUB_URL
    else process.env.AGENT_SOCIETY_HUB_URL = oldUrl
    if (oldToken === undefined) delete process.env.AGENT_SOCIETY_HUB_TOKEN
    else process.env.AGENT_SOCIETY_HUB_TOKEN = oldToken
  }
})

test('marks the task failed when execution throws', async () => {
  const oldUrl = process.env.AGENT_SOCIETY_HUB_URL
  const oldToken = process.env.AGENT_SOCIETY_HUB_TOKEN
  const oldFetch = globalThis.fetch
  process.env.AGENT_SOCIETY_HUB_URL = 'http://hub.test'
  process.env.AGENT_SOCIETY_HUB_TOKEN = 'token'
  try {
    const { ctx, polls } = mockCtx()
    const agent = ctx.agents.list()[0]
    agent.whenIdle = async () => {
      throw new Error('agent crashed')
    }
    const hub = mockHub()
    globalThis.fetch = hub.fetchImpl
    apply(ctx, { hubUrl: 'http://hub.test', pollSeconds: 30 })
    for (const poll of polls) await poll()
    await new Promise((r) => setTimeout(r, 10))
    const taskUpdates = hub.calls.filter((c) => c.path.endsWith('/updates') && c.body?.lease_token === LEASE)
    const statuses = taskUpdates.map((c) => c.body.status)
    assert.ok(statuses.includes('failed'), `expected failed update, got ${statuses}`)
  } finally {
    globalThis.fetch = oldFetch
    if (oldUrl === undefined) delete process.env.AGENT_SOCIETY_HUB_URL
    else process.env.AGENT_SOCIETY_HUB_URL = oldUrl
    if (oldToken === undefined) delete process.env.AGENT_SOCIETY_HUB_TOKEN
    else process.env.AGENT_SOCIETY_HUB_TOKEN = oldToken
  }
})
