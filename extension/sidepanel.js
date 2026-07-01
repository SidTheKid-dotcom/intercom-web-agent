const BACKEND_URL = 'http://localhost:8000';
const POLL_INTERVAL_MS = 2500;
const MAX_LIVE_AGENT_STEPS = 6;
const MAX_LIVE_AGENT_ACTIONS_PER_STEP = 3;
const LIVE_AGENT_NAV_TIMEOUT_MS = 18000;
const LIVE_AGENT_SETTLE_TIMEOUT_MS = 1400;
const LIVE_AGENT_RECOVERY_LIMIT = 1;
const MAX_BROWSER_CRAWL_PAGES = 150;
const BROWSER_CRAWL_NAV_TIMEOUT_MS = 18000;
const BROWSER_CRAWL_STEP_DELAY_MS = 900;

let conversationHistory = [];
let currentUrl = '';
let currentSiteKey = '';
let currentJobId = '';
let isActivated = false;
let pollTimer = null;
let mediaRecorder = null;
let mediaStream = null;
let recordingChunks = [];
let isRecording = false;
let isLiveAgentMode = false;
let liveAgentRunning = false;
let liveAgentStopRequested = false;
let liveAgentHistory = [];
let liveAgentLastSnapshotId = '';
let liveAgentLastSnapshot = null;
let browserCrawlRunning = false;
let browserCrawlStopRequested = false;

const activateBtn = document.getElementById('activate-btn');
const liveCrawlBtn = document.getElementById('live-crawl-btn');
const liveAgentBtn = document.getElementById('live-agent-btn');
const liveAgentStopBtn = document.getElementById('live-agent-stop-btn');
const voiceBtn = document.getElementById('voice-btn');
const statusEl = document.getElementById('status');
const pageInfoEl = document.getElementById('page-info');
const pageTitleEl = document.getElementById('page-title');
const messagesEl = document.getElementById('messages');
const liveAgentTraceEl = document.getElementById('live-agent-trace');
const messageInput = document.getElementById('message-input');
const sendBtn = document.getElementById('send-btn');
const introSplash = document.getElementById('intro-splash');
const introContinue = document.getElementById('intro-continue');
const appEl = document.getElementById('app');

let introParticleTeardown = null;

function initIntroParticles() {
  const canvas = document.getElementById('intro-particles');
  const splash = document.getElementById('intro-splash');
  if (!canvas || !splash) return function () {};
  if (window.matchMedia('(prefers-reduced-motion: reduce)').matches) {
    return function () {};
  }

  const ctx = canvas.getContext('2d', { alpha: true });
  const n = 40;
  const linkDist = 72;
  const ps = [];
  let w = 0;
  let h = 0;
  let raf = 0;
  let running = true;

  function layout() {
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    w = splash.clientWidth;
    h = splash.clientHeight;
    if (w < 1 || h < 1) return;
    canvas.width = w * dpr;
    canvas.height = h * dpr;
    canvas.style.width = w + 'px';
    canvas.style.height = h + 'px';
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  }

  function seed() {
    ps.length = 0;
    for (let i = 0; i < n; i++) {
      ps.push({
        x: Math.random() * w,
        y: Math.random() * h,
        vx: (Math.random() - 0.5) * 0.4,
        vy: (Math.random() - 0.5) * 0.4,
      });
    }
  }

  function step() {
    if (ps.length < n) return;
    for (const p of ps) {
      p.x += p.vx;
      p.y += p.vy;
      if (p.x < 0) {
        p.x = 0;
        p.vx *= -1;
      } else if (p.x > w) {
        p.x = w;
        p.vx *= -1;
      }
      if (p.y < 0) {
        p.y = 0;
        p.vy *= -1;
      } else if (p.y > h) {
        p.y = h;
        p.vy *= -1;
      }
    }
  }

  function draw() {
    if (w < 1) return;
    ctx.clearRect(0, 0, w, h);
    if (ps.length < n) return;
    for (let i = 0; i < n; i++) {
      for (let j = i + 1; j < n; j++) {
        const a = ps[i];
        const b = ps[j];
        const dx = a.x - b.x;
        const dy = a.y - b.y;
        const d = Math.hypot(dx, dy);
        if (d < linkDist) {
          const o = 0.06 * (1 - d / linkDist);
          ctx.strokeStyle = `rgba(130, 140, 200, ${o})`;
          ctx.lineWidth = 0.5;
          ctx.beginPath();
          ctx.moveTo(a.x, a.y);
          ctx.lineTo(b.x, b.y);
          ctx.stroke();
        }
      }
    }
    for (const p of ps) {
      ctx.fillStyle = 'rgba(210, 210, 230, 0.2)';
      ctx.beginPath();
      ctx.arc(p.x, p.y, 0.9, 0, Math.PI * 2);
      ctx.fill();
    }
  }

  function loop() {
    if (!running) return;
    step();
    draw();
    raf = requestAnimationFrame(loop);
  }

  function onResize() {
    layout();
    if (w > 0 && h > 0) {
      if (ps.length === 0) {
        seed();
      } else {
        for (const p of ps) {
          p.x = Math.min(p.x, w);
          p.y = Math.min(p.y, h);
        }
      }
    }
  }

  layout();
  onResize();
  raf = requestAnimationFrame(loop);
  window.addEventListener('resize', onResize);
  const ro = new ResizeObserver(onResize);
  ro.observe(splash);

  return function stopIntroParticles() {
    running = false;
    cancelAnimationFrame(raf);
    window.removeEventListener('resize', onResize);
    ro.disconnect();
  };
}

async function applyLabMetaXml() {
  const kicker = document.getElementById('intro-kicker');
  if (!kicker) return;
  try {
    const text = await (await fetch(chrome.runtime.getURL('lab-meta.xml'))).text();
    const doc = new DOMParser().parseFromString(text, 'application/xml');
    if (doc.querySelector('parsererror')) return;
    const fromXml = doc
      .getElementsByTagNameNS('urn:intercom:lab', 'kicker')[0]
      ?.textContent?.trim();
    if (fromXml) kicker.textContent = fromXml;
  } catch (e) {
    // Keep default copy.
  }
}

async function sendToContentTab(tabId, message) {
  try {
    return await chrome.tabs.sendMessage(tabId, message);
  } catch (err) {
    try {
      await chrome.scripting.executeScript({
        target: { tabId },
        files: ['content.js'],
      });
    } catch (injectErr) {
      const msg = injectErr?.message || String(injectErr);
      throw new Error(
        'Cannot run on this page. Open a normal http(s) page and refresh it, then try again. ' +
          msg
      );
    }
    return await chrome.tabs.sendMessage(tabId, message);
  }
}

function finishIntro() {
  if (!introSplash || introSplash.classList.contains('intro-splash--exit')) return;
  if (introContinue) introContinue.disabled = true;
  if (typeof introParticleTeardown === 'function') {
    introParticleTeardown();
    introParticleTeardown = null;
  }
  let completed = false;
  const complete = () => {
    if (completed) return;
    completed = true;
    if (introSplash && introSplash.parentNode) {
      introSplash.remove();
    }
    if (appEl) {
      appEl.removeAttribute('aria-hidden');
    }
    startBoot();
  };
  introSplash.classList.add('intro-splash--exit');
  introSplash.addEventListener(
    'transitionend',
    (e) => {
      if (e.target === introSplash && e.propertyName === 'opacity') complete();
    },
    { once: true }
  );
  setTimeout(complete, 700);
}

