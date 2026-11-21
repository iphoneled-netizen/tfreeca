import os
import re
import sys
import json
import time
import socket
import threading
import subprocess
from datetime import datetime
from functools import partial
from html import escape
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from urllib.parse import urlparse, parse_qs, urlencode

from bs4 import BeautifulSoup
from selenium import webdriver
from selenium.common.exceptions import TimeoutException
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

# ===================== 설정 =====================
BASE = "https://www.torrentzoa.com/"
PAGES = (1, 2)                 # 게시판별 수집할 페이지
BOARDS = (
    ("tent", "예능", BASE + "board.php?mode=lists&b_id=tent&page={page}", PAGES, "tent", False),
    ("tdrama", "드라마", BASE + "board.php?mode=lists&b_id=tdrama&page={page}", PAGES, "tdrama", False),
    ("tmovie", "영화", BASE + "board.php?mode=lists&b_id=tmovie&page={page}", PAGES, "tmovie", False),
    ("tmovie_week", "주간순위", BASE + "top100.php?b_id=tmovie&hit=Week", (1,), "tmovie", True),
    ("tmovie_month", "월간순위", BASE + "top100.php?b_id=tmovie&hit=Month", (1,), "tmovie", True),
)
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
SKIP_RANKING_WIDGET_RE = re.compile(r"(?:^|[^a-z])(?:best|hot)(?:[^a-z]|$)")

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

MOVIE_UHD_RE = re.compile(r"(?:\bUHD\b|\b4K\b|\b2160p\b)", re.IGNORECASE)
MOVIE_AI_SUB_RE = re.compile(
    r"(?:AI\s*[-_.]?\s*(?:번역\s*)?자막|AI\s*번역|AI\s*한글\s*자막|기계\s*번역)",
    re.IGNORECASE,
)
MOVIE_KOR_SUB_RE = re.compile(
    r"(?:한글\s*자막|자체\s*한글|자체\s*자막|정식\s*자막|\bKOR(?:EAN)?\s*SUB\b|(?:\[|\()(?:한글|자막|KOR)(?:\]|\)))",
    re.IGNORECASE,
)
MOVIE_TAG_INLINE = re.compile(
    r"[\[(]\s*(?:한글\s*자막|AI\s*[-_.]?\s*(?:번역\s*)?자막|AI\s*번역|AI\s*한글\s*자막|기계\s*번역|자체\s*자막|정식\s*자막|무자막|영문\s*자막|UHD|4K|HDR(?:10)?|더빙|개봉예정|최신)\s*[\])]",
    re.IGNORECASE,
)
MOVIE_RELEASE_TAIL = re.compile(
    r"[.\s_\-]*(?:\d{3,4}p|\bUHD\b|\b4K\b|\bHDR(?:10)?\b|\bDV\b|(?:x|h)\.?26[45]|WEB[-_. ]?DL|WEBRip|BluRay|BDRip|HDRip|FHDRip|HEVC|AAC|DDP|Atmos|한글\s*자막|AI\s*[-_.]?\s*(?:번역\s*)?자막|AI\s*번역|자체\s*자막|정식\s*자막).*$",
    re.IGNORECASE,
)


def parse_movie_meta(raw):
    uhd = bool(MOVIE_UHD_RE.search(raw))
    ai_sub = bool(MOVIE_AI_SUB_RE.search(raw))
    kor_sub = bool(MOVIE_KOR_SUB_RE.search(raw))
    quality = 0
    if uhd:
        quality = 2160
    elif re.search(r"\b1440p\b", raw, re.IGNORECASE):
        quality = 1440
    elif re.search(r"\b1080p\b", raw, re.IGNORECASE):
        quality = 1080
    elif re.search(r"\b720p\b", raw, re.IGNORECASE):
        quality = 720
    return uhd, kor_sub, ai_sub, quality


def clean_title(title, board_id="tent"):
    t = BRACKET_PREFIX.sub("", (title or "").strip())
    t = REMOVE_ONAIR_BEST.sub("", t)
    if board_id == "tmovie":
        t = MOVIE_TAG_INLINE.sub(" ", t)
        t = MOVIE_RELEASE_TAIL.sub("", t)
    else:
        t = RELEASE_TAIL.sub("", t)
    t = re.sub(r"[\[(][^\]\)]*$", "", t)
    t = re.sub(r"\s{2,}", " ", t).strip(" ._-/|")
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
def is_excluded_widget(tag, is_ranking=False):
    chain = attr_chain(tag)
    if is_ranking:
        return bool(SKIP_RANKING_WIDGET_RE.search(chain))
    return bool(SKIP_WIDGET_RE.search(chain))


