// Cover image fallback: when a cover image fails to load (Steam's CDN often
// 404s DLC header/cover URLs), try the parent game's URL if one was provided
// via data-fallback. If THAT also fails, remove the cover element entirely
// (so a sibling placeholder div can take over its space — see library_card.html).
// Exposed as a global because inline onerror handlers (templated) call it.
//
// The element removed on terminal failure depends on context:
//   - If [data-cover-fallback-container] is set (e.g. ".cgt-library-card__cover"),
//     we remove the closest matching ancestor. Lets grid cards collapse to
//     their placeholder.
//   - Otherwise we hide the immediate parent (detail-pane behavior).
//   - For [class*="thumb"] list rows, we just remove the img.
// Collapse the .cgt-detail-hero block only when every img inside it has been
// hidden. Called after either the hero or the logo fails — each one hides
// itself first, then calls this to decide whether the whole block goes away.
// This lets the logo remain visible over a dark background when the hero
// fails, and vice-versa. Both failing → nothing left to show → collapse.
window.cgtHeroBlockCheck = function(img) {
  var block = img.closest('.cgt-detail-hero');
  if (!block) return;
  var hasVisible = false;
  block.querySelectorAll('img').forEach(function(i) {
    if (i.style.display !== 'none') hasVisible = true;
  });
  if (!hasVisible) block.style.display = 'none';
};

// Detail-pane hero image failure handler. Mirrors cgtCoverFallback's fallback
// logic (try data-fallback URL first), but on terminal failure hides only the
// hero img rather than removing the whole block — so a logo overlay can still
// show. Block is collapsed via cgtHeroBlockCheck only when both are gone.
// If data-cgt-entry-id is set and cgtAutoFetchHero is registered (library
// page), fires a background SGDB hero fetch just like logos do.
window.cgtHeroFailed = function(img) {
  var fb = img.dataset.fallback;
  if (fb && img.src !== fb) {
    img.src = fb;
    img.dataset.fallback = '';
    return;
  }
  img.style.display = 'none';
  window.cgtHeroBlockCheck(img);
  if (typeof window.cgtAutoFetchHero === 'function') {
    var entryId = img.dataset.cgtEntryId;
    if (entryId && img.dataset.cgtFreshOpen) window.cgtAutoFetchHero(img, parseInt(entryId, 10));
  }
};

// Auto-fetch hero: called from cgtHeroFailed when the hero img 404s on a
// fresh pane open. POSTs to SGDB, stores the result, updates img.src in-place.
window.cgtAutoFetchHero = function(img, entryId) {
  img.onerror = null;
  fetch('/library/entries/' + entryId + '/auto-fetch-hero', {method: 'POST'})
    .then(function(r) {
      if (r.ok && r.status === 200) {
        return r.json().then(function(data) {
          if (data.url) {
            img.style.display = '';
            var block = img.closest('.cgt-detail-hero');
            if (block) block.style.display = '';
            img.src = data.url;
          }
        });
      }
    })
    .catch(function() { /* silent */ });
};

// Auto-fetch logo: called from onerror on the hero logo img when it 404s.
// If the img has data-cgt-logo-reload-url/target set, reloads the pane after
// storing the logo so it renders cleanly. Otherwise updates img.src in-place.
window.cgtAutoFetchLogo = function(img, entryId) {
  img.onerror = null;
  var reloadUrl = img.dataset.cgtLogoReloadUrl;
  var reloadTarget = img.dataset.cgtLogoReloadTarget;
  fetch('/library/entries/' + entryId + '/auto-fetch-logo', {method: 'POST'})
    .then(function(r) {
      if (r.ok && r.status === 200) {
        return r.json().then(function(data) {
          if (data.url) {
            if (reloadUrl && reloadTarget) {
              htmx.ajax('GET', reloadUrl, {target: reloadTarget, swap: 'innerHTML'});
            } else {
              img.style.display = '';
              var block = img.closest('.cgt-detail-hero');
              if (block) block.style.display = '';
              img.src = data.url;
            }
          }
        });
      }
      // 204 = nothing found; leave img hidden
    })
    .catch(function() { /* silent — best-effort */ });
};