if (introContinue) {
  introContinue.addEventListener('click', finishIntro);
  introContinue.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' || e.key === ' ') {
      e.preventDefault();
      finishIntro();
    }
  });
}

document.addEventListener('DOMContentLoaded', () => {
  applyLabMetaXml();
  introParticleTeardown = initIntroParticles();
  if (introContinue) {
    introContinue.focus();
  } else {
    if (appEl) appEl.removeAttribute('aria-hidden');
    startBoot();
  }
});

activateBtn.addEventListener('click', () => startSiteCrawl(false));
liveCrawlBtn.addEventListener('click', () => {
  if (browserCrawlRunning) {
    browserCrawlStopRequested = true;
    setStatus('loading', 'Stopping live crawl...');
    return;
  }
  startBrowserSessionCrawl();
});
liveAgentBtn.addEventListener('click', toggleLiveAgentMode);
if (liveAgentStopBtn) {
  liveAgentStopBtn.addEventListener('click', () => {
    liveAgentStopRequested = true;
    setStatus('idle', 'Stopping live agent...');
    addLiveAgentTrace('stop', 'Stop requested', '');
  });
}
sendBtn.addEventListener('click', () => sendMessage());
messageInput.addEventListener('keydown', (e) => {
  if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault();
    sendMessage();
  }
});
messageInput.addEventListener('input', () => {
  messageInput.style.height = 'auto';
  messageInput.style.height = Math.min(messageInput.scrollHeight, 120) + 'px';
});
voiceBtn.addEventListener('click', toggleVoiceRecording);

async function startBoot() {
  try {
    const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
    if (!tab || !tab.url || !tab.url.startsWith('http')) {
      setStatus('error', 'Open a normal website first.');
      return;
    }

    currentUrl = tab.url;
    pageTitleEl.textContent = `📄 ${tab.title || tab.url}`;
    pageInfoEl.classList.remove('hidden');

    setStatus('loading', 'Checking site memory...');
    const check = await fetchCheck(tab.url);
    handleCheckResponse(check, tab.title || tab.url);
  } catch (e) {
    setStatus('error', `Initialization error: ${e.message}`);
  }
}

async function fetchCheck(url) {
  const res = await fetch(`${BACKEND_URL}/check?url=${encodeURIComponent(url)}`);
  if (!res.ok) {
    throw new Error(`Check failed (${res.status})`);
  }
  return res.json();
}

async function getCookiesForActiveUrl(url) {
  if (!url || !url.startsWith('http')) return [];
  try {
    return await chrome.cookies.getAll({ url });
  } catch (cookieError) {
    console.warn('Cookie collection failed:', cookieError);
    return [];
  }
}

function handleCheckResponse(data, fallbackTitle) {
  currentSiteKey = data.site_key || '';

  if (data.exists) {
    isActivated = true;
    conversationHistory = [];
    setChatEnabled(true);
    activateBtn.style.display = 'none';
    messagesEl.innerHTML = '';
    setStatus(
      'ready',
      `Ready · ${data.chunk_count || 0} chunks · ${data.html_pages || 0} pages · ${data.pdf_count || 0} PDFs`
    );
    addMessage(
      'system',
      `Loaded site memory for "${fallbackTitle}". Ask anything about this site or its PDFs.`
    );
    return;
  }

  if (data.status === 'queued' || data.status === 'running') {
    currentJobId = data.job_id || '';
    conversationHistory = [];
    activateBtn.style.display = 'none';
    setChatEnabled(false);
    messagesEl.innerHTML = '';
    addMessage('system', 'Site crawl is already in progress. Chat will unlock when indexing finishes.');
    updateCrawlStatusMessage(data);
    if (currentJobId) startPolling(currentJobId);
    return;
  }

  activateBtn.disabled = false;
  activateBtn.style.display = '';
  setChatEnabled(true);
  isActivated = false;
  setStatus('ready', 'Current page ready');
  addMessage(
    'system',
    'This site is not indexed yet. You can still ask about the currently open page or PDF; Activate builds full-site memory.'
  );
}

function toggleLiveAgentMode() {
  setLiveAgentMode(!isLiveAgentMode);
}

function setLiveAgentMode(enabled) {
  isLiveAgentMode = enabled;
  liveAgentBtn.classList.toggle('btn-live-agent--active', enabled);
  liveAgentBtn.textContent = enabled ? 'Live On' : 'Live Agent';
  messageInput.placeholder = enabled
    ? 'Tell the live agent what to do on this page...'
    : 'Ask about this site...';

  if (enabled) {
    setChatEnabled(true);
    setStatus('ready', 'Live agent mode');
    if (!liveAgentHistory.length) {
      addMessage(
        'system',
        'Live Agent is on. Give it a page task, and it will observe the current page, fill fields, and take safe actions step by step.'
      );
    }
    return;
  }

  setChatEnabled(isActivated);
  setStatus(isActivated ? 'ready' : 'idle', isActivated ? 'Ready' : 'Site not indexed');
}

