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
