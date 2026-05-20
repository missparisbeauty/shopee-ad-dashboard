/**
 * 統一 API 呼叫入口
 * 前端所有 fetch 必須走這裡，方便日後換真實蝦皮 Open API
 */
const BASE = '/api/v1';
const TIMEOUT_MS = 10000;

async function request(path, opts = {}) {
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), TIMEOUT_MS);
  try {
    const res = await fetch(BASE + path, { ...opts, signal: ctrl.signal });
    if (!res.ok) {
      const body = await res.json().catch(() => ({}));
      throw new ApiError(body?.error?.message || res.statusText, body?.error?.code || 'HTTP_' + res.status);
    }
    const json = await res.json();
    if (json.error) throw new ApiError(json.error.message, json.error.code);
    return json.data;
  } catch (e) {
    if (e.name === 'AbortError') throw new ApiError('請求逾時', 'TIMEOUT');
    if (e instanceof ApiError) throw e;
    throw new ApiError(e.message || '網路錯誤', 'NETWORK');
  } finally {
    clearTimeout(timer);
  }
}

class ApiError extends Error {
  constructor(message, code) { super(message); this.code = code; }
}

const api = {
  getKpi:        (period = 'week', opts = {}) => {
    const q = new URLSearchParams({ period });
    if (opts.source) q.set('source', opts.source);
    if (opts.shop)   q.set('shop',   opts.shop);
    return request(`/kpi?${q}`);
  },
  getKpiTrend:   (days = 7, opts = {}) => {
    const q = new URLSearchParams({ days });
    if (opts.source) q.set('source', opts.source);
    if (opts.shop)   q.set('shop',   opts.shop);
    return request(`/kpi/trend?${q}`);
  },
  getAccounts:   ()                        => request('/accounts'),
  getHeatmap:    (accountId = 'S001')     => request(`/heatmap?account_id=${accountId}`),
  getEvents:     ()                        => request('/events'),
  getSuggestions:(type = 'all')            => request(`/suggestions?type=${type}`),
  applySuggestion:(id)                     => request(`/suggestions/${id}/apply`, { method: 'POST' }),
  getRising:     ()                        => request('/products/rising'),
  getOptimized:  ()                        => request('/products/optimized'),
  getActions:    ()                        => request('/actions'),
  listSnapshots: ()                        => request('/snapshots'),
  takeSnapshot:  ()                        => request('/snapshots/take', { method:'POST' }),
  compareSnapshots: (weeks=4)              => request(`/snapshots/compare?weeks=${weeks}`),
  csvUrl:        (snapId)                  => `${BASE}/snapshots/${snapId}/csv`,
  getCompetitorKeywords: ()                => request('/competitor/keywords'),
  getCompetitorPrices:   (cat='3C')        => request(`/competitor/price-distribution?category=${cat}`),
  // 單品調整
  getAdSettings: (pid)                      => request(`/products/${pid}/ad-settings`),
  submitAdjust:  (body)                     => request('/products/adjust', { method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(body) }),
  listAdjustments:(status='all')            => request(`/adjustments?status=${status}`),
  markAdjustDone:(id)                       => request(`/adjustments/${id}/done`, { method:'POST' }),
  // OAuth
  getShopeeAuthUrl: ()                      => request('/auth/shopee/url'),
  listConnections:  ()                      => request('/auth/shopee/connections'),
  disconnectShop:   (shopId)                => request(`/auth/shopee/${shopId}`, { method:'DELETE' }),
  // CSV upload
  uploadAdReport: async (shop, file, opts = {}) => {
    const fd = new FormData();
    fd.append('shop', shop);
    fd.append('file', file);
    if (opts.force) fd.append('force', 'true');
    const res = await fetch(BASE + '/upload/ad-report', { method:'POST', body: fd });
    let body = {};
    try { body = await res.json(); } catch (_) {}
    if (!res.ok) {
      // 重複上傳 (409) → 帶結構化錯誤資訊讓前端決定要不要強制
      if (res.status === 409 && body?.detail?.error === 'duplicate_upload') {
        const err = new ApiError(body.detail.message, 'DUPLICATE');
        err.duplicate_of = body.detail.existing_upload_id;
        throw err;
      }
      const msg = body?.error?.message || body?.detail || res.statusText || '上傳失敗';
      const code = body?.error?.code || ('HTTP_' + res.status);
      throw new ApiError(msg, code);
    }
    if (body?.error) throw new ApiError(body.error.message, body.error.code);
    return body.data;
  },
  listUploads:    ()                        => request('/uploads'),
  deleteUpload:   (id)                      => request(`/uploads/${id}`, { method:'DELETE' }),
  // SendGrid Email 狀態
  getEmailStatus:   ()                      => request('/email/status'),
  // 外部 API（蝦皮 v4 公開 + Google Trends）
  getExternalStatus:()                      => request('/external/status'),
  getShopeeItem:    (shopId, itemId)        => request(`/external/shopee/item?shop_id=${shopId}&item_id=${itemId}`),
  syncShopeeHealth: (shopId)                => request(`/external/shopee/sync-health?shop_id=${shopId}`, { method:'POST' }),
  searchShopee:     (keyword, opts={}) => {
    const q = new URLSearchParams({ keyword });
    if (opts.limit) q.set('limit', opts.limit);
    if (opts.by)    q.set('by',    opts.by);
    return request(`/external/shopee/search?${q}`);
  },
  getTrendsDaily:   (geo='TW')              => request(`/external/trends/daily?geo=${geo}`),
  exploreKeywordTrends:(keyword, geo='TW')  => request(`/external/trends/explore?keyword=${encodeURIComponent(keyword)}&geo=${geo}`),
  // AI 顧問
  getAiStatus:      ()                      => request('/ai/status'),
  getAiInsights:    (body={}) => request('/ai/insights', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify(body),
  }),
  // Shopee Open API
  getShopeeStatus:  ()                      => request('/shopee/status'),
  syncShopeeProducts:(shopId)               => request(`/shopee/products/sync?shop_id=${encodeURIComponent(shopId)}`, { method:'POST' }),
  listShopeeProducts:(shopId)               => request('/shopee/products' + (shopId ? `?shop_id=${encodeURIComponent(shopId)}` : '')),
  // 資料夾自動上傳 watcher
  getWatcherStatus: ()                      => request('/watcher/status'),
  startWatcher:     (interval=5)            => request(`/watcher/start?scan_interval=${interval}`, { method:'POST' }),
  stopWatcher:      ()                      => request('/watcher/stop', { method:'POST' }),
  scanWatcherNow:   ()                      => request('/watcher/scan-now', { method:'POST' }),
  addWatchDir:      (path) => request('/watcher/dirs/add', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({ path }),
  }),
  removeWatchDir:   (path) => request('/watcher/dirs/remove', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({ path }),
  }),
  // 真實廣告資料聚合
  getAdDataStatus:  ()                      => request('/ad-data/status'),
  getAdDataProducts:(opts={}) => {
    const q = new URLSearchParams();
    if (opts.shop)   q.set('shop',   opts.shop);
    if (opts.period) q.set('period', opts.period);
    if (opts.limit)  q.set('limit',  opts.limit);
    return request(`/ad-data/products${q.toString() ? '?' + q : ''}`);
  },
  // 關鍵字 / 版位真實表現（從關鍵字/版位 CSV 聚合）
  getKeywordsPerformance: (opts={}) => {
    const q = new URLSearchParams();
    if (opts.shop)   q.set('shop',   opts.shop);
    if (opts.period) q.set('period', opts.period);
    if (opts.limit)  q.set('limit',  opts.limit);
    return request(`/keywords/performance${q.toString() ? '?' + q : ''}`);
  },
  getPlacementsPerformance: (opts={}) => {
    const q = new URLSearchParams();
    if (opts.shop)   q.set('shop',   opts.shop);
    if (opts.period) q.set('period', opts.period);
    if (opts.limit)  q.set('limit',  opts.limit);
    return request(`/placements/performance${q.toString() ? '?' + q : ''}`);
  },
  // 競品
  listCompetitors: ()                       => request('/competitors/list'),
  addCompetitor:   (body)                   => request('/competitors/list', { method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(body) }),
  removeCompetitor:(cid)                    => request(`/competitors/list/${cid}`, { method:'DELETE' }),
  getCompetitorNew:(  )                     => request('/competitor/new-products'),
  getCompetitorDrops:()                     => request('/competitor/price-drops'),
  // 真實毛利
  getProfitConfig: (opts={}) => {
    const q = new URLSearchParams();
    if (opts.shop) q.set('shop', opts.shop);
    if (opts.days) q.set('days', opts.days);
    return request('/profit/config' + (q.toString() ? '?'+q : ''));
  },
  updateProfitConfig:(pid, body)            => request(`/profit/config/${pid}`, { method:'PUT', headers:{'Content-Type':'application/json'}, body: JSON.stringify(body) }),
  deleteProfitConfig:(pid)                  => request(`/profit/config/${pid}`, { method:'DELETE' }),
  getProfitSummary:(period='week', opts={}) => {
    const q = new URLSearchParams({ period });
    if (opts.source) q.set('source', opts.source);
    if (opts.shop)   q.set('shop',   opts.shop);
    return request(`/profit/summary?${q}`);
  },
  // 預算與規則
  getBudgetPacing: (opts={}) => {
    const q = new URLSearchParams();
    if (opts.shop)   q.set('shop',   opts.shop);
    if (opts.source) q.set('source', opts.source);
    return request('/budget/pacing' + (q.toString() ? '?'+q : ''));
  },
  getBudgetAlerts: ()                       => request('/budget/alerts'),
  listRules:       ()                       => request('/rules'),
  addRule:         (body)                   => request('/rules', { method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(body) }),
  removeRule:      (id)                     => request(`/rules/${id}`, { method:'DELETE' }),
  runRules:        (opts={}) => {
    const q = new URLSearchParams();
    if (opts.shop)     q.set('shop',     opts.shop);
    if (opts.dry_run)  q.set('dry_run', 'true');
    return request('/rules/run' + (q.toString() ? '?'+q : ''), { method:'POST' });
  },
  previewRules:    (shop) => request('/rules/preview' + (shop ? '?shop='+encodeURIComponent(shop) : ''), { method:'POST' }),
  // 商品健康度
  getHealthScores: (opts={}) => {
    const q = new URLSearchParams();
    if (opts.shop)   q.set('shop',   opts.shop);
    if (opts.source) q.set('source', opts.source);
    return request('/products/health-scores' + (q.toString() ? '?'+q : ''));
  },
  // 關鍵字（探索已改走真實 exploreKeywordTrends → Google Trends）
  getKeywordGroups:()                       => request('/keywords/groups'),
  getSeasonalCalendar:()                    => request('/keywords/seasonal-calendar'),
  // 受眾與素材
  listAudiences:   ()                       => request('/audiences'),
  addAudience:     (body)                   => request('/audiences', { method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(body) }),
  removeAudience:  (id)                     => request(`/audiences/${id}`, { method:'DELETE' }),
  generateCopy:    (body)                   => request('/creative/generate-copy', { method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(body) }),
  // CRM + 月報
  listCustomers:   ()                       => request('/customers'),
  addCustomer:     (body)                   => request('/customers', { method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(body) }),
  removeCustomer:  (id)                     => request(`/customers/${id}`, { method:'DELETE' }),
  getMonthlyReport:(cid)                    => request(`/reports/monthly/${cid}`),
  sendReport:      (body)                   => request('/reports/send', { method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(body) }),
  getReportLog:    ()                       => request('/reports/log'),
  // 團隊 + LINE
  listTeam:        ()                       => request('/team'),
  addTeamMember:   (body)                   => request('/team', { method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(body) }),
  removeTeamMember:(id)                     => request(`/team/${id}`, { method:'DELETE' }),
  getNotifyConfig: ()                       => request('/notify/config'),
  updateNotifyConfig:(body)                 => request('/notify/config', { method:'PUT', headers:{'Content-Type':'application/json'}, body: JSON.stringify(body) }),
  testNotify:      ()                       => request('/notify/test', { method:'POST' }),
  // 跨平台
  getMultiPlatform:()                       => request('/accounts/multi-platform'),
};

window.api = api;
window.ApiError = ApiError;