async function startBrowserSessionCrawl() {
  browserCrawlRunning = true;
  browserCrawlStopRequested = false;
  liveCrawlBtn.classList.add('btn-live-crawl--active');
  liveCrawlBtn.textContent = 'Stop Crawl';
  activateBtn.disabled = true;
  liveAgentBtn.disabled = true;
  setChatEnabled(false);
  messagesEl.innerHTML = '';
  addMessage(
    'system',
    'Starting Live Crawl. Intercom will use the current browser session, follow same-site links from rendered pages, and pause only if the page needs a human.'
  );

  let jobId = '';
  let siteKey = '';
  let crawled = 0;
  let failedPages = 0;
  const visited = new Set();
  const queued = new Set();
  const queue = [];

  try {
    const [startTab] = await chrome.tabs.query({ active: true, currentWindow: true });
    if (!startTab || !startTab.url || !startTab.url.startsWith('http')) {
      throw new Error('Open a normal website before starting Live Crawl.');
    }

    const startUrl = normalizeBrowserCrawlUrl(startTab.url);
    const startHost = getBrowserCrawlSiteHost(startUrl);
    if (!startHost) throw new Error('Could not determine site host.');

    currentUrl = startUrl;
    pageTitleEl.textContent = `📄 ${startTab.title || startUrl}`;
    pageInfoEl.classList.remove('hidden');
    setStatus('loading', 'Creating live crawl job...');

    const startResponse = await fetch(`${BACKEND_URL}/ingest/browser-crawl/start`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        start_url: startUrl,
        title: startTab.title || null,
        force_refresh: true,
      }),
    });
    if (!startResponse.ok) {
      const error = await startResponse.json().catch(() => ({}));
      throw new Error(error.detail || `Live crawl start failed (${startResponse.status})`);
    }
    const startResult = await startResponse.json();
    jobId = startResult.job_id || '';
    siteKey = startResult.site_key || '';
    currentJobId = jobId;
    currentSiteKey = siteKey;

    enqueueBrowserCrawlUrl(queue, queued, startUrl, 0, null);

    while (queue.length > 0 && crawled < MAX_BROWSER_CRAWL_PAGES && !browserCrawlStopRequested) {
      const target = queue.shift();
      if (!target || visited.has(target.url)) continue;

      setStatus(
        'loading',
        `Live crawling ${crawled + 1}/${Math.min(queued.size, MAX_BROWSER_CRAWL_PAGES)}`
      );

      const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
      if (!tab || !tab.id) throw new Error('Active tab is no longer available.');

      const currentTabUrl = normalizeBrowserCrawlUrl(tab.url || '');
      if (currentTabUrl !== target.url) {
        await navigateTabAndWait(tab.id, target.url);
      }

      await new Promise((resolve) => setTimeout(resolve, BROWSER_CRAWL_STEP_DELAY_MS));
      const pageData = await sendToContentTab(tab.id, { type: 'scrape' });
      const pageUrl = normalizeBrowserCrawlUrl(pageData?.url || target.url);

      if (!isSameBrowserCrawlSite(pageUrl, startHost)) {
        visited.add(target.url);
        continue;
      }

      visited.add(target.url);
      visited.add(pageUrl);

      const ingestResult = await ingestRenderedBrowserPage({
        jobId,
        url: pageUrl,
        title: pageData?.title || tab.title || pageUrl,
        pageText: pageData?.text || '',
        discoveredFrom: target.discoveredFrom,
        depth: target.depth,
        discoveredCount: queued.size,
      });
      if (ingestResult?.indexed === false || ingestResult?.status === 'page_failed') {
        failedPages += 1;
        addMessage(
          'system',
          `Skipped one page after ingest failed${ingestResult?.error ? `: ${ingestResult.error}` : '.'}`
        );
      } else {
        crawled += 1;
      }

      const discoveredLinks = Array.isArray(pageData?.links) ? pageData.links : [];
      for (const link of discoveredLinks) {
        const nextUrl = normalizeBrowserCrawlUrl(link.href || '', pageUrl);
        if (!nextUrl || visited.has(nextUrl) || queued.has(nextUrl)) continue;
        if (!isSameBrowserCrawlSite(nextUrl, startHost)) continue;
        if (shouldSkipBrowserCrawlUrl(nextUrl)) continue;
        enqueueBrowserCrawlUrl(queue, queued, nextUrl, target.depth + 1, pageUrl);
      }

      addMessage(
        'system',
        `Indexed ${crawled} page${crawled === 1 ? '' : 's'} · failed ${failedPages} · discovered ${queued.size} same-site URL${queued.size === 1 ? '' : 's'}`
      );
    }

    const stopped = browserCrawlStopRequested;
    await finishBrowserCrawlJob(
      jobId,
      stopped || failedPages ? 'completed_with_issues' : 'completed',
      queued.size
    );

    isActivated = crawled > 0;
    setChatEnabled(isActivated);
    setStatus(
      isActivated ? 'ready' : 'error',
      isActivated
        ? `Ready · Live Crawl indexed ${crawled} pages`
        : 'Live Crawl finished without indexed pages'
    );
    addMessage(
      'system',
      stopped
        ? `Live Crawl stopped after indexing ${crawled} page${crawled === 1 ? '' : 's'}.`
        : `Live Crawl finished after indexing ${crawled} page${crawled === 1 ? '' : 's'}${failedPages ? ` with ${failedPages} failed page${failedPages === 1 ? '' : 's'}` : ''}.`
    );
  } catch (error) {
    if (jobId) {
      try {
        await finishBrowserCrawlJob(jobId, 'failed', queued.size, error.message);
      } catch (finishError) {
        console.warn('Live crawl finish failed:', finishError);
      }
    }
    setStatus('error', error.message);
    addMessage('system', `Live Crawl error: ${error.message}`);
  } finally {
    browserCrawlRunning = false;
    browserCrawlStopRequested = false;
    liveCrawlBtn.classList.remove('btn-live-crawl--active');
    liveCrawlBtn.textContent = 'Live Crawl';
    activateBtn.disabled = false;
    liveAgentBtn.disabled = false;
    setChatEnabled(isActivated || isLiveAgentMode);
  }
}

function enqueueBrowserCrawlUrl(queue, queued, url, depth, discoveredFrom) {
  if (!url || queued.has(url) || queued.size >= MAX_BROWSER_CRAWL_PAGES * 3) return;
  queued.add(url);
  queue.push({ url, depth, discoveredFrom });
}

async function ingestRenderedBrowserPage({
  jobId,
  url,
  title,
  pageText,
  discoveredFrom,
  depth,
  discoveredCount,
}) {
  const response = await fetch(`${BACKEND_URL}/ingest/browser-crawl/page`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      job_id: jobId,
      url,
      title,
      page_text: pageText,
      discovered_from: discoveredFrom,
      crawl_depth: depth,
      discovered_count: discoveredCount,
    }),
  });
  if (!response.ok) {
    const error = await response.json().catch(() => ({}));
    throw new Error(error.detail || `Live crawl page ingest failed (${response.status})`);
  }
  return response.json();
}

async function finishBrowserCrawlJob(jobId, status, discoveredCount, errorSummary) {
  const response = await fetch(`${BACKEND_URL}/ingest/browser-crawl/finish`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      job_id: jobId,
      status,
      discovered_count: discoveredCount,
      error_summary: errorSummary || null,
    }),
  });
  if (!response.ok) {
    const error = await response.json().catch(() => ({}));
    throw new Error(error.detail || `Live crawl finish failed (${response.status})`);
  }
  return response.json();
}

async function navigateTabAndWait(tabId, url) {
  await chrome.tabs.update(tabId, { url });
  await new Promise((resolve) => {
    let settled = false;
    const cleanup = () => {
      if (settled) return;
      settled = true;
      chrome.tabs.onUpdated.removeListener(listener);
      clearTimeout(timer);
      resolve();
    };
    const listener = (updatedTabId, changeInfo) => {
      if (updatedTabId === tabId && changeInfo.status === 'complete') {
        cleanup();
      }
    };
    const timer = setTimeout(cleanup, BROWSER_CRAWL_NAV_TIMEOUT_MS);
    chrome.tabs.onUpdated.addListener(listener);
  });
}

function normalizeBrowserCrawlUrl(rawUrl, baseUrl) {
  try {
    const parsed = new URL(rawUrl, baseUrl || undefined);
    if (!['http:', 'https:'].includes(parsed.protocol)) return '';
    parsed.hash = '';
    for (const key of Array.from(parsed.searchParams.keys())) {
      const lowered = key.toLowerCase();
      if (
        lowered.startsWith('utm_') ||
        ['gclid', 'fbclid', 'ref', 'source', 'mc_cid', 'mc_eid'].includes(lowered)
      ) {
        parsed.searchParams.delete(key);
      }
    }
    parsed.searchParams.sort();
    if (parsed.pathname !== '/' && parsed.pathname.endsWith('/')) {
      parsed.pathname = parsed.pathname.slice(0, -1);
    }
    return parsed.toString();
  } catch (e) {
    return '';
  }
}

