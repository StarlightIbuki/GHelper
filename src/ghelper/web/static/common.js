/* ghelper — shared JS utilities */

function escapeHtml(v) {
    return String(v || '').replace(/[&<>"']/g, (m) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[m]));
}

function normalizeUrl(url) {
    const trimmed = String(url || '').trim().replace(/[?#].*$/, '').replace(/\/$/, '');
    // GitHub treats owner/repo as case-insensitive, so canonicalize that segment
    // to lowercase to match the server (_normalize_target_url) and keep tracked
    // PR comparisons consistent regardless of input casing.
    return trimmed.replace(/^(https?:\/\/github\.com\/)([^/\s]+\/[^/\s]+)(\/pull\/\d+)/i, (m, prefix, repo, suffix) => prefix + repo.toLowerCase() + suffix);
}

function extractRepoFromUrl(url) {
    const m = String(url || '').trim().match(/^https?:\/\/github\.com\/([^/]+\/[^/]+)\/pull\/\d+\/?$/i);
    return m ? m[1] : '';
}
