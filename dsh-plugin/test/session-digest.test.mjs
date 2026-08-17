import { test } from 'node:test'
import assert from 'node:assert/strict'

import { deterministicSummary } from '../lib/summarizer.js'
import {
  digestEventIdForRound,
  extractFields,
} from '../lib/session-digest-watcher.js'
import { buildSessionDigest } from '../lib/digest.js'

const EVENTS = [
  { type: 'user/message', time: 1, data: { message: { content: [{ type: 'text', text: '分析这个 repo' }] } } },
  { type: 'tool/call', time: 2, data: {} },
  { type: 'assistant/message', time: 3, data: { message: { content: [{ type: 'text', text: '结论：AOT 是认知心理学范式' }] } } },
  { type: 'user/message', time: 4, data: { message: { content: [{ type: 'text', text: '继续' }] } } },
  { type: 'assistant/message', time: 5, data: { message: { content: [{ type: 'text', text: '补充：双过程理论' }] } } },
]

test('extractFields picks objective, last result, and counts', () => {
  const fields = extractFields({ id: 's1', events: EVENTS })
  assert.equal(fields.objective, '分析这个 repo')
  assert.equal(fields.resultText, '补充：双过程理论')
  assert.equal(fields.toolCount, 1)
  assert.equal(fields.messageCount, 4)
})

test('extractFields tolerates missing content', () => {
  const fields = extractFields({ id: 's2', events: [{ type: 'turn/start', time: 1 }] })
  assert.equal(fields.objective, '')
  assert.equal(fields.resultText, '')
  assert.equal(fields.messageCount, 0)
})

test('round event id is idempotent per round and differs across rounds', () => {
  const a = digestEventIdForRound('p1', 'session-1', 7)
  const b = digestEventIdForRound('p1', 'session-1', 7)
  const c = digestEventIdForRound('p1', 'session-1', 8)
  const d = digestEventIdForRound('p2', 'session-1', 7)
  assert.equal(a, b)
  assert.notEqual(a, c)
  assert.notEqual(a, d)
  assert.equal(a.length, 64)
})

test('buildSessionDigest accepts eventId and summary overrides', () => {
  const digest = buildSessionDigest(
    {
      principalId: 'p1',
      sessionId: 'session-1',
      actorId: 'a1',
      nodeId: 'n1',
      title: 'T',
      workspace: '/w',
      objective: 'o',
      status: 'done',
      resultText: 'r',
      toolCount: 1,
      messageCount: 2,
      createdAt: 1,
    },
    { eventId: 'x'.repeat(64), summary: '摘要文本' },
  )
  assert.equal(digest.event_id, 'x'.repeat(64))
  assert.equal(digest.payload.summary, '摘要文本')
})

test('deterministicSummary concatenates bounded fields', () => {
  const text = deterministicSummary({
    title: 'T',
    objective: '分析 repo',
    resultText: '结论是 X',
    toolCount: 3,
    messageCount: 5,
  })
  assert.ok(text.includes('分析 repo'))
  assert.ok(text.includes('结论是 X'))
  assert.ok(text.includes('3'))
  const long = deterministicSummary({ title: 'T', objective: 'y'.repeat(5000), resultText: '', toolCount: 0, messageCount: 0 })
  assert.ok(long.length <= 600)
})
