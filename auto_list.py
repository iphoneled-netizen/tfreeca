import os
import re
import sys
import time
import socket
import threading
import subprocess
from datetime import datetime
from functools import partial
from html import escape
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

from bs4 import BeautifulSoup
from selenium import webdriver
from selenium.common.exceptions import TimeoutException
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

# ===================== 설정 =====================
BASE = "https://www.torrentzoa.com/"
BOARDS = (
    ("tent", "예능"),
    ("tdrama", "드라마"),
)
LIST_URL = BASE + "board.php?mode=lists&b_id={board_id}&page={page}"

PAGES = (1, 2)                 # 게시판별 수집할 페이지
HEADLESS = True                # False면 크롬 창을 띄우고 최소화
INTERVAL = 12 * 60 * 60        # 갱신 주기: 12시간
PAGE_LOAD_TIMEOUT = 12         # 페이지 로딩 최대 대기(초)

OUT_DIR = os.environ.get(
    "GITHUB_WORKSPACE",
    r"D:\_C_note\tfreeca"
)
OUT_NAME = "index.html"
output_file = os.path.join(OUT_DIR, OUT_NAME)
debug_file = os.path.join(OUT_DIR, "debug_page1.html")

PORT = 8888
SERVER_LOG = False             # True면 접속 로그 출력
REFRESH_COOLDOWN = 120         # /refresh 연속 호출 제한(초)
OPEN_BROWSER = False           # True면 갱신 후 PC 브라우저도 열기
META_REFRESH = 0               # 보고 있는 창 자동 새로고침(초). 0=끔

NOTICE_WORDS = ("notice", "공지")
SKIP_WIDGET_RE = re.compile(r"(?:^|[^a-z])(?:best|hot|popular|rank)(?:[^a-z]|$)")

# ===================== 제목 정리 =====================
BRACKET_PREFIX = re.compile(
    r"^\s*(?:\[(?:종영|완결|방영|NEW|추천)\]\s*)+"
)
REMOVE_ONAIR_BEST = re.compile(
    r"(?:\[\s*(?:방영중|BEST)\s*\]|\(\s*(?:방영중|BEST)\s*\)|방영중|\bBEST\b)",
    re.IGNORECASE,
)
RELEASE_TAIL = re.compile(
    r"[.\s_\-]*(?:\d{3,4}p|(?:x|h)\.?26[45]|WEB[-_. ]?DL|HEVC|AAC).*$",
    re.IGNORECASE,
)


def clean_title(title):
    t = BRACKET_PREFIX.sub("", (title or "").strip())
    t = REMOVE_ONAIR_BEST.sub("", t)
    t = RELEASE_TAIL.sub("", t)
    t = re.sub(r"\s{2,}", " ", t).strip(" ._-")
    return t


# ===================== 공통 유틸 =====================
def abs_url(href):
    href = (href or "").strip()
    if href.startswith("http"):
        return href
    if href.startswith("//"):
        return "https:" + href
    return BASE + href.lstrip("/")


def attr_chain(tag, depth=5):
    names, node = [], tag
    for _ in range(depth):
        if node is None or getattr(node, "name", None) is None:
            break
        names.extend(node.get("class") or [])
        if node.get("id"):
            names.append(node.get("id"))
        node = node.parent
    return " ".join(names).lower()


def lan_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()


def is_github_action_mode():
    return (
        "--github-action" in sys.argv
        or "--once" in sys.argv
        or os.environ.get("GITHUB_ACTIONS") == "true"
        or os.environ.get("RUN_ONCE") == "1"
    )


