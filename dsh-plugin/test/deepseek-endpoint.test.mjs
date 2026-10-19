import { test } from 'node:test';
import assert from 'node:assert/strict';
import { deepSeekMessagesEndpoint } from '../lib/deepseek-endpoint.js';

test('legacy official roots use the Messages endpoint', () => {
  for (const root of ['https://api.deepseek.com', 'https://api.deepseek.com/', 'https://api.deepseek.com/v1']) {
    assert.equal(deepSeekMessagesEndpoint(root), 'https://api.deepseek.com/anthropic');
  }
});

test('native and custom endpoints remain unchanged', () => {
  for (const root of ['https://api.deepseek.com/anthropic', 'https://gateway.example/v1', 'http://localhost:3000']) {
    assert.equal(deepSeekMessagesEndpoint(root), root);
  }
});
