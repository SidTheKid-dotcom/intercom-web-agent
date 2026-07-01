// Content script — runs in the context of every web page
// Handles: DOM observation, action execution, scrolling, and highlighting.
let liveObservationSeq = 0;
let liveObservationCache = null;
let liveRefMap = new Map();
let liveOverlayNodes = [];

// Re-bind on each injection so a fresh listener exists after extension reload + executeScript.
function intercomMessageHandler(message, sender, sendResponse) {
  if (message.type === 'scrape') {
    const data = scrapePage();
    sendResponse(data);
  }

  if (message.type === 'scrape_live_agent') {
    const data = observePage({ compact: true, previousSnapshotId: message.previousSnapshotId });
    sendResponse(data);
  }

  if (message.type === 'observe_live_agent') {
    const data = observePage({
      compact: true,
      previousSnapshotId: message.previousSnapshotId,
      includeScreenshotHints: !!message.includeScreenshotHints,
    });
    sendResponse(data);
  }

  if (message.type === 'scroll_to') {
    try {
      const element = resolveActionElement(message.ref || message.selector);
      if (element) {
        element.scrollIntoView({ behavior: 'smooth', block: 'center' });
        flashElement(element);
        sendResponse(buildActionResult(element, { effect: 'scrolled' }));
      } else {
        sendResponse({ success: false, error: 'Element not found' });
      }
    } catch (e) {
      sendResponse({ success: false, error: e.message });
    }
  }

  if (message.type === 'highlight') {
    highlightText(message.text);
    sendResponse({ success: true });
  }

  if (message.type === 'show_live_overlay') {
    try {
      showLiveOverlay(Array.isArray(message.refs) ? message.refs : []);
      sendResponse({ success: true });
    } catch (e) {
      sendResponse({ success: false, error: e.message });
    }
  }

  if (message.type === 'clear_live_overlay') {
    clearLiveOverlay();
    sendResponse({ success: true });
  }

  if (message.type === 'wait_for_settle') {
    waitForPageSettle(message.timeoutMs || 1200).then((result) => sendResponse(result));
    return true;
  }

  if (message.type === 'click') {
    try {
      const element = resolveActionElement(message.ref || message.selector);
      if (element) {
        flashElement(element);
        const before = getElementActionState(element);
        element.click();
        setTimeout(() => {
          sendResponse(buildActionResult(element, { before, effect: 'clicked' }));
        }, 80);
        return true;
      } else if (looksLikeSearchAction(message.selector || message.ref)) {
        const submitted = submitSearchFromPage();
        if (submitted) {
          sendResponse({ success: true, fallback: 'search_submit' });
        } else {
          sendResponse({ success: false, error: 'Element not found' });
        }
      } else {
        sendResponse({ success: false, error: 'Element not found' });
      }
    } catch (e) {
      sendResponse({ success: false, error: e.message });
    }
  }

  if (message.type === 'hover') {
    try {
      const element = resolveActionElement(message.ref || message.selector);
      if (!element) {
        sendResponse({ success: false, error: 'Element not found' });
        return true;
      }
      const before = getPageActionState();
      flashElement(element);
      ['mouseover', 'mouseenter', 'mousemove'].forEach((type) => {
        element.dispatchEvent(
          new MouseEvent(type, {
            bubbles: true,
            cancelable: true,
            view: window,
            clientX: element.getBoundingClientRect().left + 4,
            clientY: element.getBoundingClientRect().top + 4,
          })
        );
      });
      setTimeout(() => {
        sendResponse(buildActionResult(element, { before, effect: 'hovered', pageBefore: before }));
      }, 160);
      return true;
    } catch (e) {
      sendResponse({ success: false, error: e.message });
    }
  }

  if (message.type === 'type_text') {
    try {
      const element = resolveActionElement(message.ref || message.selector);
      if (element) {
        const before = getElementActionState(element);
        setElementText(element, message.text || '');
        sendResponse(buildActionResult(element, { before, effect: 'typed' }));
      } else {
        sendResponse({ success: false, error: 'Element not found' });
      }
    } catch (e) {
      sendResponse({ success: false, error: e.message });
    }
  }

  if (message.type === 'select_value') {
    try {
      const element = resolveActionElement(message.ref || message.selector);
      if (!element || element.tagName !== 'SELECT') {
        sendResponse({ success: false, error: 'Select element not found' });
      } else {
        const before = getElementActionState(element);
        const wanted = String(message.value || '').trim().toLowerCase();
        const option = Array.from(element.options).find((opt) => {
          return (
            opt.value.toLowerCase() === wanted ||
            opt.textContent.trim().toLowerCase() === wanted
          );
        });
        if (!option) {
          sendResponse({ success: false, error: 'Matching option not found' });
        } else {
          element.focus();
          element.value = option.value;
          element.dispatchEvent(new Event('input', { bubbles: true }));
          element.dispatchEvent(new Event('change', { bubbles: true }));
          element.blur();
          sendResponse(buildActionResult(element, { before, effect: 'selected' }));
        }
      }
    } catch (e) {
      sendResponse({ success: false, error: e.message });
    }
  }

  if (message.type === 'check') {
    try {
      const element = resolveActionElement(message.ref || message.selector);
      if (!element || !['checkbox', 'radio'].includes(element.type)) {
        sendResponse({ success: false, error: 'Checkbox or radio element not found' });
      } else {
        const before = getElementActionState(element);
        element.focus();
        const checked = message.checked !== false;
        const nativeSetter = Object.getOwnPropertyDescriptor(
          HTMLInputElement.prototype,
          'checked'
        )?.set;
        if (nativeSetter) {
          nativeSetter.call(element, checked);
        } else {
          element.checked = checked;
        }
        element.dispatchEvent(new Event('input', { bubbles: true }));
        element.dispatchEvent(new Event('change', { bubbles: true }));
        element.blur();
        sendResponse(buildActionResult(element, { before, effect: 'checked' }));
      }
    } catch (e) {
      sendResponse({ success: false, error: e.message });
    }
  }

  if (message.type === 'submit') {
    try {
      const element = resolveActionElement(message.ref || message.selector);
      const form = element?.tagName === 'FORM' ? element : element?.closest('form');
      if (form) {
        const before = getElementActionState(element || form);
        if (typeof form.requestSubmit === 'function') {
          const submitter =
            element && element !== form && ['BUTTON', 'INPUT'].includes(element.tagName)
              ? element
              : undefined;
          form.requestSubmit(submitter);
        } else {
          form.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }));
          form.submit();
        }
        sendResponse(buildActionResult(element || form, { before, effect: 'submitted' }));
      } else if (element) {
        const before = getElementActionState(element);
        element.click();
        sendResponse(buildActionResult(element, { before, effect: 'clicked_submit' }));
      } else {
        sendResponse({ success: false, error: 'Form or submit control not found' });
      }
    } catch (e) {
      sendResponse({ success: false, error: e.message });
    }
  }

  return true; // Keep channel open for async
}