window.cgtCoverFallback = function(img) {
  var fb = img.dataset.fallback;
  if (fb && img.src !== fb) {
    img.src = fb;
    img.dataset.fallback = '';  // prevent loop if the parent also 404s
    return;
  }
  var containerSel = img.dataset.coverFallbackContainer;
  if (containerSel) {
    var container = img.closest(containerSel);
    if (container) { container.remove(); return; }
  }
  if (img.classList.contains('cgt-list-row-thumb')) {
    img.remove();
    return;
  }
  if (img.parentElement) img.parentElement.style.display = 'none';
};

// Toolbar drawer helpers (cgtToggleDrawer / cgtInitDrawer) live inline in
// base.html so they're defined before per-page inline scripts run — app.js
// is `defer`red and wouldn't be ready in time otherwise.

// Local-time helper: render any element with [data-utc] in the browser's
// locale. Re-run on HTMX swaps so refetched fragments (e.g. the Steam page's
// #steam-sync-status block after a sync finishes) don't show the raw UTC
// fallback text.
function cgtRenderLocalTimes(root) {
  (root || document).querySelectorAll('.local-time[data-utc]').forEach(function(el) {
    var d = new Date(el.dataset.utc + 'Z');
    el.textContent = d.toLocaleString(undefined, {month:'long', day:'numeric', year:'numeric', hour:'numeric', minute:'2-digit'});
  });
}
cgtRenderLocalTimes();
document.addEventListener('htmx:afterSettle', function(e) {
  if (e.target instanceof Element) cgtRenderLocalTimes(e.target);
});

// Placeholder title fit: shrink font until the title fits within the
// placeholder box. Runs after page load and after library content swaps.
// Binary-searches between 8px and the CSS default size in ~8 iterations.
window.cgtFitPlaceholderTitles = function(root) {
  (root || document).querySelectorAll('.cgt-library-card__placeholder-title').forEach(function(title) {
    title.style.fontSize = '';
    var ph = title.closest('.cgt-library-card__placeholder');
    if (!ph || !ph.clientHeight) return;
    var cs = getComputedStyle(ph);
    var available = ph.clientHeight
      - parseFloat(cs.paddingTop)
      - parseFloat(cs.paddingBottom);
    // Subtract the platform line height if present
    var platform = ph.querySelector('.cgt-library-card__placeholder-platform');
    if (platform) available -= platform.offsetHeight + 4;
    if (title.scrollHeight <= available) return;
    var hi = parseFloat(getComputedStyle(title).fontSize);
    var lo = 8;
    for (var i = 0; i < 8; i++) {
      var mid = (hi + lo) / 2;
      title.style.fontSize = mid + 'px';
      if (title.scrollHeight > available) hi = mid; else lo = mid;
    }
    title.style.fontSize = lo + 'px';
  });
};
document.addEventListener('DOMContentLoaded', function() { window.cgtFitPlaceholderTitles(); });
document.addEventListener('htmx:afterSettle', function(e) {
  if (e.target.id === 'library-content' || e.target.closest('#library-content')) {
    window.cgtFitPlaceholderTitles(e.target.closest('#library-content') || e.target);
  }
});

// Toast initialisation.
//
// Background: the server emits toasts via HTMX out-of-band swap into
// #toast-container. Bootstrap's .toast CSS hides any toast that doesn't have
// the .show class, so a toast inserted via HTMX won't appear until something
// calls Bootstrap.Toast.show() on it.
//
// We use a MutationObserver on #toast-container instead of listening for HTMX
// events (htmx:afterSettle etc.) because the observer fires regardless of
// which event HTMX dispatches for the swap — more robust to HTMX version
// quirks and OOB swap edge cases.
//
// Each toast also gets:
// - A 6s autohide via Bootstrap.Toast options
// - A 'hidden.bs.toast' listener that removes the element from the DOM so
//   the container doesn't accumulate stale toasts over a long session.
function cgtToast(message, type) {
  var container = document.getElementById('toast-container');
  if (!container) return;
  var color = type === 'error' ? 'var(--ctp-red)' : 'var(--ctp-green)';
  var el = document.createElement('div');
  el.className = 'toast align-items-center border-0';
  el.setAttribute('role', 'status');
  el.innerHTML =
    '<div class="d-flex">' +
    '<div class="toast-body small" style="color:' + color + ';">' + message + '</div>' +
    '<button type="button" class="btn-close btn-close-white me-2 m-auto" data-bs-dismiss="toast"></button>' +
    '</div>';
  container.appendChild(el);
  _initToast(el);
}

