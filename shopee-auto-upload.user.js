// ==UserScript==
// @name         蝦皮廣告報表 → 代操儀表板 自動上傳
// @namespace    shopee-ad-dashboard
// @version      1.0.0
// @description  在你已登入的蝦皮賣家中心，攔截你下載的廣告報表 CSV，自動傳到代操儀表板。不存帳密、不違反 ToS（你本人操作的延伸）。
// @author       missparisbeauty
// @match        https://seller.shopee.tw/*
// @run-at       document-idle
// @grant        GM_xmlhttpRequest
// @grant        GM_setValue
// @grant        GM_getValue
// @grant        GM_addStyle
// @connect      run.app
// @connect      localhost
// ==/UserScript==

/*
  運作原理
  ─────────
  1. 你在自己瀏覽器登入蝦皮賣家中心（人工，過 CAPTCHA 一次）
  2. 你照常去「廣告報表」頁，選日期，點蝦皮自己的「下載/匯出」按鈕
  3. 本腳本攔截那個 CSV（在你已登入的 session 內，蝦皮看到的是真人）
  4. 跳出小視窗 → 你選這份是哪個店家/客戶 → 自動傳到 dashboard
  5. 也可開「自動模式」：偵測到 CSV 直接用預設店家上傳，不再問

  為什麼這樣不會被擋：因為不是機器人登入，是你本人 session 內的請求。
*/