if (window.__intercomMessageHandler) {
  try {
    chrome.runtime.onMessage.removeListener(window.__intercomMessageHandler);
  } catch (e) {
    /* invalid after reload */
  }
}
window.__intercomMessageHandler = intercomMessageHandler;
chrome.runtime.onMessage.addListener(intercomMessageHandler);

function scrapePage(options = {}) {
  const compact = !!options.compact;
  const observation = compact ? observePage({ compact: true }) : null;
  // Get page metadata
  const title = document.title;
  const url = window.location.href;
  const metaDesc =
    document.querySelector('meta[name="description"]')?.content || '';

  // Get all headings
  const headings = Array.from(
    document.querySelectorAll('h1, h2, h3, h4, h5, h6')
  )
    .map((h) => `${h.tagName}: ${h.innerText.trim()}`)
    .filter((h) => h.length > 4)
    .slice(0, compact ? 40 : 120)
    .join('\n');

  // Get main text content
  const bodyText = compact
    ? (document.body.innerText || '').slice(0, 900)
    : document.body.innerText;

  // Get links (capped at 100)
  const linkItems = Array.from(document.querySelectorAll('a[href]'))
    .filter(isElementVisible)
    .slice(0, compact ? 24 : 100)
    .map((a) => {
      const text = a.innerText.trim();
      const titleAttr = (a.getAttribute('title') || '').trim();
      return {
        href: a.href,
        text,
        title: titleAttr,
        selector: getBestSelector(a),
        visible: isElementVisible(a),
      };
    })
    .filter((item) => item.href);
  const links = linkItems
    .map((item) => {
      const label = item.text || item.title || item.href;
      return label
        ? `[${label}](${item.href}) selector="${item.selector}"`
        : null;
    })
    .filter(Boolean)
    .join('\n');

  // Get interactive elements
  const interactiveEls = Array.from(
    document.querySelectorAll(
      'input:not([type="hidden"]), select, textarea, button, a[href], [role="button"], [contenteditable="true"]'
    )
  )
    .filter(isElementVisible)
    .slice(0, compact ? 20 : 140)
    .map((el) => {
      const selector = getBestSelector(el);
      const typeStr = el.type ? ` type="${el.type}"` : '';
      const nameStr = el.name ? ` name="${el.name}"` : '';
      const requiredStr = el.required ? ' required' : '';
      const disabledStr = el.disabled ? ' disabled' : '';
      
      const label = getElementLabel(el);
      const placeholder = el.placeholder || '';
      const text = (el.innerText || el.value || '').trim();
      const value =
        el.tagName === 'INPUT' || el.tagName === 'TEXTAREA' || el.tagName === 'SELECT'
          ? el.value
          : '';
      const checked =
        ['checkbox', 'radio'].includes(el.type) ? ` | Checked: ${el.checked}` : '';
      const options =
        el.tagName === 'SELECT'
          ? ` | Options: ${Array.from(el.options)
              .slice(0, 20)
              .map((opt) => `"${opt.textContent.trim() || opt.value}" value="${opt.value}"`)
              .join(', ')}`
          : '';
      
      const details = [
        label ? `Label: "${label}"` : '',
        placeholder ? `Placeholder: "${placeholder}"` : '',
        value ? `Value: "${value}"` : '',
        text && text !== value ? `Text: "${text}"` : ''
      ].filter(Boolean).join(' | ');

      return `[${el.tagName}${typeStr}${nameStr}${requiredStr}${disabledStr}] selector: "${selector}" -> ${details}${checked}${options}`;
    })
    .filter(Boolean)
    .join('\n');

  const fullText = `Title: ${title}
URL: ${url}
Description: ${metaDesc}

--- Headings ---
${headings}

--- Interactive Elements ---
${interactiveEls}

--- Page Content ---
${bodyText}

--- Links on Page ---
${links}`;

  const fingerprint = [
    title,
    window.location.pathname,
    headings.split('\n').slice(0, 4).join(' | '),
    bodyText.replace(/\s+/g, ' ').trim().slice(0, compact ? 180 : 320),
  ]
    .filter(Boolean)
    .join(' || ');

  return {
    url,
    title,
    text: fullText,
    links: linkItems,
    fingerprint,
    snapshot: observation?.snapshot || null,
    snapshotId: observation?.snapshotId || null,
  };
}