function _initToast(el) {
  if (!el || el.dataset.cgtInited) return;
  if (typeof bootstrap === 'undefined') {
    console.warn('[toast] bootstrap not loaded; cannot init toast');
    return;
  }
  el.dataset.cgtInited = '1';
  var t = new bootstrap.Toast(el, { delay: 6000 });
  el.addEventListener('hidden.bs.toast', function() { el.remove(); });
  t.show();
}

function _initToastContainer() {
  var container = document.getElementById('toast-container');
  if (!container) return;

  // Catch any toasts that happen to be present at page load.
  container.querySelectorAll('.toast').forEach(_initToast);

  // Catch any toast inserted later via HTMX OOB swap, JS, anything.
  var observer = new MutationObserver(function(mutations) {
    mutations.forEach(function(m) {
      m.addedNodes.forEach(function(node) {
        if (node.nodeType !== 1) return;
        if (node.classList && node.classList.contains('toast')) {
          _initToast(node);
        }
      });
    });
  });
  observer.observe(container, { childList: true });
}

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', _initToastContainer);
} else {
  _initToastContainer();
}

// Resizable offcanvas detail pane.
//
// Any .offcanvas[data-cgt-resize-key] gets a left-edge drag handle. Dragging
// updates the offcanvas width via the Bootstrap CSS variable; on mouseup the
// new width is persisted to localStorage under the data-attribute's key.
// On open, the saved width is restored before show() so the pane appears at
// the user's preferred size from the first frame.
//
// Width is clamped to [300px, 80% of viewport] to keep the pane usable on
// small screens and prevent off-screen drags.
function _initOffcanvasResize(offcanvasEl) {
  if (!offcanvasEl || offcanvasEl.dataset.cgtResizeInited) return;
  offcanvasEl.dataset.cgtResizeInited = '1';

  var key = offcanvasEl.dataset.cgtResizeKey;
  var handle = offcanvasEl.querySelector('.offcanvas-resize-handle');
  if (!key || !handle) return;

  function clampWidth(px) {
    var min = 300;
    var max = Math.floor(window.innerWidth * 0.8);
    return Math.max(min, Math.min(max, px));
  }

  function applyWidth(px) {
    offcanvasEl.style.setProperty('--bs-offcanvas-width', clampWidth(px) + 'px');
  }

  // Restore saved width on init AND every time the offcanvas opens (the user
  // might resize one pane while the other is closed).
  var saved = parseInt(localStorage.getItem(key), 10);
  if (saved) applyWidth(saved);
  offcanvasEl.addEventListener('show.bs.offcanvas', function() {
    var s = parseInt(localStorage.getItem(key), 10);
    if (s) applyWidth(s);
  });

  var dragging = false;
  handle.addEventListener('mousedown', function(e) {
    dragging = true;
    handle.classList.add('cgt-resizing');
    // Prevent text selection during drag.
    document.body.style.userSelect = 'none';
    e.preventDefault();
  });
  document.addEventListener('mousemove', function(e) {
    if (!dragging) return;
    // Pane is anchored to the right edge — new width = viewport width minus
    // the mouse X position.
    applyWidth(window.innerWidth - e.clientX);
  });
  document.addEventListener('mouseup', function() {
    if (!dragging) return;
    dragging = false;
    handle.classList.remove('cgt-resizing');
    document.body.style.userSelect = '';
    var currentWidth = parseInt(offcanvasEl.style.getPropertyValue('--bs-offcanvas-width'), 10);
    if (currentWidth) localStorage.setItem(key, String(currentWidth));
  });
}

function _initAllOffcanvasResize() {
  document.querySelectorAll('.offcanvas[data-cgt-resize-key]').forEach(_initOffcanvasResize);
}

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', _initAllOffcanvasResize);
} else {
  _initAllOffcanvasResize();
}

// ─── Chrome title dynamic sizing ──────────────────────────────────────────
//
// After any detail-pane content swap, shrink .cgt-pane-chrome-title in 0.5px
// steps until it fits on one line (no overflow), down to a 10px floor.
// A ResizeObserver re-runs the fit whenever the user drags the pane wider or
// narrower. Works for both #library-detail-content and
// #completion-detail-content — lives here so it doesn't have to be duplicated
// in every page template that hosts a detail pane.
var _cgtChromeTitleRO = null;
window.cgtFitChromeTitle = function(contentEl) {
  var el = contentEl && contentEl.querySelector('.cgt-pane-chrome-title');
  if (!el) return;
  el.style.fontSize = '';  // reset to CSS-defined max before measuring
  var px = parseFloat(getComputedStyle(el).fontSize);
  var min = 10;
  while (el.scrollWidth > el.clientWidth + 1 && px > min) {
    px -= 0.5;
    el.style.fontSize = px + 'px';
  }
};

