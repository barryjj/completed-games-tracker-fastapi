# Vendored assets

Third-party files served from `/static/vendor/`, committed rather than fetched
so the desktop app works offline.

| file | version | source |
|---|---|---|
| `bootstrap.bundle.min.js` | 5.x | getbootstrap.com |
| `bootstrap.min.css` | 5.x | getbootstrap.com |
| `htmx.min.js` | 2.x | htmx.org |

## Local modification

The trailing `sourceMappingURL` comment has been **removed** from both Bootstrap
files. We don't ship the `.map` files (they're larger than the assets), so every
page load requested two that don't exist and logged 404s in the console — noise
that buries real errors, which matters because the desktop shell's devtools is
where anything actually gets diagnosed.

**Re-apply this after any re-vendor**, or the 404s come back:

    # bootstrap.bundle.min.js — drop the trailing //# sourceMappingURL=... line
    # bootstrap.min.css       — drop the trailing /*# sourceMappingURL=... */

Nothing else is patched; these are otherwise the upstream distributions.
