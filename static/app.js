const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));
const CSRF = $('meta[name=csrf]').content;
const norm = s => (s || '').toLowerCase().replace(/[^a-z0-9]/g, '');
const keyOf = r => norm(r.manufacturer) + '|' + norm(r.mpn || r.name);

function floatList(input, load, onEmpty) {
  const box = document.createElement('div');
  box.className = 'list-group shadow suggest';
  box.hidden = true;
  document.body.append(box);
  let items = [], active = -1, timer, seq = 0;
  const choose = it => { box.hidden = true; it.pick ? it.pick() : (location.href = it.href); };
  const selectable = () => items.map((it, i) => it.header ? -1 : i).filter(i => i >= 0);
  const draw = () => {
    box.innerHTML = '';
    items.forEach((it, i) => {
      const el = document.createElement(it.header ? 'div' : 'a');
      el.className = it.header ? 'list-group-item small text-secondary bg-body-tertiary py-1' : 'list-group-item list-group-item-action d-flex gap-2 align-items-center' + (i === active ? ' active' : '');
      el.innerHTML = it.header || it.html;
      if (!it.header) { el.href = it.href || '#'; el.onmousedown = e => { e.preventDefault(); choose(it); }; }
      box.append(el);
    });
    const r = input.getBoundingClientRect();
    Object.assign(box.style, {left: r.left + scrollX + 'px', top: r.bottom + scrollY + 2 + 'px', width: Math.max(r.width, 300) + 'px'});
    box.hidden = !items.length;
  };
  const run = async () => { const id = ++seq; const list = await load(input.value.trim()); if (id === seq) { items = list; active = -1; draw(); } };
  input.addEventListener('input', () => { clearTimeout(timer); timer = setTimeout(run, 150); });
  input.addEventListener('focus', run);
  input.addEventListener('blur', () => setTimeout(() => box.hidden = true, 120));
  input.addEventListener('keydown', e => {
    const sel = selectable();
    if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
      if (box.hidden || !sel.length) return;
      e.preventDefault();
      const pos = sel.indexOf(active) + (e.key === 'ArrowDown' ? 1 : -1);
      active = sel[(pos + sel.length) % sel.length]; draw();
    } else if (e.key === 'Enter' && active >= 0 && !box.hidden) { e.preventDefault(); choose(items[active]); }
    else if (e.key === 'Escape') box.hidden = true;
  });
}

$$('[data-suggest]').forEach(input => {
  const pick = input.dataset.suggest === 'part';
  const idField = pick && $(input.dataset.id), newField = pick && $(input.dataset.newName);
  if (pick) input.form.addEventListener('submit', e => { if (!idField.value && !newField.value) { e.preventDefault(); input.focus(); } });
  if (pick) input.addEventListener('input', () => { idField.value = ''; newField.value = ''; });
  floatList(input, async q => {
    if (!q) return [];
    const d = await (await fetch('/api/suggest?q=' + encodeURIComponent(q))).json();
    const out = [];
    if (d.parts.length) out.push({header: 'Parts'});
    d.parts.forEach(p => out.push({
      html: `${p.img ? `<img class="thumb-xs" src="${esc(p.img)}" referrerpolicy="no-referrer" alt="">` : '<i class="bi bi-cpu thumb-xs text-center"></i>'}<span>${esc(p.name)}<small class="d-block text-secondary">${esc(p.sub)}</small></span>`,
      href: '/parts/' + p.id, pick: pick ? () => { input.value = p.name; idField.value = p.id; newField.value = ''; } : null}));
    if (!pick && d.categories.length) out.push({header: 'Categories'});
    if (!pick) d.categories.forEach(c => out.push({html: `<i class="bi bi-folder"></i>${esc(c.path)}`, href: '/parts?cat=' + c.id}));
    out.push(pick
      ? {html: `<i class="bi bi-plus-lg"></i>Create part “${esc(q)}”`, pick: () => { input.value = q; idField.value = ''; newField.value = q; }}
      : {html: `<i class="bi bi-search"></i>Search “${esc(q)}” including distributors`, pick: () => { input.value = q; input.form.submit(); }});
    return out;
  });
});