document.body.addEventListener('htmx:afterSettle', function(e) {
  var t = e.detail.target;
  if (t.id !== 'library-detail-content' && t.id !== 'completion-detail-content') return;
  window.cgtFitChromeTitle(t);
  // Re-fit whenever the pane is resized by dragging.
  if (_cgtChromeTitleRO) _cgtChromeTitleRO.disconnect();
  var header = t.querySelector('.cgt-pane-chrome');
  if (header && window.ResizeObserver) {
    _cgtChromeTitleRO = new ResizeObserver(function() { window.cgtFitChromeTitle(t); });
    _cgtChromeTitleRO.observe(header);
  }
});

// ─── Detail-pane back navigation ──────────────────────────────────────────
//
// When the library / completion detail pane navigates internally (e.g. user
// clicks a DLC under "Games in this collection" to swap the pane to the
// child's detail), we want a "← Back" button in the new pane's header so
// they can pop back to the parent.
//
// Implementation:
// - Two stacks (one per pane type) hold the entry/completion IDs the user
//   has navigated through during this offcanvas session.
// - The opener functions (openLibraryDetail / openCompletionDetail in the
//   page templates) reset their stack to [initial_id] on a fresh open.
// - HTMX `beforeRequest` listener pushes the new ID onto the stack when a
//   request targets the pane's content div (deduped: skip if it equals the
//   top — covers back-button clicks).
// - HTMX `afterSwap` injects/removes the back button in the rendered pane
//   header based on stack depth.

window._cgtPaneStacks = { library: [], completion: [] };

window.cgtPaneInitLibrary = function(entryId) {
  // Fresh open from a library row click — start the stack over.
  window._cgtPaneStacks.library = [String(entryId)];
};
window.cgtPaneInitCompletion = function(completionId) {
  window._cgtPaneStacks.completion = [String(completionId)];
};

function _cgtPaneTargetKind(target) {
  if (!target || !target.id) return null;
  if (target.id === 'library-detail-content') return 'library';
  if (target.id === 'completion-detail-content') return 'completion';
  return null;
}

function _cgtPaneIdFromPath(kind, path) {
  // /library/entries/123/detail → "123"; /completions/45/detail → "45".
  var re = kind === 'library' ? /\/library\/entries\/(\d+)\/detail/ : /\/completions\/(\d+)\/detail/;
  var m = path && path.match(re);
  return m ? m[1] : null;
}

document.body.addEventListener('htmx:beforeRequest', function(e) {
  var kind = _cgtPaneTargetKind(e.detail.target);
  if (!kind) return;
  var path = e.detail.requestConfig && e.detail.requestConfig.path;
  var id = _cgtPaneIdFromPath(kind, path);
  if (!id) return;
  var stack = window._cgtPaneStacks[kind];
  // Skip dedupe (back-button click is followed by a request that lands on
  // the previous top — we don't want to re-push and double-stack it).
  if (stack[stack.length - 1] === id) return;
  stack.push(id);
});

document.body.addEventListener('htmx:afterSwap', function(e) {
  var kind = _cgtPaneTargetKind(e.detail.target);
  if (!kind) return;
  var stack = window._cgtPaneStacks[kind];
  // Target the dedicated nav bar (collapses via CSS :empty when no button).
  var nav = e.detail.target.querySelector('.cgt-pane-nav');
  if (!nav) return;
  // Clear any prior injection so depth is always reflected correctly.
  nav.innerHTML = '';
  if (stack.length <= 1) return;
  var btn = document.createElement('button');
  btn.type = 'button';
  btn.className = 'btn btn-sm btn-outline-secondary cgt-pane-back';
  btn.setAttribute('aria-label', 'Back to previous detail');
  btn.textContent = '← Back';
  btn.addEventListener('click', function() {
    stack.pop();  // remove current
    var prev = stack[stack.length - 1];
    var path = kind === 'library'
      ? '/library/entries/' + prev + '/detail'
      : '/completions/' + prev + '/detail';
    var targetId = kind === 'library' ? '#library-detail-content' : '#completion-detail-content';
    htmx.ajax('GET', path, { target: targetId, swap: 'innerHTML' });
  });
  nav.appendChild(btn);
});

