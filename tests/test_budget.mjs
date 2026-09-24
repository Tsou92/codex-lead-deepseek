import assert from 'node:assert/strict';
import {createBudget} from '../plugins/budget.mjs';

const config = {maxToolCalls: 4, maxSearchCalls: 1, maxFetchCalls: 2, maxSubagentStarts: 1};
const budget = createBudget(config);
assert.equal(budget.decide('web_search'), null);
assert.equal(budget.decide('web_search').kind, 'deny');
assert.equal(budget.decide('subagent'), null);
assert.equal(budget.decide('subagent').kind, 'deny');
assert.equal(budget.decide('read'), null);
assert.equal(budget.decide('web_fetch'), null);
assert.equal(budget.decide('read').kind, 'deny');
assert.equal(budget.state.accepted_total, 4);
assert.equal(budget.state.denied, 3);
console.log('budget tests: passed (per-tool and shared total limits)');