function getBrowserCrawlSiteHost(url) {
  try {
    const host = new URL(url).hostname.toLowerCase();
    const parts = host.split('.').filter(Boolean);
    const twoPartSuffixes = new Set(['co.uk', 'org.uk', 'gov.uk', 'ac.uk', 'co.in', 'org.in', 'ac.in']);
    const lastTwo = parts.slice(-2).join('.');
    if (twoPartSuffixes.has(lastTwo) && parts.length >= 3) {
      return parts.slice(-3).join('.');
    }
    return parts.length >= 2 ? parts.slice(-2).join('.') : host;
  } catch (e) {
    return '';
  }
}

function isSameBrowserCrawlSite(url, rootHost) {
  try {
    const host = new URL(url).hostname.toLowerCase();
    return host === rootHost || host.endsWith(`.${rootHost}`);
  } catch (e) {
    return false;
  }
}

function shouldSkipBrowserCrawlUrl(url) {
  try {
    const parsed = new URL(url);
    const path = parsed.pathname.toLowerCase();
    return (
      path.match(/\.(zip|rar|7z|tar|gz|mp4|mov|avi|webm|mp3|wav|jpg|jpeg|png|gif|svg|webp)$/) ||
      parsed.searchParams.has('logout') ||
      path.includes('/logout') ||
      path.includes('/signout')
    );
  } catch (e) {
    return true;
  }
}

async function startSiteCrawl(forceRefresh) {
  try {
    activateBtn.disabled = true;
    setChatEnabled(false);
    setStatus('loading', 'Preparing full-site crawl...');
    conversationHistory = [];
    messagesEl.innerHTML = '';
    addMessage('system', 'Starting full-site crawl. Intercom will unlock after pages and PDFs finish indexing.');

    const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
    if (!tab || !tab.url || !tab.url.startsWith('http')) {
      throw new Error('Open a normal website first.');
    }

    currentUrl = tab.url;
    pageTitleEl.textContent = `📄 ${tab.title || tab.url}`;
    pageInfoEl.classList.remove('hidden');

    let pageText = null;
    try {
      const pageData = await sendToContentTab(tab.id, { type: 'scrape' });
      pageText = pageData?.text || null;
    } catch (scrapeError) {
      console.warn('Initial page scrape failed:', scrapeError);
    }

    let cookies = [];
    try {
      cookies = await chrome.cookies.getAll({ url: tab.url });
    } catch (cookieError) {
      console.warn('Cookie collection failed:', cookieError);
    }

    const response = await fetch(`${BACKEND_URL}/ingest/site`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        start_url: tab.url,
        title: tab.title || null,
        page_text: pageText,
        user_agent: navigator.userAgent,
        cookies,
        force_refresh: !!forceRefresh,
      }),
    });

    if (!response.ok) {
      const error = await response.json().catch(() => ({}));
      throw new Error(error.detail || `Site crawl failed (${response.status})`);
    }

    const result = await response.json();
    currentJobId = result.job_id || '';
    currentSiteKey = result.site_key || '';
    activateBtn.style.display = 'none';

    if (result.status === 'completed' || result.status === 'completed_with_issues') {
      const check = await fetchCheck(tab.url);
      handleCheckResponse(check, tab.title || tab.url);
      return;
    }

    updateCrawlStatusMessage({
      status: result.status || 'queued',
      pages_discovered: 0,
      pages_crawled: 0,
      pdfs_discovered: 0,
      pdfs_processed: 0,
      chunks_written: 0,
      failures: 0,
    });
    startPolling(currentJobId);
  } catch (error) {
    activateBtn.disabled = false;
    activateBtn.style.display = '';
    setStatus('error', error.message);
  }
}

function startPolling(jobId) {
  stopPolling();
  if (!jobId) return;
  refreshJobStatus(jobId);
  pollTimer = setInterval(() => refreshJobStatus(jobId), POLL_INTERVAL_MS);
}

function stopPolling() {
  if (pollTimer) {
    clearInterval(pollTimer);
    pollTimer = null;
  }
}

async function refreshJobStatus(jobId) {
  try {
    const response = await fetch(`${BACKEND_URL}/ingest/site/${encodeURIComponent(jobId)}`);
    if (!response.ok) {
      const error = await response.json().catch(() => ({}));
      throw new Error(error.detail || `Status check failed (${response.status})`);
    }
    const status = await response.json();
    updateCrawlStatusMessage(status);

    if (status.status === 'completed' || status.status === 'completed_with_issues') {
      stopPolling();
      const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
      const check = await fetchCheck((tab && tab.url) || currentUrl);
      messagesEl.innerHTML = '';
      if (check.exists && (check.chunk_count || 0) > 0) {
        isActivated = true;
        setChatEnabled(true);
        setStatus(
          'ready',
          `Ready · ${check.chunk_count || status.chunks_written || 0} chunks · ${status.pages_crawled || 0} pages · ${status.pdfs_processed || 0} PDFs`
        );
        addMessage(
          'system',
          status.failures
            ? `Site crawl finished with ${status.failures} issue(s), but indexed memory is ready to use.`
            : 'Site crawl finished. The indexed site memory is ready.'
        );
      } else {
        isActivated = false;
        setChatEnabled(false);
        activateBtn.disabled = false;
        activateBtn.style.display = '';
        setStatus('error', 'Crawl finished without indexed content');
        addMessage(
          'system',
          'This crawl finished, but no content was indexed. The site likely blocked backend crawling. Try re-running from an accessible page.'
        );
      }
    } else if (status.status === 'failed') {
      stopPolling();
      activateBtn.disabled = false;
      activateBtn.style.display = '';
      setStatus('error', 'Site crawl failed');
      addMessage('system', 'The crawl failed. Try activating again or check the backend logs.');
    }
  } catch (error) {
    stopPolling();
    activateBtn.disabled = false;
    activateBtn.style.display = '';
    setStatus('error', error.message);
  }
}

function updateCrawlStatusMessage(status) {
  const label =
    status.status === 'queued'
      ? 'Queued'
      : `Crawling · ${status.pages_crawled || 0}/${status.pages_discovered || 0} pages · ${status.pdfs_processed || 0}/${status.pdfs_discovered || 0} PDFs · ${status.chunks_written || 0} chunks`;
  setStatus('loading', label);
}

function setChatEnabled(enabled) {
  if (!isLiveAgentMode) {
    isActivated = enabled;
  }
  const controlsEnabled = enabled || isLiveAgentMode;
  messageInput.disabled = !controlsEnabled;
  sendBtn.disabled = !controlsEnabled;
  voiceBtn.disabled = !controlsEnabled;
  if (controlsEnabled) {
    messageInput.focus();
  }
}

