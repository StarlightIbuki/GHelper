/* gh-rerunner — shared JS utilities */

function escapeHtml(v) {
    return String(v || '').replace(/[&<>"']/g, (m) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[m]));
}

function normalizeUrl(url) {
    return String(url || '').trim().replace(/[?#].*$/, '').replace(/\/$/, '');
}

function extractRepoFromUrl(url) {
    const m = String(url || '').trim().match(/^https?:\/\/github\.com\/([^/]+\/[^/]+)\/pull\/\d+\/?$/i);
    return m ? m[1] : '';
}