let categoryCache;
const loadCategories = async () => categoryCache ||= await (await fetch('/api/categories')).json();
$$('.cat-picker').forEach(el => {
  const allowNew = 'new' in el.dataset;
  el.innerHTML = `<div class="input-group"><input class="form-control" placeholder="${esc(el.dataset.placeholder || 'No category')}" autocomplete="off"><button type="button" class="btn btn-outline-secondary" title="Clear"><i class="bi bi-x-lg"></i></button></div><input type="hidden" name="${el.dataset.name || 'category_id'}">${allowNew ? '<input type="hidden" name="category_new">' : ''}`;
  const [input, clear] = [$('input', el), $('button', el)], [idField, newField] = $$('input[type=hidden]', el);
  el.setValue = (id, label) => { idField.value = id || ''; if (newField) newField.value = ''; input.value = label || ''; };
  el.setValue(el.dataset.value, el.dataset.label);
  clear.onclick = () => el.setValue('', '');
  floatList(input, async q => {
    const words = q.toLowerCase().split(/\s+/).filter(Boolean);
    const exclude = el.dataset.exclude ? el.dataset.exclude.split(',') : [];
    const hits = (await loadCategories()).filter(c => !exclude.includes(c.id) && words.every(w => c.path.toLowerCase().includes(w))).slice(0, 8);
    const out = hits.map(c => {
      const parts = c.path.split(' / '), name = parts.pop();
      return {html: `<span>${parts.length ? `<small class="text-secondary">${esc(parts.join(' / '))} /</small> ` : ''}${esc(name)}</span><small class="ms-auto text-secondary">${c.count}</small>`, pick: () => el.setValue(c.id, c.path)};
    });
    if (allowNew && q && !hits.some(c => c.path.toLowerCase() === q.toLowerCase()))
      out.push({html: `<i class="bi bi-plus-lg"></i>Create “${esc(q)}” <small class="text-secondary">(use “A / B” for nested)</small>`, pick: () => { idField.value = ''; newField.value = q; input.value = q + ' (new)'; }});
    return out;
  });
});

document.addEventListener('submit', e => { const m = e.target.dataset.confirm; if (m && !confirm(m)) e.preventDefault(); });
document.addEventListener('change', e => { if (e.target.dataset.autosubmit !== undefined) e.target.form.submit(); });
document.addEventListener('click', e => {
  const pdf = e.target.closest('[data-pdf]');
  if (pdf) { e.preventDefault(); $('#pdfTitle').textContent = pdf.dataset.title; $('#pdfFrame').src = pdf.dataset.pdf + '#toolbar=1&navpanes=0'; $('#pdfDownload').href = pdf.dataset.pdf + '?download=1'; $('#pdfTab').href = pdf.dataset.pdf; bootstrap.Modal.getOrCreateInstance('#pdfModal').show(); }
  const copy = e.target.closest('[data-copy]');
  if (copy) navigator.clipboard.writeText(copy.dataset.copy).then(() => { copy.innerHTML = '<i class="bi bi-check2"></i>'; });
  const thumb = e.target.closest('[data-gallery]');
  if (thumb) { $('#mainImg').src = thumb.src; $$('[data-gallery]').forEach(t => t.classList.toggle('active', t === thumb)); }
  const go = e.target.closest('[data-go-tab]');
  if (go) { e.preventDefault(); bootstrap.Tab.getOrCreateInstance($(`[data-bs-target="#tab-${go.dataset.goTab}"]`)).show(); }
});
$('#pdfModal')?.addEventListener('hidden.bs.modal', () => $('#pdfFrame').src = 'about:blank');

const hash = location.hash.slice(1), tabButton = hash && $(`[data-bs-target="#tab-${hash}"]`);
if (tabButton) bootstrap.Tab.getOrCreateInstance(tabButton).show();
$$('[data-bs-toggle=tab]').forEach(t => t.addEventListener('shown.bs.tab', () => history.replaceState(null, '', '#' + t.dataset.bsTarget.replace('#tab-', ''))));

$$('.dropzone').forEach(zone => {
  const input = $('input[type=file]', zone);
  ['dragover', 'dragenter'].forEach(n => zone.addEventListener(n, e => { e.preventDefault(); zone.classList.add('over'); }));
  ['dragleave', 'drop'].forEach(n => zone.addEventListener(n, () => zone.classList.remove('over')));
  zone.addEventListener('drop', e => { e.preventDefault(); input.files = e.dataTransfer.files; input.form.submit(); });
});

$('#addSpec')?.addEventListener('click', () => { const row = $('#specRow').content.cloneNode(true); $('#specBody').append(row); $$('#specBody input').at(-3).focus(); });
document.addEventListener('click', e => { if (e.target.closest('.del-row')) e.target.closest('tr').remove(); });

const lowest = g => { const m = {}; g.offers.forEach(o => o.breaks.forEach(b => { if (!(b.currency in m) || b.price < m[b.currency]) m[b.currency] = b.price; })); return Object.entries(m).map(([c, p]) => `${p} ${c}`).join(' / ') || 'No price'; };

function mergeResult(groups, r, meta) {
  r.offers.forEach(o => { o.source = r.provider; o.cached = meta.cached; o.fetched = meta.fetched; });
  const key = keyOf(r);
  const g = groups.get(key) || (groups.set(key, {...r, providers: [], offers: [], datasheets: [], specs: []}), groups.get(key));
  if (!g.providers.includes(r.provider)) g.providers.push(r.provider);
  g.offers.push(...r.offers);
  r.datasheets.forEach(d => g.datasheets.includes(d) || g.datasheets.push(d));
  r.specs.forEach(s => g.specs.some(x => x.name === s.name) || g.specs.push(s));
  g.image ||= r.image; g.description ||= r.description;
}

