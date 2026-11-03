import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { parse } from '@vue/compiler-sfc'

test('answer evidence and feedback share one initially collapsed native disclosure', () => {
  const { descriptor } = parse(readFileSync(new URL('../src/App.vue', import.meta.url), 'utf8'))
  const elements = []
  function visit(node) {
    if (node.type === 1) elements.push(node)
    for (const child of node.children || []) visit(child)
  }
  visit(descriptor.template.ast)
  const disclosure = elements.find(node => node.tag === 'details' && node.props.some(
    prop => prop.name === 'class' && prop.value?.content.includes('answer-details')))
  assert.ok(disclosure)
  assert.equal(disclosure.props.some(prop => prop.name === 'open' || prop.arg?.content === 'open'), false)
  assert.ok(disclosure.children.some(node => node.tag === 'summary'))
  assert.ok(disclosure.children.some(node => node.tag === 'AnswerFeedback'))
  assert.ok(disclosure.children.some(node => node.props?.some(prop => prop.name === 'for')))
  assert.equal(elements.filter(node => node.tag === 'AnswerFeedback').length, 1)
})