// ─── SteamGridDB art picker ────────────────────────────────────────────────
//
// Shared between library.html and completions.html. The modal HTML lives in
// partials/sgdb_picker_modal.html, included in both pages.
//
// openSgdbPicker(entryId, imageType, detailTarget)
//   detailTarget — CSS selector for the pane content div to reload after
//   applying ('library-detail-content' or '#completion-detail-content').
//   Defaults to '#library-detail-content' for backward compat.
//
// applySgdbCover — POSTs the chosen URL, then:
//   hero/logo → reloads whichever detail pane opened the picker
//   v/h       → refreshes #library-content if it exists (library page only)

var _sgdbModal = null;
var _sgdbCurrentEntryId = null;
var _sgdbCurrentImageType = null;
var _sgdbCurrentDetailTarget = '#library-detail-content';
var _sgdbCurrentDetailId = null;
var _SGDB_LABELS = {v: 'vertical cover', h: 'horizontal cover', hero: 'hero image', logo: 'logo'};

// detailId: the id to reload the pane with once a cover is applied —
// entry.id for the library pane, but completion.id for the completion pane
// (a different primary key from the entry.id also passed in as entryId).
// Defaults to entryId for backward compat when omitted (library pane callers).
window.openSgdbPicker = function(entryId, imageType, detailTarget, detailId) {
  if (!_sgdbModal) {
    _sgdbModal = new bootstrap.Modal(document.getElementById('sgdbPickerModal'));
  }
  _sgdbCurrentEntryId = entryId;
  _sgdbCurrentImageType = imageType;
  _sgdbCurrentDetailTarget = detailTarget || '#library-detail-content';
  _sgdbCurrentDetailId = detailId || entryId;
  var label = _SGDB_LABELS[imageType] || imageType;
  document.getElementById('sgdbPickerModalLabel').textContent = 'Find ' + label;
  var grid = document.getElementById('sgdb-picker-grid');
  grid.innerHTML = '<p class="text-secondary"><small>Searching SteamGridDB&hellip;</small></p>';
  _sgdbModal.show();
  htmx.ajax('GET', '/integrations/steamgriddb/search?entry_id=' + entryId + '&image_type=' + imageType + '&page=0', {
    target: '#sgdb-picker-grid',
    swap: 'innerHTML',
  });
};

window.rerunSgdbSearch = function(entryId, imageType) {
  var term = (document.getElementById('sgdb-search-term') || {}).value || '';
  var url = '/integrations/steamgriddb/search?entry_id=' + entryId
    + '&image_type=' + imageType + '&page=0'
    + (term ? '&query=' + encodeURIComponent(term) : '');
  htmx.ajax('GET', url, {target: '#sgdb-picker-grid', swap: 'innerHTML'});
};

// cgtAfterArtReset — called from hx-on::after-request on the Reset art buttons.
// Reloads the detail pane (so the More menu and header visual update) and for
// v/h types also refreshes the library grid card (so the cover reverts too).
//
// detailId: the id to reload the pane with — entry.id for the library pane,
// but completion.id for the completion pane (a different primary key from
// the entry.id also passed in for the cover-override POST). Defaults to
// entryId for backward compat when omitted (library pane callers).
window.cgtAfterArtReset = function(entryId, imageType, detailTarget, detailId) {
  var target = detailTarget || '#library-detail-content';
  var isCompletionPane = target === '#completion-detail-content';
  var reloadId = detailId || entryId;
  var detailUrl = isCompletionPane ? '/completions/' + reloadId + '/detail' : '/library/entries/' + reloadId + '/detail';
  htmx.ajax('GET', detailUrl, {
    target: target,
    swap: 'innerHTML',
  });
  if (imageType === 'v' || imageType === 'h') {
    // v/h covers also appear in whichever list/grid is on the current page
    // — #library-content on the Library page, #completions-content on the
    // Completions page. Refresh whichever one actually exists.
    var gridId = document.getElementById('library-content') ? 'library-content' : (document.getElementById('completions-content') ? 'completions-content' : null);
    if (gridId) {
      htmx.ajax('GET', window.location.pathname + window.location.search, {
        target: '#' + gridId,
        swap: 'innerHTML',
        select: '#' + gridId,
      });
    }
  }
};

