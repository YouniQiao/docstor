/* docstor docsify 插件：整站目录树侧边栏 / 锚点注入 / 右侧问题面板 / 语言切换 */
(function () {
  'use strict';

  var API = '/api';
  // 侧边栏宽度：先套用上次拖拽结果，避免刷新时闪一下
  try {
    var _w = parseInt(localStorage.getItem('ds.sidebarW') || '0', 10);
    if (_w >= 240 && _w <= 640) document.documentElement.style.setProperty('--ds-sidebar-w', _w + 'px');
  } catch (e) {}

  var state = {
    lang: 'cn', section: '', slug: '',
    anchors: {},        // title -> anchorId
    tree: null,         // /api/tree 结果
    treeLang: '',
    issues: [],         // 当前页问题
    activeRule: '',     // 右侧面板当前筛选
    expanded: {},       // 记住用户展开过的层级（换页后恢复）
    meta: {},           // 当前板块的 {slug: 更新时间}
    metaSec: null       // meta 对应的板块
  };

  window.__ds_state = state;

  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"]/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c];
    });
  }

  function parseRoute() {
    // #/cn/capabilities/abilitykit-overview
    var h = decodeURI(location.hash.replace(/^#\/?/, ''));
    var parts = h.split('/').filter(Boolean);
    // 从问题页点进来时地址是 #/cn/sec/slug#anchor，docsify 会把它规整成
    // ...slug?id=anchor —— 末尾的 ?id= / #anchor 不是 slug 的一部分
    if (parts.length) parts[parts.length - 1] = parts[parts.length - 1].split('?')[0].split('#')[0];
    state.lang = parts[0] || 'cn';
    state.section = parts[1] || '';
    state.slug = (parts[2] || '').replace(/\.md$/, '');
  }

  function fetchJSON(url) {
    return fetch(url, { credentials: 'same-origin' }).then(function (r) {
      if (!r.ok) throw new Error(r.status);
      return r.json();
    });
  }

  /* ---------------- 目录树侧边栏 ---------------- */

  function loadTree(retry) {
    retry = retry || 0;
    if (state.tree && state.treeLang === state.lang) return Promise.resolve();
    return fetchJSON(API + '/tree?lang=' + state.lang)
      .then(function (d) { state.tree = d; state.treeLang = state.lang; })
      .catch(function (e) {
        if (retry < 4) {
          return new Promise(function (res) { setTimeout(res, 400 * (retry + 1)); })
            .then(function () { return loadTree(retry + 1); });
        }
        window.__ds_err = 'tree:' + e.message;
      });
  }

  function issuesOf(secKey, slug) {
    if (!state.tree || !slug) return 0;
    return (state.tree.issues || {})[secKey + '/' + slug] || 0;
  }

  // 子树问题数（分组徽标）
  function issuesUnder(secKey, node) {
    var n = issuesOf(secKey, node.s);
    (node.c || []).forEach(function (k) { n += issuesUnder(secKey, k); });
    return n;
  }

  function badge(n) {
    var b = document.createElement('span');
    b.className = 'ds-badge';
    b.textContent = n;
    b.title = '该章节检查出 ' + n + ' 个问题';
    return b;
  }

  function makeNode(node, secKey) {
    var li = document.createElement('li');
    li.className = 'ds-node';
    li.dataset.key = node.i;

    var row = document.createElement('div');
    row.className = 'ds-row';

    var caret = document.createElement('span');
    caret.className = 'ds-caret';
    caret.textContent = '▸';
    if (node.c && node.c.length) {
      row.appendChild(caret);
    } else {
      caret.classList.add('blank');
      row.appendChild(caret);
    }

    var labelEl;
    if (node.s) {
      labelEl = document.createElement('a');
      labelEl.href = '#/' + state.lang + '/' + node.k + '/' + node.s;
      labelEl.dataset.slug = node.s;
    } else {
      labelEl = document.createElement('span');
      labelEl.className = 'ds-name';
    }
    labelEl.appendChild(document.createTextNode(node.n));
    row.appendChild(labelEl);

    var cnt = node.c && node.c.length ? issuesUnder(secKey, node) : issuesOf(secKey, node.s);
    if (cnt > 0) row.appendChild(badge(cnt));

    li.appendChild(row);

    if (node.c && node.c.length) {
      li._kids = node.c;
      li._sec = secKey;
      var sub = document.createElement('ul');
      sub.className = 'ds-sub';
      li.appendChild(sub);
      caret.onclick = function (ev) { ev.stopPropagation(); toggleLi(li); };
      row.onclick = function (ev) {
        if (ev.target.closest('.ds-caret')) return;   // 箭头自己处理
        // 统一行为：点名称=切换展开/收起；名称本身是链接的，浏览器照常跳转
        toggleLi(li);
      };
    }
    return li;
  }

  function toggleLi(li) {
    if (li.classList.contains('open')) {
      li.classList.remove('open');
      if (li.dataset.key) delete state.expanded[li.dataset.key];
      return;
    }
    openLi(li);
  }

  function openLi(li) {
    var sub = li.querySelector(':scope > ul');
    if (li._kids && sub && (!li.dataset.built || sub.childElementCount !== li._kids.length)) {
      sub.innerHTML = '';                       // 先清空，保证幂等（避免重复挂载）
      li._kids.forEach(function (k) { sub.appendChild(makeNode(k, li._sec)); });
      li.dataset.built = '1';
    }
    li.classList.add('open');
    if (li.dataset.key) state.expanded[li.dataset.key] = 1;
  }

  function renderSidebar() {
    var nav = document.querySelector('.sidebar-nav');
    if (!nav || !state.tree) return;
    // docsify 每次换页可能重建 .sidebar-nav，不能只信 dataset 标记
    if (nav.dataset.dsRendered === state.treeLang && nav.querySelector('.ds-tree')) return;

    nav.innerHTML = '';
    var ul = document.createElement('ul');
    ul.className = 'ds-tree';

    var home = document.createElement('li');
    home.className = 'ds-home';
    var ha = document.createElement('a');
    ha.href = '#/' + (state.lang === 'en' ? 'en' : 'cn');
    ha.textContent = state.lang === 'en' ? 'Developer Docs Center' : '开发者文档中心';
    home.appendChild(ha);
    ul.appendChild(home);

    var sections = (state.tree.sections || []);
    if (!sections.length) {
      var empty = document.createElement('li');
      empty.className = 'ds-empty-li';
      empty.textContent = '（该语言还没有镜像文档）';
      ul.appendChild(empty);
    }
    sections.forEach(function (sec) {
      var li = document.createElement('li');
      li.className = 'ds-sec';
      li.dataset.key = 'sec:' + sec.k;
      li._sec = sec.k;
      li._kids = sec.c || [];

      var row = document.createElement('div');
      row.className = 'ds-row';
      var caret = document.createElement('span');
      caret.className = 'ds-caret';
      caret.textContent = '▸';
      row.appendChild(caret);
      var name = document.createElement('span');
      name.className = 'ds-name';
      name.textContent = sec.n;
      row.appendChild(name);
      var total = 0;
      (sec.c || []).forEach(function (k) { total += issuesUnder(sec.k, k); });
      if (total > 0) row.appendChild(badge(total));

      li.appendChild(row);
      var sub = document.createElement('ul');
      sub.className = 'ds-sub';
      li.appendChild(sub);
      row.onclick = function () { toggleLi(li); };
      ul.appendChild(li);
    });

    nav.appendChild(ul);
    nav.dataset.dsRendered = state.treeLang;
    restoreExpanded();
  }

  // 换页后侧边栏会重建，把之前展开过的层级恢复回来
  function restoreExpanded() {
    for (var pass = 0; pass < 10; pass++) {
      var changed = false;
      document.querySelectorAll('.sidebar-nav li[data-key]').forEach(function (li) {
        if (state.expanded[li.dataset.key] && !li.classList.contains('open')) {
          openLi(li);
          changed = true;
        }
      });
      if (!changed) break;
    }
  }

  function findChain(nodes, slug) {
    for (var i = 0; i < (nodes || []).length; i++) {
      var n = nodes[i];
      if (n.s === slug) return [n];
      var sub = findChain(n.c, slug);
      if (sub) return [n].concat(sub);
    }
    return null;
  }

  function revealPath() {
    if (!state.tree || !state.section || !state.slug) return;
    var sec = null;
    (state.tree.sections || []).forEach(function (s) { if (s.k === state.section) sec = s; });
    if (!sec) return;
    var chain = findChain(sec.c, state.slug);
    if (!chain) return;

    var nav = document.querySelector('.sidebar-nav');
    var secLi = null;
    nav.querySelectorAll('li.ds-sec').forEach(function (li) {
      if (li._sec === state.section) secLi = li;
    });
    if (!secLi) return;

    openLi(secLi);
    var curUl = secLi.querySelector(':scope > ul');
    for (var i = 0; i < chain.length; i++) {
      var li = null;
      curUl.querySelectorAll(':scope > li.ds-node').forEach(function (x) {
        if (x.dataset.key === chain[i].i) li = x;
      });
      if (!li) break;
      if (i < chain.length - 1) {
        openLi(li);
        curUl = li.querySelector(':scope > ul');
      } else {
        li.classList.add('active');
        var a = li.querySelector('a');
        if (a) a.classList.add('active');
      }
    }
  }

  function updateBrand() {
    var h1 = document.querySelector('.sidebar > h1, .sidebar .app-name');
    if (h1) h1.textContent = state.lang === 'en' ? 'HarmonyOS Developers' : '鸿蒙开发者';
    if (state.lang === 'en') document.title = 'HarmonyOS Developer Documentation';
  }

  function sidebarNow() {
    updateBrand();
    if (!state.tree) return;
    renderSidebar();
    revealPath();
  }

  /* ---------- 锚点：把官方 anchorId 挂到对应标题上 ---------- */
  function loadAnchors() {
    if (!state.lang || !state.section) return Promise.resolve();
    var key = '/docs/' + state.lang + '/' + state.section + '/_anchors.json';
    return fetchJSON(key).then(function (data) {
      var doc = data[state.slug] || [];
      state.anchors = {};
      doc.forEach(function (a) { state.anchors[a.title] = a.anchorId; });
    }).catch(function () { state.anchors = {}; });
  }

  function injectAnchors() {
    var heads = document.querySelectorAll('.markdown-section h1,.markdown-section h2,.markdown-section h3,.markdown-section h4,.markdown-section h5');
    heads.forEach(function (h) {
      var t = (h.textContent || '').trim();
      var id = state.anchors[t];
      if (id) h.id = id;
    });
  }

  /* ---------- 侧边栏拖拽调宽 ---------- */
  function buildResizer() {
    if (document.querySelector('.ds-resizer')) return;
    var bar = document.createElement('div');
    bar.className = 'ds-resizer';
    bar.title = '拖动调整侧边栏宽度（双击恢复默认）';
    document.body.appendChild(bar);

    var dragging = false, startX = 0, startW = 0;
    function curW() {
      return parseInt(getComputedStyle(document.documentElement).getPropertyValue('--ds-sidebar-w'), 10) || 312;
    }
    function apply(w) {
      w = Math.max(240, Math.min(640, Math.round(w)));
      document.documentElement.style.setProperty('--ds-sidebar-w', w + 'px');
      return w;
    }
    bar.addEventListener('mousedown', function (ev) {
      dragging = true; startX = ev.clientX; startW = curW();
      bar.classList.add('dragging'); document.body.classList.add('ds-resizing');
      ev.preventDefault();
    });
    window.addEventListener('mousemove', function (ev) {
      if (!dragging) return;
      apply(startW + (ev.clientX - startX));
    });
    window.addEventListener('mouseup', function () {
      if (!dragging) return;
      dragging = false;
      bar.classList.remove('dragging'); document.body.classList.remove('ds-resizing');
      try { localStorage.setItem('ds.sidebarW', String(curW())); } catch (e) {}
    });
    bar.addEventListener('dblclick', function () {
      apply(312);
      try { localStorage.setItem('ds.sidebarW', '312'); } catch (e) {}
    });
    // 触屏也支持
    bar.addEventListener('touchstart', function (ev) {
      var t = ev.touches[0]; dragging = true; startX = t.clientX; startW = curW();
    }, { passive: true });
    window.addEventListener('touchmove', function (ev) {
      if (!dragging) return;
      apply(startW + (ev.touches[0].clientX - startX));
    }, { passive: true });
    window.addEventListener('touchend', function () {
      if (!dragging) return;
      dragging = false;
      try { localStorage.setItem('ds.sidebarW', String(curW())); } catch (e) {}
    });
  }

  /* ---------- 更新时间（官网式：标题下小字） ---------- */
  function loadMeta() {
    if (!state.lang || !state.section) return Promise.resolve();
    var want = state.lang + '/' + state.section;
    if (state.metaSec === want) return Promise.resolve();
    var key = '/docs/' + state.lang + '/' + state.section + '/_meta.json';
    return fetchJSON(key).then(function (d) {
      state.meta = d || {};
      state.metaSec = want;
    }).catch(function () { state.meta = {}; state.metaSec = want; });
  }

  function renderUpdated() {
    var old = document.querySelector('.ds-updated');
    if (old) old.remove();
    if (!state.slug) return;
    var h1 = document.querySelector('.markdown-section h1');
    var item = (state.meta || {})[state.slug];
    if (!h1 || !item) return;
    var t = typeof item === 'string' ? item : (item.t || '');
    var u = typeof item === 'object' && item ? item.u : '';
    if (!t && !u) return;
    var el = document.createElement('div');
    el.className = 'ds-updated';
    if (t) {
      var span = document.createElement('span');
      span.textContent = '更新时间：' + t;
      el.appendChild(span);
    }
    if (u) {
      if (t) {
        var dot = document.createElement('span');
        dot.className = 'ds-updated-sep';
        dot.textContent = '·';
        el.appendChild(dot);
      }
      var a = document.createElement('a');
      a.className = 'ds-src-link';
      a.href = u;
      a.target = '_blank';
      a.rel = 'noopener noreferrer';
      a.textContent = '原文链接 ↗';
      a.title = '在官网打开：' + u;
      el.appendChild(a);
    }
    h1.insertAdjacentElement('afterend', el);
  }


  /* ---------- 右侧 TOC（本页目录） ---------- */
  var _tocTick = false;

  function buildToc() {
    if (document.querySelector('.ds-toc')) return;
    var el = document.createElement('aside');
    el.className = 'ds-toc';
    el.innerHTML =
      '<div class="ds-toc-head"><span class="ds-toc-title">本页目录</span>' +
      '<button class="ds-toc-btn" id="ds-toc-toggle" title="折叠 / 展开目录">−</button></div>' +
      '<ul class="ds-toc-list" id="ds-toc-list"></ul>';
    document.body.appendChild(el);

    var btn = el.querySelector('#ds-toc-toggle');
    btn.onclick = function () {
      var collapse = !el.classList.contains('collapsed');
      el.classList.toggle('collapsed', collapse);
      document.body.classList.toggle('toc-collapsed', collapse);
      btn.textContent = collapse ? '+' : '−';
      try { localStorage.setItem('ds.tocCollapsed', collapse ? '1' : '0'); } catch (e) {}
    };
    var saved = false;
    try { saved = localStorage.getItem('ds.tocCollapsed') === '1'; } catch (e) {}
    if (saved) { el.classList.add('collapsed'); document.body.classList.add('toc-collapsed'); btn.textContent = '+'; }

    window.addEventListener('scroll', onTocScroll, { passive: true });
    window.addEventListener('resize', onTocScroll);
  }

  function onTocScroll() {
    if (_tocTick) return;
    _tocTick = true;
    requestAnimationFrame(function () {
      _tocTick = false;
      var heads = state.tocHeads || [];
      if (!heads.length) return;
      var cur = -1;
      for (var i = 0; i < heads.length; i++) {
        if (heads[i].getBoundingClientRect().top <= 120) cur = i; else break;
      }
      if (cur < 0) cur = 0;      // 页面顶部时默认高亮第一条
      var links = document.querySelectorAll('.ds-toc-list a');
      for (var j = 0; j < links.length; j++) links[j].classList.toggle('active', j === cur);
    });
  }

  function renderToc() {
    var el = document.querySelector('.ds-toc');
    if (!el) return;
    var list = el.querySelector('.ds-toc-list');
    list.innerHTML = '';
    state.tocHeads = [];

    var heads = [].slice.call(document.querySelectorAll('.markdown-section h2, .markdown-section h3'))
      .filter(function (h) { return (h.textContent || '').trim().length > 0; });

    if (!state.slug || heads.length < 2) {       // 首页或几乎没有小节时不显示
      el.style.display = 'none';
      document.body.classList.remove('toc-on');
      return;
    }
    el.style.display = '';
    document.body.classList.add('toc-on');

    heads.forEach(function (h, i) {
      if (!h.id) h.id = 'ds-h-' + i;
      var li = document.createElement('li');
      li.className = h.tagName.toLowerCase();
      var a = document.createElement('a');
      a.href = 'javascript:void(0)';
      a.textContent = h.textContent.trim();
      a.onclick = function (ev) {
        ev.preventDefault();
        var top = h.getBoundingClientRect().top + window.pageYOffset - 76;
        window.scrollTo({ top: top, behavior: 'smooth' });
      };
      li.appendChild(a);
      list.appendChild(li);
    });
    state.tocHeads = heads;
    onTocScroll();
  }

  /* ---------- 顶栏 ---------- */
  function buildTopbar() {
    if (document.querySelector('.ds-topbar')) return;
    var bar = document.createElement('div');
    bar.className = 'ds-topbar';
    // 文案全部交给 updateTopbar() 按当前语言刷新，这里只搭骨架
    bar.innerHTML =
      '<button class="ds-btn" id="ds-lang">EN</button>' +
      '<button class="ds-btn primary" id="ds-issues-btn">本页问题 <b>0</b></button>' +
      '<a class="ds-btn primary" id="ds-all-issues" href="/issues">全量问题</a>';
    document.body.appendChild(bar);

    document.getElementById('ds-lang').onclick = switchLang;
    document.getElementById('ds-issues-btn').onclick = function () { togglePanel(true); };
  }

  /* 中英切换：优先跳同篇文档的另一语言版本，该篇没有对应语言就回该语言首页 */
  function switchLang() {
    parseRoute();          // 404 页不会触发 doneEach，state 可能是上一页的，先按地址栏重算
    var to = state.lang === 'en' ? 'cn' : 'en';
    if (!state.slug) { location.hash = '#/' + to; return; }   // 首页直接换首页
    var target = '/docs/' + to + '/' + state.section + '/' + state.slug + '.md';
    fetch(target, { method: 'HEAD' })
      .then(function (r) {
        location.hash = r.ok ? '#/' + to + '/' + state.section + '/' + state.slug
                             : '#/' + to;
      })
      .catch(function () { location.hash = '#/' + to; });
  }

  function updateTopbar() {
    var en = state.lang === 'en';

    var lb = document.getElementById('ds-lang');
    if (lb) {
      lb.textContent = en ? '中文' : 'EN';
      lb.title = en ? '切换到中文' : 'Switch to English';
    }

    var btn = document.getElementById('ds-issues-btn');
    if (btn) {
      btn.innerHTML = (en ? 'Page issues' : '本页问题') + ' <b>' + issuesOf(state.section, state.slug) + '</b>';
      btn.style.display = state.slug ? '' : 'none';          // 首页没有“本页”
    }

    var all = document.getElementById('ds-all-issues');
    if (all) {
      all.textContent = en ? 'All issues' : '全量问题';
      all.href = '/issues' + (en ? '?lang=en' : '');         // 英文页跳到只看英文的问题
    }

    var ph = document.querySelector('.ds-panel-head h3');
    if (ph) ph.textContent = en ? 'Page issues' : '本页问题';
    var cl = document.getElementById('ds-close');
    if (cl) cl.textContent = en ? 'Close' : '关闭';
  }

  /* ---------- 右侧问题面板 ---------- */
  function buildPanel() {
    if (document.querySelector('.ds-panel')) return;
    var p = document.createElement('aside');
    p.className = 'ds-panel';
    p.innerHTML =
      '<div class="ds-panel-head"><h3>本页问题</h3>' +
      '<button class="ds-btn" id="ds-close">关闭</button></div>' +
      '<div class="ds-panel-filters" id="ds-filters"></div>' +
      '<div class="ds-panel-body" id="ds-panel-body"></div>';
    document.body.appendChild(p);
    document.getElementById('ds-close').onclick = function () { togglePanel(false); };
  }

  function togglePanel(open) {
    buildPanel();
    var p = document.querySelector('.ds-panel');
    p.classList.toggle('open', open);
    if (open) loadIssues();
  }

  function loadIssues() {
    if (!state.slug) return;
    var url = API + '/doc-issues?lang=' + state.lang + '&section=' + state.section + '&slug=' + encodeURIComponent(state.slug);
    fetchJSON(url).then(function (d) {
      state.issues = d.issues || [];
      renderFilters();
      renderIssues();
    }).catch(function (e) {
      document.getElementById('ds-panel-body').innerHTML = '<div class="ds-empty">加载失败：' + esc(e.message) + '</div>';
    });
  }

  function renderFilters() {
    var rules = {};
    state.issues.forEach(function (i) { var k = i.rule_label || i.rule; rules[k] = (rules[k] || 0) + 1; });
    var box = document.getElementById('ds-filters');
    var html = '<span class="ds-chip' + (state.activeRule ? '' : ' on') + '" data-rule="">全部 ' + state.issues.length + '</span>';
    Object.keys(rules).forEach(function (k) {
      html += '<span class="ds-chip' + (state.activeRule === k ? ' on' : '') + '" data-rule="' + esc(k) + '">' + esc(k) + ' ' + rules[k] + '</span>';
    });
    box.innerHTML = html;
    box.querySelectorAll('.ds-chip').forEach(function (c) {
      c.onclick = function () { state.activeRule = c.dataset.rule; renderFilters(); renderIssues(); };
    });
  }

  function renderIssues() {
    var body = document.getElementById('ds-panel-body');
    var list = state.issues.filter(function (i) {
      return !state.activeRule || (i.rule_label || i.rule) === state.activeRule;
    });
    if (!list.length) {
      body.innerHTML = '<div class="ds-empty">本页没有检查出问题 🎉</div>';
      return;
    }
    body.innerHTML = list.map(function (i, idx) {
      return '<div class="ds-issue sev-' + esc(i.severity) + '" data-i="' + idx + '">' +
        '<div class="t"><span class="k">' + esc(i.rule_label || i.rule) + '</span>' + esc(i.anchor_text || '页面级') + '</div>' +
        '<div class="m">' + esc(i.message) + '</div></div>';
    }).join('');
    body.querySelectorAll('.ds-issue').forEach(function (el) {
      el.onclick = function () { jumpTo(list[+el.dataset.i]); };
    });
  }

  function jumpTo(issue) {
    var loc = {};
    try { loc = JSON.parse(issue.locator || '{}'); } catch (e) { /* ignore */ }
    var target = loc.anchorId ? document.getElementById(loc.anchorId) : null;
    if (target) {
      target.scrollIntoView({ behavior: 'smooth', block: 'start' });
      target.classList.add('ds-flash');
      setTimeout(function () { target.classList.remove('ds-flash'); }, 1800);
    }
  }

  /* ---------- 生命周期 ---------- */
  var plugin = function (hook) {
    window.__ds_hook = hook;
    hook.doneEach(function () {
      window.__ds_done = (window.__ds_done || 0) + 1;
      parseRoute();
      if (state.slug) document.title = decodeURI(state.slug) + ' · docstor';

      loadTree().then(function () {
        sidebarNow();
        buildTopbar();
        updateTopbar();
        // docsify 有时晚一拍才建好 .sidebar-nav，补跑几次保证展开到当前页
        [120, 400, 900].forEach(function (ms) {
          setTimeout(function () { sidebarNow(); }, ms);
        });
      });

      loadMeta().then(function () { renderUpdated(); });

      loadAnchors().then(function () {
        injectAnchors();
        buildToc();
        renderToc();
        renderUpdated();
        if (document.querySelector('.ds-panel.open')) loadIssues();
      });
    });
    hook.mounted(function () { buildPanel(); buildResizer(); buildToc(); });
  };

  window.DocstorPlugin = plugin;
  window.$docsify = window.$docsify || {};
  window.$docsify.plugins = (window.$docsify.plugins || []).concat([plugin]);
})();