function observePage(options = {}) {
  const compact = !!options.compact;
  const snapshotId = `snap-${Date.now()}-${++liveObservationSeq}`;
  const viewport = {
    width: window.innerWidth,
    height: window.innerHeight,
    scrollX: Math.round(window.scrollX),
    scrollY: Math.round(window.scrollY),
  };
  const title = document.title || '';
  const url = window.location.href;
  const metaDescription = document.querySelector('meta[name="description"]')?.content || '';
  const headings = Array.from(document.querySelectorAll('h1, h2, h3, h4, h5, h6'))
    .filter(isElementVisible)
    .slice(0, compact ? 24 : 80)
    .map((heading) => ({
      level: heading.tagName.toLowerCase(),
      text: normalizeWhitespace(heading.innerText || heading.textContent || '').slice(0, 180),
    }))
    .filter((heading) => heading.text);

  const primaryCandidates = Array.from(
    document.querySelectorAll(
      'input:not([type="hidden"]), select, textarea, button, a[href], [role="button"], [role="link"], [role="checkbox"], [role="radio"], [contenteditable="true"], summary'
    )
  )
    .filter(isElementVisible)
    .sort((a, b) => {
      const ar = a.getBoundingClientRect();
      const br = b.getBoundingClientRect();
      const viewportBias = Number(isInViewport(b)) - Number(isInViewport(a));
      if (viewportBias) return viewportBias;
      return ar.top - br.top || ar.left - br.left;
    })
    .slice(0, compact ? 80 : 180);

  const hiddenNavCandidates = Array.from(
    document.querySelectorAll(
      'header a[href], nav a[href], [class*="nav"] a[href], [class*="menu"] a[href], [class*="dropdown"] a[href], .navbar a[href], .dropdown-menu a[href], li a[href]'
    )
  )
    .filter((el) => !primaryCandidates.includes(el))
    .slice(0, compact ? 80 : 180);

  const elementCandidates = [...primaryCandidates, ...hiddenNavCandidates];

  liveRefMap = new Map();
  const elements = elementCandidates.map((el, index) => {
    const ref = `e${index + 1}`;
    liveRefMap.set(ref, el);
    return describeElement(el, ref);
  });

  const links = elements
    .filter((item) => item.tag === 'a' && item.href)
    .slice(0, compact ? 32 : 100)
    .map((item) => ({
      ref: item.ref,
      href: item.href,
      text: item.text || item.label || item.href,
      selector: item.selector,
      sameHost: safeHostname(item.href) === window.location.hostname,
    }));

  const forms = Array.from(document.querySelectorAll('form'))
    .filter(isElementVisible)
    .slice(0, 12)
    .map((form, index) => {
      const existing = elements.find((item) => liveRefMap.get(item.ref) === form);
      const ref = existing?.ref || `f${index + 1}`;
      if (!existing) liveRefMap.set(ref, form);
      const controls = Array.from(
        form.querySelectorAll('input:not([type="hidden"]), select, textarea, button, input[type="submit"]')
      )
        .map((control) => elements.find((item) => liveRefMap.get(item.ref) === control)?.ref)
        .filter(Boolean);
      return {
        ref,
        label: getFormLabel(form),
        selector: getBestSelector(form),
        controls,
      };
    });

  const bodyText = normalizeWhitespace(document.body?.innerText || '');
  const visibleText = bodyText.slice(0, compact ? 1200 : 5000);
  const fingerprint = buildObservationFingerprint({ title, url, headings, visibleText, elements });
  const snapshot = {
    id: snapshotId,
    page: {
      url,
      title,
      description: metaDescription,
      path: window.location.pathname,
      viewport,
      readyState: document.readyState,
      timestamp: Date.now(),
    },
    summary: {
      headings,
      visibleText,
      fingerprint,
    },
    elements,
    forms,
    links,
    diff: buildObservationDiff(liveObservationCache?.snapshot, { fingerprint, elements }),
    screenshotHints: options.includeScreenshotHints ? buildScreenshotHints(elements) : [],
  };

  liveObservationCache = { snapshotId, snapshot };

  return {
    url,
    title,
    text: formatSnapshotForPrompt(snapshot),
    links: links.map((link) => ({
      href: link.href,
      text: link.text,
      title: '',
      selector: link.selector,
      ref: link.ref,
      visible: true,
    })),
    fingerprint,
    snapshotId,
    snapshot,
  };
}