async function sendMessage(textOverride) {
  const message = (textOverride || messageInput.value || '').trim();
  if (!message) return;

  if (!textOverride) {
    messageInput.value = '';
    messageInput.style.height = 'auto';
  }

  if (isLiveAgentMode) {
    await runLiveAgent(message);
    return;
  }

  addMessage('user', message);
  conversationHistory.push({ role: 'user', content: message });

  const loadingEl = addLoadingIndicator();
  messageInput.disabled = true;
  sendBtn.disabled = true;
  voiceBtn.disabled = true;

  try {
    const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
    currentUrl = (tab && tab.url) || currentUrl;
    let livePageContext = null;
    if (tab && tab.id) {
      try {
        const livePage = await sendToContentTab(tab.id, { type: 'scrape' });
        livePageContext = livePage?.text || null;
      } catch (liveError) {
        console.warn('Live page scrape skipped:', liveError);
      }
    }
    const cookies = await getCookiesForActiveUrl(currentUrl);

    const response = await fetch(`${BACKEND_URL}/chat`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        url: currentUrl,
        message,
        conversation_history: conversationHistory.slice(-10),
        live_page_context: livePageContext,
        user_agent: navigator.userAgent,
        cookies,
      }),
    });

    if (!response.ok) {
      const error = await response.json().catch(() => ({}));
      throw new Error(error.detail || `Chat failed (${response.status})`);
    }

    const result = await response.json();
    loadingEl.remove();
    addMessage('assistant', result.answer, {
      action:
        result.actions && result.actions.length > 0 ? result.actions[0] : null,
      sources: result.sources || [],
      speakableText: result.answer,
    });
    conversationHistory.push({ role: 'assistant', content: result.answer });

    if (result.actions && result.actions.length > 0) {
      for (const action of result.actions) {
        await executeAction(action);
        await new Promise((resolve) => setTimeout(resolve, 600));
      }
    }
  } catch (error) {
    loadingEl.remove();
    addMessage('system', `Error: ${error.message}`);
  } finally {
    messageInput.disabled = false;
    sendBtn.disabled = false;
    voiceBtn.disabled = false;
    messageInput.focus();
  }
}

async function runLiveAgent(goal) {
  if (liveAgentRunning) return;

  liveAgentRunning = true;
  liveAgentStopRequested = false;
  liveAgentBtn.disabled = true;
  if (liveAgentStopBtn) liveAgentStopBtn.classList.remove('hidden');
  addMessage('user', goal);
  liveAgentHistory.push({ role: 'user', content: goal });

  let loadingEl = addLoadingIndicator();
  let actionHistory = [];
  let recentActionKeys = [];
  let recoveryAttempts = 0;
  let progressState = createLiveAgentProgressState();
  messageInput.disabled = true;
  sendBtn.disabled = true;
  voiceBtn.disabled = true;
  resetLiveAgentTrace();
  setStatus('loading', 'Live agent observing page...');

  try {
    for (let step = 1; step <= MAX_LIVE_AGENT_STEPS; step++) {
      if (liveAgentStopRequested) {
        addMessage('system', 'Live Agent stopped.');
        setStatus('idle', 'Live agent stopped');
        break;
      }

      const observeStart = performance.now();
      const observation = await observeLivePage({ includeScreenshotHints: recoveryAttempts > 0 });
      addLiveAgentTrace('observe', observation.title || observation.url, `${Math.round(performance.now() - observeStart)}ms`);

      const pauseReason = getLiveAgentPauseReason({
        observation,
        progressState,
        recentActionKeys,
      });
      if (pauseReason) {
        setStatus('idle', 'Live agent paused');
        addMessage('system', pauseReason);
        break;
      }

      const decisionStart = performance.now();
      const result = await requestLiveAgentDecision({
        goal,
        observation,
        actionHistory,
      });
      addLiveAgentTrace('think', result.status || 'working', `${Math.round(performance.now() - decisionStart)}ms`);

      if (loadingEl) {
        loadingEl.remove();
        loadingEl = null;
      }

      const status = result.status || 'working';
      const actions = Array.isArray(result.actions) ? result.actions : [];
      const answer = result.answer || 'Live agent step ready.';
      addMessage('assistant', answer, { speakableText: answer });
      liveAgentHistory.push({ role: 'assistant', content: answer });

      if (status === 'done') {
        setStatus('ready', 'Live agent done');
        break;
      }

      if (status === 'needs_user' || status === 'blocked') {
        setStatus(status === 'needs_user' ? 'idle' : 'error', answer);
        break;
      }

      const validatedActions = validateLiveAgentActions(actions, observation);
      if (validatedActions.length === 0) {
        setStatus('idle', 'Live agent paused');
        addMessage('system', 'Live Agent paused because there were no safe next actions.');
        break;
      }

      const burstKey = validatedActions.slice(0, 2).map(actionToKey).join(' | ');
      if (recentActionKeys[recentActionKeys.length - 1] === burstKey) {
        progressState.repeatedActionBursts += 1;
      } else {
        progressState.repeatedActionBursts = 0;
      }
      recentActionKeys.push(burstKey);
      recentActionKeys = recentActionKeys.slice(-4);

      setStatus('loading', `Live agent step ${step}/${MAX_LIVE_AGENT_STEPS}`);
      const execution = await executeLiveAgentActions({
        actions: validatedActions,
        step,
        observation,
        actionHistory,
      });

      if (execution.failed) {
        if (recoveryAttempts < LIVE_AGENT_RECOVERY_LIMIT) {
          recoveryAttempts += 1;
          addLiveAgentTrace('recover', execution.failed.error || 'Action failed', `try ${recoveryAttempts}`);
          if (step < MAX_LIVE_AGENT_STEPS && !loadingEl) loadingEl = addLoadingIndicator();
          continue;
        }
        setStatus('idle', 'Live agent needs help');
        addMessage('system', `Live Agent paused after recovery failed: ${execution.failed.error || 'action failed'}`);
        break;
      }
      recoveryAttempts = 0;
      progressState = updateLiveAgentProgressState(progressState, observation, execution);

      if (step < MAX_LIVE_AGENT_STEPS) {
        loadingEl = addLoadingIndicator();
      } else {
        setStatus('idle', 'Live agent reached step limit');
        addMessage(
          'system',
          'Live Agent reached the basic step limit. Send another instruction to continue from the current page.'
        );
      }
    }
  } catch (error) {
    if (loadingEl) loadingEl.remove();
    setStatus('error', error.message);
    addMessage('system', `Live Agent error: ${error.message}`);
  } finally {
    liveAgentRunning = false;
    liveAgentStopRequested = false;
    liveAgentBtn.disabled = false;
    if (liveAgentStopBtn) liveAgentStopBtn.classList.add('hidden');
    messageInput.disabled = false;
    sendBtn.disabled = false;
    voiceBtn.disabled = false;
    setChatEnabled(true);
  }
}

function actionToKey(action) {
  if (!action || typeof action !== 'object') return 'unknown';
  return [
    action.type || '',
    action.ref || '',
    action.selector || '',
    action.url || '',
    action.text || '',
    action.value || '',
    action.sourceRef || '',
    String(action.checked),
  ].join('::');
}

