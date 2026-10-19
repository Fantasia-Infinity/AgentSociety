import { test } from 'node:test'
import assert from 'node:assert/strict'

import { deterministicSummary, summarizeLiveSession, summarizeStandalone, SUMMARIZE_SYSTEM } from '../lib/summarizer.js'
import {
  digestEventIdForRound,
  digestLeasePath,
  extractFields,
  tryAcquireDigestLease,
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

test('digest leases are independent for different rounds', () => {
  const first = digestLeasePath('/tmp/dsh-home', 'round-a')
  const second = digestLeasePath('/tmp/dsh-home', 'round-b')
  assert.notEqual(first, second)
  assert.match(first, /agent-society-digest-leases/u)
  assert.match(first, /\.lease$/u)
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

test('messageCountOf counts only user and assistant messages', async () => {
  const { messageCountOf } = await import('../lib/session-digest-watcher.js')
  const events = [
    { type: 'user/message', time: 1 },
    { type: 'tool/call', time: 2 },
    { type: 'assistant/message', time: 3 },
    { type: 'activity/status', time: 4 },
    { type: 'user/message', time: 5 },
    { type: 'session/title', time: 6 },
  ]
  assert.equal(messageCountOf(events), 3)
})

test('digest lease deduplicates concurrent rounds and recovers after expiry', async () => {
  const { mkdtempSync, readFileSync, rmSync, writeFileSync } = await import('node:fs')
  const { join } = await import('node:path')
  const { tmpdir } = await import('node:os')
  const dir = mkdtempSync(join(tmpdir(), 'dsh-digest-lease-'))
  const path = join(dir, 'digest.lease')
  try {
    const first = tryAcquireDigestLease(path, 'round-a', 1000)
    assert.equal(first.status, 'acquired')
    assert.equal(tryAcquireDigestLease(path, 'round-a', 1001).status, 'busy')
    first.release()
    const second = tryAcquireDigestLease(path, 'round-a', 1002)
    assert.equal(second.status, 'acquired')
    second.release()
    writeFileSync(path, JSON.stringify({ owner: 'old', eventId: 'old', createdAt: 1000 }))
    const recovered = tryAcquireDigestLease(path, 'round-b', 1000 + 90 * 1000)
    assert.equal(recovered.status, 'acquired')
    assert.notEqual(JSON.parse(readFileSync(path, 'utf8')).owner, 'old')
    recovered.release()
  } finally {
    rmSync(dir, { recursive: true, force: true })
  }
})

test('live summarizer uses the target session request header and route', async () => {
  let preparedConfig
  let request
  const ctx = {
    get(name) {
      assert.equal(name, 'llm')
      return {
        async prepareCall(config) {
          preparedConfig = config
          return {
            config,
            async *stream(options) {
              request = options
              yield { type: 'text-delta', index: 0, text: '目标摘要' }
            },
          }
        },
      }
    },
  }
  const header = {
    config: { provider: 'target-provider', model: 'target-model', temperature: 0.2 },
    system: 'target system',
    tools: [{ name: 'target_tool', description: 'target', parameters: { type: 'object' } }],
  }
  const text = await summarizeLiveSession({
    ctx,
    session: { deriveMessages: () => [{ role: 'user', content: [], source: { kind: 'user' }, id: 'history' }] },
    requestHeader: header,
    maxTokens: 768,
  })
  assert.equal(text, '目标摘要')
  assert.equal(preparedConfig.provider, 'target-provider')
  assert.equal(preparedConfig.model, 'target-model')
  assert.equal(preparedConfig.temperature, 0.2)
  assert.equal(request.system, 'target system')
  assert.deepEqual(request.tools, header.tools)
  assert.equal(request.messages.length, 2)
})

test('standalone summarizer keeps a fixed system prompt and structured payload', async () => {
  let request
  const ctx = {
    get() {
      return {
        async prepareCall(config) {
          return {
            config,
            async *stream(options) {
              request = options
              yield { type: 'text-delta', index: 0, text: '独立摘要' }
            },
          }
        },
      }
    },
  }
  const text = await summarizeStandalone({
    ctx,
    fields: { title: 'T', objective: 'O', resultText: 'R', messageCount: 2, toolCount: 1 },
    provider: 'p',
    model: 'm',
    maxTokens: 4096,
  })
  assert.equal(text, '独立摘要')
  assert.equal(request.system, SUMMARIZE_SYSTEM)
  assert.deepEqual(JSON.parse(request.messages[0].content[0].text), {
    title: 'T', objective: 'O', result: 'R', messageCount: 2, toolCount: 1,
  })
})