function describeElement(el, ref) {
  const rect = el.getBoundingClientRect();
  const tag = el.tagName.toLowerCase();
  const label = getElementLabel(el);
  const text = normalizeWhitespace(el.innerText || el.value || el.textContent || '').slice(0, 160);
  const value =
    tag === 'input' || tag === 'textarea' || tag === 'select'
      ? normalizeWhitespace(el.value || '').slice(0, 120)
      : '';
  const role = el.getAttribute('role') || inferRole(el);
  const type = el.getAttribute('type') || '';
  const describedBy = getDescribedByText(el);
  const form = el.closest('form');
  return {
    ref,
    selector: getBestSelector(el),
    tag,
    role,
    type,
    label,
    text,
    value,
    placeholder: el.getAttribute('placeholder') || '',
    name: el.getAttribute('name') || '',
    href: el.href || '',
    hiddenNav: !isElementVisible(el) && !!el.href,
    required: !!el.required,
    disabled: !!el.disabled || el.getAttribute('aria-disabled') === 'true',
    checked: ['checkbox', 'radio'].includes(el.type) ? !!el.checked : null,
    options:
      tag === 'select'
        ? Array.from(el.options)
            .slice(0, 24)
            .map((opt) => ({ label: normalizeWhitespace(opt.textContent || opt.value), value: opt.value }))
        : [],
    rect: {
      x: Math.round(rect.left),
      y: Math.round(rect.top),
      width: Math.round(rect.width),
      height: Math.round(rect.height),
    },
    inViewport: isInViewport(el),
    visible: isElementVisible(el),
    formSelector: form ? getBestSelector(form) : '',
    describedBy,
  };
}

function formatSnapshotForPrompt(snapshot) {
  const page = snapshot.page;
  const headingLines = snapshot.summary.headings
    .map((heading) => `${heading.level.toUpperCase()}: ${heading.text}`)
    .join('\n');
  const elementLines = snapshot.elements
    .map((el) => {
      const bits = [
        `ref=${el.ref}`,
        `<${el.tag}>`,
        el.role ? `role=${el.role}` : '',
        el.type ? `type=${el.type}` : '',
        el.label ? `label="${el.label}"` : '',
        el.text ? `text="${el.text}"` : '',
        el.placeholder ? `placeholder="${el.placeholder}"` : '',
        el.value ? `value="${el.value}"` : '',
        el.href ? `href="${el.href}"` : '',
        el.checked !== null ? `checked=${el.checked}` : '',
        el.required ? 'required' : '',
        el.disabled ? 'disabled' : '',
        el.inViewport ? 'in_viewport' : 'offscreen',
        el.hiddenNav ? 'hidden_nav_link' : '',
        `selector="${el.selector}"`,
      ].filter(Boolean);
      const options = el.options?.length
        ? ` options=${el.options.map((opt) => `"${opt.label}" value="${opt.value}"`).join(', ')}`
        : '';
      return `${bits.join(' | ')}${options}`;
    })
    .join('\n');
  const formLines = snapshot.forms
    .map((form) => `ref=${form.ref} label="${form.label}" controls=${form.controls.join(', ')} selector="${form.selector}"`)
    .join('\n');
  const linkLines = snapshot.links
    .map((link) => `ref=${link.ref} text="${link.text}" href="${link.href}" selector="${link.selector}"`)
    .join('\n');
  const diff = snapshot.diff
    ? `changed=${snapshot.diff.changedRefs.join(', ')} added=${snapshot.diff.addedRefs.join(', ')} removed=${snapshot.diff.removedRefs.join(', ')}`
    : 'none';

  return `Title: ${page.title}
URL: ${page.url}
Description: ${page.description}
Viewport: ${page.viewport.width}x${page.viewport.height} scroll=${page.viewport.scrollX},${page.viewport.scrollY} ready=${page.readyState}
Snapshot ID: ${snapshot.id}
Fingerprint: ${snapshot.summary.fingerprint}

--- Headings ---
${headingLines}

--- Forms ---
${formLines}

--- Interactive Elements ---
${elementLines}

--- Page Content ---
${snapshot.summary.visibleText}

--- Links on Page ---
${linkLines}

--- Snapshot Diff ---
${diff}`;
}

function setElementText(element, text) {
  element.focus();

  if (element.tagName === 'INPUT' || element.tagName === 'TEXTAREA') {
    const nativeSetter = Object.getOwnPropertyDescriptor(
      window[element.tagName === 'INPUT' ? 'HTMLInputElement' : 'HTMLTextAreaElement'].prototype,
      'value'
    )?.set;
    if (nativeSetter) {
      nativeSetter.call(element, text);
    } else {
      element.value = text;
    }
  } else if (element.isContentEditable) {
    element.innerText = text;
  } else {
    element.value = text;
  }

  element.dispatchEvent(new Event('input', { bubbles: true }));
  element.dispatchEvent(new Event('change', { bubbles: true }));
  element.blur();
}