def parse_page(soup, board_key="tent", link_board_id=None, is_ranking=False):
    items = []
    target_b_id = link_board_id or board_key
    board_token = f"b_id={target_b_id}"
    is_movie = target_b_id == "tmovie"

    for a in soup.select("a[href*='mode=view'], a[href*='wr_id=']"):
        href = a.get("href") or ""
        if board_token not in href:
            continue

        if is_excluded_widget(a, is_ranking=is_ranking):
            continue

        raw = a.get_text(" ", strip=True)
        if not raw or raw.startswith("[이용안내]"):
            continue

        if is_movie:
            uhd, kor_sub, ai_sub, quality = parse_movie_meta(raw)
        else:
            uhd, kor_sub, ai_sub, quality = False, False, False, 0

        title = clean_title(raw, "tmovie" if is_movie else board_key)
        if len(title) < 2:
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
            "board": board_key,
            "title": title,
            "url": abs_url(href),
            "date": date_txt,
            "today": is_today,
            "uhd": uhd,
            "kor_sub": kor_sub,
            "ai_sub": ai_sub,
            "quality": quality,
        })
    return items


def default_search_form(target_b_id):
    return {
        "action": BASE + "board.php",
        "method": "get",
        "queryName": "sc",
        "placeholder": "티프리카 검색...",
        "hidden": [
            {"name": "mode", "value": "lists"},
            {"name": "b_id", "value": target_b_id},
        ],
        "selects": [],
    }


def extract_search_form(soup, target_b_id):
    best_score = -999
    best_cfg = None
    search_param_names = ("sc", "stx", "q", "query", "keyword", "search", "sword", "search_text", "k")

    for form in soup.find_all("form"):
        if form.find("input", {"type": re.compile(r"^password$", re.IGNORECASE)}):
            continue
        action_raw = (form.get("action") or "").strip()
        if any(w in action_raw.lower() for w in ("login", "register", "member", "logout")):
            continue

        text_inp = None
        for inp in form.find_all("input"):
            itype = (inp.get("type") or "text").strip().lower()
            iname = (inp.get("name") or "").strip()
            if itype in ("text", "search") and iname:
                text_inp = inp
                if iname.lower() in search_param_names:
                    break
        if text_inp is None:
            continue

        query_name = (text_inp.get("name") or "").strip()
        if not query_name:
            continue

        method = (form.get("method") or "get").strip().lower()
        if method not in ("get", "post"):
            method = "get"

        full_action = abs_url(action_raw) if action_raw and action_raw != "#" else (BASE + "board.php")
        parsed_action = urlparse(full_action)
        clean_action = f"{parsed_action.scheme}://{parsed_action.netloc}{parsed_action.path}"

        hidden_map = {}
        for k, vals in parse_qs(parsed_action.query, keep_blank_values=True).items():
            if k:
                hidden_map[k] = vals[-1] if vals else ""

        for inp in form.find_all("input"):
            itype = (inp.get("type") or "").strip().lower()
            iname = (inp.get("name") or "").strip()
            if itype == "hidden" and iname:
                hidden_map[iname] = inp.get("value") or ""

        selects = []
        for sel in form.find_all("select"):
            sname = (sel.get("name") or "").strip()
            if not sname:
                continue
            opts = []
            for opt in sel.find_all("option"):
                label = opt.get_text(" ", strip=True)
                val = opt.get("value")
                if val is None:
                    val = label
                opts.append({
                    "value": val,
                    "label": label or val,
                    "selected": opt.has_attr("selected"),
                })
            if opts:
                selects.append({"name": sname, "options": opts})

        score = 0
        if f"b_id={target_b_id}" in action_raw or hidden_map.get("b_id") == target_b_id:
            score += 20
        elif "b_id" in hidden_map or "board.php" in clean_action.lower() or hidden_map.get("mode") == "lists":
            score += 15
        if query_name.lower() in search_param_names:
            score += 8
        if selects:
            score += 4
        if "search.php" in clean_action.lower():
            score -= 5
        if method == "get":
            score += 2

        if "b_id" in hidden_map or clean_action.lower().endswith("board.php"):
            hidden_map["b_id"] = target_b_id
            if clean_action.lower().endswith("board.php") and "mode" not in hidden_map:
                hidden_map["mode"] = "lists"

        cfg = {
            "action": clean_action,
            "method": method,
            "queryName": query_name,
            "placeholder": (text_inp.get("placeholder") or "").strip(),
            "hidden": [{"name": k, "value": v} for k, v in hidden_map.items()],
            "selects": selects,
        }
        if score > best_score:
            best_score = score
            best_cfg = cfg

    return best_cfg