function importResult(g, project) {
  const form = $('#importForm');
  $('[name=data]', form).value = JSON.stringify(g);
  $('[name=project]', form).value = project || '';
  form.submit();
}

function showResult(g, project) {
  $('#resultBody').innerHTML = `<div class="row g-3"><div class="col-md-4">${g.image ? `<img class="img-fluid rounded" src="${esc(g.image)}" referrerpolicy="no-referrer" alt="">` : ''}</div><div class="col-md-8">
    <h5>${esc(g.name)}</h5><div class="text-secondary">${esc(g.manufacturer)} ${esc(g.mpn)}</div><p class="mt-2">${esc(g.description)}</p>
    ${g.datasheets.map(d => `<a class="btn btn-sm btn-outline-secondary me-1" target="_blank" rel="noopener noreferrer" href="${esc(d)}"><i class="bi bi-file-earmark-pdf"></i> Datasheet</a>`).join('')}</div></div>
    <div class="table-responsive mt-3"><table class="table table-sm"><thead><tr><th>Distributor</th><th>Price breaks</th><th>Stock</th><th></th></tr></thead>${g.offers.map(o => `<tr><td>${esc(o.distributor)}</td><td>${o.breaks.map(b => esc(`${b.qty}+: ${b.price} ${b.currency}`)).join('<br>') || 'No price'}</td><td>${o.stock ?? 'unknown'}</td><td>${/^https?:/.test(o.url) ? `<a target="_blank" rel="noopener noreferrer" href="${esc(o.url)}">Open</a>` : ''}<div class="small text-secondary">${o.cached ? 'cached' : 'live'} ${esc((o.fetched || '').slice(0, 16))}</div></td></tr>`).join('')}</table></div>
    ${g.specs.length ? `<table class="table table-sm">${g.specs.slice(0, 20).map(s => `<tr><th>${esc(s.name)}</th><td>${esc(s.value)} ${esc(s.unit)}</td></tr>`).join('')}</table>` : ''}`;
  $('#resultImport').textContent = project ? 'Import and add to project' : 'Import to library';
  $('#resultImport').onclick = () => importResult(g, project);
  bootstrap.Modal.getOrCreateInstance('#resultModal').show();
}

async function loadProviders({q, names, grid, status, skipKeys = [], project = '', limit = 0}) {
  const groups = new Map(), errors = [];
  let pending = names.length;
  const render = () => {
    $$('[data-ext]', grid).forEach(n => n.remove());
    let list = [...groups.entries()].filter(([k]) => !skipKeys.includes(k)).map(([, g]) => g).sort((a, b) => b.providers.length - a.providers.length);
    if (limit) list = list.slice(0, limit);
    list.forEach(g => {
      const col = document.createElement('div');
      col.className = 'col'; col.dataset.ext = '';
      col.innerHTML = `<div class="card h-100"><a href="#" class="card-img-wrap">${g.image ? `<img src="${esc(g.image)}" referrerpolicy="no-referrer" loading="lazy" alt="">` : '<i class="bi bi-cpu fs-1 text-secondary"></i>'}</a>
        <div class="card-body pb-2"><a href="#" class="fw-semibold text-decoration-none">${esc(g.name)}</a><div class="small text-secondary">${esc(g.manufacturer)}</div>
        <div class="small text-truncate">${esc(g.description)}</div></div>
        <div class="card-footer d-flex justify-content-between align-items-center small"><span><span class="badge text-bg-info me-1">Distributor</span>${g.datasheets.length ? '<i class="bi bi-file-earmark-pdf"></i> ' : ''}${esc(lowest(g))}</span>
        <button class="btn btn-sm btn-success">${project ? 'Import + add' : 'Import'}</button></div></div>`;
      $$('a', col).forEach(a => a.onclick = e => { e.preventDefault(); showResult(g, project); });
      $('button', col).onclick = () => importResult(g, project);
      grid.append(col);
    });
  };
  const finish = () => status.innerHTML = errors.map(e => `<div class="text-warning small">${esc(e)}</div>`).join('') || (groups.size ? '' : '<span class="text-secondary small">No distributor results.</span>');
  status.innerHTML = '<span class="text-secondary small"><span class="spinner-border spinner-border-sm"></span> Searching distributors…</span>';
  names.forEach(async n => {
    try {
      const r = await fetch(`/api/search/${n}?q=${encodeURIComponent(q)}`), d = await r.json();
      if (!r.ok) throw new Error(d.error || r.status);
      d.results.forEach(x => mergeResult(groups, x, d)); render();
    } catch (e) { errors.push(e.message); }
    if (--pending === 0) finish();
  });
}

const ext = $('#ext');
if (ext) loadProviders({q: ext.dataset.q, names: JSON.parse(ext.dataset.names), grid: ext, status: $('#extStatus'),
  skipKeys: JSON.parse(ext.dataset.skip || '[]').concat($$('[data-key]').map(n => n.dataset.key)), project: ext.dataset.project, limit: +ext.dataset.limit || 0});