function resolveActionElement(selector) {
  if (!selector) return null;
  const selectorText = String(selector);
  if (liveRefMap.has(selectorText)) {
    const refElement = liveRefMap.get(selectorText);
    if (refElement && document.contains(refElement)) return refElement;
  }
  try {
    const direct = document.querySelector(selectorText);
    if (direct) return direct;
  } catch (e) {
    // Fall through to heuristics.
  }

  const hrefMatch = selectorText.match(/a\[href=['"]([^'"]+)['"]\]/i);
  if (hrefMatch && hrefMatch[1]) {
    const wanted = normalizeComparableUrl(hrefMatch[1]);
    const hrefCandidate = Array.from(document.querySelectorAll('a[href]')).find((a) => {
      return normalizeComparableUrl(a.href) === wanted;
    });
    if (hrefCandidate) return hrefCandidate;

    const partialCandidate = Array.from(document.querySelectorAll('a[href]')).find((a) => {
      return a.href.includes(hrefMatch[1]) || hrefMatch[1].includes(a.getAttribute('href') || '');
    });
    if (partialCandidate) return partialCandidate;
  }

  const titleMatch = selectorText.match(/\[(?:title|aria-label|placeholder|name)=['"]([^'"]+)['"]\]/i);
  if (titleMatch && titleMatch[1]) {
    const wanted = normalizeComparableText(titleMatch[1]);
    const titledCandidate = Array.from(
      document.querySelectorAll('a[href], button, [role="button"], input, select, textarea')
    ).find((el) => {
      const title = normalizeComparableText(el.getAttribute('title') || '');
      const aria = normalizeComparableText(el.getAttribute('aria-label') || '');
      const placeholder = normalizeComparableText(el.getAttribute('placeholder') || '');
      const name = normalizeComparableText(el.getAttribute('name') || '');
      const text = normalizeComparableText(el.innerText || '');
      return (
        title === wanted ||
        aria === wanted ||
        placeholder === wanted ||
        name === wanted ||
        text === wanted
      );
    });
    if (titledCandidate) return titledCandidate;
  }

  const tagMatch = selectorText.match(/^([a-z0-9_-]+)/i);
  const tagName = tagMatch ? tagMatch[1].toLowerCase() : '';
  const quotedBits = Array.from(selectorText.matchAll(/['"]([^'"]{2,})['"]/g))
    .map((match) => normalizeComparableText(match[1]))
    .filter(Boolean);
  if (quotedBits.length > 0) {
    const candidates = Array.from(
      document.querySelectorAll(tagName || 'a[href], button, [role="button"], input, select, textarea')
    ).filter(isElementVisible);
    const fuzzyCandidate = candidates.find((el) => {
      const haystack = normalizeComparableText(
        [
          el.innerText || '',
          el.value || '',
          el.getAttribute('aria-label') || '',
          el.getAttribute('title') || '',
          el.getAttribute('placeholder') || '',
          el.getAttribute('name') || '',
        ].join(' | ')
      );
      return quotedBits.some((needle) => haystack.includes(needle));
    });
    if (fuzzyCandidate) return fuzzyCandidate;
  }

  return null;
}

function looksLikeSearchAction(selector) {
  const text = normalizeComparableText(selector);
  return (
    text.includes('search') ||
    text.includes('submit') ||
    text.includes('go') ||
    text.includes('magnifier')
  );
}

function submitSearchFromPage() {
  const active = document.activeElement;
  const searchInput = findSearchInput() || active;
  if (
    searchInput &&
    ['INPUT', 'TEXTAREA'].includes(searchInput.tagName) &&
    isElementVisible(searchInput)
  ) {
    searchInput.focus();
    searchInput.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', code: 'Enter', bubbles: true }));
    searchInput.dispatchEvent(new KeyboardEvent('keypress', { key: 'Enter', code: 'Enter', bubbles: true }));
    searchInput.dispatchEvent(new KeyboardEvent('keyup', { key: 'Enter', code: 'Enter', bubbles: true }));
    const form = searchInput.closest('form');
    if (form) {
      if (typeof form.requestSubmit === 'function') {
        form.requestSubmit();
      } else {
        form.submit();
      }
    }
    return true;
  }

  const fallbackButton = Array.from(
    document.querySelectorAll('button, input[type="submit"], input[type="button"], [role="button"]')
  )
    .filter(isElementVisible)
    .find((el) => {
      const haystack = normalizeComparableText(
        [
          el.innerText || '',
          el.value || '',
          el.getAttribute('aria-label') || '',
          el.getAttribute('title') || '',
          el.getAttribute('name') || '',
        ].join(' | ')
      );
      return haystack.includes('search') || haystack.includes('go');
    });

  if (fallbackButton) {
    fallbackButton.click();
    return true;
  }

  return false;
}

function findSearchInput() {
  return Array.from(document.querySelectorAll('input, textarea'))
    .filter(isElementVisible)
    .find((el) => {
      const haystack = normalizeComparableText(
        [
          el.getAttribute('type') || '',
          el.getAttribute('name') || '',
          el.getAttribute('aria-label') || '',
          el.getAttribute('placeholder') || '',
          el.getAttribute('title') || '',
          el.id || '',
        ].join(' | ')
      );
      return haystack.includes('search') || haystack.includes('query') || haystack.includes('keyword');
    });
}