def collect_items():
    started_at = time.time()
    driver = make_driver()
    all_items = []
    search_forms = {}
    counts = {board_key: 0 for board_key, *_ in BOARDS}
    try:
        for b_idx, (board_key, board_name, url_tpl, pages_tuple, link_b_id, is_ranking) in enumerate(BOARDS):
            board_seen_urls = set()
            for p_idx, page in enumerate(pages_tuple):
                url = url_tpl.format(page=page) if "{page}" in url_tpl else url_tpl
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
                        EC.presence_of_element_located((By.CSS_SELECTOR, "a[href*='mode=view'], a[href*='wr_id=']"))
                    )
                except TimeoutException:
                    print(f"[경고] [{board_name}] {page}페이지 목록 대기 시간 초과, 현재 DOM으로 파싱합니다.", flush=True)

                html = driver.page_source
                if b_idx == 0 and p_idx == 0 and debug_file and not is_github_action_mode():
                    os.makedirs(OUT_DIR, exist_ok=True)
                    with open(debug_file, "w", encoding="utf-8") as f:
                        f.write(html)
                soup = BeautifulSoup(html, "html.parser")
                if p_idx == 0 and board_key not in search_forms:
                    found_form = extract_search_form(soup, link_b_id or board_key)
                    if found_form:
                        search_forms[board_key] = found_form
                for item in parse_page(soup, board_key, link_b_id, is_ranking):
                    if item["url"] not in board_seen_urls:
                        board_seen_urls.add(item["url"])
                        all_items.append(item)
                        counts[board_key] += 1
    finally:
        driver.quit()

    for board_key, _, _, _, link_b_id, _ in BOARDS:
        if board_key not in search_forms:
            target = link_b_id or board_key
            if target in search_forms:
                search_forms[board_key] = search_forms[target]
            else:
                search_forms[board_key] = default_search_form(target)

    summary = " · ".join(f"{name} {counts[bkey]}" for bkey, name, *_ in BOARDS)
    print(f"[수집 완료] 총 {len(all_items)}건 ({summary}, {time.time() - started_at:.1f}초)", flush=True)
    return all_items, search_forms


# ===================== HTML 생성 =====================
def extract_program_key(title):
    base = re.split(r"(?:[\s._-]+E\d+|[\s._-]+\d+회|[\s._-]+\d{6}\b)", title, maxsplit=1, flags=re.IGNORECASE)[0]
    base = re.sub(r"[\s._-]{2,}", " ", base).strip(" ._-")
    return (base or title).lower()


def extract_movie_key(title):
    t = (title or "").strip()
    t = re.sub(r"\([^)]*(?:19|20)\d{2}[^)]*\)", " ", t)
    t = re.sub(r"\[[^\]]*\]", " ", t)
    t = re.sub(r"(?<=.)[\s._-]+(?:19|20)\d{2}\b.*$", "", t)
    last_ko = -1
    for idx, ch in enumerate(t):
        if "가" <= ch <= "힣":
            last_ko = idx
    if last_ko >= 0:
        head = t[: last_ko + 1]
        tail = t[last_ko + 1 :]
        m = re.match(r"^\s*(\d{1,2})(?=\s|[._-]|$)", tail)
        if m:
            head = head + m.group(1)
        t = head
    key = re.sub(r"[^0-9a-zA-Z가-힣]+", "", t).lower()
    return key or re.sub(r"[^0-9a-zA-Z가-힣]+", "", (title or "")).lower()


