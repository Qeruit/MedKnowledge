import assert from 'node:assert/strict'
import test from 'node:test'
import { filePageQuery } from '../src/fileQuery.js'

test('cleared date and review inputs are serialized as null', () => {
  assert.deepEqual(filePageQuery(1, 10, { fileName: '', reviewStatus: '', startDate: '', endDate: '' }), {
    current: 1, size: 10, fileName: null, reviewStatus: null, startDate: null, endDate: null,
  })
})
test('partial date range preserves the provided date', () => {
  assert.deepEqual(filePageQuery(2, 10, { startDate: '2026-10-01', endDate: '' }), {
    current: 2, size: 10, fileName: null, reviewStatus: null, startDate: '2026-10-01', endDate: null,
  })
})
test('query serialization does not mutate the form', () => {
  const form = { fileName: 'public', reviewStatus: 'draft', startDate: '', endDate: '2026-10-19' }
  const result = filePageQuery(1, 10, form)
  assert.equal(result.reviewStatus, 'draft')
  assert.equal(result.endDate, '2026-10-19')
  assert.equal(form.startDate, '')
})