function createLiveAgentProgressState() {
  return {
    lastFingerprint: '',
    stagnantPageCount: 0,
    repeatedActionBursts: 0,
    lastUrl: '',
    lastChangedRefs: [],
  };
}

async function observeLivePage(options = {}) {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  if (!tab || !tab.id || !tab.url || !tab.url.startsWith('http')) {
    throw new Error('Open a normal website before using Live Agent.');
  }

  currentUrl = tab.url;
  pageTitleEl.textContent = `📄 ${tab.title || tab.url}`;
  pageInfoEl.classList.remove('hidden');

  const livePage = await sendToContentTab(tab.id, {
    type: 'observe_live_agent',
    previousSnapshotId: liveAgentLastSnapshotId,
    includeScreenshotHints: !!options.includeScreenshotHints,
  });
  if (!livePage?.text || !livePage?.snapshot) {
    throw new Error('No live page context available.');
  }
  liveAgentLastSnapshotId = livePage.snapshotId || '';
  liveAgentLastSnapshot = livePage.snapshot || null;
  if (options.includeScreenshotHints) {
    livePage.screenshot = await captureLiveAgentScreenshot(tab.id, livePage.snapshot?.screenshotHints || []);
  }
  return livePage;
}

async function captureLiveAgentScreenshot(tabId, hints = []) {
  try {
    const refs = hints.map((hint) => hint.ref).filter(Boolean).slice(0, 24);
    await sendToContentTab(tabId, { type: 'show_live_overlay', refs });
    const result = await chrome.runtime.sendMessage({
      type: 'capture_visible_tab',
      quality: 58,
    });
    await sendToContentTab(tabId, { type: 'clear_live_overlay' });
    if (!result?.success) {
      addLiveAgentTrace('vision', result?.error || 'screenshot unavailable', '');
      return null;
    }
    addLiveAgentTrace('vision', `captured ${refs.length || 'visible'} refs`, '');
    return {
      dataUrl: result.dataUrl,
      refs,
      capturedAt: Date.now(),
    };
  } catch (error) {
    try {
      await sendToContentTab(tabId, { type: 'clear_live_overlay' });
    } catch (clearError) {
      console.warn('Live overlay cleanup failed:', clearError);
    }
    addLiveAgentTrace('vision', `screenshot skipped: ${error.message}`, '');
    return null;
  }
}

async function requestLiveAgentDecision({ goal, observation, actionHistory }) {
  const response = await fetch(`${BACKEND_URL}/live-agent`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      url: observation.url || currentUrl,
      goal,
      live_page_context: observation.text || '',
      live_page_snapshot: observation.snapshot || null,
      live_page_screenshot: observation.screenshot
        ? {
            available: true,
            refs: observation.screenshot.refs,
            captured_at: observation.screenshot.capturedAt,
          }
        : null,
      action_history: actionHistory.slice(-8),
      conversation_history: liveAgentHistory.slice(-6),
    }),
  });

  if (!response.ok) {
    const error = await response.json().catch(() => ({}));
    throw new Error(error.detail || `Live agent failed (${response.status})`);
  }
  return response.json();
}

function validateLiveAgentActions(actions, observation) {
  const validRefs = new Set((observation.snapshot?.elements || []).map((item) => item.ref));
  const validFormRefs = new Set((observation.snapshot?.forms || []).map((item) => item.ref));
  const pageUrl = observation.url || currentUrl;

  return actions
    .slice(0, MAX_LIVE_AGENT_ACTIONS_PER_STEP)
    .map((action) => normalizeLiveAgentAction(action, observation))
    .filter((action) => {
      if (!action.type) return false;
      if (action.type === 'navigate') return isSafeNavigationUrl(action.url, pageUrl);
      if (action.type === 'highlight') return !!action.text;
      if (action.ref) return validRefs.has(action.ref) || validFormRefs.has(action.ref);
      return !!action.selector;
    });
}

function normalizeLiveAgentAction(action, observation) {
  if (!action || typeof action !== 'object') return {};
  const normalized = {
    type: String(action.type || '').trim(),
    ref: action.ref ? String(action.ref).trim() : '',
    selector: action.selector ? String(action.selector).trim() : '',
    url: action.url ? String(action.url).trim() : '',
    text: typeof action.text === 'string' ? action.text : '',
    value: typeof action.value === 'string' ? action.value : '',
    checked: action.checked !== false,
    expectsNavigation: !!action.expectsNavigation,
  };
  const target = findSnapshotTarget(observation, normalized.ref);
  if (normalized.type === 'click' && target?.href && isSafeNavigationUrl(target.href, observation.url || currentUrl)) {
    return {
      ...normalized,
      type: 'navigate',
      url: target.href,
      sourceRef: normalized.ref,
    };
  }
  return normalized;
}

function findSnapshotTarget(observation, ref) {
  if (!ref) return null;
  return (
    (observation.snapshot?.elements || []).find((item) => item.ref === ref) ||
    (observation.snapshot?.forms || []).find((item) => item.ref === ref) ||
    null
  );
}

function isSafeNavigationUrl(rawUrl, baseUrl) {
  try {
    const parsed = new URL(rawUrl, baseUrl || undefined);
    return ['http:', 'https:'].includes(parsed.protocol);
  } catch (e) {
    return false;
  }
}

function getLiveAgentPauseReason({ observation, progressState }) {
  const fingerprint = observation.fingerprint || observation.snapshot?.summary?.fingerprint || '';
  const url = observation.url || '';
  if (progressState.lastFingerprint && fingerprint === progressState.lastFingerprint && url === progressState.lastUrl) {
    progressState.stagnantPageCount += 1;
  } else {
    progressState.stagnantPageCount = 0;
  }
  progressState.lastFingerprint = fingerprint;
  progressState.lastUrl = url;

  if (progressState.repeatedActionBursts >= 1) {
    return 'Live Agent paused because it kept proposing the same action without progress. Try a more specific instruction or continue from the current page state.';
  }
  if (progressState.stagnantPageCount >= 3) {
    return 'Live Agent paused because the page state did not change across multiple observations. Try a more specific instruction or continue manually from the current page.';
  }
  return '';
}

function updateLiveAgentProgressState(progressState, observation, execution) {
  const next = { ...progressState };
  next.lastFingerprint = observation.fingerprint || observation.snapshot?.summary?.fingerprint || '';
  next.lastUrl = observation.url || currentUrl;
  next.lastChangedRefs = execution.results
    .filter((item) => item.result?.changed || item.result?.after?.url !== item.result?.before?.url)
    .map((item) => item.action.ref || item.result?.ref || '')
    .filter(Boolean);
  if (execution.results.some((item) => item.result?.changed || item.action.type === 'navigate')) {
    next.stagnantPageCount = 0;
  }
  return next;
}