def make_driver():
    options = Options()
    options.page_load_strategy = "eager"
    if HEADLESS or is_github_action_mode():
        options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--disable-extensions")
    options.add_argument("--disable-background-networking")
    options.add_argument("--disable-notifications")
    options.add_argument("--mute-audio")
    options.add_argument("--blink-settings=imagesEnabled=false")
    options.add_argument("--window-size=1600,1000")
    options.add_argument("--log-level=3")
    options.add_experimental_option("excludeSwitches", ["enable-logging"])
    options.add_experimental_option(
        "prefs",
        {
            "profile.managed_default_content_settings.images": 2,
            "profile.default_content_setting_values.notifications": 2,
        },
    )
    options.add_argument(
        "user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        " (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
    )
    driver = webdriver.Chrome(options=options)
    driver.set_page_load_timeout(PAGE_LOAD_TIMEOUT)
    driver.set_script_timeout(PAGE_LOAD_TIMEOUT)
    try:
        driver.execute_cdp_cmd("Network.enable", {})
        driver.execute_cdp_cmd(
            "Network.setBlockedURLs",
            {
                "urls": [
                    "*.png", "*.jpg", "*.jpeg", "*.gif", "*.webp", "*.svg",
                    "*.woff", "*.woff2", "*.ttf", "*.mp4",
                    "*googlesyndication*", "*doubleclick*", "*adservice*", "*analytics*",
                ]
            },
        )
    except Exception:
        pass
    if not HEADLESS:
        try:
            driver.minimize_window()
        except Exception:
            pass
    return driver


def open_in_browser(path):
    url = "file:///" + os.path.abspath(path).replace("\\", "/")
    for exe in (
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    ):
        if os.path.exists(exe):
            subprocess.Popen([exe, "--new-window", url])
            return True
    return False


# ===================== 파싱 =====================
def is_excluded_widget(tag):
    return bool(SKIP_WIDGET_RE.search(attr_chain(tag)))


def parse_page(soup, board_id="tent"):
    items = []
    board_token = f"b_id={board_id}"
    for a in soup.select("a[href*='mode=view']"):
        href = a.get("href") or ""
        if board_token not in href:
            continue

        if is_excluded_widget(a):
            continue

        raw = a.get_text(" ", strip=True)
        if not raw or raw.startswith("[이용안내]"):
            continue

        title = clean_title(raw)
        if len(title) < 3:
            continue

        row = a.find_parent("tr") or a.find_parent("li") or a.parent
        texts = [c.get_text(strip=True).lower() for c in row.find_all(["td"])] if row else []

        if any(t in NOTICE_WORDS for t in texts[:2]):
            continue

        date_txt, is_today = "-", False
        for t in texts:
            if re.fullmatch(r"\d{1,2}:\d{2}", t):
                date_txt, is_today = t, True
                break
            if re.fullmatch(r"\d{1,2}-\d{2}", t):
                date_txt = t
                break

        items.append({
            "board": board_id,
            "title": title,
            "url": abs_url(href),
            "date": date_txt,
            "today": is_today,
        })
    return items


def collect_items():
    started_at = time.time()
    driver = make_driver()
    all_items = []
    seen_urls = set()
    counts = {board_id: 0 for board_id, _ in BOARDS}
    try:
        for b_idx, (board_id, board_name) in enumerate(BOARDS):
            for p_idx, page in enumerate(PAGES):
                url = LIST_URL.format(board_id=board_id, page=page)
                print(f"[수집 중] [{board_name}] {page}페이지 로딩...", flush=True)
                try:
                    driver.get(url)
                except TimeoutException:
                    try:
                        driver.execute_script("window.stop();")
                    except Exception:
                        pass
                try:
                    WebDriverWait(driver, 10).until(
                        EC.presence_of_element_located((By.CSS_SELECTOR, "a[href*='mode=view']"))
                    )
                except TimeoutException:
                    print(f"[경고] [{board_name}] {page}페이지 목록 대기 시간 초과, 현재 DOM으로 파싱합니다.", flush=True)

                html = driver.page_source
                if b_idx == 0 and p_idx == 0 and debug_file and not is_github_action_mode():
                    os.makedirs(OUT_DIR, exist_ok=True)
                    with open(debug_file, "w", encoding="utf-8") as f:
                        f.write(html)
                soup = BeautifulSoup(html, "html.parser")
                for item in parse_page(soup, board_id):
                    if item["url"] not in seen_urls:
                        seen_urls.add(item["url"])
                        all_items.append(item)
                        counts[board_id] += 1
    finally:
        driver.quit()
    summary = " · ".join(f"{name} {counts[bid]}" for bid, name in BOARDS)
    print(f"[수집 완료] 총 {len(all_items)}건 ({summary}, {time.time() - started_at:.1f}초)", flush=True)
    return all_items


# ===================== HTML 생성 =====================
def extract_program_key(title):
    base = re.split(r"(?:[\s._-]+E\d+|[\s._-]+\d+회|[\s._-]+\d{6}\b)", title, maxsplit=1, flags=re.IGNORECASE)[0]
    base = re.sub(r"[\s._-]{2,}", " ", base).strip(" ._-")
    return (base or title).lower()


def build_html(items, updated_at):
    meta_refresh_tag = (
        f'<meta http-equiv="refresh" content="{META_REFRESH}">'
        if META_REFRESH > 0 else ""
    )

    rows_html = []
    for idx, item in enumerate(items, 1):
        board_id = item.get("board", "tent")
        today_attr = "1" if item["today"] else "0"
        prog_key = extract_program_key(item["title"])
        date_cls = "date today" if item["today"] else "date"
        today_mark = '<span class="today-mark">오늘</span>' if item["today"] else ""
        rows_html.append(
            f'<li class="row" data-index="{idx}" data-board="{escape(board_id)}" '
            f'data-today="{today_attr}" data-title="{escape(item["title"].lower())}" '
            f'data-prog="{escape(prog_key)}" data-url="{escape(item["url"])}">'
            f'<button type="button" class="fav-btn" title="관심 프로그램 즐겨찾기" aria-label="즐겨찾기">☆</button>'
            f'<span class="num">{idx}</span>'
            f'<a class="title" href="{escape(item["url"])}" target="_blank" rel="noopener noreferrer">'
            f'{escape(item["title"])}</a>'
            f'{today_mark}'
            f'<span class="{date_cls}">{escape(item["date"])}</span>'
            f'<button type="button" class="copy-btn" title="링크 복사">복사</button>'
            f'</li>'
        )

    list_markup = "\n".join(rows_html)

    template = r"""<!doctype html>
<html lang="ko">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="Cache-Control" content="no-cache, no-store, must-revalidate">
<meta http-equiv="Pragma" content="no-cache">
<meta http-equiv="Expires" content="0">
__META_REFRESH__
<title>예능 · 드라마 최신 목록</title>
<style>
  :root {
    --bg: #0f172a;
    --card: #1e293b;
    --card-hover: #263349;
    --line: #334155;
    --text: #f1f5f9;
    --muted: #94a3b8;
    --accent: #38bdf8;
    --today: #34d399;
    --fav: #fbbf24;
  }
  body.light {
    --bg: #f8fafc;
    --card: #ffffff;
    --card-hover: #f1f5f9;
    --line: #e2e8f0;
    --text: #0f172a;
    --muted: #64748b;
    --accent: #2563eb;
    --today: #059669;
    --fav: #d97706;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0;
    padding: 20px 14px 64px;
    background: var(--bg);
    color: var(--text);
    font-family: -apple-system, BlinkMacSystemFont, "Apple SD Gothic Neo", "Noto Sans KR", sans-serif;
  }
  .wrap { max-width: 960px; margin: 0 auto; }
  .top {
    display: flex;
    flex-wrap: wrap;
    justify-content: space-between;
    align-items: center;
    gap: 12px;
    margin-bottom: 14px;
  }
  .top-right {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 8px;
  }
  .meta { font-size: 13px; color: var(--muted); font-variant-numeric: tabular-nums; }
  .toolbar {
    display: flex;
    flex-direction: column;
    gap: 10px;
    margin-bottom: 14px;
  }
  .tabs {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 6px;
  }
  .controls {
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
  }
  .search-wrap {
    position: relative;
    flex: 1 1 240px;
    display: flex;
  }
  input[type="search"] {
    width: 100%;
    padding: 9px 34px 9px 12px;
    border: 1px solid var(--line);
    border-radius: 8px;
    background: var(--card);
    color: var(--text);
    font-size: 14px;
    outline: none;
  }
  input[type="search"]:focus { border-color: var(--accent); }
  .clear-btn {
    position: absolute;
    right: 6px;
    top: 50%;
    transform: translateY(-50%);
    border: none;
    background: transparent;
    color: var(--muted);
    padding: 4px 7px;
    font-size: 14px;
  }
  button, select {
    padding: 8px 12px;
    border: 1px solid var(--line);
    border-radius: 8px;
    background: var(--card);
    color: var(--text);
    font-size: 13px;
    cursor: pointer;
  }
  button:hover { background: var(--card-hover); }
  button.active {
    border-color: var(--accent);
    color: var(--accent);
    font-weight: 600;
  }
  .board-switch {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 8px;
    margin-bottom: 8px;
  }
  .board-btn {
    padding: 7px 16px;
    font-size: 16px;
    font-weight: 700;
    border: 1px solid var(--line);
    border-radius: 8px;
    background: var(--card);
    color: var(--text);
    cursor: pointer;
  }
  .board-btn.active {
    background: var(--accent);
    border-color: var(--accent);
    color: #0f172a;
  }
  body.light .board-btn.active {
    color: #ffffff;
  }
  ul.list {
    list-style: none;
    margin: 0;
    padding: 0;
    background: var(--card);
    border: 1px solid var(--line);
    border-radius: 10px;
    overflow: hidden;
  }
  li.row {
    display: flex;
    align-items: center;
    gap: 10px;
    padding: 10px 14px;
    border-bottom: 1px solid var(--line);
  }
  li.row:last-child { border-bottom: none; }
  li.row:hover { background: var(--card-hover); }
  li.row.is-fav { background: rgba(251, 191, 36, 0.06); }
  li.row.is-visited .title { color: var(--muted); }
  .fav-btn {
    padding: 2px 6px;
    border: none;
    background: transparent;
    color: var(--muted);
    font-size: 16px;
    line-height: 1;
    flex-shrink: 0;
  }
  .fav-btn.on { color: var(--fav); }
  .num {
    width: 28px;
    flex-shrink: 0;
    font-size: 12px;
    color: var(--muted);
    font-variant-numeric: tabular-nums;
    text-align: right;
  }
  .title {
    flex: 1;
    color: var(--text);
    text-decoration: none;
    font-size: 14px;
    line-height: 1.45;
    word-break: keep-all;
  }
  .title:hover { color: var(--accent); text-decoration: underline; }
  .today-mark {
    flex-shrink: 0;
    font-size: 11px;
    font-weight: 700;
    color: var(--today);
  }
  .date {
    flex-shrink: 0;
    font-size: 12px;
    color: var(--muted);
    font-variant-numeric: tabular-nums;
    min-width: 44px;
    text-align: right;
  }
  .date.today { color: var(--today); font-weight: 600; }
  .copy-btn {
    padding: 4px 8px;
    font-size: 11px;
    flex-shrink: 0;
  }
  .empty { padding: 28px; text-align: center; color: var(--muted); font-size: 14px; }
  .to-top {
    position: fixed;
    right: 18px;
    bottom: 18px;
    box-shadow: 0 4px 12px rgba(0, 0, 0, 0.25);
  }
</style>
</head>
<body>
  <div class="wrap">
    <div class="top">
      <div>
        <div class="board-switch" role="tablist" aria-label="게시판 선택">
          <button type="button" class="board-btn active" data-board="tent" onclick="setBoard('tent')">예능</button>
          <button type="button" class="board-btn" data-board="tdrama" onclick="setBoard('tdrama')">드라마</button>
        </div>
        <div class="meta">갱신: __UPDATED_AT__ · 표시 <span id="count">0</span>건 / 전체 <span id="boardTotal">0</span>건 · 오늘 <span id="boardToday">0</span>건</div>
      </div>
      <div class="top-right">
        <button id="themeBtn" type="button" onclick="toggleTheme()">라이트 모드</button>
        <button id="clearVisitBtn" type="button" onclick="clearVisited()">본 기록 초기화</button>
        <button id="refreshBtn" type="button" onclick="refreshNow()">새로고침</button>
      </div>
    </div>

    <div class="toolbar">
      <div class="tabs">
        <button type="button" class="tab-btn active" data-tab="all" onclick="setTab('all')">전체 (<span id="allCount">0</span>)</button>
        <button type="button" class="tab-btn" data-tab="today" onclick="setTab('today')">오늘 (<span id="todayCount">0</span>)</button>
        <button type="button" class="tab-btn" data-tab="fav" onclick="setTab('fav')">즐겨찾기 (<span id="favCount">0</span>)</button>
        <button type="button" class="tab-btn" data-tab="unseen" onclick="setTab('unseen')">안 본 글 (<span id="unseenCount">0</span>)</button>
        <button id="pinFavBtn" type="button" class="active" onclick="togglePinFav()">즐겨찾기 상단 고정</button>
      </div>
      <div class="controls">
        <div class="search-wrap">
          <input id="q" type="search" placeholder="프로그램명 검색..." oninput="applyFilter()">
          <button type="button" class="clear-btn" title="검색어 지우기" onclick="clearSearch()">×</button>
        </div>
        <select id="sortSel" aria-label="정렬" onchange="applyFilter()">
          <option value="latest">최신순</option>
          <option value="title">제목순</option>
        </select>
      </div>
    </div>

    <ul class="list" id="list">
      __LIST_MARKUP__
      <li id="emptyNotice" class="empty" style="display:none;">표시할 항목이 없습니다.</li>
    </ul>
  </div>

  <button type="button" class="to-top" onclick="window.scrollTo({top:0,behavior:'smooth'})">맨 위로</button>

<script>
  var FAV_KEY = "tfreeca_fav_programs_v1";
  var VISIT_KEY = "tfreeca_visited_urls_v1";
  var THEME_KEY = "tfreeca_theme_v1";
  var currentBoard = "tent";
  var currentTab = "all";
  var pinFav = true;

  function loadSet(key) {
    try {
      var raw = localStorage.getItem(key);
      return new Set(raw ? JSON.parse(raw) : []);
    } catch (e) {
      return new Set();
    }
  }
  function saveSet(key, setObj) {
    try {
      localStorage.setItem(key, JSON.stringify(Array.from(setObj)));
    } catch (e) {}
  }

  var favSet = loadSet(FAV_KEY);
  var visitSet = loadSet(VISIT_KEY);

  function initTheme() {
    try {
      var saved = localStorage.getItem(THEME_KEY);
      if (saved === "light") document.body.classList.add("light");
    } catch (e) {}
    updateThemeLabel();
  }
  function updateThemeLabel() {
    var btn = document.getElementById("themeBtn");
    btn.textContent = document.body.classList.contains("light") ? "다크 모드" : "라이트 모드";
  }
  function toggleTheme() {
    document.body.classList.toggle("light");
    try {
      localStorage.setItem(THEME_KEY, document.body.classList.contains("light") ? "light" : "dark");
    } catch (e) {}
    updateThemeLabel();
  }

  function setBoard(board) {
    currentBoard = board;
    document.querySelectorAll(".board-btn").forEach(function(b) {
      b.classList.toggle("active", b.dataset.board === board);
    });
    applyFilter();
  }

  function setTab(tab) {
    currentTab = tab;
    document.querySelectorAll(".tab-btn").forEach(function(b) {
      b.classList.toggle("active", b.dataset.tab === tab);
    });
    applyFilter();
  }

  function togglePinFav() {
    pinFav = !pinFav;
    document.getElementById("pinFavBtn").classList.toggle("active", pinFav);
    applyFilter();
  }

  function clearSearch() {
    document.getElementById("q").value = "";
    applyFilter();
  }

  function clearVisited() {
    visitSet.clear();
    saveSet(VISIT_KEY, visitSet);
    applyFilter();
  }

  function syncRowStates() {
    var boardTotal = 0;
    var boardToday = 0;
    var favMatches = 0;
    var unseenMatches = 0;
    document.querySelectorAll("#list .row").forEach(function(el) {
      var prog = el.dataset.prog || "";
      var url = el.dataset.url || "";
      var isFav = favSet.has(prog);
      var isVisited = visitSet.has(url);
      if (el.dataset.board === currentBoard) {
        boardTotal += 1;
        if (el.dataset.today === "1") boardToday += 1;
        if (isFav) favMatches += 1;
        if (!isVisited) unseenMatches += 1;
      }
      el.classList.toggle("is-fav", isFav);
      el.classList.toggle("is-visited", isVisited);
      var fb = el.querySelector(".fav-btn");
      if (fb) {
        fb.textContent = isFav ? "★" : "☆";
        fb.classList.toggle("on", isFav);
      }
    });
    document.getElementById("boardTotal").textContent = boardTotal;
    document.getElementById("boardToday").textContent = boardToday;
    document.getElementById("allCount").textContent = boardTotal;
    document.getElementById("todayCount").textContent = boardToday;
    document.getElementById("favCount").textContent = favMatches;
    document.getElementById("unseenCount").textContent = unseenMatches;
  }

  function applyFilter() {
    syncRowStates();
    var kw = document.getElementById("q").value.trim().toLowerCase();
    var sortMode = document.getElementById("sortSel").value;
    var listEl = document.getElementById("list");
    var emptyEl = document.getElementById("emptyNotice");
    var rows = Array.from(listEl.querySelectorAll(".row"));

    rows.sort(function(a, b) {
      if (pinFav) {
        var af = favSet.has(a.dataset.prog) ? 1 : 0;
        var bf = favSet.has(b.dataset.prog) ? 1 : 0;
        if (af !== bf) return bf - af;
      }
      if (sortMode === "title") {
        return a.dataset.title.localeCompare(b.dataset.title, "ko");
      }
      return Number(a.dataset.index) - Number(b.dataset.index);
    });

    var visible = 0;
    rows.forEach(function(el) {
      listEl.appendChild(el);
      var matchBoard = el.dataset.board === currentBoard;
      var matchKw = !kw || el.dataset.title.indexOf(kw) !== -1;
      var isFav = favSet.has(el.dataset.prog);
      var isVisited = visitSet.has(el.dataset.url);
      var matchTab = true;
      if (currentTab === "today") matchTab = el.dataset.today === "1";
      else if (currentTab === "fav") matchTab = isFav;
      else if (currentTab === "unseen") matchTab = !isVisited;

      var show = matchBoard && matchKw && matchTab;
      el.style.display = show ? "flex" : "none";
      if (show) {
        visible += 1;
        var numEl = el.querySelector(".num");
        if (numEl) numEl.textContent = String(visible);
      }
    });
    if (emptyEl) {
      listEl.appendChild(emptyEl);
      emptyEl.style.display = visible === 0 ? "block" : "none";
    }
    document.getElementById("count").textContent = visible;
  }

  document.getElementById("list").addEventListener("click", function(e) {
    var row = e.target.closest(".row");
    if (!row) return;
    if (e.target.closest(".fav-btn")) {
      var prog = row.dataset.prog || "";
      if (favSet.has(prog)) favSet.delete(prog);
      else favSet.add(prog);
      saveSet(FAV_KEY, favSet);
      applyFilter();
      return;
    }
    if (e.target.closest(".copy-btn")) {
      var btn = e.target.closest(".copy-btn");
      var url = row.dataset.url || "";
      if (!url) return;
      var done = function() {
        btn.textContent = "복사됨";
        setTimeout(function() { btn.textContent = "복사"; }, 1200);
      };
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(url).then(done).catch(function() {
          fallbackCopy(url, done);
        });
      } else {
        fallbackCopy(url, done);
      }
      return;
    }
    var link = e.target.closest("a.title");
    if (link) {
      visitSet.add(row.dataset.url || "");
      saveSet(VISIT_KEY, visitSet);
      setTimeout(applyFilter, 100);
    }
  });

  function fallbackCopy(text, onDone) {
    var ta = document.createElement("textarea");
    ta.value = text;
    ta.style.position = "fixed";
    ta.style.opacity = "0";
    document.body.appendChild(ta);
    ta.select();
    try {
      document.execCommand("copy");
      onDone();
    } catch (e) {}
    document.body.removeChild(ta);
  }

  function refreshNow() {
    var btn = document.getElementById("refreshBtn");
    if (location.protocol === "file:" || location.hostname.endsWith("github.io")) {
      location.replace(location.pathname + "?t=" + Date.now());
      return;
    }
    btn.disabled = true;
    btn.textContent = "갱신 중...";
    fetch("/refresh")
      .then(function(res) {
        return res.text().then(function(msg) { return { ok: res.ok, msg: msg }; });
      })
      .then(function(result) {
        if (result.ok) location.reload();
        else alert(result.msg);
      })
      .catch(function() {
        location.reload();
      })
      .finally(function() {
        btn.disabled = false;
        btn.textContent = "새로고침";
      });
  }

  initTheme();
  applyFilter();
</script>
</body>
</html>
"""

    return (
        template
        .replace("__META_REFRESH__", meta_refresh_tag)
        .replace("__UPDATED_AT__", escape(updated_at))
        .replace("__LIST_MARKUP__", list_markup)
    )


# ===================== 갱신 및 서버 =====================
_refresh_lock = threading.Lock()
_last_refresh_ts = 0.0


def update_once(force=False):
    global _last_refresh_ts
    with _refresh_lock:
        now = time.time()
        if not force and (now - _last_refresh_ts) < REFRESH_COOLDOWN:
            wait_sec = int(REFRESH_COOLDOWN - (now - _last_refresh_ts))
            return False, f"{wait_sec}초 후 다시 시도하세요."

        items = collect_items()
        updated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        html = build_html(items, updated_at)

        os.makedirs(OUT_DIR, exist_ok=True)
        with open(output_file, "w", encoding="utf-8") as f:
            f.write(html)

        _last_refresh_ts = time.time()
        print(f"[{updated_at}] 저장 완료: {output_file} ({len(items)}건)", flush=True)
        return True, "updated"


class AppHandler(SimpleHTTPRequestHandler):
    def log_message(self, format, *args):
        if SERVER_LOG:
            super().log_message(format, *args)

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/refresh":
            qs = parse_qs(parsed.query)
            force = qs.get("force", ["0"])[0] == "1"
            try:
                ok, msg = update_once(force=force)
                status = 200 if ok else 429
            except Exception as exc:
                status, msg = 500, f"갱신 실패: {exc}"
            body = msg.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if parsed.path in ("/", ""):
            self.path = "/" + OUT_NAME
        return super().do_GET()


def scheduler_loop():
    while True:
        time.sleep(INTERVAL)
        try:
            update_once(force=True)
        except Exception as exc:
            print(f"[자동갱신 오류] {exc}", file=sys.stderr, flush=True)


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    update_once(force=True)

    if is_github_action_mode():
        print("[종료] 일회성 실행 완료 (--github-action)", flush=True)
        return

    if OPEN_BROWSER:
        open_in_browser(output_file)

    t = threading.Thread(target=scheduler_loop, daemon=True)
    t.start()

    handler = partial(AppHandler, directory=OUT_DIR)
    server = ThreadingHTTPServer(("0.0.0.0", PORT), handler)
    print(f"서버 실행 중: http://{lan_ip()}:{PORT} (종료: Ctrl+C)", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
