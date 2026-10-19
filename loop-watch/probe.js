// loop-watch, browser-режим. Выполнять во встроенном браузере на https://github.com (вход — аккаунт пользователя).
// Перед кодом подставить строку из `python watch.py cfg-js` (const CFG = …;).
// Один fetch на репозиторий: страница списка issue (сортировка по обновлению) содержит JSON со всеми issue.
const out = [];
for (const r of CFG.repos) {
  const res = await fetch(`/${r.repo}/issues?q=${encodeURIComponent('sort:updated-desc')}`, {credentials: 'include'});
  if (res.status !== 200) { out.push({repo: r.repo, error: res.status}); continue; }
  const html = await res.text();
  const m = html.match(/<script type="application\/json" data-target="react-app\.embeddedData">([\s\S]*?)<\/script>/);
  if (!m) { out.push({repo: r.repo, error: 'no-embedded-json (нет входа или GitHub сменил разметку)'}); continue; }
  const items = (JSON.parse(m[1]).payload?.issuesIndexContentRoute?.results || []).map(x => x.issue).filter(Boolean);
  for (const i of items) {
    if (r.issues.includes(i.number) || (r.report_new_issues && i.number > r.max_n)) {
      out.push({repo: r.repo, n: i.number, t: (i.title || '').slice(0, 80), u: i.updatedAt, c: i.commentCount || 0});
    }
  }
}
JSON.stringify(out);
