'use strict';
/*
 * 本机任务监控工作台前端交互。
 * 只通过同源 /api/* 读取真实数据；所有文本用 textContent 渲染，绝不使用 innerHTML/eval。
 * 兼容 CSP script-src 'self'，无外部依赖。
 */
(function () {
  var REFRESH_MS = 5000;
  var EVENTS_PAGE = 100;
  var CODEX_PAGE = 100;
  var RUN_PAGE = 50;

  var STATUS_LABEL = {
    running: '运行中', completed: '执行完成', failed: '失败', pending: '等待中',
    queued: '排队中', cancelled: '已取消', canceled: '已取消', interrupted: '已中断',
    needs_attention: '需处理', preparing: '准备中', scope_violation: '越界改动',
    timed_out: '超时', log_limit_exceeded: '日志达上限',
    unknown: '未知'
  };
  /* idle=本轮空闲，不等于审核通过；审核结论看 review_status */
  var AGENT_STATUS_LABEL = {
    startup: '启动中', running: '运行中', idle: '本轮空闲（不等于审核通过）',
    ended: '已结束', disposed: '已释放', failed: '失败'
  };
  var REVIEW_LABEL = {
    pending: '待审核', accepted: '已采纳', revision_requested: '要求返工',
    rejected: '已拒绝', unknown: '未记录'
  };
  var EVENT_LABEL = {
    agent_created: '代理创建', agent_finished: '代理结束', message: '消息',
    tool_call: '工具调用', tool_result: '工具结果', usage: '用量',
    subagent_started: '子代理启动', subagent_finished: '子代理结束',
    telemetry_status: '遥测状态'
  };

  var state = {
    overview: null,
    runs: [],
    hasMore: false,
    filters: { workspace: '', q: '', status: '' },
    selectedRunId: null,
    autoSelected: false,
    detail: null,
    detailSig: '',
    detailRunId: null,
    activeTab: 'overview',
    autoRefresh: true,
    view: 'tasks',
    events: { items: [], consumedOffset: 0, nextCursor: null, hasMore: false, agentId: '', warnings: [], loading: false },
    expandedEvents: {},
    codex: { threadId: null, items: [], nextCursor: null, hasMore: false, mode: 'latest', total: null, windowStart: 0 },
    expandedCodex: {},
    ticking: false
  };
  var controllers = {};
  var seqs = {};
  var eventsSeq = 0;
  var codexSeq = 0;
  var refreshTimer = null;
  var searchTimer = null;

  /* ---------- 基础工具 ---------- */

  function $(id) { return document.getElementById(id); }
  function h(tag, cls, text) {
    var e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text !== undefined && text !== null) e.textContent = String(text);
    return e;
  }
  function clear(n) { if (!n) return; while (n.firstChild) n.removeChild(n.firstChild); }
  function setText(n, t) { if (n) n.textContent = t === null || t === undefined ? '' : String(t); }
  function safeClass(s) { return String(s == null ? 'unknown' : s).replace(/[^a-zA-Z0-9_-]/g, '_'); }

  function num(v) {
    if (v === null || v === undefined || v === '') return '未记录';
    var n = Number(v);
    if (!isFinite(n)) return String(v);
    return n.toLocaleString('zh-CN');
  }
  function codexTotal(v) {
    var n = Number(v);
    if (!Number.isFinite(n) || n < 0) return 0;
    return Math.floor(n);
  }
  function textOf(v) {
    if (v === null || v === undefined) return '';
    if (typeof v === 'string') return v;
    try { return JSON.stringify(v, null, 2); } catch (e) { return String(v); }
  }
  function oneline(v) {
    if (v === null || v === undefined) return '';
    if (typeof v === 'string') return v;
    try { return JSON.stringify(v); } catch (e) { return String(v); }
  }
  function fmtTime(ts) {
    if (ts === null || ts === undefined || ts === '') return '未记录';
    var d = new Date(ts);
    if (isNaN(d.getTime())) return String(ts);
    try {
      return d.toLocaleString('zh-CN', { hour12: false, year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit' });
    } catch (e) { return d.toISOString(); }
  }
  function fmtDuration(s) {
    if (s === null || s === undefined || s === '') return '未记录';
    var n = Number(s);
    if (!isFinite(n)) return String(s);
    if (n < 60) return Math.round(n) + ' 秒';
    var m = Math.floor(n / 60), sec = Math.round(n % 60);
    if (m < 60) return m + ' 分 ' + sec + ' 秒';
    return Math.floor(m / 60) + ' 时 ' + (m % 60) + ' 分';
  }
  function statusLabel(s) { return STATUS_LABEL[s] || (s ? '未知（' + s + '）' : '未记录'); }
  function reviewLabel(s) { return REVIEW_LABEL[s] || (s ? '未知（' + s + '）' : '未记录'); }
  function agentStatusLabel(s) { return AGENT_STATUS_LABEL[s] || (s ? '未知（' + s + '）' : '未记录'); }
  /* 任务目标摘要：首行/首句 + 最多 max 字省略号；完整 goal 仍保留在下发指令与概览 */
  function goalSummary(goal, max) {
    max = max || 70;
    if (goal === null || goal === undefined || goal === '') return '（无目标记录）';
    var s = typeof goal === 'string' ? goal : oneline(goal);
    s = String(s).replace(/\r/g, '').split('\n')[0];
    var m = s.match(/^[^。！？!?;；]+[。！？!?;；]?/);
    if (m && m[0]) s = m[0];
    s = s.trim();
    if (!s) return '（无目标记录）';
    if (s.length > max) s = s.slice(0, max) + '…';
    return s;
  }

  function badge(text, cls) {
    return h('span', 'badge ' + (cls ? 'badge-' + safeClass(cls) : ''), text);
  }
  function reviewBadge(s) {
    var t = reviewLabel(s);
    var cls = s === 'accepted' ? 'accepted' : (s === 'revision_requested' ? 'revision_requested' : (s === 'rejected' ? 'rejected' : (s === 'pending' ? 'pending' : 'unknown')));
    return badge('审核：' + t, 'review-' + cls);
  }

  function kv(label, value) {
    var w = h('div', 'kv');
    w.appendChild(h('span', 'kv-label', label));
    if (value instanceof Node) w.appendChild(value);
    else w.appendChild(h('span', 'kv-value', value === null || value === undefined || value === '' ? '未记录' : String(value)));
    return w;
  }
  function kvGrid(pairs) {
    var g = h('div', 'kv-grid');
    pairs.forEach(function (p) { g.appendChild(kv(p[0], p[1])); });
    return g;
  }
  function section(title) {
    var s = h('section', 'section-block');
    s.appendChild(h('h3', 'section-title', title));
    return s;
  }
  function empty(text) { return h('div', 'empty-state', text); }

  /* 长文本预览：默认最多 300 字，展开状态记录在 state.expandedEvents，刷新后保留 */
  var TEXT_PREVIEW = 300;
  function previewBlock(text, key) {
    var s = text === null || text === undefined ? '' : String(text);
    var wrap = h('div', 'preview-block');
    if (!s) { wrap.appendChild(h('div', 'muted', '（空）')); return wrap; }
    var expanded = !!state.expandedEvents[key];
    var box = h('pre', 'event-pre preview-text', expanded || s.length <= TEXT_PREVIEW ? s : s.slice(0, TEXT_PREVIEW) + '…');
    wrap.appendChild(box);
    if (s.length > TEXT_PREVIEW) {
      var btn = h('button', 'btn btn-small preview-toggle', expanded ? '收起全文' : '展开全文');
      btn.type = 'button';
      btn.setAttribute('aria-expanded', expanded ? 'true' : 'false');
      btn.addEventListener('click', function () {
        var ex = !state.expandedEvents[key];
        state.expandedEvents[key] = ex;
        box.textContent = ex ? s : s.slice(0, TEXT_PREVIEW) + '…';
        btn.textContent = ex ? '收起全文' : '展开全文';
        btn.setAttribute('aria-expanded', ex ? 'true' : 'false');
      });
      wrap.appendChild(btn);
    }
    return wrap;
  }

  /* 记录完整度：已知取值给中文短语，未知按原文展示，不编造状态；技术细节放 details */
  var COVERAGE_LABEL = {
    collected: '已采集', telemetry: '已采集', full: '已采集',
    parent_usage_only: '仅主代理', main_only: '仅主代理',
    partial: '部分记录', partial_records: '部分记录',
    recovered: '归档恢复', archived: '归档恢复', restored: '归档恢复'
  };
  function coverageLabel(c) {
    if (c === null || c === undefined || c === '') return '未记录';
    if (typeof c === 'object') {
      var v = c.kind || c.status || c.source || c.coverage;
      return v ? coverageLabel(v) : (c.note ? String(c.note) : '未记录');
    }
    var s = String(c);
    return COVERAGE_LABEL[s] || s;
  }
  function coverageRow(c) {
    var w = h('div', 'coverage-row');
    w.appendChild(kv('记录完整度', coverageLabel(c)));
    if (c !== null && c !== undefined && c !== '') {
      var d = document.createElement('details');
      d.className = 'coverage-details';
      d.appendChild(h('summary', null, '技术说明'));
      d.appendChild(h('div', 'usage-note', textOf(c)));
      w.appendChild(d);
    }
    return w;
  }

  /* ---------- 请求（AbortController + 序列号防旧覆盖） ---------- */

  function apiGet(path, key) {
    var prev = controllers[key];
    if (prev) { try { prev.abort(); } catch (e) {} }
    var seq = (seqs[key] || 0) + 1;
    seqs[key] = seq;
    var ctrl = new AbortController();
    controllers[key] = ctrl;
    return fetch(path, {
      signal: ctrl.signal,
      credentials: 'same-origin',
      headers: { 'Accept': 'application/json' }
    }).then(function (res) {
      if (seq !== seqs[key]) return null;
      if (res.status === 401) { var e401 = new Error('认证失败（401）'); e401.status = 401; throw e401; }
      if (!res.ok) { var e = new Error('HTTP ' + res.status); e.status = res.status; throw e; }
      return res.json();
    }).then(function (data) {
      if (seq !== seqs[key]) return null;
      return data;
    }).catch(function (err) {
      if (err && err.name === 'AbortError') return null;
      throw err;
    });
  }

  function setConn(ok, text) {
    var n = $('conn-status');
    n.textContent = text;
    n.className = 'conn-status ' + (ok ? 'is-ok' : 'is-bad');
  }
  function showError(msg) { setText($('error-text'), msg); $('error-banner').hidden = false; }
  function hideError() { $('error-banner').hidden = true; }
  function handleErr(err) {
    if (err && err.status === 401) {
      setConn(false, '认证失败');
      showError('认证失败（401）：请通过 `leadseek monitor start` 输出的本机 URL 重新打开页面完成令牌交换，然后点“重试”。');
      return;
    }
    setConn(false, '连接失败');
    showError('无法读取本机 API：' + ((err && err.message) || err) + '。请确认监控服务仍在运行，然后点“重试”。');
  }

  /* ---------- 顶部计数与用量 ---------- */

  function renderCounters() {
    var c = (state.overview && state.overview.counts) || {};
    setText($('count-total'), num(c.total));
    setText($('count-running'), num(c.running));
    setText($('count-review-pending'), num(c.review_pending));
    setText($('count-accepted'), num(c.accepted));
    setText($('count-failed'), num(c.failed));
  }

  function usageLine(u, coverage, kind) {
    u = u || {};
    var wrap = h('div', 'usage-grid');
    var inputLabel = kind === 'deepseek' ? '输入（非缓存）' : (kind === 'codex' ? '输入（含缓存）' : '输入');
    var totalLabel = kind === 'deepseek' ? '接口合计' : (kind === 'codex' ? '会话累计合计' : '合计');
    wrap.appendChild(kv(inputLabel, num(u.input_tokens)));
    wrap.appendChild(kv('缓存读', num(u.cache_read_tokens)));
    wrap.appendChild(kv('缓存写', num(u.cache_write_tokens)));
    wrap.appendChild(kv('输出', num(u.output_tokens)));
    wrap.appendChild(kv(totalLabel, num(u.total_tokens)));
    if (u.source !== undefined && u.source !== null) wrap.appendChild(kv('来源', textOf(u.source)));
    if (coverage !== undefined && coverage !== null && coverage !== '') {
      if (kind === 'codex') wrap.appendChild(kv('统计范围', textOf(coverage)));
      else wrap.appendChild(coverageRow(coverage));
    }
    return wrap;
  }

  function codexSessions() {
    var o = state.overview || {};
    if (o.codex && Array.isArray(o.codex.sessions)) return o.codex.sessions;
    if (Array.isArray(o.codex_sessions)) return o.codex_sessions;
    return [];
  }

  function renderUsageStrip() {
    var o = state.overview || {};
    var dsBox = $('ds-usage-summary');
    clear(dsBox);
    dsBox.appendChild(h('div', 'usage-title', 'DeepSeek 已记录 token'));
    var ds = (o.usage && o.usage.deepseek) || {};
    dsBox.appendChild(usageLine(ds, ds.coverage, 'deepseek'));
    var dsNote = document.createElement('details');
    dsNote.className = 'usage-details';
    dsNote.appendChild(h('summary', null, '统计口径'));
    dsNote.appendChild(h('div', 'usage-note',
      'Harness DeepSeek：inputTokens 为非缓存输入；cacheRead / cacheWrite 独立计数；接口已报告的 total 已包含缓存部分，不重复相加。缺失显示“未记录”。'));
    dsBox.appendChild(dsNote);

    var cBox = $('codex-usage-summary');
    clear(cBox);
    cBox.appendChild(h('div', 'usage-title', 'Codex 会话累计（按会话展示，不跨会话求和）'));
    var sessions = codexSessions();
    if (!sessions.length) {
      cBox.appendChild(h('div', 'usage-note', '无关联 Codex 会话或未记录'));
    } else {
      sessions.slice(0, 3).forEach(function (s) {
        var row = h('div', 'usage-session');
        row.appendChild(h('code', 'codex-thread', s.thread_id || '未记录'));
        row.appendChild(h('span', 'usage-session-val',
          '累计合计 ' + num(s.usage && s.usage.total_tokens) + ' · 范围 ' + (s.usage_scope ? textOf(s.usage_scope) : '未记录')));
        cBox.appendChild(row);
      });
      if (sessions.length > 3) cBox.appendChild(h('div', 'usage-note', '另有 ' + (sessions.length - 3) + ' 个会话，可在“Codex 会话”中查看'));
    }
    var cNote = document.createElement('details');
    cNote.className = 'usage-details';
    cNote.appendChild(h('summary', null, '统计口径'));
    cNote.appendChild(h('div', 'usage-note',
      'Codex input_tokens 含 cached_input_tokens，reasoning 为输出子集；范围为整个关联会话累计，包含多次请求与缓存，不能归属单个 DeepSeek 任务，不代表此功能费用或节省量。'));
    cBox.appendChild(cNote);
  }

  /* 警告归类：部分/缺失优先，其次归档恢复，最后采集；无证据不贴标签、不改写原文 */
  var WARN_KINDS = [
    { label: '部分/缺失', re: /部分|不完整|缺失|截断|上限|丢失|partial|truncat|limit|missing|incomplete/i },
    { label: '归档恢复', re: /归档|恢复|重建|recover|repair|archiv|restore/i },
    { label: '采集', re: /采集|遥测|telemetry|hook/i }
  ];
  function warnKind(w) {
    var s = String(w);
    for (var i = 0; i < WARN_KINDS.length; i++) if (WARN_KINDS[i].re.test(s)) return WARN_KINDS[i].label;
    return '';
  }
  function renderWarnings(list, container) {
    var box = container || $('overview-warnings');
    if (!box) return;
    clear(box);
    var items = list || [];
    if (!items.length) { box.hidden = true; return; }
    box.hidden = false;
    box.appendChild(h('summary', 'warning-head', '记录说明与缺失提示 · ' + items.length + ' 条'));
    items.forEach(function (w) {
      var row = h('div', 'warning-item');
      var kind = warnKind(w);
      if (kind) row.appendChild(h('span', 'warning-tag', kind));
      row.appendChild(h('span', 'warning-text', String(w)));
      box.appendChild(row);
    });
  }

  /* ---------- 任务列表 ---------- */

  function runListSig() {
    return state.runs.map(function (r) {
      return [r.run_id, r.status, r.review_status, r.usage && r.usage.total_tokens, goalSummary(r.goal, 70), r.subagent_count].join(':');
    }).join('|');
  }

  function renderRunList(force) {
    var list = $('run-list');
    var sig = runListSig();
    var scroll = list.scrollTop;
    if (force || sig !== list._sig) {
      list._sig = sig;
      clear(list);
      state.runs.forEach(function (r) { list.appendChild(runItem(r)); });
      list.scrollTop = scroll;
    }
    var status = $('run-list-status');
    if (!state.runs.length) status.textContent = '无匹配任务，可调整搜索或筛选条件';
    else status.textContent = '已加载 ' + state.runs.length + ' 个任务' + (state.hasMore ? '（还有更多）' : '');
    $('load-more-runs').hidden = !state.hasMore;
  }

  function runItem(r) {
    var li = h('li', 'run-item');
    var btn = h('button', 'run-item-btn');
    btn.type = 'button';
    var selected = r.run_id === state.selectedRunId;
    if (selected) btn.classList.add('is-selected');
    btn.setAttribute('aria-pressed', selected ? 'true' : 'false');

    var title = h('div', 'run-item-title', goalSummary(r.goal, 70));
    btn.appendChild(title);

    var sub = h('div', 'run-item-sub');
    sub.appendChild(h('code', 'run-id', r.run_id || '未记录'));
    sub.appendChild(badge(statusLabel(r.status), 'status-' + safeClass(r.status)));
    sub.appendChild(reviewBadge(r.review_status));
    sub.appendChild(h('span', 'run-item-token muted', 'token ' + num(r.usage && r.usage.total_tokens)));
    if (r.subagent_count !== null && r.subagent_count !== undefined) {
      sub.appendChild(badge('子代理 ' + num(r.subagent_count), 'subagents'));
    }
    btn.appendChild(sub);

    var meta = h('div', 'run-item-meta');
    meta.appendChild(h('span', 'muted', fmtTime(r.started_at)));
    if (r.workspace) meta.appendChild(h('span', 'muted run-item-ws', r.workspace));
    btn.appendChild(meta);

    btn.setAttribute('aria-label',
      '任务 ' + (r.run_id || '未记录') + '，执行 ' + statusLabel(r.status) + '，审核 ' + reviewLabel(r.review_status) +
      '，token ' + num(r.usage && r.usage.total_tokens) + '：' + goalSummary(r.goal, 70));
    btn.addEventListener('click', function () { selectRun(r.run_id); });
    li.appendChild(btn);
    return li;
  }

  function selectRun(id) {
    if (!id || state.selectedRunId === id) return;
    state.selectedRunId = id;
    state.detail = null;
    state.detailSig = '';
    state.detailRunId = null;
    state.activeTab = 'overview';
    resetEvents();
    setNav('tasks');
    $('detail-empty').hidden = false;
    $('detail-empty').textContent = '正在加载任务…';
    $('detail-content').hidden = true;
    $('global-view').hidden = true;
    renderRunList(true);
    loadDetail(true).then(function () {
      return fetchEvents({ replace: true, after: 0 });
    }).catch(handleErr);
  }

  function resetEvents() {
    eventsSeq++;
    state.events = { items: [], consumedOffset: 0, nextCursor: null, hasMore: false, agentId: '', warnings: [], loading: false };
    state.expandedEvents = {};
  }

  /* ---------- overview 拉取 ---------- */

  function overviewQuery(limit, offset) {
    var q = new URLSearchParams();
    if (state.filters.workspace) q.set('workspace', state.filters.workspace);
    if (state.filters.q) q.set('q', state.filters.q);
    if (state.filters.status) q.set('status', state.filters.status);
    q.set('limit', String(limit));
    q.set('offset', String(offset));
    return q;
  }

  function loadOverview(reset) {
    var limit = reset ? RUN_PAGE : Math.max(state.runs.length, RUN_PAGE);
    return apiGet('/api/overview?' + overviewQuery(limit, 0).toString(), 'overview').then(function (data) {
      if (!data) return null;
      state.overview = data;
      state.runs = Array.isArray(data.runs) ? data.runs : [];
      state.hasMore = !!data.has_more;
      renderCounters();
      renderUsageStrip();
      renderWarnings(data.warnings);
      renderWorkspaces(data.workspaces);
      renderRunList(true);
      if (state.view !== 'tasks') renderGlobalView();
      hideError();
      setConn(true, '已连接');
      setText($('last-updated'), '数据更新于 ' + fmtTime(data.updated_at));
      if (reset && state.view === 'tasks' && !state.runs.length) {
        $('detail-content').hidden = true;
        $('detail-empty').hidden = false;
        $('detail-empty').textContent = '无匹配任务，可调整搜索或筛选条件';
      }
      if (reset && !state.autoSelected && state.view === 'tasks' && state.runs.length) {
        state.autoSelected = true;
        selectRun(state.runs[0].run_id);
      }
      return data;
    });
  }

  function loadMoreRuns() {
    return apiGet('/api/overview?' + overviewQuery(RUN_PAGE, state.runs.length).toString(), 'overviewMore').then(function (data) {
      if (!data) return;
      var seen = {};
      state.runs.forEach(function (r) { seen[r.run_id] = 1; });
      (data.runs || []).forEach(function (r) { if (!seen[r.run_id]) { state.runs.push(r); seen[r.run_id] = 1; } });
      state.hasMore = !!data.has_more;
      renderRunList(true);
    }).catch(handleErr);
  }

  function renderWorkspaces(list) {
    var sel = $('workspace-filter');
    var cur = state.filters.workspace;
    var wanted = [''].concat(list || []);
    var existing = Array.prototype.map.call(sel.options, function (o) { return o.value; });
    var same = existing.length === wanted.length && wanted.every(function (v, i) { return existing[i] === v; });
    if (same) { sel.value = cur; return; }
    clear(sel);
    wanted.forEach(function (w) {
      var o = document.createElement('option');
      o.value = w;
      o.textContent = w === '' ? '全部工作区' : w;
      sel.appendChild(o);
    });
    sel.value = cur;
  }

  /* ---------- 任务详情 ---------- */

  function loadDetail(force) {
    var id = state.selectedRunId;
    if (!id) return Promise.resolve(null);
    return apiGet('/api/runs/' + encodeURIComponent(id), 'detail').then(function (data) {
      if (!data || !data.run) return null;
      if (id !== state.selectedRunId) return null;
      state.detail = data;
      renderDetail(force);
      return data;
    });
  }

  function captureScroll() {
    var panels = {};
    Array.prototype.forEach.call(document.querySelectorAll('.tab-panel'), function (p) { panels[p.id] = p.scrollTop; });
    return {
      win: window.scrollY || document.documentElement.scrollTop || 0,
      list: $('run-list') ? $('run-list').scrollTop : 0,
      events: $('event-list') ? $('event-list').scrollTop : 0,
      panels: panels
    };
  }
  function restoreScroll(s) {
    if (!s) return;
    requestAnimationFrame(function () {
      window.scrollTo(0, s.win);
      if ($('run-list')) $('run-list').scrollTop = s.list;
      if ($('event-list')) $('event-list').scrollTop = s.events;
      Object.keys(s.panels).forEach(function (id) {
        var p = $(id);
        if (p) p.scrollTop = s.panels[id];
      });
    });
  }

  function detailSignature(d) {
    return JSON.stringify([d.run, d.task, d.prompt, d.result, d.decisions, d.changes, d.agents, d.usage, d.warnings, d.codex_thread_ids]);
  }

  function renderDetail(force) {
    var d = state.detail;
    if (!d) return;
    var runChanged = state.detailRunId !== d.run.run_id;
    var sig = detailSignature(d);
    if (!force && !runChanged && sig === state.detailSig) return;
    var scroll = captureScroll();
    state.detailSig = sig;
    state.detailRunId = d.run.run_id;

    $('detail-empty').hidden = true;
    $('detail-content').hidden = false;
    $('global-view').hidden = true;
    renderDetailHeader(d);
    if (runChanged) { buildEventsScaffold(); renderAgentFilter(); }
    renderOverviewPanel(d);
    renderPromptPanel(d);
    renderReviewPanel(d);
    renderChangesPanel(d);
    renderAgentsPanel(d);
    renderTokenPanel(d);
    applyTabState();
    restoreScroll(scroll);
  }

  function renderDetailHeader(d) {
    var r = d.run || {};
    var titleText = goalSummary(r.goal, 70);
    setText($('detail-title'), titleText === '（无目标记录）' ? (r.run_id || '任务详情') : titleText);
    var badges = $('detail-badges');
    clear(badges);
    badges.appendChild(badge('执行：' + statusLabel(r.status), 'status-' + safeClass(r.status)));
    badges.appendChild(reviewBadge(r.review_status));
    if (r.mode) badges.appendChild(badge('模式 ' + r.mode, 'mode'));
    if (r.model) badges.appendChild(badge(r.model, 'model'));

    var meta = $('detail-meta');
    clear(meta);
    meta.appendChild(kv('任务 ID', r.run_id));
    meta.appendChild(kv('工作区', r.workspace || null));
    meta.appendChild(kv('开始', fmtTime(r.started_at)));
    meta.appendChild(kv('结束', fmtTime(r.finished_at)));
    meta.appendChild(kv('耗时', fmtDuration(r.elapsed_seconds)));
    meta.appendChild(kv('会话 ID', r.session_id || null));
    meta.appendChild(kv('Codex thread', r.codex_thread_id || null));
    meta.appendChild(kv('上一任务', r.previous_run_id || null));
    meta.appendChild(kv('改动文件', num(r.changed_file_count)));
    meta.appendChild(kv('工具调用', num(r.tool_count)));
    meta.appendChild(kv('子代理', num(r.subagent_count)));
  }

  function artifactBlock(runId, name, label) {
    var block = h('div', 'section-block');
    block.appendChild(h('h3', 'section-title', label));
    var btn = h('button', 'btn btn-small', '读取 ' + name + ' 工件');
    btn.type = 'button';
    var out = h('div', 'artifact-out');
    btn.addEventListener('click', function () {
      btn.disabled = true;
      var old = btn.textContent;
      btn.textContent = '读取中…';
      apiGet('/api/runs/' + encodeURIComponent(runId) + '/artifacts/' + name, 'artifact:' + runId + ':' + name)
        .then(function (a) {
          btn.disabled = false;
          btn.textContent = old;
          if (!a) return;
          clear(out);
          out.appendChild(h('div', 'usage-note', '工件 ' + (a.name || name) + (a.truncated ? '（已截断展示）' : '')));
          out.appendChild(h('pre', 'event-pre', a.text ? String(a.text) : '（空）'));
        })
        .catch(function (e) {
          btn.disabled = false;
          btn.textContent = old;
          out.appendChild(h('div', 'warning-item', '读取失败：' + ((e && e.message) || e)));
        });
    });
    block.appendChild(btn);
    block.appendChild(out);
    return block;
  }

  function decisionItem(dec) {
    var item = h('article', 'decision-item');
    var head = h('div', 'decision-head');
    head.appendChild(h('span', 'decision-action', dec.action || '未记录动作'));
    head.appendChild(h('span', 'decision-owner muted', dec.owner || '未记录 owner'));
    head.appendChild(h('span', 'decision-time muted', fmtTime(dec.timestamp)));
    item.appendChild(head);
    item.appendChild(h('div', 'decision-reason', dec.reason ? String(dec.reason) : '未记录 reason'));
    var meta = h('div', 'decision-meta');
    meta.appendChild(h('span', 'muted', 'run: ' + (dec.run_id || '未记录')));
    meta.appendChild(h('span', 'muted', ' codex: ' + (dec.codex_thread_id || '未记录')));
    item.appendChild(meta);
    return item;
  }

  function codexLink(threadId) {
    var b = h('button', 'btn btn-small codex-link', threadId);
    b.type = 'button';
    b.title = '打开 Codex 会话详情';
    b.addEventListener('click', function () { openCodex(threadId); });
    return b;
  }

  function renderOverviewPanel(d) {
    var p = $('panel-overview');
    clear(p);
    var r = d.run || {};

    var s1 = section('任务');
    var fullGoal = r.goal === null || r.goal === undefined || r.goal === ''
      ? '' : (typeof r.goal === 'string' ? r.goal : oneline(r.goal));
    var goalWrap = h('div', 'goal-summary');
    goalWrap.appendChild(h('div', 'goal-summary-text', goalSummary(r.goal, 70)));
    if (fullGoal) {
      var gd = document.createElement('details');
      gd.className = 'field-details';
      gd.appendChild(h('summary', null, '完整目标'));
      gd.appendChild(h('pre', 'event-pre', fullGoal));
      goalWrap.appendChild(gd);
    }
    s1.appendChild(kvGrid([
      ['任务 ID', r.run_id],
      ['工作区', r.workspace || null],
      ['状态', statusLabel(r.status)],
      ['审核状态', reviewLabel(r.review_status)]
    ]));
    s1.appendChild(kv('目标', goalWrap));
    var taskKeys = ['task', 'prompt'].filter(function (k) { return d[k] !== null && d[k] !== undefined && d[k] !== ''; });
    if (taskKeys.length) {
      var td = document.createElement('details');
      td.className = 'field-details';
      td.appendChild(h('summary', null, '完整任务字段（task / prompt）'));
      taskKeys.forEach(function (k) {
        td.appendChild(h('h4', 'section-subtitle', k));
        td.appendChild(h('pre', 'event-pre', textOf(d[k])));
      });
      s1.appendChild(td);
    }
    p.appendChild(s1);

    var s2 = section('执行结果（worker_report）');
    if (d.result !== null && d.result !== undefined && d.result !== '') {
      s2.appendChild(previewBlock(textOf(d.result), 'overview-result'));
      var rd = document.createElement('details');
      rd.className = 'field-details';
      rd.appendChild(h('summary', null, '完整执行结果'));
      rd.appendChild(h('pre', 'event-pre', textOf(d.result)));
      s2.appendChild(rd);
    } else {
      s2.appendChild(empty('未记录执行结果（worker_report）'));
    }
    p.appendChild(s2);

    var s3 = section('审核与决策');
    var decs = (d.decisions || []).slice();
    if (decs.length) decs.forEach(function (x) { s3.appendChild(decisionItem(x)); });
    else s3.appendChild(empty('未记录审核决策；执行完成不等于已验收'));
    p.appendChild(s3);

    var s4 = section('关联 Codex 会话');
    var threads = (d.codex_thread_ids || []).slice();
    if (!threads.length) s4.appendChild(empty('未记录关联 Codex thread'));
    else threads.forEach(function (t) { s4.appendChild(codexLink(t)); });
    p.appendChild(s4);

    p.appendChild(artifactBlock(r.run_id, 'prompt', '下发指令工件'));
    p.appendChild(artifactBlock(r.run_id, 'patch', '补丁工件'));
    p.appendChild(artifactBlock(r.run_id, 'stderr', '错误输出工件'));

    if (d.warnings && d.warnings.length) {
      var sw = section('警告');
      d.warnings.forEach(function (w) { sw.appendChild(h('div', 'warning-item', String(w))); });
      p.appendChild(sw);
    }
  }

  function copyBtn(getText, label) {
    var b = h('button', 'btn btn-small copy-btn', label || '复制');
    b.type = 'button';
    b.addEventListener('click', function () { copyText(getText(), b); });
    return b;
  }

  function renderPromptPanel(d) {
    var p = $('panel-prompt');
    clear(p);
    var prompt = d.prompt;
    var toolbar = h('div', 'toolbar');
    toolbar.appendChild(copyBtn(function () {
      if (prompt === null || prompt === undefined) return '';
      if (typeof prompt === 'object') return textOf(prompt);
      return String(prompt);
    }, '复制完整指令'));
    p.appendChild(toolbar);

    if (prompt === null || prompt === undefined || prompt === '') {
      p.appendChild(empty('未记录下发指令（prompt）'));
      return;
    }
    if (typeof prompt === 'object' && !Array.isArray(prompt)) {
      var keys = Object.keys(prompt);
      if (!keys.length) { p.appendChild(empty('下发的指令对象为空')); return; }
      keys.forEach(function (k) {
        var s = section(k === 'task' ? '任务 task' : k === 'prompt' ? '指令 prompt' : k === 'worker_report' ? '工作回报 worker_report' : k);
        s.appendChild(copyBtn(function () { return textOf(prompt[k]); }, '复制'));
        var v = prompt[k];
        if (v === null || v === undefined || v === '') s.appendChild(empty('未记录'));
        else if (typeof v === 'object') s.appendChild(h('pre', 'event-pre', textOf(v)));
        else s.appendChild(h('pre', 'event-pre', String(v)));
        p.appendChild(s);
      });
      return;
    }
    var s = section('完整指令');
    s.appendChild(copyBtn(function () { return typeof prompt === 'string' ? prompt : textOf(prompt); }, '复制'));
    s.appendChild(h('pre', 'event-pre', typeof prompt === 'string' ? prompt : textOf(prompt)));
    p.appendChild(s);
  }

  function renderReviewPanel(d) {
    var p = $('panel-review');
    clear(p);
    var r = d.run || {};
    var decs = (d.decisions || []);
    var latestCodex = null;
    for (var di = decs.length - 1; di >= 0; di--) {
      var cand = decs[di] || {};
      if (String(cand.owner || '').toLowerCase() === 'codex') { latestCodex = cand; break; }
    }
    var s0 = section('验收状态');
    s0.appendChild(kvGrid([
      ['执行状态', statusLabel(r.status) + '（执行完成 ≠ 已验收）'],
      ['审核状态', reviewLabel(r.review_status)],
      ['最近 Codex 记录时间', latestCodex ? fmtTime(latestCodex.timestamp) : '未记录']
    ]));
    p.appendChild(s0);

    var s1 = section('审核记录');
    if (!decs.length) {
      s1.appendChild(empty('未记录 Codex 审核决策'));
    } else {
      decs.forEach(function (dec) {
        var item = decisionItem(dec);
        if (String(dec.action || '').indexOf('revision') >= 0 || String(dec.action || '').indexOf('reject') >= 0) {
          item.appendChild(h('div', 'decision-direction', '返工方向：' + (dec.reason ? String(dec.reason) : '未记录 reason')));
        }
        s1.appendChild(item);
      });
    }
    p.appendChild(s1);
  }

  /* ---------- 执行会话（事件） ---------- */

  function buildEventsScaffold() {
    var p = $('panel-events');
    clear(p);
    var toolbar = h('div', 'toolbar');
    var label = h('label', 'field field-inline');
    label.appendChild(h('span', null, '代理筛选'));
    var sel = h('select', 'agent-select');
    sel.id = 'event-agent-filter';
    sel.setAttribute('aria-label', '按代理筛选事件');
    sel.addEventListener('change', function () {
      state.events.agentId = sel.value;
      state.events.items = [];
      state.events.consumedOffset = 0;
      state.events.nextCursor = null;
      state.events.hasMore = false;
      state.expandedEvents = {};
      fetchEvents({ replace: true, after: 0 });
    });
    label.appendChild(sel);
    toolbar.appendChild(label);

    var refreshBtn = h('button', 'btn btn-small', '刷新事件');
    refreshBtn.type = 'button';
    refreshBtn.addEventListener('click', function () {
      fetchEvents({ after: state.events.consumedOffset || 0 });
    });
    toolbar.appendChild(refreshBtn);

    var moreBtn = h('button', 'btn btn-small', '加载更多事件');
    moreBtn.type = 'button';
    moreBtn.id = 'events-load-more';
    moreBtn.hidden = true;
    moreBtn.addEventListener('click', function () {
      var after = (state.events.nextCursor !== null && state.events.nextCursor !== undefined)
        ? state.events.nextCursor : state.events.consumedOffset;
      fetchEvents({ after: after || 0 });
    });
    toolbar.appendChild(moreBtn);
    p.appendChild(toolbar);

    var status = h('div', 'list-status', '');
    status.id = 'events-status';
    status.setAttribute('role', 'status');
    p.appendChild(status);

    var warn = h('div', 'warnings');
    warn.id = 'events-warnings';
    p.appendChild(warn);

    var list = h('div', 'event-list');
    list.id = 'event-list';
    p.appendChild(list);
  }

  function evKey(ev) {
    return [ev.index, ev.type, ev.agent_id, ev.tool, ev.call_id].join('|');
  }

  function fetchEvents(opts) {
    var id = state.selectedRunId;
    if (!id) return Promise.resolve();
    opts = opts || {};
    var mySeq = ++eventsSeq;
    var rid = id;
    var baseAfter = (opts.after === null || opts.after === undefined) ? 0 : Number(opts.after);
    if (!isFinite(baseAfter) || baseAfter < 0) baseAfter = 0;
    var agentFilter = state.events.agentId;
    var q = new URLSearchParams();
    q.set('limit', String(EVENTS_PAGE));
    q.set('after', String(baseAfter));
    if (agentFilter) q.set('agent_id', agentFilter);
    state.events.loading = true;
    renderEventsStatus();
    return apiGet('/api/runs/' + encodeURIComponent(rid) + '/events?' + q.toString(), 'events:' + rid)
      .then(function (data) {
        if (!data) return null;
        /* 异步结果必须核对当前 runid / 代理筛选 / 请求序列，防止旧请求覆盖新状态 */
        if (mySeq !== eventsSeq || state.selectedRunId !== rid || state.events.agentId !== agentFilter) return null;
        if (opts.replace) state.events.items = [];
        var seen = {};
        state.events.items.forEach(function (e) { seen[evKey(e)] = 1; });
        var incoming = data.items || [];
        incoming.forEach(function (e) {
          var k = evKey(e);
          if (!seen[k]) { seen[k] = 1; state.events.items.push(e); }
        });
        state.events.items.sort(function (a, b) {
          return (a.index === null || a.index === undefined ? 0 : a.index) - (b.index === null || b.index === undefined ? 0 : b.index);
        });
        /* after 是过滤后的 offset：优先 next_cursor，否则用 after + 本页条数推进 */
        var nextC = (data.next_cursor === undefined) ? null : data.next_cursor;
        state.events.nextCursor = nextC;
        state.events.consumedOffset = (nextC !== null && nextC !== undefined) ? nextC : baseAfter + incoming.length;
        state.events.hasMore = !!data.has_more;
        state.events.warnings = data.warnings || [];
        renderEvents();
        return data;
      })
      .catch(handleErr)
      .then(function () {
        if (mySeq === eventsSeq) { state.events.loading = false; renderEventsStatus(); }
      });
  }

  function renderEventsStatus() {
    var st = $('events-status');
    if (!st) return;
    if (state.events.loading) st.textContent = '加载事件中…';
    else if (!state.events.items.length) st.textContent = '没有可观测事件（可能是历史记录缺少 telemetry）';
    else st.textContent = '已加载 ' + state.events.items.length + ' 条事件' + (state.events.hasMore ? '（还有更多）' : '');
    var more = $('events-load-more');
    if (more) more.hidden = !state.events.hasMore;
    var warn = $('events-warnings');
    if (warn) {
      clear(warn);
      (state.events.warnings || []).forEach(function (w) { warn.appendChild(h('div', 'warning-item', String(w))); });
    }
  }

  function eventBody(ev) {
    var body = h('div', 'event-body');
    var pairs = [];
    if (ev.step_id) pairs.push(['step_id', ev.step_id]);
    if (ev.turn_id) pairs.push(['turn_id', ev.turn_id]);
    if (ev.call_id) pairs.push(['call_id', ev.call_id]);
    if (ev.session_id) pairs.push(['session_id', ev.session_id]);
    if (ev.model) pairs.push(['model', ev.model]);
    if (pairs.length) body.appendChild(kvGrid(pairs));
    if (ev.input !== null && ev.input !== undefined) {
      var fi = h('div', 'event-field');
      fi.appendChild(h('div', 'kv-label', 'input'));
      fi.appendChild(h('pre', 'event-pre', textOf(ev.input)));
      body.appendChild(fi);
    }
    if (ev.result !== null && ev.result !== undefined) {
      var fr = h('div', 'event-field');
      fr.appendChild(h('div', 'kv-label', 'result'));
      fr.appendChild(h('pre', 'event-pre', textOf(ev.result)));
      body.appendChild(fr);
    }
    if (ev.usage !== null && ev.usage !== undefined) {
      var fu = h('div', 'event-field');
      fu.appendChild(h('div', 'kv-label', 'usage'));
      fu.appendChild(usageLine(ev.usage, null));
      body.appendChild(fu);
    }
    if (!body.firstChild) body.appendChild(h('div', 'muted', '无可展开的观测字段'));
    return body;
  }

  function eventItem(ev) {
    var wrap = h('article', 'event-item');
    var head = h('div', 'event-head');
    head.appendChild(badge(EVENT_LABEL[ev.type] || ev.type || '事件', 'event-' + safeClass(ev.type || 'unknown')));
    head.appendChild(h('span', 'event-time muted', fmtTime(ev.timestamp)));
    if (ev.agent_id) head.appendChild(h('code', 'event-agent', ev.agent_id));
    if (ev.role) head.appendChild(h('span', 'event-role', String(ev.role)));
    if (ev.tool) head.appendChild(h('code', 'event-tool', String(ev.tool)));
    if (ev.status) head.appendChild(h('span', 'event-status', String(ev.status)));
    wrap.appendChild(head);

    if (ev.text !== null && ev.text !== undefined && ev.text !== '') {
      wrap.appendChild(previewBlock(String(ev.text), 'ev-text|' + evKey(ev)));
    }

    var hasBody = ev.input !== null && ev.input !== undefined || ev.result !== null && ev.result !== undefined ||
      ev.usage !== null && ev.usage !== undefined || ev.call_id || ev.session_id || ev.step_id || ev.turn_id || ev.model;
    if (hasBody) {
      var key = evKey(ev);
      var expanded = !!state.expandedEvents[key];
      var toggle = h('button', 'btn btn-small event-toggle', expanded ? '收起参数/输出' : '展开参数/输出');
      toggle.type = 'button';
      toggle.setAttribute('aria-expanded', expanded ? 'true' : 'false');
      var body = eventBody(ev);
      body.hidden = !expanded;
      toggle.addEventListener('click', function () {
        var ex = !state.expandedEvents[key];
        state.expandedEvents[key] = ex;
        body.hidden = !ex;
        toggle.textContent = ex ? '收起参数/输出' : '展开参数/输出';
        toggle.setAttribute('aria-expanded', ex ? 'true' : 'false');
      });
      wrap.appendChild(toggle);
      wrap.appendChild(body);
    }

    var actions = h('div', 'event-actions');
    actions.appendChild(copyBtn(function () { return JSON.stringify(ev, null, 2); }, '复制事件'));
    wrap.appendChild(actions);
    return wrap;
  }

  function renderEvents() {
    var list = $('event-list');
    if (!list) return;
    var scroll = list.scrollTop;
    clear(list);
    state.events.items.forEach(function (ev) { list.appendChild(eventItem(ev)); });
    list.scrollTop = scroll;
    renderEventsStatus();
  }

  function renderAgentFilter() {
    var sel = $('event-agent-filter');
    if (!sel) return;
    var agents = (state.detail && state.detail.agents) || [];
    var ids = agents.map(function (a) { return a.agent_id; }).filter(Boolean);
    var cur = state.events.agentId;
    clear(sel);
    var all = document.createElement('option');
    all.value = '';
    all.textContent = '全部代理';
    sel.appendChild(all);
    ids.forEach(function (id) {
      var o = document.createElement('option');
      o.value = id;
      o.textContent = id;
      sel.appendChild(o);
    });
    sel.value = ids.indexOf(cur) >= 0 ? cur : '';
    if (sel.value !== cur) state.events.agentId = sel.value;
  }

  /* ---------- 子代理拓扑 ---------- */

  function agentNode(agent, childrenMap, depth, visited) {
    var node = h('div', 'agent-node');
    node.setAttribute('data-depth', String(depth));
    var head = h('div', 'agent-head');
    head.appendChild(h('code', 'agent-id', agent.agent_id || '未记录'));
    head.appendChild(badge(agentStatusLabel(agent.status), 'status-' + safeClass(agent.status)));
    if (agent.model) head.appendChild(h('span', 'agent-model muted', agent.model));
    head.appendChild(h('span', 'agent-parent muted', 'parent: ' + (agent.parent_id || '无（根）')));
    head.appendChild(h('span', 'agent-tools muted', '工具 ' + num(agent.tool_count)));
    head.appendChild(h('span', 'agent-usage muted', 'token ' + num(agent.usage && agent.usage.total_tokens)));
    if (agent.session_id) head.appendChild(h('code', 'agent-session', agent.session_id));
    node.appendChild(head);

    var kids = childrenMap[agent.agent_id] || [];
    if (kids.length) {
      var box = h('div', 'agent-children');
      kids.forEach(function (k) {
        if (visited[k.agent_id]) return;
        visited[k.agent_id] = 1;
        box.appendChild(agentNode(k, childrenMap, depth + 1, visited));
      });
      node.appendChild(box);
    }
    return node;
  }

  function renderAgentsPanel(d) {
    var p = $('panel-agents');
    clear(p);
    var agents = d.agents || [];
    if (!agents.length) {
      p.appendChild(empty('未记录子代理或代理事件'));
      return;
    }
    var byId = {};
    agents.forEach(function (a) { if (a.agent_id) byId[a.agent_id] = a; });
    var childrenMap = {};
    var roots = [];
    agents.forEach(function (a) {
      if (a.parent_id && byId[a.parent_id]) {
        if (!childrenMap[a.parent_id]) childrenMap[a.parent_id] = [];
        childrenMap[a.parent_id].push(a);
      } else {
        roots.push(a);
      }
    });
    var s = section('代理拓扑（缩进表示父子关系）');
    var tree = h('div', 'agent-tree');
    var visited = {};
    roots.forEach(function (a) {
      if (visited[a.agent_id]) return;
      visited[a.agent_id] = 1;
      tree.appendChild(agentNode(a, childrenMap, 0, visited));
    });
    s.appendChild(tree);
    p.appendChild(s);
  }

  /* ---------- 文件改动 ---------- */

  function renderChangesPanel(d) {
    var p = $('panel-changes');
    clear(p);
    var changes = d.changes;
    var s = section('文件改动');
    if (changes === null || changes === undefined || (Array.isArray(changes) && !changes.length)) {
      s.appendChild(empty('未记录文件改动'));
    } else if (Array.isArray(changes)) {
      changes.forEach(function (c) {
        var item = h('div', 'change-item');
        if (typeof c === 'string') {
          item.appendChild(h('code', 'change-path', c));
        } else if (c && typeof c === 'object') {
          item.appendChild(h('code', 'change-path', c.path || c.file || c.name || '未记录路径'));
          var pairs = [];
          ['status', 'action', 'change_type', 'additions', 'deletions', 'diff'].forEach(function (k) {
            if (c[k] !== undefined && c[k] !== null) pairs.push([k, k === 'diff' ? undefined : c[k]]);
          });
          if (pairs.length) item.appendChild(kvGrid(pairs.filter(function (x) { return x[1] !== undefined; })));
          if (c.diff) item.appendChild(h('pre', 'event-pre', String(c.diff)));
        } else {
          item.appendChild(h('span', null, oneline(c)));
        }
        s.appendChild(item);
      });
    } else {
      s.appendChild(h('pre', 'event-pre', textOf(changes)));
    }
    p.appendChild(s);
    p.appendChild(artifactBlock(d.run.run_id, 'patch', '补丁工件 patch'));
  }

  /* ---------- Token ---------- */

  function renderTokenPanel(d) {
    var p = $('panel-token');
    clear(p);
    var s1 = section('本任务 DeepSeek 用量（已记录）');
    s1.appendChild(usageLine(d.usage || {}, d.usage ? d.usage.coverage : null, 'deepseek'));
    s1.appendChild(h('div', 'usage-note',
      'Harness DeepSeek：inputTokens 为非缓存输入，cacheRead / cacheWrite 独立计数；接口已报告的 total 已包含缓存部分。未知字段显示“未记录”，不按 0 计算，不自行推算费用或节省率。'));
    p.appendChild(s1);

    var s2 = section('关联 Codex 会话累计 token（非本任务归属）');
    var threads = (d.codex_thread_ids || []).filter(function (t) { return !!t; });
    if (!threads.length) {
      s2.appendChild(empty('未记录关联 Codex thread，无累计数据可展示'));
    } else {
      threads.forEach(function (t) {
        var sessions = codexSessions();
        var found = null;
        for (var i = 0; i < sessions.length; i++) { if (sessions[i].thread_id === t) { found = sessions[i]; break; } }
        var block = h('div', 'codex-session-block');
        block.appendChild(h('h4', 'section-subtitle', 'thread ' + t));
        block.appendChild(codexLink(t));
        if (found) {
          block.appendChild(usageLine(found.usage || {}, found.usage_scope, 'codex'));
          block.appendChild(h('div', 'usage-note',
            '可用性：' + (found.available === undefined || found.available === null ? '未记录' : String(found.available)) +
            '；Codex input_tokens 含 cached_input_tokens，reasoning 为输出子集；范围为整个关联会话累计，包含多次请求与缓存，不能归属单个 DeepSeek 任务，不代表此功能费用或节省量。'));
        } else {
          block.appendChild(empty('本地概览未返回该会话用量（可能未记录或已过期）'));
        }
        s2.appendChild(block);
      });
    }
    p.appendChild(s2);
  }

  /* ---------- 全局视图与 Codex 会话抽屉 ---------- */

  function renderGlobalView() {
    var title = $('global-view-title');
    var body = $('global-view-body');
    clear(body);
    if (state.view === 'codex') {
      setText(title, 'Codex 会话');
      var sessions = codexSessions();
      if (!sessions.length) { body.appendChild(empty('没有关联的 Codex 会话')); return; }
      var list = h('div', 'codex-session-list');
      sessions.forEach(function (s) {
        var item = h('button', 'btn codex-session-item');
        item.type = 'button';
        item.appendChild(h('code', 'codex-thread', s.thread_id || '未记录'));
        item.appendChild(h('span', 'codex-session-meta',
          (s.model || '模型未记录') + ' · ' + (s.status || '状态未记录') + ' · 合计 ' + num(s.usage && s.usage.total_tokens) +
          ' · 范围 ' + (s.usage_scope ? textOf(s.usage_scope) : '未记录')));
        item.addEventListener('click', function () { openCodex(s.thread_id); });
        list.appendChild(item);
      });
      body.appendChild(list);
      return;
    }
    if (state.view === 'decisions') {
      setText(title, 'Codex 全局决策流');
      var decs = (state.overview && state.overview.decisions) || [];
      if (!decs.length) { body.appendChild(empty('未记录全局决策（含无 run_id 的规划/集成审核）')); return; }
      decs.forEach(function (dec) { body.appendChild(decisionItem(dec)); });
      return;
    }
    setText(title, '');
  }

  function openCodex(threadId) {
    if (!threadId) return;
    codexSeq++;
    state.codex = { threadId: threadId, items: [], nextCursor: null, hasMore: false, mode: 'latest', total: null, windowStart: 0 };
    state.expandedCodex = {};
    var drawer = $('codex-drawer');
    drawer.hidden = false;
    setText($('codex-drawer-title'), 'Codex 会话 ' + threadId);
    clear($('codex-meta'));
    clear($('codex-items'));
    setText($('codex-status'), '加载中…');
    $('codex-load-more').hidden = true;
    updateCodexModeButtons();
    loadCodexLatest();
    var close = $('codex-close');
    if (close && !close._bound) {
      close._bound = true;
      close.addEventListener('click', closeCodex);
    }
  }

  function closeCodex() {
    $('codex-drawer').hidden = true;
    codexSeq++;
    state.codex.threadId = null;
  }

  function codexErr(e) {
    if (e && e.status === 401) handleErr(e);
    else setText($('codex-status'), '加载失败：' + ((e && e.message) || e));
  }

  function updateCodexModeButtons() {
    var c = state.codex;
    var latest = $('codex-latest');
    var start = $('codex-from-start');
    if (latest) latest.textContent = c.mode === 'start' ? '回到最新' : '查看最新记录';
    if (start) start.textContent = '从头查看';
  }

  function loadCodexFromStart() {
    var c = state.codex;
    if (!c.threadId) return;
    c.mode = 'start';
    c.items = [];
    c.nextCursor = null;
    c.hasMore = false;
    c.windowStart = 0;
    updateCodexModeButtons();
    renderCodexItems([]);
    $('codex-items').scrollTop = 0;
    setText($('codex-status'), '加载中…');
    $('codex-load-more').hidden = true;
    fetchCodexWindow(0, false);
  }

  function loadCodexLatest() {
    var c = state.codex;
    if (!c.threadId) return;
    var tid = c.threadId;
    c.mode = 'latest';
    updateCodexModeButtons();
    $('codex-load-more').hidden = true;
    var probeSeq = ++codexSeq;
    apiGet('/api/codex/' + encodeURIComponent(tid) + '?limit=1&after=0', 'codex:' + tid)
      .then(function (data) {
        if (!data) return null;
        if (probeSeq !== codexSeq || state.codex.threadId !== tid) return null;
        c.total = codexTotal(data.total);
        return fetchCodexWindow(Math.max(0, c.total - CODEX_PAGE), false);
      })
      .catch(codexErr);
  }

  function refreshCodexIfNeeded() {
    var c = state.codex;
    var drawer = $('codex-drawer');
    if (!c.threadId || !drawer || drawer.hidden) return;
    if (c.mode !== 'latest') return;
    loadCodexLatest();
  }

  function loadCodexMore() {
    var c = state.codex;
    if (!c.threadId || c.mode !== 'start') return;
    var next = (c.nextCursor === null || c.nextCursor === undefined) ? c.items.length : c.nextCursor;
    fetchCodexWindow(next, true);
  }

  function fetchCodexWindow(after, append) {
    var c = state.codex;
    if (!c.threadId) return Promise.resolve(null);
    var tid = c.threadId;
    var start = Math.max(0, Number(after) || 0);
    var mySeq = ++codexSeq;
    var q = new URLSearchParams();
    q.set('limit', String(CODEX_PAGE));
    q.set('after', String(start));
    return apiGet('/api/codex/' + encodeURIComponent(tid) + '?' + q.toString(), 'codex:' + tid)
      .then(function (data) {
        if (!data) return null;
        /* 抽屉关闭/切换或旧请求过期后，响应不得覆盖当前内容 */
        if (mySeq !== codexSeq || state.codex.threadId !== tid) return null;
        applyCodexPage(data, append, start);
        return data;
      })
      .catch(function (e) { codexErr(e); return null; });
  }

  function applyCodexPage(data, append, after) {
    var c = state.codex;
    renderCodexMeta(data.session, data);
    var items = data.items || [];
    if (!append) c.items = [];
    var seen = {};
    c.items.forEach(function (e) { seen[evKey(e)] = 1; });
    items.forEach(function (e) { var k = evKey(e); if (!seen[k]) { seen[k] = 1; c.items.push(e); } });
    if (!append) c.windowStart = after;
    if (data.total !== undefined && data.total !== null) c.total = codexTotal(data.total);
    c.nextCursor = data.next_cursor === undefined ? null : data.next_cursor;
    c.hasMore = !!data.has_more;
    renderCodexStatus(items.length);
    $('codex-load-more').hidden = !(c.mode === 'start' && c.hasMore);
    renderCodexItems(data.warnings);
  }

  function renderCodexStatus(fetched) {
    var c = state.codex;
    var total = (c.total === null || c.total === undefined) ? null : c.total;
    var totalText = total === null ? '未知' : String(total);
    if (!c.items.length) {
      var emptyMsg = '没有公开消息';
      if (total !== null && total > 0) emptyMsg = '暂无已加载的公开记录（共 ' + totalText + ' 条，缺失如实提示）';
      setText($('codex-status'), emptyMsg);
      return;
    }
    var first = Math.max(0, Number(c.windowStart) || 0);
    var startNo = first + 1;
    var endNo = first + c.items.length;
    var tail = c.mode === 'start'
      ? '（已暂停跟随，可加载更早记录）'
      : '（自动跟随最新，每5秒刷新）';
    setText($('codex-status'), '第' + startNo + '-' + endNo + '/' + totalText + '条 公开记录 ' + tail);
  }

  function renderCodexMeta(session, data) {
    var meta = $('codex-meta');
    clear(meta);
    if (!session) return;
    meta.appendChild(kv('thread', session.thread_id || state.codex.threadId));
    meta.appendChild(kv('模型', session.model || null));
    meta.appendChild(kv('状态', session.status || null));
    meta.appendChild(kv('开始', fmtTime(session.started_at)));
    meta.appendChild(kv('更新', fmtTime(session.updated_at)));
    meta.appendChild(kv('可用', session.available === undefined || session.available === null ? null : String(session.available)));
    meta.appendChild(kv('累计合计', num(session.usage && session.usage.total_tokens)));
    meta.appendChild(kv('统计范围', session.usage_scope ? textOf(session.usage_scope) : null));
    if (data && data.warnings && data.warnings.length) {
      data.warnings.forEach(function (w) { meta.appendChild(h('div', 'warning-item', String(w))); });
    }
  }

  function renderCodexItems(warnings) {
    var list = $('codex-items');
    var scroll = list.scrollTop;
    clear(list);
    (warnings || []).forEach(function (w) { list.appendChild(h('div', 'warning-item', String(w))); });
    state.codex.items.forEach(function (ev) {
      var wrap = h('article', 'event-item');
      var head = h('div', 'event-head');
      head.appendChild(badge(EVENT_LABEL[ev.type] || ev.type || '条目', 'event-' + safeClass(ev.type || 'unknown')));
      head.appendChild(h('span', 'event-time muted', fmtTime(ev.timestamp)));
      if (ev.role) head.appendChild(h('span', 'event-role', String(ev.role)));
      if (ev.tool) head.appendChild(h('code', 'event-tool', String(ev.tool)));
      if (ev.turn_id) head.appendChild(h('code', 'event-turn', String(ev.turn_id)));
      wrap.appendChild(head);
      if (ev.text) wrap.appendChild(previewBlock(String(ev.text), 'codex-text|' + evKey(ev)));

      var hasBody = ev.input !== null && ev.input !== undefined || ev.result !== null && ev.result !== undefined || ev.usage !== null && ev.usage !== undefined;
      if (hasBody) {
        var key = 'codex|' + evKey(ev);
        var expanded = !!state.expandedCodex[key];
        var toggle = h('button', 'btn btn-small event-toggle', expanded ? '收起参数/输出' : '展开参数/输出');
        toggle.type = 'button';
        toggle.setAttribute('aria-expanded', expanded ? 'true' : 'false');
        var body = eventBody(ev);
        body.hidden = !expanded;
        toggle.addEventListener('click', function () {
          var ex = !state.expandedCodex[key];
          state.expandedCodex[key] = ex;
          body.hidden = !ex;
          toggle.textContent = ex ? '收起参数/输出' : '展开参数/输出';
          toggle.setAttribute('aria-expanded', ex ? 'true' : 'false');
        });
        wrap.appendChild(toggle);
        wrap.appendChild(body);
      }
      var actions = h('div', 'event-actions');
      actions.appendChild(copyBtn(function () { return JSON.stringify(ev, null, 2); }, '复制'));
      wrap.appendChild(actions);
      list.appendChild(wrap);
    });
    list.scrollTop = scroll;
  }

  /* ---------- 复制与提示 ---------- */

  function copyText(text, btn) {
    var value = text === null || text === undefined ? '' : String(text);
    function ok() {
      toast('已复制');
      if (btn) {
        var old = btn.textContent;
        btn.textContent = '已复制';
        setTimeout(function () { btn.textContent = old; }, 1200);
      }
    }
    function fail() { toast('复制失败，请手动选择文本'); }
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(value).then(ok).catch(function () { fallbackCopy(value) ? ok() : fail(); });
    } else {
      fallbackCopy(value) ? ok() : fail();
    }
  }
  function fallbackCopy(value) {
    try {
      var ta = document.createElement('textarea');
      ta.value = value;
      ta.setAttribute('readonly', 'readonly');
      ta.className = 'copy-fallback';
      document.body.appendChild(ta);
      ta.select();
      var done = document.execCommand('copy');
      document.body.removeChild(ta);
      return done;
    } catch (e) { return false; }
  }
  function toast(msg) {
    var t = $('toast');
    if (!t) return;
    t.textContent = msg;
    t.classList.add('is-visible');
    clearTimeout(t._tid);
    t._tid = setTimeout(function () { t.classList.remove('is-visible'); }, 1600);
  }

  /* ---------- 页签与导航 ---------- */

  function applyTabState() {
    Array.prototype.forEach.call(document.querySelectorAll('.tab-btn'), function (b) {
      var name = b.getAttribute('data-tab');
      var on = name === state.activeTab;
      b.classList.toggle('is-active', on);
      b.setAttribute('aria-selected', on ? 'true' : 'false');
      var panel = $('panel-' + name);
      if (panel) { panel.hidden = !on; panel.classList.toggle('is-active', on); }
    });
  }

  function setNav(view) {
    state.view = view;
    Array.prototype.forEach.call(document.querySelectorAll('.nav-item'), function (b) {
      var on = b.getAttribute('data-view') === view;
      b.classList.toggle('is-active', on);
      if (on) b.setAttribute('aria-current', 'page');
      else b.removeAttribute('aria-current');
    });
  }

  function showView(view) {
    setNav(view);
    if (view === 'tasks') {
      $('global-view').hidden = true;
      var hasDetail = !!state.detail;
      $('detail-content').hidden = !hasDetail;
      $('detail-empty').hidden = hasDetail;
      if (!hasDetail) $('detail-empty').textContent = '请选择左侧任务查看详情';
    } else {
      $('detail-empty').hidden = true;
      $('detail-content').hidden = true;
      $('global-view').hidden = false;
      renderGlobalView();
    }
  }

  /* ---------- 刷新循环 ---------- */

  function tick() {
    if (!state.autoRefresh || state.ticking) return;
    state.ticking = true;
    refreshCodexIfNeeded();
    loadOverview(false)
      .then(function () {
        if (state.selectedRunId && state.view === 'tasks') {
          return loadDetail(false).then(function () {
            return fetchEvents({ after: state.events.consumedOffset || 0 });
          });
        }
        return null;
      })
      .catch(handleErr)
      .then(function () { state.ticking = false; });
  }

  function setAutoRefresh(on) {
    state.autoRefresh = !!on;
    var btn = $('refresh-toggle');
    btn.setAttribute('aria-pressed', on ? 'true' : 'false');
    btn.textContent = '自动刷新：' + (on ? '开' : '关');
    if (refreshTimer) { clearInterval(refreshTimer); refreshTimer = null; }
    if (on) refreshTimer = setInterval(tick, REFRESH_MS);
  }

  /* ---------- 事件绑定与启动 ---------- */

  function bind() {
    Array.prototype.forEach.call(document.querySelectorAll('.tab-btn'), function (b) {
      b.addEventListener('click', function () {
        state.activeTab = b.getAttribute('data-tab');
        applyTabState();
        if (state.activeTab === 'events' && !state.events.items.length && !state.events.loading) {
          fetchEvents({ replace: true, after: 0 });
        }
      });
    });
    Array.prototype.forEach.call(document.querySelectorAll('.nav-item'), function (b) {
      b.addEventListener('click', function () { showView(b.getAttribute('data-view')); });
    });

    var search = $('search-input');
    search.addEventListener('input', function () {
      clearTimeout(searchTimer);
      searchTimer = setTimeout(function () {
        state.filters.q = search.value.trim();
        loadOverview(true).catch(handleErr);
      }, 300);
    });
    $('workspace-filter').addEventListener('change', function (e) {
      state.filters.workspace = e.target.value;
      loadOverview(true).catch(handleErr);
    });
    $('status-filter').addEventListener('change', function (e) {
      state.filters.status = e.target.value;
      loadOverview(true).catch(handleErr);
    });
    $('load-more-runs').addEventListener('click', loadMoreRuns);
    $('refresh-toggle').addEventListener('click', function () { setAutoRefresh(!state.autoRefresh); });
    $('error-retry').addEventListener('click', function () {
      hideError();
      loadOverview(true).then(function () {
        if (state.selectedRunId) return loadDetail(true).then(function () { return fetchEvents({ after: 0 }); });
        return null;
      }).catch(handleErr);
    });
    $('codex-load-more').addEventListener('click', loadCodexMore);
    var codexLatestBtn = $('codex-latest');
    if (codexLatestBtn) codexLatestBtn.addEventListener('click', loadCodexLatest);
    var codexStartBtn = $('codex-from-start');
    if (codexStartBtn) codexStartBtn.addEventListener('click', loadCodexFromStart);
    document.addEventListener('keydown', function (e) {
      if (e.key === 'Escape' && !$('codex-drawer').hidden) closeCodex();
    });
  }

  function start() {
    bind();
    applyTabState();
    setAutoRefresh(true);
    loadOverview(true).catch(handleErr);
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', start);
  else start();
})();