async function executeLiveAgentActions({ actions, step, observation, actionHistory }) {
  const results = [];
  for (const action of actions) {
    if (liveAgentStopRequested) break;
    const label = describeActionForTrace(action, observation);
    const startedAt = performance.now();
    addLiveAgentTrace('act', label, action.ref || action.selector || action.url || '');
    const result = await executeAction(action);
    const elapsed = `${Math.round(performance.now() - startedAt)}ms`;
    addLiveAgentTrace(result?.success ? 'verify' : 'failed', result?.label || result?.error || label, elapsed);
    actionHistory.push({
      step,
      action,
      target: label,
      result,
      url_before: observation.url || currentUrl,
      snapshot_before: observation.snapshotId || '',
    });
    results.push({ action, result });
    if (!result?.success) {
      return { failed: result, results };
    }
    await waitForLiveAgentSettle(action);
    if (isProgresslessAction(action, result)) {
      const noProgress = {
        ...result,
        success: false,
        error: `${action.type} on ${result.label || action.ref || action.selector || 'target'} did not change the page. Replan with a different link, hover, or direct navigation.`,
        noProgress: true,
      };
      actionHistory[actionHistory.length - 1].result = noProgress;
      results[results.length - 1].result = noProgress;
      return { failed: noProgress, results };
    }
  }
  return { failed: null, results };
}

function isProgresslessAction(action, result) {
  if (!['click', 'hover', 'submit'].includes(action.type)) return false;
  if (action.expectsNavigation) return !result.changed;
  return !result.changed && !result.href;
}

function describeActionForTrace(action, observation) {
  if (action.type === 'navigate') return `navigate ${action.url}`;
  if (action.type === 'highlight') return `highlight "${action.text}"`;
  const target =
    (observation.snapshot?.elements || []).find((item) => item.ref === action.ref) ||
    (observation.snapshot?.forms || []).find((item) => item.ref === action.ref);
  const label = target?.label || target?.text || target?.href || action.selector || action.ref || 'target';
  return `${action.type} ${label}`.slice(0, 120);
}

async function waitForLiveAgentSettle(action) {
  const startedAt = performance.now();
  if (action.type === 'navigate') {
    addLiveAgentTrace('wait', 'navigation load', '');
    return;
  }
  try {
    const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
    if (!tab?.id) return;
    const result = await sendToContentTab(tab.id, {
      type: 'wait_for_settle',
      timeoutMs: action.expectsNavigation ? LIVE_AGENT_NAV_TIMEOUT_MS : LIVE_AGENT_SETTLE_TIMEOUT_MS,
    });
    addLiveAgentTrace('wait', 'page settled', `${result?.elapsedMs || Math.round(performance.now() - startedAt)}ms`);
  } catch (error) {
    addLiveAgentTrace('wait', `settle skipped: ${error.message}`, '');
  }
}

async function executeAction(action) {
  try {
    const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
    switch (action.type) {
      case 'navigate': {
        currentUrl = action.url || currentUrl;
        const navResult = await chrome.runtime.sendMessage({
          type: 'navigate',
          url: action.url,
          timeoutMs: LIVE_AGENT_NAV_TIMEOUT_MS,
        });
        if (!navResult?.success) throw new Error(navResult?.error || 'Navigation failed');
        addMessage('system', 'Opening the next page.');
        currentUrl = navResult.url || currentUrl;
        return { ...navResult, effect: 'navigated', label: action.url };
      }
      case 'scroll_to': {
        const res = await sendToContentTab(tab.id, {
          type: 'scroll_to',
          ref: action.ref,
          selector: action.selector,
        });
        if (!res?.success) throw new Error(res?.error || 'Element not found');
        addMessage('system', `Moved to ${res.label || 'the relevant part of the page'}.`);
        return res;
      }
      case 'highlight':
        await sendToContentTab(tab.id, { type: 'highlight', text: action.text });
        addMessage('system', 'Highlighted the relevant text.');
        return { success: true };
      case 'click': {
        const res = await sendToContentTab(tab.id, {
          type: 'click',
          ref: action.ref,
          selector: action.selector,
        });
        if (!res?.success) throw new Error(res?.error || 'Element not found');
        addMessage('system', `Clicked ${res.label || 'the next item on the page'}.`);
        return res;
      }
      case 'hover': {
        const res = await sendToContentTab(tab.id, {
          type: 'hover',
          ref: action.ref,
          selector: action.selector,
        });
        if (!res?.success) throw new Error(res?.error || 'Element not found');
        addMessage('system', `Opened ${res.label || 'the menu'} for more options.`);
        return res;
      }
      case 'type_text': {
        const res = await sendToContentTab(tab.id, {
          type: 'type_text',
          ref: action.ref,
          selector: action.selector,
          text: action.text,
        });
        if (!res?.success) throw new Error(res?.error || 'Element not found');
        addMessage('system', `Filled ${res.label || 'a field on the page'}.`);
        return res;
      }
      case 'select_value': {
        const res = await sendToContentTab(tab.id, {
          type: 'select_value',
          ref: action.ref,
          selector: action.selector,
          value: action.value,
        });
        if (!res?.success) throw new Error(res?.error || 'Option not found');
        addMessage('system', `Selected ${action.value || 'an option'}.`);
        return res;
      }
      case 'check': {
        const res = await sendToContentTab(tab.id, {
          type: 'check',
          ref: action.ref,
          selector: action.selector,
          checked: action.checked !== false,
        });
        if (!res?.success) throw new Error(res?.error || 'Control not found');
        addMessage('system', 'Updated a checkbox or radio option.');
        return res;
      }
      case 'submit': {
        const res = await sendToContentTab(tab.id, {
          type: 'submit',
          ref: action.ref,
          selector: action.selector,
        });
        if (!res?.success) throw new Error(res?.error || 'Form not found');
        addMessage('system', 'Submitted the current form.');
        return res;
      }
      default:
        addMessage('system', `Unknown action: ${action.type}`);
        return { success: false, error: `Unknown action: ${action.type}` };
    }
  } catch (e) {
    addMessage('system', `⚠️ Action failed: ${e.message}`);
    return { success: false, error: e.message };
  }
}

async function toggleVoiceRecording() {
  if (isRecording) {
    stopVoiceRecording();
  } else {
    await startVoiceRecording();
  }
}

async function startVoiceRecording() {
  if (!navigator.mediaDevices || !window.MediaRecorder) {
    addMessage('system', 'Voice mode is not available in this browser context.');
    return;
  }

  try {
    mediaStream = await navigator.mediaDevices.getUserMedia({ audio: true });
    recordingChunks = [];
    mediaRecorder = new MediaRecorder(mediaStream);
    mediaRecorder.ondataavailable = (event) => {
      if (event.data && event.data.size > 0) recordingChunks.push(event.data);
    };
    mediaRecorder.onstop = async () => {
      const mimeType = mediaRecorder.mimeType || 'audio/webm';
      const blob = new Blob(recordingChunks, { type: mimeType });
      cleanupVoiceStream();
      isRecording = false;
      voiceBtn.classList.remove('btn-voice--recording');
      voiceBtn.textContent = '🎙';
      setStatus('loading', 'Transcribing voice note...');
      await submitVoiceBlob(blob);
    };

    mediaRecorder.start();
    isRecording = true;
    voiceBtn.classList.add('btn-voice--recording');
    voiceBtn.textContent = '■';
    setStatus('loading', 'Recording... tap again to stop');
  } catch (error) {
    cleanupVoiceStream();
    addMessage('system', `Voice error: ${error.message}`);
  }
}

