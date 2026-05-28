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
    if (entryId) window.cgtAutoFetchHero(img, parseInt(entryId, 10));
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
  var header = e.detail.target.querySelector('.offcanvas-header');
  if (!header) return;
  // Clean up any prior injection so the button reflects the current depth.
  var existing = header.querySelector('.cgt-pane-back');
  if (existing) existing.remove();
  if (stack.length <= 1) return;
  var btn = document.createElement('button');
  btn.type = 'button';
  btn.className = 'btn btn-sm btn-outline-secondary me-2 cgt-pane-back';
  btn.setAttribute('aria-label', 'Back to previous detail');
  btn.textContent = '←';
  btn.addEventListener('click', function() {
    stack.pop();  // remove current
    var prev = stack[stack.length - 1];
    var path = kind === 'library'
      ? '/library/entries/' + prev + '/detail'
      : '/completions/' + prev + '/detail';
    var targetId = kind === 'library' ? '#library-detail-content' : '#completion-detail-content';
    htmx.ajax('GET', path, { target: targetId, swap: 'innerHTML' });
  });
  // Insert before the title so the back arrow reads as the leftmost control.
  header.insertBefore(btn, header.firstChild);
});
