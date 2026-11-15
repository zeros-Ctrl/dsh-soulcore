import test from 'node:test'
import assert from 'node:assert/strict'
import { toEvent, compactionMessages, formatSearch } from '../lib/glue.js'

test('records full original text and stable source metadata', () => {
  const original = '长消息'.repeat(3000) + '尾部关键决定'
  const event = toEvent({ session: { id: 's1', header: { cwd: 'C:\\projects\\same' } } }, original, {}, { event_id: 'user-42' })
  assert.equal(event.text, original)
  assert.equal(event.event_id, 'user-42')
  assert.equal(event.session_id, 's1')
  assert.equal(event.cwd, 'C:\\projects\\same')
  const compacted = compactionMessages([{ id: 'message-42', role: 'user', source: { kind: 'user' }, content: original }])
  assert.equal(compacted[0].content, original)
  assert.equal(compacted[0].source_event_id, 'message-42')
})

test('tool verification evidence uses structured result and original call identity', () => {
  const items = compactionMessages([
    { role: 'assistant', content: [{ type: 'tool-call', id: 'call-1', name: 'exec_command', arguments: { cmd: 'python -m unittest' } }] },
    { role: 'tool', tool_call_id: 'call-1', output: { exit_code: 0, stdout: 'All tests passed' } },
    { role: 'assistant', content: '我保证所有测试已通过' },
  ])
  assert.equal(items[0].role, 'tool')
  assert.equal(items[0].evidence.command, 'python -m unittest')
  assert.equal(items[0].evidence.exit_code, 0)
  assert.equal(items[0].source_event_id, 'tool:call-1')
  assert.equal(items[1].evidence, undefined)
})

test('text saying passed does not manufacture a structured exit code', () => {
  const [item] = compactionMessages([{ role: 'tool', content: 'passed; exit_code=0', name: 'exec_command' }])
  assert.equal(item.evidence.exit_code, undefined)
})

test('tool evidence binds the actual triggering human event without borrowing snapshots', () => {
  const items = compactionMessages([
    { id: 'user-1', role: 'user', source: { kind: 'user' }, content: '运行第一批测试' },
    { id: 'snapshot', role: 'user', source: { kind: 'runtime' }, content: '注入' },
    { role: 'assistant', content: [{ type: 'tool-call', id: 'call-1', name: 'exec_command', arguments: { cmd: 'pytest first.py' } }] },
    { id: 'user-2', role: 'user', source: { kind: 'user' }, content: '运行第二批测试' },
    { role: 'assistant', content: [{ type: 'tool-call', id: 'call-2', name: 'exec_command', arguments: { cmd: 'pytest second.py' } }] },
    { role: 'tool', tool_call_id: 'call-1', output: { exit_code: 0, stdout: '1 passed' } },
    { role: 'tool', tool_call_id: 'call-2', output: { exit_code: 0, stdout: '1 passed' } },
  ])
  const evidence = items.filter((item) => item.role === 'tool')
  assert.equal(evidence[0].evidence.source_user_event_id, 'user-1')
  assert.equal(evidence[1].evidence.source_user_event_id, 'user-2')
})

test('search formatting exposes epistemic state rather than treating every claim as completed', () => {
  const states = ['suggested', 'accepted', 'implemented', 'verified']
  const output = formatSearch(states.map((state) => ({ path: 'project/p', kind: 'decision', content: '方案', epistemic_state: state })))
  for (const label of ['提议', '已接受', '已实施', '有验证证据']) assert.ok(output.includes(label))
})
