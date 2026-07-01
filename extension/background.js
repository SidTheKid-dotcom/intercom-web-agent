// Background service worker — handles side panel + tab-level operations

// Open side panel when extension icon is clicked
chrome.sidePanel.setPanelBehavior({ openPanelOnActionClick: true });

// Handle messages from sidepanel for tab-level operations
chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (message.type === 'navigate') {
    navigateActiveTab(message.url, message.timeoutMs || 18000).then(sendResponse);
    return true; // Keep message channel open for async response
  }

  if (message.type === 'capture_visible_tab') {
    chrome.tabs.captureVisibleTab(
      undefined,
      { format: 'jpeg', quality: Math.max(30, Math.min(90, message.quality || 62)) },
      (dataUrl) => {
        if (chrome.runtime.lastError) {
          sendResponse({ success: false, error: chrome.runtime.lastError.message });
          return;
        }
        sendResponse({ success: true, dataUrl });
      }
    );
    return true;
  }

  if (message.type === 'get_tab_id') {
    chrome.tabs.query({ active: true, currentWindow: true }, (tabs) => {
      sendResponse({ tabId: tabs[0]?.id });
    });
    return true;
  }
});

async function navigateActiveTab(url, timeoutMs) {
  if (!url || !/^https?:\/\//i.test(url)) {
    return { success: false, error: 'Only http(s) navigation is supported.' };
  }

  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  if (!tab?.id) return { success: false, error: 'Active tab is no longer available.' };

  const startedAt = Date.now();
  try {
    await chrome.tabs.update(tab.id, { url });
  } catch (error) {
    return { success: false, error: error.message || String(error) };
  }

  return new Promise((resolve) => {
    let settled = false;
    const cleanup = (result) => {
      if (settled) return;
      settled = true;
      chrome.tabs.onUpdated.removeListener(listener);
      clearTimeout(timer);
      resolve({
        success: result.success,
        url: result.url || url,
        elapsedMs: Date.now() - startedAt,
        error: result.error,
      });
    };
    const listener = (updatedTabId, changeInfo, updatedTab) => {
      if (updatedTabId !== tab.id) return;
      if (changeInfo.status === 'complete') {
        cleanup({ success: true, url: updatedTab.url });
      }
    };
    const timer = setTimeout(() => {
      chrome.tabs.get(tab.id, (latestTab) => {
        cleanup({
          success: true,
          url: latestTab?.url || url,
          error: 'Navigation timed out, continuing from current page state.',
        });
      });
    }, timeoutMs);
    chrome.tabs.onUpdated.addListener(listener);
  });
}
