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
