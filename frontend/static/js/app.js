document.querySelectorAll('.local-time[data-utc]').forEach(function(el) {
  var d = new Date(el.dataset.utc + 'Z');
  el.textContent = d.toLocaleString(undefined, {month:'long', day:'numeric', year:'numeric', hour:'numeric', minute:'2-digit'});
});

// Auto-initialise any new toast elements after HTMX swaps content into the page.
// Servers push toasts via out-of-band swap into #toast-container; this picks them
// up, shows them with a 10s auto-hide, and removes them from the DOM on dismiss
// so the container doesn't bloat over time.
function _initNewToasts() {
  document.querySelectorAll('#toast-container .toast:not(.show)').forEach(function(el) {
    if (el.dataset.cgtInited) return;
    el.dataset.cgtInited = '1';
    var toast = new bootstrap.Toast(el, { delay: 10000 });
    el.addEventListener('hidden.bs.toast', function() { el.remove(); });
    toast.show();
  });
}

document.body.addEventListener('htmx:afterSettle', _initNewToasts);
// Also catch toasts rendered server-side on a fresh page load (rare, but possible).
document.addEventListener('DOMContentLoaded', _initNewToasts);
