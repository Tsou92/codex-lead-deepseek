// One process-wide budget shared by the lead Harness agent and its children.
import {writeFileSync} from 'node:fs';

export const name = 'leadseek-budget';
export const inject = ['tools'];

export function createBudget(config, save = () => {}) {
  const state = {accepted_total: 0, attempted_total: 0, accepted_by_tool: {}, denied: 0};
  const limits = {web_search: config.maxSearchCalls, web_fetch: config.maxFetchCalls,
                  subagent: config.maxSubagentStarts};
  return {
    state,
    decide(tool) {
      state.attempted_total++;
      const used = state.accepted_by_tool[tool] || 0;
      if (state.accepted_total >= config.maxToolCalls ||
          (limits[tool] !== undefined && used >= limits[tool])) {
        state.denied++;
        save(state);
        return {kind: 'deny', reason: 'BLOCKED: delegated task tool budget reached. Stop and report partial results to Codex; do not retry or use another tool to bypass this limit.'};
      }
      state.accepted_total++;
      state.accepted_by_tool[tool] = used + 1;
      save(state);
      return null;
    }
  };
}

export function apply(ctx, config) {
  for (const field of ['maxToolCalls', 'maxSearchCalls', 'maxFetchCalls', 'maxSubagentStarts']) {
    if (!Number.isSafeInteger(config[field]) || config[field] < 0) throw new Error(`Invalid ${field}`);
  }
  const budget = createBudget(config, state => writeFileSync(config.receiptPath, JSON.stringify(state), {mode: 0o600}));
  writeFileSync(config.receiptPath, JSON.stringify(budget.state), {mode: 0o600});
  ctx.on('tools/pre-execute', async (exec, next) => budget.decide(exec.name) || await next());
}
