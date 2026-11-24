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

  var aliases = null;              // 旧 slug → [new_lang, new_section, new_slug]
  var aliasDone = {};              // 防重定向环

  function loadAliases() {
    if (aliases) return Promise.resolve(aliases);
    return fetchJSON(API + '/aliases').then(function (d) {
      aliases = (d && d.aliases) || {};
      return aliases;
    }).catch(function () { aliases = {}; return aliases; });
  }

  function maybeAliasRedirect() {
    if (!aliases || !state.slug) return false;
    var key = state.lang + '/' + state.section + '/' + state.slug;
    var to = aliases[key];
    if (!to || aliasDone[key]) return false;
    aliasDone[key] = 1;
    var qs = location.hash.indexOf('?') >= 0 ? location.hash.slice(location.hash.indexOf('?')) : '';
    location.replace('#/' + to[0] + '/' + to[1] + '/' + to[2] + qs);
    return true;
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
  // 隐藏节点（hided=1）只在「当前页所在的链路」上显示，与官网侧栏行为一致
  function kidVisible(node) {
    return !node.h || (state.chainKeys && state.chainKeys[node.i]);
  }

  function issuesUnder(secKey, node) {
    var n = issuesOf(secKey, node.s);
    (node.c || []).forEach(function (k) { if (kidVisible(k)) n += issuesUnder(secKey, k); });
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
    if (li._kids && sub && (!li.dataset.built || sub.childElementCount !== li._kids.filter(kidVisible).length)) {
      var sb = document.querySelector('.sidebar');
      var y = sb ? sb.scrollTop : 0;            // 兜底：万一还有瞬时塌陷
      var frag = document.createDocumentFragment();
      li._kids.filter(kidVisible).forEach(function (k) { frag.appendChild(makeNode(k, li._sec)); });
      if (sub.replaceChildren) sub.replaceChildren(frag);
      else { sub.innerHTML = ''; sub.appendChild(frag); }
      li.dataset.built = '1';
      if (sb && Math.abs(sb.scrollTop - y) > 1) sb.scrollTop = y;
    }
    li.classList.add('open');
    if (li.dataset.key) state.expanded[li.dataset.key] = 1;
  }

  function renderSidebar() {
    var nav = document.querySelector('.sidebar-nav');
    if (!nav || !state.tree) return;
    // docsify 每次换页可能重建 .sidebar-nav，不能只信 dataset 标记
    if (nav.dataset.dsRendered === state.treeLang && nav.querySelector('.ds-tree')) return;

    // 注意：不能先 nav.innerHTML='' 再逐个 append——中间那一刻侧栏高度是 0，
    // 浏览器会立刻把 .sidebar 的 scrollTop 掐成 0（用户反馈的「展开就回到顶部」就是这个）。
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

    if (nav.replaceChildren) nav.replaceChildren(ul);
    else { nav.innerHTML = ''; nav.appendChild(ul); }
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

  var revealScrolled = false;         // revealPath 是否主动把当前页带进可视区（是的话就别再还原旧位置）

  function revealPath() {
    revealScrolled = false;
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

  // 侧栏滚动位置「钉住」：换页时 docsify/浏览器会把 scrollTop 冲回 0，
  // 而这不是通过 JS 赋值做的（拦截不到），只能每帧盯一会儿把它按回去。
  var sbPin = {el: null, y: 0, until: 0, raf: 0, touched: false};
  (function () {
    function mark() { sbPin.touched = true; }
    // pointerdown 发生在跳转之前：趁这一刻记住用户看到的侧栏位置（后面 scrollTop 会被冲掉）
    ['pointerdown', 'mousedown'].forEach(function (t) {
      document.addEventListener(t, function (e) {
        var sb = document.querySelector('.sidebar');
        if (sb && e.target && e.target.closest && e.target.closest('.sidebar')) state.sbY = sb.scrollTop;
      }, {passive: true, capture: true});
    });
    ['wheel', 'touchmove', 'keydown'].forEach(function (t) {
      document.addEventListener(t, function (e) {
        if (e.target && e.target.closest && e.target.closest('.sidebar')) mark();
      }, {passive: true, capture: true});
    });
    // 用户自己滚侧栏时持续记录位置（钉住期间不记，免得把被冲掉的 0 当成用户意图）
    document.addEventListener('scroll', function (e) {
      var t = e.target;
      if (!t || !t.classList || !t.classList.contains('sidebar') || sbPin.raf) return;
      state.sbY = t.scrollTop;
    }, {passive: true, capture: true});
  })();

  function pinSidebarScroll(sb, y, ms) {
    if (!sb) return;
    sbPin.el = sb; sbPin.y = y; sbPin.touched = false;
    sbPin.until = (window.performance ? performance.now() : Date.now()) + (ms || 700);
    if (sbPin.raf) return;
    (function tick() {
      sbPin.raf = 0;
      var now = window.performance ? performance.now() : Date.now();
      var el = sbPin.el;
      if (el && el.isConnected && !sbPin.touched && Math.abs(el.scrollTop - sbPin.y) > 1) {
        el.scrollTop = sbPin.y;
      }
      if (now < sbPin.until) sbPin.raf = requestAnimationFrame(tick);
    })();
  }

  // 当前页这条在可视区外时才把它带到中间；在可视区里就什么都不做
  function activeInView(sb) {
    var a = document.querySelector('.sidebar-nav a.active');
    if (!a || !sb) return true;
    var r = a.getBoundingClientRect(), br = sb.getBoundingClientRect();
    return r.top >= br.top + 30 && r.bottom <= br.bottom - 30;
  }

  function ensureActiveVisible() {
    var a = document.querySelector('.sidebar-nav a.active');
    var sb = document.querySelector('.sidebar');
    if (!a || !sb || activeInView(sb)) return;
    var r = a.getBoundingClientRect(), br = sb.getBoundingClientRect();
    sb.scrollTop += r.top - br.top - sb.clientHeight / 2;
    sbPin.y = sb.scrollTop;
    revealScrolled = true;
  }

  function updateBrand() {
    var h1 = document.querySelector('.sidebar > h1, .sidebar .app-name');
    if (h1) h1.textContent = state.lang === 'en' ? 'HarmonyOS Developers' : '鸿蒙开发者';
    if (state.lang === 'en') document.title = 'HarmonyOS Developer Documentation';
  }

  // 当前页在树上的链路（含祖先），用于决定哪些隐藏节点要显示出来
  function computeChainKeys() {
    var keys = {};
    if (!state.tree || !state.section || !state.slug) return keys;
    var sec = null;
    (state.tree.sections || []).forEach(function (s) { if (s.k === state.section) sec = s; });
    if (!sec) return keys;
    (findChain(sec.c, state.slug) || []).forEach(function (n) { if (n && n.i) keys[n.i] = 1; });
    return keys;
  }

  function sidebarNow() {
    updateBrand();
    if (!state.tree) return;
    var sb = document.querySelector('.sidebar');
    // docsify 换页时会先清空 .sidebar-nav，那一瞬间高度归零、scrollTop 被浏览器掐成 0，
    // 等我们跑过来读就晚了。所以用 beforeEach 里提前存下的位置兜底（取两者较大值）。
    var y = Math.max(sb ? sb.scrollTop : 0, state.sbY || 0);
    state.chainKeys = computeChainKeys();
    renderSidebar();               // 换页时 docsify 会清空 .sidebar-nav，这里整树重建
    revealPath();                  // 沿途展开会把子树重建一遍（新树从收起开始，高度会缩一下）
    // 重建过程中 scrollTop 会被冲掉（而且不是 JS 赋的，拦不住），所以先还位再钉住；
    // 只有「当前页那一条不在可视区里」时才不钉，改成把它带进视野。
    if (sb && y > 0 && sb.scrollTop !== y) sb.scrollTop = y;
    if (sb && activeInView(sb)) {
      pinSidebarScroll(sb, y, 800);
    } else {
      requestAnimationFrame(function () { ensureActiveVisible(); pinSidebarScroll(sb, sbPin.y, 800); });
    }
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
      '<button class="ds-btn primary" id="ds-issues-btn">本页问题 <b>0</b></button>';
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
        '<div class="m">' + issueMsg(i) + '</div></div>';
    }).join('');
    body.querySelectorAll('.ds-issue').forEach(function (el) {
      el.onclick = function (ev) {
        if (ev.target.closest && ev.target.closest('a')) return;   // 点链接本身直接开官网
        jumpTo(list[+el.dataset.i]);
      };
    });
  }

  // 链接类问题：描述后面接「原文链接文字 + 原文地址」的超链接，与全量问题页一致
  function issueMsg(i) {
    var msg = String(i.message || '');
    if (!i.rule || i.rule.indexOf('link.') !== 0) return esc(msg);
    var loc = {};
    try { loc = JSON.parse(i.locator || '{}'); } catch (e) { /* ignore */ }
    var href = i.href || '', text = i.link_text || loc.target || '';
    if (!href) return esc(msg);
    var k = msg.lastIndexOf('：');
    var prefix = k > 0 ? msg.slice(0, k + 1) : msg;
    return esc(prefix) + '<a class="ds-issue-link" href="' + esc(href) +
      '" target="_blank" rel="noopener" title="' + esc(href) + '">' + esc(text) + '</a>';
  }

  // 优先跳到正文里那条链接本身，找不到再退到所在小节
  function normHref(h) {                       // docsify 会把站内链接改写成 #/<lang>/<sec>/<slug>?id=<锚点>
    return String(h || '').replace(/^#?\/*/, '/').split('?')[0].split('#')[0];
  }

  function findLinkEl(target) {
    var host = document.querySelector('.markdown-section') || document.body;
    var links = host.querySelectorAll('a[href]'), base = normHref(target);
    var frag = (target.split('#')[1] || '');
    var fallback = null;
    for (var i = 0; i < links.length; i++) {
      var h = links[i].getAttribute('href') || '';
      if (normHref(h) !== base) continue;
      if (!frag || h.indexOf(frag) >= 0) return links[i];   // 锚点也对得上就是它
      if (!fallback) fallback = links[i];
    }
    return fallback;
  }

  function jumpTo(issue) {
    var loc = {};
    try { loc = JSON.parse(issue.locator || '{}'); } catch (e) { /* ignore */ }
    var el = loc.target ? findLinkEl(loc.target) : null;
    if (!el && loc.anchorId) el = document.getElementById(loc.anchorId);
    if (!el) return;
    el.scrollIntoView({ behavior: 'smooth', block: 'center' });
    el.classList.add('ds-flash');
    setTimeout(function () { el.classList.remove('ds-flash'); }, 1800);
  }

  /* ---------- 生命周期 ---------- */
  loadAliases();                      // 提前拉别名表，首屏就位

  var plugin = function (hook) {
    window.__ds_hook = hook;
    hook.doneEach(function () {
      window.__ds_done = (window.__ds_done || 0) + 1;
      parseRoute();
      if (maybeAliasRedirect()) return;   // 旧 slug 先跳到新 slug，锚点才对得上
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
    // 换页渲染前先记下侧栏位置（docsify 清空 .sidebar-nav 会把它冲掉）
    hook.beforeEach(function () {
      var sb = document.querySelector('.sidebar');
      if (sb) state.sbY = sb.scrollTop;
      // 注意：不要 return 任何值——docsify 会拿返回值替换正文 markdown，返回 '' 会把整页变 404
    });
    hook.mounted(function () { buildPanel(); buildResizer(); buildToc(); });
  };

  window.DocstorPlugin = plugin;
  window.$docsify = window.$docsify || {};
  window.$docsify.plugins = (window.$docsify.plugins || []).concat([plugin]);
})();

/* ── 返回顶部：右下角按钮，滚动超过约一屏才淡入 ── */
(function () {
  function init() {
    var btn = document.getElementById('ds-top');
    if (!btn) return;
    // 宽屏滚的是 window/body，窄屏可能滚 .content，所以取三者最大值，不猜容器
    function curY() {
      var doc = document.scrollingElement || document.documentElement;
      var c = document.querySelector('.content');
      return Math.max(window.pageYOffset || 0, (doc && doc.scrollTop) || 0, c ? c.scrollTop : 0);
    }
    function upd() { btn.classList.toggle('on', curY() > 400); }
    // 用 CSS scroll-behavior: smooth 让浏览器自己做缓动（JS 的 behavior:'smooth' 在这套主题下不生效），
    // 420ms 后仍未到顶（后台标签页会跳过动画）就直接置 0 兜底
    function toTop() {
      if (curY() <= 0) return;
      var docEl = document.scrollingElement || document.documentElement;
      var c = document.querySelector('.content');
      if (docEl) docEl.style.scrollBehavior = 'smooth';
      if (c) c.style.scrollBehavior = 'smooth';
      window.scrollTo(0, 0);
      if (c) c.scrollTo(0, 0);
      setTimeout(function () {
        if (curY() > 0) {
          window.scrollTo(0, 0);
          if (docEl) docEl.scrollTop = 0;
          if (c) c.scrollTop = 0;
        }
        if (docEl) docEl.style.scrollBehavior = '';
        if (c) c.style.scrollBehavior = '';
      }, 420);
    }
    btn.addEventListener('click', toTop);
    document.addEventListener('scroll', upd, true);   // 捕获阶段才能收到 .content 的滚动事件
    window.addEventListener('resize', upd);
    setInterval(upd, 600);                            // 切页后滚动位置会被 docsify 重置
    upd();
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
  else init();
})();
