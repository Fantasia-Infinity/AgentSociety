import { test } from 'node:test'
import assert from 'node:assert/strict'

import {
  pickTargetSession,
  extractAnswer,
} from '../lib/answer.js'

const ROW = (sessionId, overrides = {}) => ({
  session_id: sessionId,
  actor_id: 'pi-node230',
  session_mode: 'per_task',
  status: 'idle',
  last_active_at: 100,
  ...overrides,
})

test('pickTargetSession prefers the most recent continuous idle session', () => {
  const rows = [
    ROW('task-latest', { session_mode: 'per_task', last_active_at: 500 }),
    ROW('continuous-old', { session_mode: 'continuous', last_active_at: 200 }),
    ROW('continuous-new', { session_mode: 'continuous', last_active_at: 400 }),
  ]
  assert.equal(pickTargetSession(rows, {}), 'continuous-new')
})

test('pickTargetSession falls back to any recent idle session', () => {
  const rows = [
    ROW('task-new', { session_mode: 'per_task', last_active_at: 900 }),
    ROW('task-old', { session_mode: 'per_task', last_active_at: 100 }),
  ]
  assert.equal(pickTargetSession(rows, {}), 'task-new')
})

test('pickTargetSession excludes working sessions and the current session', () => {
  const rows = [
    ROW('working', { status: 'working', last_active_at: 999 }),
    ROW('mine', { last_active_at: 800 }),
    ROW('other', { last_active_at: 700 }),
  ]
  assert.equal(pickTargetSession(rows, { currentSessionId: 'mine' }), 'other')
})

test('pickTargetSession filters by actor and returns undefined when empty', () => {
  const rows = [ROW('a', { actor_id: 'pi-other' })]
  assert.equal(pickTargetSession(rows, { actorId: 'pi-node230' }), undefined)
  assert.equal(pickTargetSession([], {}), undefined)
})

test('extractAnswer pulls the ANSWER marker text', () => {
  assert.equal(extractAnswer('ANSWER: 42'), '42')
  assert.equal(extractAnswer('prefix\nANSWER: hello world\n'), 'hello world')
  assert.equal(extractAnswer('no marker text'), 'no marker text')
  assert.equal(extractAnswer('x'.repeat(9000)).length, 8000)
})

import { answerQuestion } from '../lib/answer.js'

function mockCtx() {
  const followups = []
  const events = []
  let idle = false
  const agent = {
    followup: (msg) => { followups.push(msg); events.push({ type: 'assistant/message', data: { message: { content: [{ type: 'text', text: 'ANSWER: injected reply' }] } } }); idle = true },
    session: { events },
    whenIdle: async () => { idle = true },
  }
  const resumed = []
  const created = []
  const ctx = {
    agents: {
      resume: async (opts) => { resumed.push(opts); return { agent, dispose: async () => {} } },
      create: async (opts) => { created.push(opts); return { agent, dispose: async () => {} } },
    },
    logger: { warn: () => {} },
  }
  return { ctx, resumed, created, followups }
}

test('answerQuestion resumes the explicit target session and appends the question', async () => {
  const { ctx, resumed, followups } = mockCtx()
  const answer = await answerQuestion(ctx, { message: 'q1', target_session_id: 'session-target' }, {
    provider: 'p', model: 'm', cwd: '/w', setup: undefined,
    hub: { listDirectory: async () => [] }, actorId: 'me',
  })
  assert.equal(answer, 'injected reply')
  assert.equal(resumed[0].resumeSessionId, 'session-target')
  assert.match(followups[0].content[0].text, /Using the conversation context above/)
  assert.match(followups[0].content[0].text, /Question: q1/)
})

test('answerQuestion resolves the default target from the directory when absent', async () => {
  const { ctx, resumed } = mockCtx()
  const rows = [{ session_id: 'continuous-x', actor_id: 'me', session_mode: 'continuous', status: 'idle', last_active_at: 5 }]
  await answerQuestion(ctx, { message: 'q2' }, {
    provider: 'p', model: 'm', cwd: '/w', setup: undefined,
    hub: { listDirectory: async (item) => { assert.equal(item.actor_id, 'me'); return rows } },
    actorId: 'me',
  })
  assert.equal(resumed[0].resumeSessionId, 'continuous-x')
})

test('answerQuestion falls back to a one-shot session on resume failure', async () => {
  const { ctx, created } = mockCtx()
  ctx.agents.resume = async () => { throw new Error('session busy') }
  const answer = await answerQuestion(ctx, { message: 'q3' }, {
    provider: 'p', model: 'm', cwd: '/w', setup: undefined,
    hub: { listDirectory: async () => [{ session_id: 'continuous-x', actor_id: 'me', session_mode: 'continuous', status: 'idle', last_active_at: 5 }] },
    actorId: 'me',
  })
  assert.equal(answer, 'injected reply')
  assert.ok(created.length === 1, 'one-shot fallback created a session')
})
