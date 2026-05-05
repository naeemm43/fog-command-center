#!/usr/bin/env python3
"""Assemble docs/index.html from the existing FOG facility map plus the
news + transaction comp tabs.

The original map HTML lives at ../fog_map_project/fog_facility_map.html (or
the path given by the FOG_MAP_HTML env var). It contains ~5.8 MB of inline
FOG_DATA / WWTP_DATA. We stream it through Python so those arrays never
have to be loaded into another tool's working memory.

This script is run once at repo init, and again any time the map data is
regenerated. The daily refresh script (refresh_data.py) only updates the
news + comp data blocks via marker replacement; it does not invoke this.
"""

from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC_MAP_DEFAULT = os.path.expanduser("~/fog_map_project/fog_facility_map.html")
SRC_MAP = os.environ.get("FOG_MAP_HTML", SRC_MAP_DEFAULT)
DST = os.path.join(ROOT, "docs", "index.html")
NEWS_JSON = os.path.join(ROOT, "data", "news_feed.json")
COMPS_JSON = os.path.join(ROOT, "data", "comp_database.json")


def slice_original(path: str) -> tuple[str, str, str]:
    """Return (style_inner, body_inner, scripts_block).

    style_inner: contents between <style> and </style> in the original.
    body_inner:  HTML between <body> and the first <script ...>; the map
                 div and floating panels live here.
    scripts_block: from the first <script ...> through the last </script>;
                 includes the leaflet CDN tags and the inline FOG_DATA /
                 WWTP_DATA / map-init code.
    """
    with open(path, encoding="utf-8") as f:
        html = f.read()

    m = re.search(r"<style>(.*?)</style>", html, re.DOTALL)
    style_inner = m.group(1) if m else ""

    body_start = html.find("<body>")
    if body_start < 0:
        raise RuntimeError("could not find <body> in original map HTML")
    body_start += len("<body>")
    first_script = html.find("<script", body_start)
    body_inner = html[body_start:first_script]

    end_close = html.rfind("</script>")
    scripts = html[first_script : end_close + len("</script>")]
    return style_inner, body_inner, scripts


def inject_map_handle(scripts: str) -> str:
    """Expose the Leaflet map instance globally so the tab switcher can
    call invalidateSize() when the map tab is shown."""
    last = scripts.rfind("</script>")
    return (
        scripts[:last]
        + "\nwindow.__fogMap = (typeof map !== 'undefined') ? map : null;\n"
        + "window.dispatchEvent(new Event('fogMapReady'));\n"
        + scripts[last:]
    )


TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8" />
<title>FOG Industry Command Center</title>
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css" crossorigin=""/>
<link rel="stylesheet" href="https://unpkg.com/leaflet.markercluster@1.5.3/dist/MarkerCluster.css" />
<link rel="stylesheet" href="https://unpkg.com/leaflet.markercluster@1.5.3/dist/MarkerCluster.Default.css" />
<link rel="stylesheet" href="https://unpkg.com/leaflet.fullscreen@3.0.2/Control.FullScreen.css" />
<style>
/* ============ Command center shell ============ */
:root {
  --topbar-bg: #1F3864;
  --topbar-text: #FFFFFF;
  --topbar-text-inactive: #8FAADC;
  --accent: #3498db;
  --target-green: #27ae60;
}
html, body {
  margin: 0; padding: 0; height: 100%;
  font-family: system-ui, -apple-system, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
  font-size: 13px; color: #222; background: #f6f7f9;
}
#topbar {
  position: fixed; top: 0; left: 0; right: 0;
  height: 52px; z-index: 5000;
  background: var(--topbar-bg); color: var(--topbar-text);
  display: flex; align-items: center; padding: 0 16px;
  box-shadow: 0 2px 8px rgba(0,0,0,0.18);
}
#topbar .title { font-size: 15px; font-weight: 600; letter-spacing: 0.2px; margin-right: 24px; }
#topbar nav.tabs { display: flex; height: 100%; }
.tab-btn {
  background: transparent; border: none; outline: none; cursor: pointer;
  color: var(--topbar-text-inactive);
  font-size: 13px; font-weight: 500;
  padding: 0 18px; height: 100%;
  border-bottom: 3px solid transparent;
  transition: color 0.12s, border-color 0.12s;
  font-family: inherit;
}
.tab-btn:hover { color: #fff; }
.tab-btn.active { color: #fff; border-bottom-color: var(--accent); }
#topbar .meta { margin-left: auto; font-size: 11px; color: var(--topbar-text-inactive); }
#topbar .meta b { color: #fff; font-weight: 600; }

.tab-content {
  display: none;
  position: absolute; top: 52px; left: 0; right: 0; bottom: 0;
  overflow: auto;
}
.tab-content.active { display: block; }
#content-map.tab-content { overflow: hidden; }

/* ============ News feed ============ */
#content-news .inner { padding: 16px; max-width: 920px; margin: 0 auto; }
.news-filter-bar {
  display: flex; gap: 8px; flex-wrap: wrap; align-items: center;
  margin-bottom: 16px;
}
.pill {
  display: inline-block; padding: 5px 12px; border: 1px solid #ccc;
  border-radius: 999px; font-size: 12px; cursor: pointer; user-select: none;
  background: #fff; color: #333;
}
.pill:hover { background: #eef; }
.pill.active { background: #1F3864; color: #fff; border-color: #1F3864; }
.search-box {
  padding: 6px 10px; border: 1px solid #ccc; border-radius: 4px; font-size: 13px;
  flex: 1; min-width: 200px;
  font-family: inherit;
}
.news-card {
  background: #fff; border: 1px solid #e2e2e2; border-left-width: 4px;
  border-radius: 4px; padding: 12px 14px; margin-bottom: 10px;
}
.news-card.cat-MA { border-left-color: #e74c3c; }
.news-card.cat-Regulation { border-left-color: #3498db; }
.news-card.cat-Market { border-left-color: #27ae60; }
.news-card.cat-CompanyNews { border-left-color: #f39c12; }
.news-card .meta { font-size: 11px; color: #888; margin-bottom: 4px; }
.news-card .meta .cat-tag {
  display: inline-block; padding: 2px 7px; border-radius: 3px;
  font-size: 10px; font-weight: 700; margin-right: 8px; color: #fff;
  letter-spacing: 0.3px;
}
.news-card .headline { font-size: 14px; font-weight: 600; margin-bottom: 6px; line-height: 1.35; }
.news-card .summary { color: #444; line-height: 1.5; margin-bottom: 6px; }
.news-card .source-link { font-size: 12px; color: #1F3864; text-decoration: none; }
.news-card .source-link:hover { text-decoration: underline; }
.news-card .target-alert {
  margin-top: 8px; padding: 6px 8px; background: #FDE8E8; border-radius: 3px;
  font-size: 12px; font-weight: 600; color: #b03030;
}
#news-load-more {
  display: block; margin: 12px auto; padding: 8px 18px;
  background: #fff; border: 1px solid #ccc; border-radius: 4px;
  cursor: pointer; font-family: inherit; font-size: 12px;
}
#news-load-more:hover { background: #eef; }
#news-count { font-size: 11px; color: #888; margin-bottom: 8px; }

/* ============ Transaction comps ============ */
#content-comps .inner { padding: 16px; }
.summary-bar {
  background: #fff; border: 1px solid #e2e2e2; border-radius: 4px;
  padding: 10px 14px; margin-bottom: 12px; font-size: 13px;
}
.summary-bar .stat { margin-right: 18px; display: inline-block; }
.summary-bar .stat b { color: #1F3864; }
.controls-row {
  display: flex; gap: 8px; flex-wrap: wrap; margin-bottom: 12px;
  align-items: center;
}
.controls-row select, .controls-row input[type=text] {
  padding: 6px 8px; border: 1px solid #ccc; border-radius: 4px; font-size: 13px;
  font-family: inherit;
}
.controls-row label { font-size: 12px; user-select: none; }
.export-btn {
  margin-left: auto; padding: 7px 14px; background: #1F3864; color: #fff;
  border: none; border-radius: 4px; cursor: pointer; font-size: 12px;
  font-family: inherit;
}
.export-btn:hover { background: #16294a; }
.comps-table-wrap {
  overflow-x: auto; background: #fff; border-radius: 4px; border: 1px solid #e2e2e2;
}
.comps-table { width: 100%; border-collapse: collapse; font-size: 12px; }
.comps-table th {
  background: #f3f5f8; padding: 8px 10px; text-align: left;
  border-bottom: 2px solid #d8d8d8; cursor: pointer; user-select: none;
  font-weight: 600; white-space: nowrap;
}
.comps-table th:hover { background: #e9ecf1; }
.comps-table th .arrow { font-size: 10px; color: #888; margin-left: 3px; }
.comps-table td { padding: 6px 10px; border-bottom: 1px solid #eee; vertical-align: top; }
.comps-table tr.row-target-market td:first-child { border-left: 3px solid var(--target-green); }
.comps-table tbody tr:not(.expanded-row):nth-of-type(2n) td { background: #fafbfc; }
.comps-table tr.expanded-row td { background: #f6f9fc !important; }
.comps-table .detail-cell { padding: 12px 14px; line-height: 1.55; }
.comps-table .row-clickable { cursor: pointer; }
.comps-table .row-clickable:hover td { background: #eef3f8 !important; }
#row-count { font-size: 11px; color: #777; margin-top: 6px; }

/* ============ Original map styles (scoped to map tab) ============ */
__ORIGINAL_STYLE__
</style>
</head>
<body>

<header id="topbar">
  <span class="title">FOG Industry Command Center</span>
  <nav class="tabs">
    <button class="tab-btn" data-tab="news">📰 News</button>
    <button class="tab-btn" data-tab="comps">📊 Transactions</button>
    <button class="tab-btn active" data-tab="map">🗺️ Map</button>
  </nav>
  <span class="meta">
    Last refreshed: <b id="meta-refreshed">—</b>
    &nbsp;·&nbsp; Deals: <b id="meta-deals">—</b>
    &nbsp;·&nbsp; News: <b id="meta-news">—</b>
  </span>
</header>

<!-- ============ Tab 1: News Feed ============ -->
<section id="content-news" class="tab-content">
  <div class="inner">
    <div class="news-filter-bar">
      <span class="pill news-pill active" data-cat="All">All</span>
      <span class="pill news-pill" data-cat="M&A">M&amp;A</span>
      <span class="pill news-pill" data-cat="Regulation">Regulation</span>
      <span class="pill news-pill" data-cat="Market">Market</span>
      <span class="pill news-pill" data-cat="Company News">Company News</span>
      <input id="news-search" class="search-box" type="text" placeholder="Search headlines..." />
    </div>
    <div id="news-count"></div>
    <div id="news-list"></div>
    <button id="news-load-more" style="display:none;">Load more</button>
  </div>
</section>

<!-- ============ Tab 2: Transaction Comps ============ -->
<section id="content-comps" class="tab-content">
  <div class="inner">
    <div id="comp-summary" class="summary-bar"></div>
    <div class="controls-row">
      <select id="comp-platform">
        <option value="All">All Platforms</option>
        <option>LES</option>
        <option>Wind River</option>
        <option>Darling</option>
        <option>Baker</option>
        <option>Other</option>
      </select>
      <select id="comp-deal-type">
        <option value="All">All Deal Types</option>
        <option>Platform</option>
        <option>Add-On</option>
        <option>Strategic</option>
        <option>Division Sale</option>
      </select>
      <select id="comp-year">
        <option value="All">All Years</option>
        <option>Earlier</option>
      </select>
      <label><input type="checkbox" id="comp-target-only" /> Target markets only</label>
      <input id="comp-search" type="text" placeholder="Search..." />
      <button id="comp-export" class="export-btn">📥 Download CSV</button>
    </div>
    <div class="comps-table-wrap">
      <table class="comps-table">
        <thead>
          <tr>
            <th data-sort="date">Date <span class="arrow">↕</span></th>
            <th data-sort="target">Target <span class="arrow">↕</span></th>
            <th data-sort="acquirer">Acquirer <span class="arrow">↕</span></th>
            <th data-sort="sponsor">Sponsor <span class="arrow">↕</span></th>
            <th data-sort="location">Location <span class="arrow">↕</span></th>
            <th data-sort="deal_type">Type <span class="arrow">↕</span></th>
            <th data-sort="deal_size">Deal Size <span class="arrow">↕</span></th>
            <th data-sort="services">Services <span class="arrow">↕</span></th>
          </tr>
        </thead>
        <tbody id="comps-tbody"></tbody>
      </table>
    </div>
    <div id="row-count"></div>
  </div>
</section>

<!-- ============ Tab 3: Interactive Map ============ -->
<section id="content-map" class="tab-content active">
__BODY_MAP_INNER__
</section>

<!-- ============ Embedded data blocks (refreshed daily) ============ -->
<!-- METADATA_START -->
<script>
const metadata = __METADATA_JSON__;
</script>
<!-- METADATA_END -->

<!-- NEWS_FEED_DATA_START -->
<script>
const newsFeedData = __NEWS_FEED_JSON__;
</script>
<!-- NEWS_FEED_DATA_END -->

<!-- COMP_DATABASE_DATA_START -->
<script>
const compDatabaseData = __COMP_DATABASE_JSON__;
</script>
<!-- COMP_DATABASE_DATA_END -->

<!-- ============ Original map scripts (Leaflet + FOG_DATA + WWTP_DATA + init) ============ -->
__MAP_SCRIPTS__

<!-- ============ Command-center shell scripts ============ -->
<script>
(function () {
  // ---------- Tab switcher ----------
  function switchTab(tabName) {
    document.querySelectorAll('.tab-content').forEach(el => el.classList.remove('active'));
    document.getElementById('content-' + tabName).classList.add('active');
    document.querySelectorAll('.tab-btn').forEach(el => el.classList.remove('active'));
    document.querySelector('[data-tab="' + tabName + '"]').classList.add('active');
    if (tabName === 'map' && window.__fogMap) {
      setTimeout(function () { window.__fogMap.invalidateSize(); }, 150);
    }
  }
  document.querySelectorAll('.tab-btn').forEach(function (btn) {
    btn.addEventListener('click', function () { switchTab(btn.dataset.tab); });
  });

  // ---------- Helpers ----------
  function escapeHtml(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
      return ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'})[c];
    });
  }
  function formatDate(s) {
    if (!s) return '';
    var d = new Date(s + (s.length === 10 ? 'T00:00:00' : ''));
    if (isNaN(d.getTime())) return s;
    return d.toLocaleDateString('en-US', { year: 'numeric', month: 'short', day: 'numeric' });
  }
  function normCat(s) {
    return String(s || '').toLowerCase().replace(/\s+/g, '').replace(/&/g, 'and');
  }

  // ---------- Metadata header ----------
  if (typeof metadata !== 'undefined') {
    var d = metadata.lastRefreshed ? new Date(metadata.lastRefreshed) : null;
    document.getElementById('meta-refreshed').textContent =
      d && !isNaN(d.getTime()) ? d.toLocaleString() : (metadata.lastRefreshed || '—');
    document.getElementById('meta-deals').textContent =
      (typeof compDatabaseData !== 'undefined') ? compDatabaseData.length : (metadata.compCount || '—');
    document.getElementById('meta-news').textContent =
      (typeof newsFeedData !== 'undefined') ? newsFeedData.length : (metadata.newsCount || '—');
  }

  // ---------- News tab ----------
  var newsActiveCategory = 'All';
  var newsSearch = '';
  var newsLimit = 100;

  function renderNews() {
    var container = document.getElementById('news-list');
    var data = (typeof newsFeedData !== 'undefined') ? newsFeedData : [];
    var items = data
      .filter(function (n) { return newsActiveCategory === 'All' || normCat(n.category) === normCat(newsActiveCategory); })
      .filter(function (n) {
        if (!newsSearch) return true;
        var q = newsSearch.toLowerCase();
        return (n.headline || '').toLowerCase().indexOf(q) >= 0 ||
               (n.summary || '').toLowerCase().indexOf(q) >= 0;
      })
      .sort(function (a, b) { return (b.date || '').localeCompare(a.date || ''); });

    var visible = items.slice(0, newsLimit);
    if (visible.length === 0) {
      container.innerHTML = '<div style="text-align:center; padding:40px; color:#888;">News feed is being populated. Data refreshes daily at 7 AM CT.</div>';
      document.getElementById('news-count').textContent = '0 items';
      document.getElementById('news-load-more').style.display = 'none';
      return;
    }

    var catBg = {'M&A':'#e74c3c','Regulation':'#3498db','Market':'#27ae60','Company News':'#f39c12'};
    var catCls = {'M&A':'cat-MA','Regulation':'cat-Regulation','Market':'cat-Market','Company News':'cat-CompanyNews'};
    container.innerHTML = visible.map(function (n) {
      var cat = n.category || 'Company News';
      var bg = catBg[cat] || '#888';
      var cls = catCls[cat] || 'cat-CompanyNews';
      var alert = n.is_target_market
        ? '<div class="target-alert">⚠️ TIER 2 ALERT: near ' + escapeHtml(n.target_market_name || 'target market') + '</div>'
        : '';
      var src = n.source_url
        ? '<a class="source-link" href="' + escapeHtml(n.source_url) + '" target="_blank" rel="noopener">Source: ' + escapeHtml(n.source || 'link') + ' →</a>'
        : (n.source ? '<span class="source-link">Source: ' + escapeHtml(n.source) + '</span>' : '');
      return '<div class="news-card ' + cls + '">' +
        '<div class="meta"><span class="cat-tag" style="background:' + bg + '">' + escapeHtml(cat) + '</span>' + escapeHtml(formatDate(n.date)) + '</div>' +
        '<div class="headline">' + escapeHtml(n.headline || '') + '</div>' +
        '<div class="summary">' + escapeHtml(n.summary || '') + '</div>' +
        src + alert +
        '</div>';
    }).join('');

    document.getElementById('news-count').textContent = 'Showing ' + visible.length + ' of ' + items.length + ' items';
    var moreBtn = document.getElementById('news-load-more');
    if (items.length > visible.length) {
      moreBtn.style.display = 'block';
      moreBtn.textContent = 'Load more (' + (items.length - visible.length) + ' older)';
    } else {
      moreBtn.style.display = 'none';
    }
  }

  document.querySelectorAll('.news-pill').forEach(function (p) {
    p.addEventListener('click', function () {
      newsActiveCategory = p.dataset.cat;
      document.querySelectorAll('.news-pill').forEach(function (x) { x.classList.toggle('active', x === p); });
      newsLimit = 100;
      renderNews();
    });
  });
  document.getElementById('news-search').addEventListener('input', function (e) {
    newsSearch = e.target.value; renderNews();
  });
  document.getElementById('news-load-more').addEventListener('click', function () {
    newsLimit += 100; renderNews();
  });

  // ---------- Comps tab ----------
  var compsState = {
    platform: 'All', dealType: 'All', year: 'All', targetOnly: false, search: '',
    sortKey: 'date', sortDir: 'desc',
    expandedRows: new Set()
  };

  function platformOf(deal) {
    var a = (deal.acquirer || '').toLowerCase();
    if (a.indexOf('wind river') >= 0) return 'Wind River';
    if (a.indexOf('liquid environmental') >= 0 || a.indexOf('(les)') >= 0 || /^les\b/.test(a)) return 'LES';
    if (a.indexOf('darling') >= 0) return 'Darling';
    if (a.indexOf('baker') >= 0) return 'Baker';
    return 'Other';
  }
  function compYear(d) { return (d.date || '').slice(0, 4); }

  function filteredComps() {
    var data = (typeof compDatabaseData !== 'undefined') ? compDatabaseData : [];
    return data.filter(function (d) {
      if (compsState.platform !== 'All' && platformOf(d) !== compsState.platform) return false;
      if (compsState.dealType !== 'All' && d.deal_type !== compsState.dealType) return false;
      if (compsState.year !== 'All') {
        if (compsState.year === 'Earlier') {
          if (parseInt(compYear(d), 10) >= 2020) return false;
        } else if (compYear(d) !== compsState.year) return false;
      }
      if (compsState.targetOnly && !d.is_target_market) return false;
      if (compsState.search) {
        var q = compsState.search.toLowerCase();
        var hay = [d.target, d.acquirer, d.sponsor, d.location, d.services, d.notes].join(' ').toLowerCase();
        if (hay.indexOf(q) < 0) return false;
      }
      return true;
    }).sort(function (a, b) {
      var k = compsState.sortKey;
      var av = a[k] == null ? '' : String(a[k]);
      var bv = b[k] == null ? '' : String(b[k]);
      if (av < bv) return compsState.sortDir === 'asc' ? -1 : 1;
      if (av > bv) return compsState.sortDir === 'asc' ? 1 : -1;
      return 0;
    });
  }

  function renderComps() {
    var data = (typeof compDatabaseData !== 'undefined') ? compDatabaseData : [];
    var total = data.length;
    var platforms = data.filter(function (d) { return d.deal_type === 'Platform'; }).length;
    var addons = data.filter(function (d) { return d.deal_type === 'Add-On'; }).length;
    var others = total - platforms - addons;
    var tm = data.filter(function (d) { return d.is_target_market; }).length;
    document.getElementById('comp-summary').innerHTML =
      '<span class="stat"><b>Total Deals:</b> ' + total + '</span>' +
      '<span class="stat"><b>Platform:</b> ' + platforms + '</span>' +
      '<span class="stat"><b>Add-On:</b> ' + addons + '</span>' +
      '<span class="stat"><b>Other:</b> ' + others + '</span>' +
      '<span class="stat"><b>In Target Markets:</b> ' + tm + '</span>';

    var rows = filteredComps();
    var tbody = document.getElementById('comps-tbody');
    tbody.innerHTML = rows.map(function (d, i) { return renderRow(d, i); }).join('');
    document.getElementById('row-count').textContent = 'Showing ' + rows.length + ' of ' + total + ' transactions';

    tbody.querySelectorAll('tr.row-clickable').forEach(function (tr) {
      tr.addEventListener('click', function () { toggleRow(parseInt(tr.dataset.idx, 10)); });
    });
  }

  function renderRow(d, i) {
    var tmCls = d.is_target_market ? 'row-target-market' : '';
    var dot = d.is_target_market ? '🎯 ' : '';
    var html = '<tr class="row-clickable ' + tmCls + '" data-idx="' + i + '">' +
      '<td>' + escapeHtml(d.date || '') + '</td>' +
      '<td><b>' + dot + escapeHtml(d.target || '') + '</b></td>' +
      '<td>' + escapeHtml(d.acquirer || '') + '</td>' +
      '<td>' + escapeHtml(d.sponsor || '—') + '</td>' +
      '<td>' + escapeHtml(d.location || '') + '</td>' +
      '<td>' + escapeHtml(d.deal_type || '') + '</td>' +
      '<td>' + escapeHtml(d.deal_size || '') + '</td>' +
      '<td>' + escapeHtml(d.services || '') + '</td>' +
      '</tr>';
    if (compsState.expandedRows.has(i)) {
      html += '<tr class="expanded-row"><td colspan="8" class="detail-cell">' +
        (d.source_url ? '<div><b>Source:</b> <a href="' + escapeHtml(d.source_url) + '" target="_blank" rel="noopener">' + escapeHtml(d.source || 'link') + ' ↗</a></div>' : '') +
        (d.multiple ? '<div><b>Multiple:</b> ' + escapeHtml(d.multiple) + '</div>' : '') +
        (d.notes ? '<div style="margin-top:6px;"><b>Notes:</b> ' + escapeHtml(d.notes) + '</div>' : '') +
        (d.is_target_market ? '<div style="margin-top:6px; color:#27ae60;"><b>Target market:</b> ' + escapeHtml(d.target_market_name || 'flagged') + '</div>' : '') +
        '</td></tr>';
    }
    return html;
  }

  function toggleRow(i) {
    if (compsState.expandedRows.has(i)) compsState.expandedRows.delete(i);
    else compsState.expandedRows.add(i);
    renderComps();
  }

  function setSort(k) {
    if (compsState.sortKey === k) {
      compsState.sortDir = compsState.sortDir === 'asc' ? 'desc' : 'asc';
    } else {
      compsState.sortKey = k;
      compsState.sortDir = 'desc';
    }
    renderComps();
  }

  function csvCell(v) {
    if (v === null || v === undefined) return '';
    var s = String(v);
    if (/[",\n]/.test(s)) return '"' + s.replace(/"/g, '""') + '"';
    return s;
  }
  function exportCsv() {
    var rows = filteredComps();
    var cols = ['date','target','acquirer','sponsor','location','deal_type','deal_size','multiple','services','source','source_url','is_target_market','target_market_name','notes'];
    var csv = [cols.join(',')].concat(rows.map(function (r) {
      return cols.map(function (c) { return csvCell(r[c]); }).join(',');
    })).join('\n');
    var blob = new Blob([csv], { type: 'text/csv' });
    var url = URL.createObjectURL(blob);
    var a = document.createElement('a');
    a.href = url; a.download = 'fog_comps_' + (new Date()).toISOString().slice(0,10) + '.csv';
    document.body.appendChild(a); a.click(); document.body.removeChild(a);
    URL.revokeObjectURL(url);
  }

  // populate year dropdown from data
  (function () {
    var data = (typeof compDatabaseData !== 'undefined') ? compDatabaseData : [];
    var years = {};
    data.forEach(function (d) { var y = compYear(d); if (y) years[y] = true; });
    var yearList = Object.keys(years).sort().reverse();
    var sel = document.getElementById('comp-year');
    var earlierOpt = sel.querySelector('option[value="Earlier"]');
    yearList.forEach(function (y) {
      if (parseInt(y, 10) < 2020) return;
      var o = document.createElement('option');
      o.value = y; o.textContent = y;
      sel.insertBefore(o, earlierOpt);
    });
  })();

  document.getElementById('comp-platform').addEventListener('change', function (e) { compsState.platform = e.target.value; renderComps(); });
  document.getElementById('comp-deal-type').addEventListener('change', function (e) { compsState.dealType = e.target.value; renderComps(); });
  document.getElementById('comp-year').addEventListener('change', function (e) { compsState.year = e.target.value; renderComps(); });
  document.getElementById('comp-target-only').addEventListener('change', function (e) { compsState.targetOnly = e.target.checked; renderComps(); });
  document.getElementById('comp-search').addEventListener('input', function (e) { compsState.search = e.target.value; renderComps(); });
  document.getElementById('comp-export').addEventListener('click', exportCsv);
  document.querySelectorAll('.comps-table th[data-sort]').forEach(function (th) {
    th.addEventListener('click', function () { setSort(th.dataset.sort); });
  });

  // initial renders
  renderNews();
  renderComps();
})();
</script>

</body>
</html>
"""


def main() -> int:
    if not os.path.exists(SRC_MAP):
        sys.stderr.write(
            f"ERROR: source map HTML not found at {SRC_MAP}\n"
            f"Set FOG_MAP_HTML env var to the path of your existing fog_facility_map.html.\n"
        )
        return 2
    if not os.path.exists(NEWS_JSON) or not os.path.exists(COMPS_JSON):
        sys.stderr.write(f"ERROR: missing seed data files in {os.path.dirname(NEWS_JSON)}\n")
        return 2

    style_inner, body_inner, scripts = slice_original(SRC_MAP)
    scripts = inject_map_handle(scripts)

    with open(NEWS_JSON, encoding="utf-8") as f:
        news = json.load(f)
    with open(COMPS_JSON, encoding="utf-8") as f:
        comps = json.load(f)

    metadata = {
        "lastRefreshed": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "newsCount": len(news),
        "compCount": len(comps),
    }

    out = (TEMPLATE
           .replace("__ORIGINAL_STYLE__", style_inner)
           .replace("__BODY_MAP_INNER__", body_inner)
           .replace("__MAP_SCRIPTS__", scripts)
           .replace("__NEWS_FEED_JSON__", json.dumps(news, indent=2))
           .replace("__COMP_DATABASE_JSON__", json.dumps(comps, indent=2))
           .replace("__METADATA_JSON__", json.dumps(metadata, indent=2)))

    os.makedirs(os.path.dirname(DST), exist_ok=True)
    with open(DST, "w", encoding="utf-8") as f:
        f.write(out)
    print(f"Wrote {DST} ({os.path.getsize(DST):,} bytes) — "
          f"{len(news)} news items, {len(comps)} deals")
    return 0


if __name__ == "__main__":
    sys.exit(main())
