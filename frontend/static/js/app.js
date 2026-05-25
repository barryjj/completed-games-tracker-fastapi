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