window.applySgdbCover = function(entryId, imageType, url) {
  var body = new URLSearchParams();
  body.append('image_type', imageType);
  body.append('url', url);
  fetch('/library/entries/' + entryId + '/cover-override', {
    method: 'POST',
    headers: {'Content-Type': 'application/x-www-form-urlencoded'},
    body: body.toString(),
  }).then(function(r) {
    if (!r.ok) return;
    if (_sgdbModal) _sgdbModal.hide();
    // Reload whichever detail pane opened the picker.
    // completion pane uses #completion-detail-content and its own endpoint,
    // keyed by completion.id (_sgdbCurrentDetailId), not entryId.
    var isCompletionPane = _sgdbCurrentDetailTarget === '#completion-detail-content';
    var detailUrl = isCompletionPane
      ? '/completions/' + _sgdbCurrentDetailId + '/detail'
      : '/library/entries/' + _sgdbCurrentDetailId + '/detail';
    htmx.ajax('GET', detailUrl, {
      target: _sgdbCurrentDetailTarget,
      swap: 'innerHTML',
    });
    if (imageType !== 'hero' && imageType !== 'logo') {
      // v/h covers also appear in whichever list/grid is on the current page
      // — #library-content on the Library page, #completions-content on the
      // Completions page. Refresh whichever one actually exists.
      var gridId = document.getElementById('library-content') ? 'library-content' : (document.getElementById('completions-content') ? 'completions-content' : null);
      if (gridId) {
        htmx.ajax('GET', window.location.pathname + window.location.search, {
          target: '#' + gridId,
          swap: 'innerHTML',
          select: '#' + gridId,
        });
      }
    }
  });
};

// ─── Dropdown fly-out submenu: flip up when there's no room below ─────────
// The Logo position submenu anchors to its trigger's top and grows downward;
// with the More menu living in the pane's pinned footer, the trigger is
// usually near the viewport bottom and the fly-out would clip. On hover,
// measure and flip to bottom-anchored when the space below can't fit it.
// Delegated on document so it survives HTMX pane swaps.
document.addEventListener('pointerover', function (e) {
  var li = e.target.closest ? e.target.closest('.cgt-dropdown-submenu') : null;
  if (!li) return;
  var menu = li.querySelector(':scope > .dropdown-menu');
  if (!menu) return;
  menu.classList.remove('cgt-submenu-up');
  var rect = li.getBoundingClientRect();
  var prev = menu.style.display;
  menu.style.display = 'block';
  var h = menu.offsetHeight;
  menu.style.display = prev;
  var spaceBelow = window.innerHeight - rect.top;
  var spaceAbove = rect.bottom;
  if (h + 8 > spaceBelow && spaceAbove > spaceBelow) {
    menu.classList.add('cgt-submenu-up');
  }
});