(function () {
  'use strict';

  // ───────────────────────── 設定（存在 Tampermonkey storage）─────────────────────────
  const CFG = {
    get dashUrl()  { return GM_getValue('dashUrl', 'https://shopee-dashboard-758403173010.asia-east1.run.app'); },
    set dashUrl(v) { GM_setValue('dashUrl', v); },
    get user()     { return GM_getValue('authUser', 'admin'); },
    set user(v)    { GM_setValue('authUser', v); },
    get pass()     { return GM_getValue('authPass', ''); },
    set pass(v)    { GM_setValue('authPass', v); },
    get defShop()  { return GM_getValue('defShop', ''); },
    set defShop(v) { GM_setValue('defShop', v); },
    get autoMode() { return GM_getValue('autoMode', false); },
    set autoMode(v){ GM_setValue('autoMode', v); },
  };

  // ───────────────────────── 樣式 ─────────────────────────
  GM_addStyle(`
    #sad-fab{position:fixed;right:20px;bottom:20px;z-index:999999;background:#EE4D2D;color:#fff;
      border:none;border-radius:30px;padding:12px 18px;font-size:14px;font-weight:700;cursor:pointer;
      box-shadow:0 4px 14px rgba(0,0,0,.3);font-family:'Microsoft JhengHei',sans-serif}
    #sad-fab:hover{background:#d8431f}
    #sad-panel{position:fixed;right:20px;bottom:70px;z-index:999999;width:340px;background:#fff;
      border-radius:10px;box-shadow:0 8px 30px rgba(0,0,0,.25);padding:16px;display:none;
      font-family:'Microsoft JhengHei',sans-serif;font-size:13px;color:#1e293b}
    #sad-panel.open{display:block}
    #sad-panel h3{margin:0 0 10px;font-size:15px;color:#EE4D2D}
    #sad-panel label{display:block;margin:8px 0 3px;font-size:12px;color:#475569}
    #sad-panel input,#sad-panel select{width:100%;box-sizing:border-box;padding:6px 8px;
      border:1px solid #cbd5e1;border-radius:6px;font-size:13px}
    #sad-panel .row{display:flex;gap:8px}
    #sad-panel button{margin-top:12px;width:100%;padding:9px;border:none;border-radius:6px;
      background:#EE4D2D;color:#fff;font-weight:700;cursor:pointer;font-size:13px}
    #sad-panel .sec{background:#94a3b8}
    #sad-toast{position:fixed;left:50%;top:24px;transform:translateX(-50%);z-index:9999999;
      background:#1e293b;color:#fff;padding:12px 22px;border-radius:8px;font-size:14px;
      display:none;font-family:'Microsoft JhengHei',sans-serif;box-shadow:0 4px 14px rgba(0,0,0,.3);max-width:80vw}
    #sad-toast.ok{background:#16a34a}#sad-toast.err{background:#dc2626}#sad-toast.warn{background:#d97706}
  `);

  // ───────────────────────── UI ─────────────────────────
  function toast(msg, type, ms) {
    let t = document.getElementById('sad-toast');
    if (!t) { t = document.createElement('div'); t.id = 'sad-toast'; document.body.appendChild(t); }
    t.textContent = msg;
    t.className = type || '';
    t.style.display = 'block';
    clearTimeout(t._timer);
    t._timer = setTimeout(() => { t.style.display = 'none'; }, ms || 4000);
  }

  function buildPanel() {
    if (document.getElementById('sad-panel')) return;
    const p = document.createElement('div');
    p.id = 'sad-panel';
    p.innerHTML = `
      <h3>📤 廣告報表自動上傳設定</h3>
      <label>Dashboard 網址</label>
      <input id="sad-url" placeholder="https://...run.app">
      <div class="row">
        <div style="flex:1"><label>登入帳號</label><input id="sad-user" placeholder="admin"></div>
        <div style="flex:1"><label>登入密碼</label><input id="sad-pass" type="password"></div>
      </div>
      <label>這個賣場對應的「店家/客戶」名稱（要跟 dashboard 客戶一致）</label>
      <input id="sad-shop" placeholder="例：科爸好皮 BODY&FACE">
      <label style="margin-top:10px"><input type="checkbox" id="sad-auto" style="width:auto;margin-right:6px">
        自動模式：偵測到報表直接用上面店家上傳（不再每次問）</label>
      <button id="sad-save">儲存設定</button>
      <button id="sad-test" class="sec">測試連線 Dashboard</button>
      <div style="margin-top:10px;font-size:11px;color:#64748b;line-height:1.5">
        用法：設定好後，去蝦皮「廣告報表」頁照常選日期、點蝦皮的下載 → 本腳本會自動攔截並上傳。
      </div>
    `;
    document.body.appendChild(p);

    document.getElementById('sad-url').value  = CFG.dashUrl;
    document.getElementById('sad-user').value = CFG.user;
    document.getElementById('sad-pass').value = CFG.pass;
    document.getElementById('sad-shop').value = CFG.defShop;
    document.getElementById('sad-auto').checked = CFG.autoMode;

    document.getElementById('sad-save').onclick = () => {
      CFG.dashUrl  = document.getElementById('sad-url').value.trim().replace(/\/$/, '');
      CFG.user     = document.getElementById('sad-user').value.trim();
      CFG.pass     = document.getElementById('sad-pass').value;
      CFG.defShop  = document.getElementById('sad-shop').value.trim();
      CFG.autoMode = document.getElementById('sad-auto').checked;
      toast('✓ 設定已儲存', 'ok');
      p.classList.remove('open');
    };
    document.getElementById('sad-test').onclick = testConnection;
  }

  function buildFab() {
    if (document.getElementById('sad-fab')) return;
    const b = document.createElement('button');
    b.id = 'sad-fab';
    b.textContent = '📤 上傳設定';
    b.onclick = () => {
      buildPanel();
      document.getElementById('sad-panel').classList.toggle('open');
    };
    document.body.appendChild(b);
  }

  // ───────────────────────── 跨域上傳（GM_xmlhttpRequest 繞 CORS）─────────────────────────
  function authHeader() {
    return 'Basic ' + btoa(CFG.user + ':' + CFG.pass);
  }

  function testConnection() {
    if (!CFG.pass) { toast('請先填登入密碼', 'warn'); return; }
    toast('測試中…');
    GM_xmlhttpRequest({
      method: 'GET',
      url: CFG.dashUrl + '/api/v1/ad-data/status',
      headers: { 'Authorization': authHeader() },
      timeout: 15000,
      onload: (r) => {
        if (r.status === 200) toast('✓ Dashboard 連線成功！', 'ok');
        else if (r.status === 401) toast('✗ 帳號或密碼錯誤', 'err');
        else toast('✗ 連線失敗 HTTP ' + r.status, 'err');
      },
      onerror: () => toast('✗ 連不到 Dashboard，檢查網址', 'err'),
      ontimeout: () => toast('✗ 連線逾時', 'err'),
    });
  }

  function uploadCsv(csvText, filename, shop) {
    if (!CFG.pass) { toast('尚未設定 Dashboard 密碼，點右下角「📤 上傳設定」', 'warn', 6000); return; }
    if (!shop)     { toast('尚未指定店家名稱', 'warn'); return; }

    toast('上傳中：' + filename + ' → ' + shop + ' …');

    // 組 multipart/form-data
    const boundary = '----SADBoundary' + Date.now();
    const CRLF = '\r\n';
    let body = '';
    body += `--${boundary}${CRLF}`;
    body += `Content-Disposition: form-data; name="shop"${CRLF}${CRLF}${shop}${CRLF}`;
    body += `--${boundary}${CRLF}`;
    body += `Content-Disposition: form-data; name="file"; filename="${filename}"${CRLF}`;
    body += `Content-Type: text/csv${CRLF}${CRLF}${csvText}${CRLF}`;
    body += `--${boundary}--${CRLF}`;

    GM_xmlhttpRequest({
      method: 'POST',
      url: CFG.dashUrl + '/api/v1/upload/ad-report',
      headers: {
        'Authorization': authHeader(),
        'Content-Type': 'multipart/form-data; boundary=' + boundary,
      },
      data: body,
      timeout: 30000,
      onload: (r) => {
        let j = {};
        try { j = JSON.parse(r.responseText); } catch (_) {}
        if (r.status === 200) {
          const s = (j.data && j.data.summary) || {};
          toast(`✓ 上傳成功！${s.row_count || ''} 筆 · 營收 NT$${(s.total_revenue||0).toLocaleString()}`, 'ok', 6000);
        } else if (r.status === 409) {
          const msg = (j.detail && j.detail.message) || '重複上傳';
          if (confirm('⚠️ ' + msg + '\n\n要強制再上傳嗎？（會造成數字加倍，通常選取消）')) {
            uploadCsvForce(csvText, filename, shop);
          }
        } else if (r.status === 401) {
          toast('✗ Dashboard 帳密錯誤', 'err', 6000);
        } else {
          toast('✗ 上傳失敗 HTTP ' + r.status + '：' + (j.detail || r.responseText || '').toString().slice(0, 120), 'err', 8000);
        }
      },
      onerror: () => toast('✗ 上傳失敗（連不到 Dashboard）', 'err', 6000),
      ontimeout: () => toast('✗ 上傳逾時', 'err', 6000),
    });
  }

  function uploadCsvForce(csvText, filename, shop) {
    const boundary = '----SADBoundary' + Date.now();
    const CRLF = '\r\n';
    let body = '';
    body += `--${boundary}${CRLF}Content-Disposition: form-data; name="shop"${CRLF}${CRLF}${shop}${CRLF}`;
    body += `--${boundary}${CRLF}Content-Disposition: form-data; name="force"${CRLF}${CRLF}true${CRLF}`;
    body += `--${boundary}${CRLF}Content-Disposition: form-data; name="file"; filename="${filename}"${CRLF}`;
    body += `Content-Type: text/csv${CRLF}${CRLF}${csvText}${CRLF}--${boundary}--${CRLF}`;
    GM_xmlhttpRequest({
      method: 'POST', url: CFG.dashUrl + '/api/v1/upload/ad-report',
      headers: { 'Authorization': authHeader(), 'Content-Type': 'multipart/form-data; boundary=' + boundary },
      data: body, timeout: 30000,
      onload: (r) => toast(r.status === 200 ? '✓ 已強制上傳' : '✗ 失敗 ' + r.status, r.status === 200 ? 'ok' : 'err', 6000),
      onerror: () => toast('✗ 強制上傳失敗', 'err'),
    });
  }

  // ───────────────────────── 偵測：這段文字像不像蝦皮廣告 CSV ─────────────────────────
  function looksLikeShopeeAdCsv(text) {
    if (!text || text.length < 30) return false;
    const head = text.slice(0, 3000);
    // 蝦皮廣告報表特徵欄位（中文）
    const markers = ['廣告名稱', '花費', '點擊數', '商品 ID', '商品ID', '點擊率',
                     '轉換數', '銷售金額', '曝光數', '瀏覽數', 'CPC成效', '投入產出比'];
    let hit = 0;
    for (const m of markers) if (head.includes(m)) hit++;
    return hit >= 3;  // 命中 3 個以上才算
  }

  function decideShopAndUpload(csvText, filename) {
    if (CFG.autoMode && CFG.defShop) {
      uploadCsv(csvText, filename, CFG.defShop);
      return;
    }
    // 問使用者這份是哪個店家
    const shop = prompt(
      '偵測到廣告報表！\n\n這份要上傳到哪個店家/客戶？\n（要跟 Dashboard 客戶名稱一致）\n\n直接 Enter 用預設：' + (CFG.defShop || '(未設定)'),
      CFG.defShop || ''
    );
    if (shop === null) { toast('已取消上傳', 'warn'); return; }
    uploadCsv(csvText, filename, (shop || CFG.defShop).trim());
  }

  // ───────────────────────── 攔截下載：3 道防線 ─────────────────────────
  const seen = new Set();
  function handleCandidate(text, filename) {
    if (!looksLikeShopeeAdCsv(text)) return false;
    const sig = filename + ':' + text.length;
    if (seen.has(sig)) return false;  // 同一份不重複觸發
    seen.add(sig);
    setTimeout(() => seen.delete(sig), 10000);
    decideShopAndUpload(text, filename || ('蝦皮廣告報表_' + new Date().toISOString().slice(0, 10) + '.csv'));
    return true;
  }

  // 防線 1：攔 fetch
  const origFetch = window.fetch;
  window.fetch = function (...args) {
    return origFetch.apply(this, args).then((res) => {
      try {
        const ct = (res.headers.get('content-type') || '').toLowerCase();
        const url = (res.url || '').toLowerCase();
        if (ct.includes('csv') || ct.includes('octet-stream') || ct.includes('excel') ||
            url.includes('export') || url.includes('download') || url.includes('report')) {
          res.clone().text().then((txt) => {
            const fn = (url.split('/').pop() || 'report').split('?')[0];
            handleCandidate(txt, fn.endsWith('.csv') ? fn : 'shopee_ad_report.csv');
          }).catch(() => {});
        }
      } catch (_) {}
      return res;
    });
  };

  // 防線 2：攔 XMLHttpRequest
  const origOpen = XMLHttpRequest.prototype.open;
  const origSend = XMLHttpRequest.prototype.send;
  XMLHttpRequest.prototype.open = function (m, u) { this._sadUrl = u; return origOpen.apply(this, arguments); };
  XMLHttpRequest.prototype.send = function () {
    this.addEventListener('load', function () {
      try {
        const u = (this._sadUrl || '').toLowerCase();
        if (u.includes('export') || u.includes('download') || u.includes('report')) {
          const txt = (typeof this.responseText === 'string') ? this.responseText : '';
          if (txt) {
            const fn = (u.split('/').pop() || 'report').split('?')[0];
            handleCandidate(txt, fn.endsWith('.csv') ? fn : 'shopee_ad_report.csv');
          }
        }
      } catch (_) {}
    });
    return origSend.apply(this, arguments);
  };

  // 防線 3：攔 blob 下載（蝦皮用 <a download> + createObjectURL 的情況）
  const origCreateURL = URL.createObjectURL;
  URL.createObjectURL = function (obj) {
    const blobUrl = origCreateURL.apply(this, arguments);
    try {
      if (obj instanceof Blob &&
          (obj.type.includes('csv') || obj.type.includes('excel') ||
           obj.type.includes('octet-stream') || obj.type === '')) {
        obj.text().then((txt) => handleCandidate(txt, '蝦皮廣告報表.csv')).catch(() => {});
      }
    } catch (_) {}
    return blobUrl;
  };

  // ───────────────────────── 啟動 ─────────────────────────
  function init() {
    buildFab();
    if (!CFG.pass) {
      setTimeout(() => toast('👉 第一次使用：點右下角「📤 上傳設定」填好 Dashboard 密碼', 'warn', 8000), 1500);
    } else {
      console.log('[蝦皮自動上傳] 已就緒。去廣告報表頁下載 CSV 就會自動傳到 Dashboard。');
    }
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