function stopVoiceRecording() {
  if (mediaRecorder && isRecording) {
    mediaRecorder.stop();
  }
}

function cleanupVoiceStream() {
  if (mediaStream) {
    mediaStream.getTracks().forEach((track) => track.stop());
  }
  mediaRecorder = null;
  mediaStream = null;
}

async function submitVoiceBlob(blob) {
  try {
    const response = await fetch(
      `${BACKEND_URL}/voice/transcribe?filename=${encodeURIComponent('voice.webm')}`,
      {
        method: 'POST',
        headers: { 'Content-Type': blob.type || 'audio/webm' },
        body: blob,
      }
    );
    if (!response.ok) {
      const error = await response.json().catch(() => ({}));
      throw new Error(error.detail || `Transcription failed (${response.status})`);
    }
    const result = await response.json();
    if (!result.transcript || !result.transcript.trim()) {
      throw new Error('No speech detected.');
    }
    setStatus('ready', 'Voice note transcribed');
    await sendMessage(result.transcript.trim());
  } catch (error) {
    setStatus('error', error.message);
    addMessage('system', `Voice error: ${error.message}`);
  }
}

function setStatus(type, text) {
  statusEl.textContent = text;
  statusEl.className = `status status-${type}`;
}

function resetLiveAgentTrace() {
  if (!liveAgentTraceEl) return;
  liveAgentTraceEl.innerHTML = '';
  liveAgentTraceEl.classList.remove('hidden');
}

function addLiveAgentTrace(phase, detail, meta) {
  if (!liveAgentTraceEl) return;
  liveAgentTraceEl.classList.remove('hidden');
  const item = document.createElement('div');
  item.className = 'live-agent-trace__item';

  const phaseEl = document.createElement('span');
  phaseEl.className = 'live-agent-trace__phase';
  phaseEl.textContent = phase;

  const detailEl = document.createElement('span');
  detailEl.className = 'live-agent-trace__detail';
  detailEl.textContent = detail || '';

  const metaEl = document.createElement('span');
  metaEl.className = 'live-agent-trace__meta';
  metaEl.textContent = meta || '';

  item.appendChild(phaseEl);
  item.appendChild(detailEl);
  item.appendChild(metaEl);
  liveAgentTraceEl.appendChild(item);
  while (liveAgentTraceEl.children.length > 18) {
    liveAgentTraceEl.removeChild(liveAgentTraceEl.firstChild);
  }
  liveAgentTraceEl.scrollTop = liveAgentTraceEl.scrollHeight;
}

function addMessage(type, text, options = {}) {
  const msgEl = document.createElement('div');
  msgEl.className = `message message-${type}`;
  msgEl.textContent = text;

  if (type === 'assistant' && options.speakableText) {
    const speakBtn = document.createElement('button');
    speakBtn.type = 'button';
    speakBtn.className = 'message-voice-btn';
    speakBtn.textContent = 'Play reply';
    speakBtn.addEventListener('click', () =>
      speakAssistantReply(options.speakableText, speakBtn)
    );
    msgEl.appendChild(speakBtn);
  }

  if (options.sources && options.sources.length > 0) {
    msgEl.appendChild(createSourcesDisclosure(options.sources));
  }

  messagesEl.appendChild(msgEl);
  messagesEl.scrollTop = messagesEl.scrollHeight;
  return msgEl;
}

function createSourceCard(source) {
  const card = document.createElement('div');
  card.className = `source-card source-card--${source.type || 'html'}`;

  const title = document.createElement('div');
  title.className = 'source-card__title';
  title.textContent = source.title || 'Source';

  const meta = document.createElement('div');
  meta.className = 'source-card__meta';
  meta.textContent =
    source.type === 'pdf' && source.page_number
      ? `PDF · page ${source.page_number}`
      : (source.type || 'html').toUpperCase();

  const snippet = document.createElement('div');
  snippet.className = 'source-card__snippet';
  snippet.textContent = source.snippet || source.url || '';

  const openBtn = document.createElement('button');
  openBtn.type = 'button';
  openBtn.className = 'source-card__open';
  openBtn.textContent = source.type === 'pdf' ? 'Open PDF' : 'Open Source';
  openBtn.addEventListener('click', () => {
    if (source.url) window.open(source.url, '_blank');
  });

  card.appendChild(title);
  card.appendChild(meta);
  card.appendChild(snippet);
  card.appendChild(openBtn);

  if (source.section_selector || source.section_id) {
    const highlightBtn = document.createElement('button');
    highlightBtn.type = 'button';
    highlightBtn.className = 'source-card__open';
    highlightBtn.textContent = 'Highlight Section';
    highlightBtn.addEventListener('click', async () => {
      try {
        const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
        if (!tab?.id) return;
        const selector = source.section_selector || `#${source.section_id}`;
        const res = await sendToContentTab(tab.id, { type: 'scroll_to', selector });
        if (!res?.success && source.url) {
          window.open(source.url, '_blank');
        }
      } catch (error) {
        addMessage('system', `Could not highlight source section: ${error.message}`);
      }
    });
    card.appendChild(highlightBtn);
  }
  return card;
}

function createSourcesDisclosure(sources) {
  const wrap = document.createElement('details');
  wrap.className = 'message-sources';

  const summary = document.createElement('summary');
  summary.className = 'message-sources__summary';
  summary.textContent = `Sources (${sources.length})`;
  wrap.appendChild(summary);

  const list = document.createElement('div');
  list.className = 'message-sources__list';
  sources.forEach((source) => {
    list.appendChild(createSourceCard(source));
  });
  wrap.appendChild(list);
  return wrap;
}

async function speakAssistantReply(text, triggerBtn) {
  try {
    triggerBtn.disabled = true;
    triggerBtn.textContent = 'Loading audio...';
    const response = await fetch(`${BACKEND_URL}/voice/speak`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ text }),
    });
    if (!response.ok) {
      const error = await response.json().catch(() => ({}));
      throw new Error(error.detail || `Speech failed (${response.status})`);
    }
    const blob = await response.blob();
    const audioUrl = URL.createObjectURL(blob);
    const audio = new Audio(audioUrl);
    audio.addEventListener('ended', () => URL.revokeObjectURL(audioUrl), { once: true });
    await audio.play();
  } catch (error) {
    addMessage('system', `Voice playback error: ${error.message}`);
  } finally {
    triggerBtn.disabled = false;
    triggerBtn.textContent = 'Play reply';
  }
}

function addLoadingIndicator() {
  const loadingEl = document.createElement('div');
  loadingEl.className = 'message message-assistant';
  loadingEl.innerHTML =
    '<div class="loading-dots"><span></span><span></span><span></span></div>';
  messagesEl.appendChild(loadingEl);
  messagesEl.scrollTop = messagesEl.scrollHeight;
  return loadingEl;
}
