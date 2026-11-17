/* Small shared rules for incremental updates and the clip editor. */
(function (root) {
  function mergeRows(current, changed, removed = []) {
    const deleted = new Set(removed), updates = new Map(changed.map(row => [row.id, row]));
    const result = current.filter(row => !deleted.has(row.id)).map(row => {
      const next = updates.get(row.id) || row; updates.delete(row.id); return next;
    });
    return result.concat([...updates.values()].filter(row => !deleted.has(row.id)));
  }
  function trim(start, end, min, max, handle) {
    if (![start, end, min, max].every(Number.isFinite) || max - min < 3) throw new Error('Choose valid trim times.');
    start = Math.max(min, Math.min(max - 3, start));
    end = Math.max(min + 3, Math.min(max, end));
    if (handle === 'start') start = Math.max(min, Math.min(end - 3, Math.max(end - 180, start)));
    else end = Math.min(max, Math.max(start + 3, Math.min(start + 180, end)));
    return {start, end};
  }
  function captionWords(words) {
    let previous = -1;
    return words.map(word => {
      const w = String(word.w || '').trim(), s = Number(word.s), e = Number(word.e);
      if (!Number.isFinite(s) || !Number.isFinite(e) || s < 0 || e <= s || s < previous - .01) throw new Error('Caption times must be in order and must not overlap.');
      previous = e;
      return {w, s, e};
    }).filter(word => word.w);
  }
  const api = {mergeRows, trim, captionWords};
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  else root.PeakCutTools = api;
})(globalThis);