// ─── Tauri desktop shell integration ──────────────────────────────────────
// Inside the desktop app, window.__TAURI__ exists (withGlobalTauri + the
// remote-origin capability in desktop/src-tauri). Reveal the [data-tauri-only]
// affordances and wire the cookie-capture buttons; in a plain browser this
// whole block is inert and the manual-paste flow stays the only path.
(function () {
  if (!window.__TAURI__) return;
  document.addEventListener('DOMContentLoaded', function () {
    // Reveal desktop-only affordances; hide bits that only make sense in a
    // browser (the DevTools cookie-paste instructions and manual fields —
    // they stay in the DOM because the capture flow fills + submits them).
    document.querySelectorAll('[data-tauri-only]').forEach(function (el) {
      el.classList.remove('d-none');
    });
    document.querySelectorAll('[data-tauri-hide]').forEach(function (el) {
      el.classList.add('d-none');
    });
    _watchForCookieExpiryToasts();
  });

  // ── Stale-cookie auto-recovery ──
  // The job poller tags cookie-expiry failure toasts with
  // data-error-code="steam_cookies_expired" and a data-retry-url. When one
  // lands: swallow it, re-capture cookies in the WebView (usually silent —
  // the Steam login outlives the captured cookies), save them via the
  // cookies-only endpoint, and re-fire the failed operation. Guarded to one
  // attempt per 10 minutes so a genuinely dead Steam login degrades to the
  // normal failure toast instead of looping.
  var COOKIE_RETRY_GUARD_KEY = 'cgt-steam-cookie-retry-at';

  function _cookieRetryGuardActive() {
    var t = parseInt(sessionStorage.getItem(COOKIE_RETRY_GUARD_KEY) || '0', 10);
    return Date.now() - t < 10 * 60 * 1000;
  }

  async function _recoverSteamCookies(toastEl) {
    sessionStorage.setItem(COOKIE_RETRY_GUARD_KEY, String(Date.now()));
    var retryUrl = toastEl.getAttribute('data-retry-url');
    var label = toastEl.getAttribute('data-job-label') || 'sync';
    toastEl.remove();
    cgtToast('Steam session expired — refreshing cookies…', 'info');
    try {
      var cookies = await window.__TAURI__.core.invoke('capture_steam_login');
      var body = new FormData();
      body.append('steam_session_id', cookies.sessionid);
      body.append('steam_login_secure', cookies.steam_login_secure);
      var save = await fetch('/integrations/steam/cookies', { method: 'POST', body: body });
      if (!save.ok) throw new Error('saving refreshed cookies failed (HTTP ' + save.status + ')');
      if (retryUrl) {
        var retry = await fetch(retryUrl, { method: 'POST', headers: { 'HX-Request': 'true' } });
        if (!retry.ok) throw new Error('restarting ' + label + ' failed (HTTP ' + retry.status + ')');
        cgtToast('Cookies refreshed — ' + label + ' restarted.', 'info');
      } else {
        cgtToast('Cookies refreshed.', 'info');
      }
    } catch (err) {
      cgtToast('Automatic cookie refresh failed: ' + String(err) + ' — use the Steam configure page to re-capture manually.', 'error');
    }
  }

  function _watchForCookieExpiryToasts() {
    var container = document.getElementById('toast-container');
    if (!container) return;
    new MutationObserver(function (mutations) {
      mutations.forEach(function (m) {
        Array.prototype.forEach.call(m.addedNodes, function (node) {
          if (!(node instanceof Element)) return;
          var toast = node.matches('[data-error-code="steam_cookies_expired"]')
            ? node
            : node.querySelector('[data-error-code="steam_cookies_expired"]');
          if (!toast) return;
          if (_cookieRetryGuardActive()) return; // let the failure toast show
          _recoverSteamCookies(toast);
        });
      });
    }).observe(container, { childList: true });
  }

  // Opens the Steam sign-in window (Rust side), waits for the cookies, then
  // fills and submits the existing credentials form — save + flash + refresh
  // behave exactly as if the values were pasted by hand.
  // PSN mirror of the Steam capture: sign-in window → npsso token → fill
  // and submit the credentials form.
  window.cgtCapturePsnNpsso = async function (btn) {
    var original = btn.textContent;
    btn.disabled = true;
    btn.textContent = 'Waiting for PlayStation sign-in…';
    try {
      var token = await window.__TAURI__.core.invoke('capture_psn_login');
      var form = document.getElementById('psn-credentials-form');
      form.querySelector('[name="psn_npsso"]').value = token.npsso;
      htmx.trigger(form, 'submit');
    } catch (err) {
      var flash = document.getElementById('psn-flash');
      if (flash) {
        flash.innerHTML = '';
        var alert = document.createElement('div');
        alert.className = 'alert alert-warning py-2';
        var small = document.createElement('small');
        small.textContent = String(err);
        alert.appendChild(small);
        flash.appendChild(alert);
      }
    } finally {
      btn.disabled = false;
      btn.textContent = original;
    }
  };

  window.cgtCaptureSteamCookies = async function (btn) {
    var original = btn.textContent;
    btn.disabled = true;
    btn.textContent = 'Waiting for Steam sign-in…';
    try {
      var cookies = await window.__TAURI__.core.invoke('capture_steam_login');
      var form = document.getElementById('steam-credentials-form');
      form.querySelector('[name="steam_session_id"]').value = cookies.sessionid;
      form.querySelector('[name="steam_login_secure"]').value = cookies.steam_login_secure;
      htmx.trigger(form, 'submit');
    } catch (err) {
      var flash = document.getElementById('steam-flash');
      if (flash) {
        flash.innerHTML = '';
        var alert = document.createElement('div');
        alert.className = 'alert alert-warning py-2';
        var small = document.createElement('small');
        small.textContent = String(err);
        alert.appendChild(small);
        flash.appendChild(alert);
      }
    } finally {
      btn.disabled = false;
      btn.textContent = original;
    }
  };
})();