def build_html(items, updated_at, search_forms=None):
    meta_refresh_tag = (
        f'<meta http-equiv="refresh" content="{META_REFRESH}">'
        if META_REFRESH > 0 else ""
    )

    forms_payload = {}
    src_forms = search_forms or {}
    for board_key, _, _, _, link_b_id, _ in BOARDS:
        target = link_b_id or board_key
        forms_payload[board_key] = src_forms.get(board_key) or src_forms.get(target) or default_search_form(target)
    search_forms_json = json.dumps(forms_payload, ensure_ascii=False).replace("</", "\u003c/")

    rows_html = []
    for idx, item in enumerate(items, 1):
        board_id = item.get("board", "tent")
        today_attr = "1" if item["today"] else "0"
        is_movie = board_id in ("tmovie", "tmovie_week", "tmovie_month")
        prog_key = (
            extract_movie_key(item["title"])
            if is_movie
            else extract_program_key(item["title"])
        )
        uhd_attr = "1" if item.get("uhd") else "0"
        kor_attr = "1" if item.get("kor_sub") else "0"
        ai_attr = "1" if item.get("ai_sub") else "0"
        quality_val = int(item.get("quality") or 0)

        tags = []
        if item.get("uhd"):
            tags.append('<span class="tag tag-uhd">UHD</span>')
        if item.get("kor_sub"):
            tags.append('<span class="tag tag-kor">한글자막</span>')
        if item.get("ai_sub"):
            tags.append('<span class="tag tag-ai">AI자막</span>')
        tags_markup = "".join(tags)

        date_cls = "date today" if item["today"] else "date"
        today_mark = '<span class="today-mark">오늘</span>' if item["today"] else ""
        rows_html.append(
            f'<li class="row" data-index="{idx}" data-board="{escape(board_id)}" '
            f'data-today="{today_attr}" data-title="{escape(item["title"].lower())}" '
            f'data-prog="{escape(prog_key)}" data-url="{escape(item["url"])}" '
            f'data-uhd="{uhd_attr}" data-kor="{kor_attr}" data-ai="{ai_attr}" data-quality="{quality_val}">'
            f'<button type="button" class="fav-btn" title="관심 항목 즐겨찾기" aria-label="즐겨찾기">☆</button>'
            f'<span class="num">{idx}</span>'
            f'<a class="title" href="{escape(item["url"])}" target="_blank" rel="noopener noreferrer">'
            f'{escape(item["title"])}</a>'
            f'{tags_markup}'
            f'{today_mark}'
            f'<button type="button" class="ver-btn" style="display:none" title="동일 영화 다른 버전 보기"></button>'
            f'<span class="{date_cls}">{escape(item["date"])}</span>'
            f'<button type="button" class="copy-btn" title="링크 복사">복사</button>'
            f'<button type="button" class="hide-btn" title="다음 회차 포함 숨기기">숨김</button>'
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
<title>예능 · 드라마 · 영화 최신 목록</title>
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
    align-items: center;
    gap: 8px;
  }
  .search-wrap {
    position: relative;
    flex: 1 1 210px;
    display: flex;
  }
  .site-search-form {
    display: flex;
    flex: 1 1 290px;
    align-items: center;
    gap: 6px;
  }
  .site-search-selects {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    flex-shrink: 0;
  }
  .site-search-input-wrap {
    flex: 1 1 150px;
    min-width: 120px;
    display: flex;
  }
  .site-search-btn {
    white-space: nowrap;
    flex-shrink: 0;
    border-color: var(--accent);
    color: var(--accent);
    font-weight: 600;
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
  .movie-controls {
    display: none;
    flex-wrap: wrap;
    align-items: center;
    gap: 6px;
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
  li.row.is-sub-row {
    padding-left: 44px;
    background: rgba(15, 23, 42, 0.25);
  }
  body.light li.row.is-sub-row {
    background: #f8fafc;
  }
  li.row.is-sub-row .num {
    color: var(--muted);
    font-size: 11px;
  }
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
  .tag {
    flex-shrink: 0;
    font-size: 11px;
    font-weight: 700;
    padding: 2px 6px;
    border-radius: 4px;
    border: 1px solid transparent;
    line-height: 1.3;
  }
  .tag-uhd {
    color: #38bdf8;
    border-color: rgba(56, 189, 248, 0.35);
    background: rgba(56, 189, 248, 0.12);
  }
  .tag-kor {
    color: #34d399;
    border-color: rgba(52, 211, 153, 0.35);
    background: rgba(52, 211, 153, 0.12);
  }
  .tag-ai {
    color: #fbbf24;
    border-color: rgba(251, 191, 36, 0.35);
    background: rgba(251, 191, 36, 0.12);
  }
  body.light .tag-uhd { color: #0284c7; border-color: #bae6fd; background: #e0f2fe; }
  body.light .tag-kor { color: #059669; border-color: #a7f3d0; background: #d1fae5; }
  body.light .tag-ai { color: #b45309; border-color: #fde68a; background: #fef3c7; }
  .ver-btn {
    padding: 3px 8px;
    font-size: 11px;
    font-weight: 600;
    color: var(--accent);
    border-color: var(--line);
    flex-shrink: 0;
  }
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
  .copy-btn, .hide-btn {
    padding: 4px 8px;
    font-size: 11px;
    flex-shrink: 0;
  }
  .hide-btn {
    color: var(--muted);
  }
  .hide-btn.on {
    color: var(--accent);
    border-color: var(--accent);
  }
  .empty { padding: 28px; text-align: center; color: var(--muted); font-size: 14px; }
  .to-top {
    position: fixed;
    right: 18px;
    bottom: 18px;
    box-shadow: 0 4px 12px rgba(0, 0, 0, 0.25);
  }
  @media (max-width: 640px) {
    body {
      padding: 12px 10px 76px;
    }
    .top {
      flex-direction: column;
      align-items: stretch;
      gap: 8px;
      margin-bottom: 10px;
    }
    .top-right {
      display: grid;
      grid-template-columns: repeat(3, 1fr);
      gap: 6px;
    }
    .top-right button {
      padding: 7px 6px;
      font-size: 12px;
      border-radius: 10px;
      white-space: nowrap;
      text-align: center;
    }
    .meta {
      font-size: 12px;
      line-height: 1.4;
    }

    .board-switch {
      flex-wrap: nowrap;
      overflow-x: auto;
      -webkit-overflow-scrolling: touch;
      scrollbar-width: none;
      gap: 6px;
      margin-bottom: 6px;
      padding-bottom: 2px;
    }
    .board-switch::-webkit-scrollbar {
      display: none;
    }
    .board-btn {
      flex-shrink: 0;
      padding: 7px 14px;
      font-size: 14px;
      border-radius: 999px;
    }

    .toolbar {
      gap: 8px;
      margin-bottom: 12px;
    }
    .tabs {
      gap: 6px;
    }
    .tabs button[data-tab="fav"],
    #pinFavBtn {
      display: none !important;
    }
    .tabs button,
    .movie-controls button,
    .movie-controls select {
      height: 34px;
      padding: 0 11px;
      font-size: 12px;
      border-radius: 999px;
    }
    .movie-controls {
      width: 100%;
      gap: 6px;
    }
    #movieGroupControls {
      flex-wrap: wrap;
      gap: 6px;
    }

    .controls {
      display: grid;
      grid-template-columns: 1fr auto;
      gap: 8px;
      align-items: center;
    }
    .search-wrap {
      grid-column: 1 / 2;
      flex: none;
      width: 100%;
    }
    #sortSel {
      grid-column: 2 / 3;
      height: 40px;
      padding: 0 10px;
      font-size: 13px;
      border-radius: 10px;
    }
    .site-search-form {
      grid-column: 1 / -1;
      flex: none;
      width: 100%;
      display: flex;
      flex-wrap: nowrap;
      gap: 6px;
    }
    .site-search-selects {
      gap: 4px;
    }
    .site-search-selects select,
    input[type="search"],
    .site-search-btn {
      height: 40px;
      font-size: 13px;
      border-radius: 10px;
    }
    .site-search-selects select {
      padding: 0 6px;
      max-width: 88px;
    }
    .site-search-input-wrap {
      flex: 1 1 auto;
      min-width: 0;
    }
    .site-search-btn {
      padding: 0 12px;
      font-size: 12px;
    }

    ul.list {
      border-radius: 12px;
    }
    li.row {
      flex-wrap: wrap;
      align-items: center;
      gap: 8px 6px;
      padding: 12px;
    }
    li.row.is-sub-row {
      padding-left: 16px;
      border-left: 3px solid var(--accent);
    }
    .fav-btn,
    .num,
    .copy-btn,
    .date {
      display: none !important;
    }
    .title {
      flex: 1 0 100%;
      font-size: 15px;
      font-weight: 600;
      line-height: 1.4;
      word-break: break-word;
    }
    .tag,
    .ver-btn,
    .today-mark {
      font-size: 11px;
      border-radius: 6px;
    }
    .ver-btn {
      padding: 3px 8px;
    }
    .hide-btn {
      margin-left: auto;
      padding: 5px 11px;
      font-size: 12px;
      border-radius: 999px;
    }

    .to-top {
      right: 12px;
      bottom: 12px;
      padding: 8px 14px;
      font-size: 12px;
      border-radius: 999px;
    }
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
          <button type="button" class="board-btn" data-board="tmovie" onclick="setBoard('tmovie')">영화</button>
          <button type="button" class="board-btn" data-board="tmovie_week" onclick="setBoard('tmovie_week')">주간순위</button>
          <button type="button" class="board-btn" data-board="tmovie_month" onclick="setBoard('tmovie_month')">월간순위</button>
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
        <button type="button" class="tab-btn" data-tab="hidden" onclick="setTab('hidden')">숨김글 (<span id="hiddenCount">0</span>)</button>
        <button id="pinFavBtn" type="button" class="active" onclick="togglePinFav()">즐겨찾기 상단 고정</button>
        <span id="movieControls" class="movie-controls">
          <span id="movieGroupControls" style="display:inline-flex; gap:6px;">
            <button id="groupMovieBtn" type="button" class="active" onclick="toggleGroupMovies()">동일 영화 묶기</button>
            <button id="uhdFirstBtn" type="button" class="active" onclick="toggleUhdFirst()">UHD 우선</button>
          </span>
          <select id="movieFilterSel" aria-label="영화 화질 자막 필터" onchange="applyFilter()">
            <option value="all">전체 화질/자막</option>
            <option value="uhd">UHD만</option>
            <option value="kor">한글자막</option>
            <option value="ai">AI자막</option>
          </select>
        </span>
      </div>
      <div class="controls">
        <div class="search-wrap">
          <input id="q" type="search" placeholder="프로그램명 검색..." oninput="applyFilter()">
          <button type="button" class="clear-btn" title="검색어 지우기" onclick="clearSearch()">×</button>
        </div>
        <form id="siteSearchForm" class="site-search-form" method="get" action="https://www.torrentzoa.com/board.php" target="_blank" rel="noopener noreferrer">
          <div id="siteSearchHidden" style="display:none;"></div>
          <div id="siteSearchSelects" class="site-search-selects"></div>
          <div class="site-search-input-wrap">
            <input id="siteSearchInput" name="sc" type="search" placeholder="티프리카 검색...">
          </div>
          <button id="siteSearchBtn" type="submit" class="site-search-btn">티프리카 검색</button>
        </form>
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
  var HIDDEN_KEY = "tfreeca_hidden_programs_v1";
  var THEME_KEY = "tfreeca_theme_v1";
  var currentBoard = "tent";
  var currentTab = "all";
  var pinFav = true;
  var groupMovies = true;
  var uhdFirst = true;
  var expandedGroups = new Set();

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
  var hiddenSet = loadSet(HIDDEN_KEY);

  function isAnyMovieBoard(board) {
    return board === "tmovie" || board === "tmovie_week" || board === "tmovie_month";
  }

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

  var SITE_SEARCH_FORMS = __SEARCH_FORMS_JSON__;
  var BOARD_SEARCH_LABELS = {
    tent: "예능",
    tdrama: "드라마",
    tmovie: "영화",
    tmovie_week: "영화",
    tmovie_month: "영화"
  };

  function renderSiteSearchForm(board) {
    var form = document.getElementById("siteSearchForm");
    var hiddenWrap = document.getElementById("siteSearchHidden");
    var selectWrap = document.getElementById("siteSearchSelects");
    var inputEl = document.getElementById("siteSearchInput");
    if (!form || !hiddenWrap || !selectWrap || !inputEl) return;

    var cfg = SITE_SEARCH_FORMS[board] || SITE_SEARCH_FORMS.tent || {
      action: "https://www.torrentzoa.com/board.php",
      method: "get",
      queryName: "sc",
      hidden: [{ name: "mode", value: "lists" }, { name: "b_id", value: "tent" }],
      selects: []
    };

    form.action = cfg.action || "https://www.torrentzoa.com/board.php";
    form.method = cfg.method || "get";

    hiddenWrap.innerHTML = "";
    (cfg.hidden || []).forEach(function(item) {
      var h = document.createElement("input");
      h.type = "hidden";
      h.name = item.name;
      h.value = item.value;
      hiddenWrap.appendChild(h);
    });

    selectWrap.innerHTML = "";
    var selects = cfg.selects || [];
    selectWrap.style.display = selects.length > 0 ? "inline-flex" : "none";
    selects.forEach(function(selCfg) {
      var sel = document.createElement("select");
      sel.name = selCfg.name;
      sel.setAttribute("aria-label", "티프리카 검색 조건");
      (selCfg.options || []).forEach(function(optCfg) {
        var opt = document.createElement("option");
        opt.value = optCfg.value;
        opt.textContent = optCfg.label;
        if (optCfg.selected) opt.selected = true;
        sel.appendChild(opt);
      });
      selectWrap.appendChild(sel);
    });

    inputEl.name = cfg.queryName || "sc";
    var label = BOARD_SEARCH_LABELS[board] || "티프리카";
    inputEl.placeholder = label + " 티프리카 검색...";
  }

  function setBoard(board) {
    currentBoard = board;
    document.querySelectorAll(".board-btn").forEach(function(b) {
      b.classList.toggle("active", b.dataset.board === board);
    });
    var mc = document.getElementById("movieControls");
    if (mc) mc.style.display = isAnyMovieBoard(board) ? "inline-flex" : "none";
    var mgc = document.getElementById("movieGroupControls");
    if (mgc) mgc.style.display = board === "tmovie" ? "inline-flex" : "none";
    var qEl = document.getElementById("q");
    if (qEl) qEl.placeholder = isAnyMovieBoard(board) ? "영화 제목 검색..." : "프로그램명 검색...";
    renderSiteSearchForm(board);
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

  function toggleGroupMovies() {
    groupMovies = !groupMovies;
    document.getElementById("groupMovieBtn").classList.toggle("active", groupMovies);
    applyFilter();
  }

  function toggleUhdFirst() {
    uhdFirst = !uhdFirst;
    document.getElementById("uhdFirstBtn").classList.toggle("active", uhdFirst);
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
    var hiddenMatches = 0;
    document.querySelectorAll("#list .row").forEach(function(el) {
      var prog = el.dataset.prog || "";
      var url = el.dataset.url || "";
      var isFav = favSet.has(prog);
      var isVisited = visitSet.has(url);
      var isHidden = hiddenSet.has(prog);
      if (el.dataset.board === currentBoard) {
        if (isHidden) {
          hiddenMatches += 1;
        } else {
          boardTotal += 1;
          if (el.dataset.today === "1") boardToday += 1;
          if (isFav) favMatches += 1;
        }
      }
      el.classList.toggle("is-fav", isFav);
      el.classList.toggle("is-visited", isVisited);
      var fb = el.querySelector(".fav-btn");
      if (fb) {
        fb.textContent = isFav ? "★" : "☆";
        fb.classList.toggle("on", isFav);
      }
      var hb = el.querySelector(".hide-btn");
      if (hb) {
        hb.textContent = isHidden ? "복원" : "숨김";
        hb.classList.toggle("on", isHidden);
      }
    });
    document.getElementById("boardTotal").textContent = boardTotal;
    document.getElementById("boardToday").textContent = boardToday;
    document.getElementById("allCount").textContent = boardTotal;
    document.getElementById("todayCount").textContent = boardToday;
    document.getElementById("favCount").textContent = favMatches;
    document.getElementById("hiddenCount").textContent = hiddenMatches;
  }

  function compareMovieRelease(a, b) {
    var au = a.dataset.uhd === "1" ? 1 : 0;
    var bu = b.dataset.uhd === "1" ? 1 : 0;
    if (au !== bu) return bu - au;
    var aq = Number(a.dataset.quality || 0);
    var bq = Number(b.dataset.quality || 0);
    if (aq !== bq) return bq - aq;
    var ak = a.dataset.kor === "1" ? 1 : 0;
    var bk = b.dataset.kor === "1" ? 1 : 0;
    if (ak !== bk) return bk - ak;
    var aa = a.dataset.ai === "1" ? 1 : 0;
    var ba = b.dataset.ai === "1" ? 1 : 0;
    if (aa !== ba) return ba - aa;
    return Number(a.dataset.index) - Number(b.dataset.index);
  }

  function applyFilter() {
    syncRowStates();
    var kw = document.getElementById("q").value.trim().toLowerCase();
    var sortMode = document.getElementById("sortSel").value;
    var movieFilter = document.getElementById("movieFilterSel")
      ? document.getElementById("movieFilterSel").value
      : "all";
    var listEl = document.getElementById("list");
    var emptyEl = document.getElementById("emptyNotice");
    var rows = Array.from(listEl.querySelectorAll(".row"));

    var matched = [];
    rows.forEach(function(el) {
      el.classList.remove("is-sub-row");
      el.style.display = "none";
      var vb = el.querySelector(".ver-btn");
      if (vb) {
        vb.style.display = "none";
        vb.textContent = "";
      }

      if (el.dataset.board !== currentBoard) return;
      if (kw && el.dataset.title.indexOf(kw) === -1) return;

      var prog = el.dataset.prog || "";
      var isFav = favSet.has(prog);
      var isHidden = hiddenSet.has(prog);

      if (currentTab === "hidden") {
        if (!isHidden) return;
      } else {
        if (isHidden) return;
        if (currentTab === "today" && el.dataset.today !== "1") return;
        if (currentTab === "fav" && !isFav) return;
      }

      if (isAnyMovieBoard(currentBoard)) {
        if (movieFilter === "uhd" && el.dataset.uhd !== "1") return;
        if (movieFilter === "kor" && el.dataset.kor !== "1") return;
        if (movieFilter === "ai" && el.dataset.ai !== "1") return;
      }
      matched.push(el);
    });

    var visible = 0;

    if (currentBoard === "tmovie" && groupMovies) {
      var groupMap = Object.create(null);
      var groupKeys = [];
      matched.forEach(function(el) {
        var key = el.dataset.prog || ("idx_" + el.dataset.index);
        if (!groupMap[key]) {
          groupMap[key] = [];
          groupKeys.push(key);
        }
        groupMap[key].push(el);
      });

      var groups = groupKeys.map(function(key) {
        var arr = groupMap[key];
        arr.sort(compareMovieRelease);
        return { key: key, primary: arr[0], subs: arr.slice(1) };
      });

      groups.sort(function(ga, gb) {
        var a = ga.primary;
        var b = gb.primary;
        if (pinFav) {
          var af = favSet.has(a.dataset.prog) ? 1 : 0;
          var bf = favSet.has(b.dataset.prog) ? 1 : 0;
          if (af !== bf) return bf - af;
        }
        if (uhdFirst) {
          var au = a.dataset.uhd === "1" ? 1 : 0;
          var bu = b.dataset.uhd === "1" ? 1 : 0;
          if (au !== bu) return bu - au;
        }
        if (sortMode === "title") {
          return a.dataset.title.localeCompare(b.dataset.title, "ko");
        }
        return Number(a.dataset.index) - Number(b.dataset.index);
      });

      groups.forEach(function(g) {
        visible += 1;
        var p = g.primary;
        p.style.display = "flex";
        var numEl = p.querySelector(".num");
        if (numEl) numEl.textContent = String(visible);
        var vb = p.querySelector(".ver-btn");
        if (vb && g.subs.length > 0) {
          var isOpen = expandedGroups.has(g.key);
          vb.style.display = "inline-block";
          vb.textContent = isOpen ? "접기" : ("+" + g.subs.length + "버전");
        }
        listEl.appendChild(p);
        if (g.subs.length > 0 && expandedGroups.has(g.key)) {
          g.subs.forEach(function(sub) {
            sub.classList.add("is-sub-row");
            sub.style.display = "flex";
            var subNum = sub.querySelector(".num");
            if (subNum) subNum.textContent = "└";
            listEl.appendChild(sub);
          });
        }
      });
    } else {
      matched.sort(function(a, b) {
        if (pinFav && currentTab !== "hidden") {
          var af = favSet.has(a.dataset.prog) ? 1 : 0;
          var bf = favSet.has(b.dataset.prog) ? 1 : 0;
          if (af !== bf) return bf - af;
        }
        if (currentBoard === "tmovie" && uhdFirst) {
          var au = a.dataset.uhd === "1" ? 1 : 0;
          var bu = b.dataset.uhd === "1" ? 1 : 0;
          if (au !== bu) return bu - au;
        }
        if (sortMode === "title") {
          return a.dataset.title.localeCompare(b.dataset.title, "ko");
        }
        return Number(a.dataset.index) - Number(b.dataset.index);
      });

      matched.forEach(function(el) {
        visible += 1;
        el.style.display = "flex";
        var numEl = el.querySelector(".num");
        if (numEl) numEl.textContent = String(visible);
        listEl.appendChild(el);
      });
    }

    if (emptyEl) {
      listEl.appendChild(emptyEl);
      emptyEl.style.display = visible === 0 ? "block" : "none";
    }
    document.getElementById("count").textContent = visible;
  }

  document.getElementById("list").addEventListener("click", function(e) {
    var row = e.target.closest(".row");
    if (!row) return;
    if (e.target.closest(".ver-btn")) {
      var gKey = row.dataset.prog || "";
      if (expandedGroups.has(gKey)) expandedGroups.delete(gKey);
      else expandedGroups.add(gKey);
      applyFilter();
      return;
    }
    if (e.target.closest(".hide-btn")) {
      var hProg = row.dataset.prog || "";
      if (hProg) {
        if (hiddenSet.has(hProg)) hiddenSet.delete(hProg);
        else hiddenSet.add(hProg);
        saveSet(HIDDEN_KEY, hiddenSet);
        applyFilter();
      }
      return;
    }
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

  var siteFormEl = document.getElementById("siteSearchForm");
  if (siteFormEl) {
    siteFormEl.addEventListener("submit", function(e) {
      var inp = document.getElementById("siteSearchInput");
      if (inp && !inp.value.trim()) {
        e.preventDefault();
        inp.focus();
      }
    });
  }

  initTheme();
  renderSiteSearchForm(currentBoard);
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
        .replace("__SEARCH_FORMS_JSON__", search_forms_json)
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

        items, search_forms = collect_items()
        updated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        html = build_html(items, updated_at, search_forms)

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