function normalizeComparableUrl(value) {
  try {
    const parsed = new URL(value, window.location.href);
    parsed.hash = '';
    return parsed.toString();
  } catch (e) {
    return String(value || '').trim();
  }
}

function normalizeComparableText(value) {
  return String(value || '')
    .replace(/\s+/g, ' ')
    .trim()
    .toLowerCase();
}

function getElementLabel(el) {
  if (el.labels && el.labels[0]) return el.labels[0].innerText.trim();
  const aria = el.getAttribute('aria-label');
  if (aria) return aria.trim();
  const labelledBy = el.getAttribute('aria-labelledby');
  if (labelledBy) {
    const label = document.getElementById(labelledBy);
    if (label) return label.innerText.trim();
  }
  const wrappingLabel = el.closest('label');
  if (wrappingLabel) return wrappingLabel.innerText.trim();
  return '';
}

function getBestSelector(el) {
  const tag = el.tagName.toLowerCase();
  const candidates = [];
  if (el.getAttribute('aria-label')) {
    candidates.push(`${tag}[aria-label="${cssEscape(el.getAttribute('aria-label'))}"]`);
  }
  if (el.getAttribute('placeholder')) {
    candidates.push(`${tag}[placeholder="${cssEscape(el.getAttribute('placeholder'))}"]`);
  }
  if (el.getAttribute('title')) {
    candidates.push(`${tag}[title="${cssEscape(el.getAttribute('title'))}"]`);
  }
  if (el.name) candidates.push(`${tag}[name="${cssEscape(el.name)}"]`);
  if (tag === 'a' && el.getAttribute('href')) {
    candidates.push(`${tag}[href="${cssEscape(el.getAttribute('href'))}"]`);
  }
  if (el.getAttribute('role')) {
    candidates.push(`${tag}[role="${cssEscape(el.getAttribute('role'))}"]`);
  }
  ['data-testid', 'data-test', 'data-cy'].forEach((attr) => {
    const value = el.getAttribute(attr);
    if (value) candidates.push(`${tag}[${attr}="${cssEscape(value)}"]`);
  });
  if (el.id && !looksDynamicId(el.id)) {
    candidates.push(`#${cssEscape(el.id)}`);
  }
  if (el.type) candidates.push(`${tag}[type="${cssEscape(el.type)}"]`);

  const unique = candidates.find(isUniqueSelector);
  if (unique) return unique;

  const parent = el.parentElement;
  if (!parent) return tag;
  const siblings = Array.from(parent.children).filter((child) => child.tagName === el.tagName);
  const index = siblings.indexOf(el) + 1;
  const parentSelector = parent.id ? `#${cssEscape(parent.id)}` : parent.tagName.toLowerCase();
  return `${parentSelector} > ${tag}:nth-of-type(${index || 1})`;
}

function looksDynamicId(value) {
  const text = String(value || '').trim();
  if (!text) return true;
  return (
    /^a-autoid[-\d_]+$/i.test(text) ||
    /^react-aria\d+/i.test(text) ||
    /[-_][0-9a-f]{6,}/i.test(text) ||
    /\d{4,}/.test(text)
  );
}

function isUniqueSelector(selector) {
  try {
    return document.querySelectorAll(selector).length === 1;
  } catch (e) {
    return false;
  }
}

