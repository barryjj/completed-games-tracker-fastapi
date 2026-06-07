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

// Local-time helper: render any element with [data-utc] in the browser's locale.
document.querySelectorAll('.local-time[data-utc]').forEach(function(el) {
  var d = new Date(el.dataset.utc + 'Z');
  el.textContent = d.toLocaleString(undefined, {month:'long', day:'numeric', year:'numeric', hour:'numeric', minute:'2-digit'});
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
var _SGDB_LABELS = {v: 'vertical cover', h: 'horizontal cover', hero: 'hero image', logo: 'logo'};

window.openSgdbPicker = function(entryId, imageType, detailTarget) {
  if (!_sgdbModal) {
    _sgdbModal = new bootstrap.Modal(document.getElementById('sgdbPickerModal'));
  }
  _sgdbCurrentEntryId = entryId;
  _sgdbCurrentImageType = imageType;
  _sgdbCurrentDetailTarget = detailTarget || '#library-detail-content';
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
window.cgtAfterArtReset = function(entryId, imageType, detailTarget) {
  var target = detailTarget || '#library-detail-content';
  htmx.ajax('GET', '/library/entries/' + entryId + '/detail', {
    target: target,
    swap: 'innerHTML',
  });
  if (imageType === 'v' || imageType === 'h') {
    var libraryContent = document.getElementById('library-content');
    if (libraryContent) {
      htmx.ajax('GET', window.location.pathname + window.location.search, {
        target: '#library-content',
        swap: 'innerHTML',
        select: '#library-content',
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
    // completion pane uses #completion-detail-content and its own endpoint.
    var isCompletionPane = _sgdbCurrentDetailTarget === '#completion-detail-content';
    var detailUrl = isCompletionPane
      ? '/completions/' + entryId + '/detail'
      : '/library/entries/' + entryId + '/detail';
    htmx.ajax('GET', detailUrl, {
      target: _sgdbCurrentDetailTarget,
      swap: 'innerHTML',
    });
    if (imageType !== 'hero' && imageType !== 'logo') {
      // v/h covers also appear in the library grid — refresh it if present
      // (not available on the completions page).
      var libraryContent = document.getElementById('library-content');
      if (libraryContent) {
        htmx.ajax('GET', window.location.pathname + window.location.search, {
          target: '#library-content',
          swap: 'innerHTML',
          select: '#library-content',
        });
      }
    }
  });
};