function cssEscape(value) {
  if (window.CSS && typeof window.CSS.escape === 'function') {
    return window.CSS.escape(value);
  }
  return String(value).replace(/["\\]/g, '\\$&');
}

function isElementVisible(el) {
  const style = window.getComputedStyle(el);
  const rect = el.getBoundingClientRect();
  return (
    style.display !== 'none' &&
    style.visibility !== 'hidden' &&
    rect.width > 0 &&
    rect.height > 0
  );
}

function isInViewport(el) {
  const rect = el.getBoundingClientRect();
  return (
    rect.bottom > 0 &&
    rect.right > 0 &&
    rect.top < (window.innerHeight || document.documentElement.clientHeight) &&
    rect.left < (window.innerWidth || document.documentElement.clientWidth)
  );
}

function normalizeWhitespace(value) {
  return String(value || '').replace(/\s+/g, ' ').trim();
}

function inferRole(el) {
  const tag = el.tagName.toLowerCase();
  if (tag === 'a') return 'link';
  if (tag === 'button') return 'button';
  if (tag === 'select') return 'combobox';
  if (tag === 'textarea') return 'textbox';
  if (tag === 'summary') return 'button';
  if (tag === 'input') {
    if (['checkbox', 'radio', 'button', 'submit'].includes(el.type)) return el.type === 'submit' ? 'button' : el.type;
    return 'textbox';
  }
  if (el.isContentEditable) return 'textbox';
  return '';
}

function getFormLabel(form) {
  const labelledBy = form.getAttribute('aria-labelledby');
  if (labelledBy) {
    const label = document.getElementById(labelledBy);
    if (label) return normalizeWhitespace(label.innerText || label.textContent || '');
  }
  const aria = form.getAttribute('aria-label');
  if (aria) return normalizeWhitespace(aria);
  const legend = form.querySelector('legend');
  if (legend) return normalizeWhitespace(legend.innerText || legend.textContent || '');
  const heading = form.querySelector('h1, h2, h3, h4, h5, h6');
  if (heading) return normalizeWhitespace(heading.innerText || heading.textContent || '');
  return '';
}

function getDescribedByText(el) {
  const describedBy = el.getAttribute('aria-describedby');
  if (!describedBy) return '';
  return describedBy
    .split(/\s+/)
    .map((id) => document.getElementById(id))
    .filter(Boolean)
    .map((node) => normalizeWhitespace(node.innerText || node.textContent || ''))
    .filter(Boolean)
    .join(' | ')
    .slice(0, 220);
}

function buildObservationFingerprint({ title, url, headings, visibleText, elements }) {
  const path = (() => {
    try {
      return new URL(url).pathname;
    } catch (e) {
      return url;
    }
  })();
  const headingText = headings.slice(0, 5).map((heading) => heading.text).join(' | ');
  const elementText = elements
    .slice(0, 16)
    .map((el) => [el.ref, el.label, el.text, el.value, el.checked].filter((v) => v !== null && v !== '').join(':'))
    .join(' | ');
  return normalizeWhitespace([title, path, headingText, visibleText.slice(0, 260), elementText].join(' || '));
}

function buildObservationDiff(previous, current) {
  if (!previous) {
    return { addedRefs: [], removedRefs: [], changedRefs: [], fingerprintChanged: true };
  }
  const previousByKey = new Map((previous.elements || []).map((item) => [elementStableKey(item), item]));
  const currentByKey = new Map((current.elements || []).map((item) => [elementStableKey(item), item]));
  const addedRefs = [];
  const removedRefs = [];
  const changedRefs = [];

  for (const [key, item] of currentByKey) {
    const before = previousByKey.get(key);
    if (!before) {
      addedRefs.push(item.ref);
      continue;
    }
    if (
      before.text !== item.text ||
      before.value !== item.value ||
      before.checked !== item.checked ||
      before.disabled !== item.disabled ||
      before.inViewport !== item.inViewport
    ) {
      changedRefs.push(item.ref);
    }
  }
  for (const [key, item] of previousByKey) {
    if (!currentByKey.has(key)) removedRefs.push(item.ref);
  }

  return {
    addedRefs: addedRefs.slice(0, 20),
    removedRefs: removedRefs.slice(0, 20),
    changedRefs: changedRefs.slice(0, 24),
    fingerprintChanged: previous.summary?.fingerprint !== current.fingerprint,
  };
}

function elementStableKey(item) {
  return [item.selector, item.href, item.name, item.label, item.text].filter(Boolean).join('::') || item.ref;
}

function buildScreenshotHints(elements) {
  return elements
    .filter((item) => item.inViewport)
    .slice(0, 30)
    .map((item) => ({
      ref: item.ref,
      label: item.label || item.text || item.href || item.selector,
      rect: item.rect,
    }));
}

function getElementActionState(el) {
  if (!el) return {};
  return {
    url: window.location.href,
    title: document.title,
    pageTextLength: document.body?.innerText?.length || 0,
    text: normalizeWhitespace(el.innerText || el.textContent || '').slice(0, 180),
    value: typeof el.value === 'string' ? el.value : '',
    checked: typeof el.checked === 'boolean' ? el.checked : null,
    disabled: !!el.disabled || el.getAttribute('aria-disabled') === 'true',
    href: el.href || '',
  };
}

function getPageActionState() {
  return {
    url: window.location.href,
    title: document.title,
    textLength: document.body?.innerText?.length || 0,
    visibleInteractiveCount: Array.from(
      document.querySelectorAll(
        'input:not([type="hidden"]), select, textarea, button, a[href], [role="button"], [role="link"], [contenteditable="true"]'
      )
    ).filter(isElementVisible).length,
  };
}

function buildActionResult(el, extra = {}) {
  const after = getElementActionState(el);
  const before = extra.before || {};
  const pageBefore = extra.pageBefore || before;
  const pageAfter = getPageActionState();
  const changed =
    before.value !== after.value ||
    before.checked !== after.checked ||
    before.url !== after.url ||
    before.disabled !== after.disabled ||
    pageBefore.url !== pageAfter.url ||
    pageBefore.title !== pageAfter.title ||
    pageBefore.textLength !== pageAfter.textLength ||
    pageBefore.visibleInteractiveCount !== pageAfter.visibleInteractiveCount;
  return {
    success: true,
    ref: findRefForElement(el),
    selector: el ? getBestSelector(el) : '',
    label: el ? getElementLabel(el) || normalizeWhitespace(el.innerText || el.value || '') : '',
    href: el?.href || '',
    effect: extra.effect || '',
    changed,
    before,
    after,
    pageBefore,
    pageAfter,
  };
}

function findRefForElement(el) {
  for (const [ref, candidate] of liveRefMap.entries()) {
    if (candidate === el) return ref;
  }
  return '';
}

function flashElement(element) {
  const originalOutline = element.style.outline;
  const originalOffset = element.style.outlineOffset;
  element.style.outline = '3px solid #6366f1';
  element.style.outlineOffset = '2px';
  setTimeout(() => {
    element.style.outline = originalOutline;
    element.style.outlineOffset = originalOffset;
  }, 2200);
}

function waitForPageSettle(timeoutMs = 1200) {
  const startedAt = Date.now();
  const initial = {
    url: window.location.href,
    title: document.title,
    textLength: document.body?.innerText?.length || 0,
    readyState: document.readyState,
  };
  return new Promise((resolve) => {
    let quietTimer = null;
    let resolved = false;
    const observer = new MutationObserver(() => {
      clearTimeout(quietTimer);
      quietTimer = setTimeout(finish, 180);
    });

    function finish() {
      if (resolved) return;
      resolved = true;
      clearTimeout(quietTimer);
      observer.disconnect();
      resolve({
        success: true,
        elapsedMs: Date.now() - startedAt,
        before: initial,
        after: {
          url: window.location.href,
          title: document.title,
          textLength: document.body?.innerText?.length || 0,
          readyState: document.readyState,
        },
      });
    }

    try {
      observer.observe(document.documentElement || document.body, {
        childList: true,
        subtree: true,
        attributes: true,
        characterData: false,
      });
    } catch (e) {
      finish();
      return;
    }
    quietTimer = setTimeout(finish, document.readyState === 'complete' ? 180 : 400);
    setTimeout(finish, timeoutMs);
  });
}

function showLiveOverlay(refs) {
  clearLiveOverlay();
  const selectedRefs = refs.length ? refs : Array.from(liveRefMap.keys()).slice(0, 24);
  const container = document.createElement('div');
  container.className = 'intercom-live-overlay-root';
  container.style.cssText = [
    'position:fixed',
    'inset:0',
    'pointer-events:none',
    'z-index:2147483647',
    'font:12px -apple-system,BlinkMacSystemFont,Segoe UI,sans-serif',
  ].join(';');
  document.documentElement.appendChild(container);
  liveOverlayNodes.push(container);

  selectedRefs.forEach((ref) => {
    const el = liveRefMap.get(ref);
    if (!el || !document.contains(el) || !isElementVisible(el) || !isInViewport(el)) return;
    const rect = el.getBoundingClientRect();
    const box = document.createElement('div');
    box.style.cssText = [
      'position:fixed',
      `left:${Math.max(0, rect.left)}px`,
      `top:${Math.max(0, rect.top)}px`,
      `width:${Math.max(4, rect.width)}px`,
      `height:${Math.max(4, rect.height)}px`,
      'border:2px solid #8b5cf6',
      'border-radius:6px',
      'box-shadow:0 0 0 1px rgba(255,255,255,0.55)',
    ].join(';');
    const badge = document.createElement('span');
    badge.textContent = ref;
    badge.style.cssText = [
      'position:absolute',
      'left:0',
      'top:-20px',
      'padding:2px 5px',
      'background:#8b5cf6',
      'color:white',
      'border-radius:999px',
      'font-weight:700',
    ].join(';');
    box.appendChild(badge);
    container.appendChild(box);
  });
}

function clearLiveOverlay() {
  liveOverlayNodes.forEach((node) => node.remove());
  liveOverlayNodes = [];
}

function safeHostname(rawUrl) {
  try {
    return new URL(rawUrl, window.location.href).hostname;
  } catch (e) {
    return '';
  }
}


function highlightText(searchText) {
  // Remove previous highlights
  document.querySelectorAll('.intercom-highlight').forEach((el) => {
    const parent = el.parentNode;
    parent.replaceChild(document.createTextNode(el.textContent), el);
    parent.normalize();
  });

  if (!searchText) return;

  const walker = document.createTreeWalker(
    document.body,
    NodeFilter.SHOW_TEXT,
    null
  );

  const matchingNodes = [];
  while (walker.nextNode()) {
    if (
      walker.currentNode.textContent
        .toLowerCase()
        .includes(searchText.toLowerCase())
    ) {
      matchingNodes.push(walker.currentNode);
    }
  }

  // Highlight first 5 matches
  let firstHighlight = null;
  matchingNodes.slice(0, 5).forEach((node) => {
    const text = node.textContent;
    const idx = text.toLowerCase().indexOf(searchText.toLowerCase());
    if (idx < 0) return;

    const before = document.createTextNode(text.substring(0, idx));
    const match = document.createTextNode(
      text.substring(idx, idx + searchText.length)
    );
    const after = document.createTextNode(
      text.substring(idx + searchText.length)
    );

    const span = document.createElement('span');
    span.className = 'intercom-highlight';
    span.style.backgroundColor = '#6366f1';
    span.style.color = 'white';
    span.style.padding = '2px 4px';
    span.style.borderRadius = '3px';
    span.appendChild(match);

    const parent = node.parentNode;
    parent.insertBefore(before, node);
    parent.insertBefore(span, node);
    parent.insertBefore(after, node);
    parent.removeChild(node);

    if (!firstHighlight) firstHighlight = span;
  });

  // Scroll to first highlight
  if (firstHighlight) {
    firstHighlight.scrollIntoView({ behavior: 'smooth', block: 'center' });
  }
}
