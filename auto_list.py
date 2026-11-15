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
LIST_URL = BASE + "board.php?mode=lists&b_id=tent&page={page}"

PAGES = (1, 2)                 # 수집할 페이지
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


def make_driver():
    options = Options()
    options.page_load_strategy = "eager"
    if HEADLESS:
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


def parse_page(soup):
    items = []
    for a in soup.select("a[href*='mode=view']"):
        href = a.get("href") or ""
        if "b_id=tent" not in href:
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
    try:
        for idx, page in enumerate(PAGES):
            url = LIST_URL.format(page=page)
            print(f"[수집 중] {page}페이지 로딩...", flush=True)
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
                print(f"[경고] {page}페이지 목록 대기 시간 초과, 현재 DOM으로 파싱합니다.", flush=True)

            html = driver.page_source
            if idx == 0 and debug_file:
                os.makedirs(OUT_DIR, exist_ok=True)
                with open(debug_file, "w", encoding="utf-8") as f:
                    f.write(html)
            soup = BeautifulSoup(html, "html.parser")
            for item in parse_page(soup):
                if item["url"] not in seen_urls:
                    seen_urls.add(item["url"])
                    all_items.append(item)
    finally:
        driver.quit()
    print(f"[수집 완료] {len(all_items)}건 ({time.time() - started_at:.1f}초)", flush=True)
    return all_items


# ===================== HTML 생성 =====================
def build_html(items, updated_at):
    meta_refresh_tag = (
        f'<meta http-equiv="refresh" content="{META_REFRESH}">'
        if META_REFRESH > 0 else ""
    )

    rows_html = []
    for idx, item in enumerate(items, 1):
        today_attr = "1" if item["today"] else "0"
        date_cls = "date today" if item["today"] else "date"
        rows_html.append(
            f'<li class="row" data-today="{today_attr}" data-title="{escape(item["title"].lower())}">'
            f'<span class="num">{idx}</span>'
            f'<a class="title" href="{escape(item["url"])}" target="_blank" rel="noopener noreferrer">'
            f'{escape(item["title"])}</a>'
            f'<span class="{date_cls}">{escape(item["date"])}</span>'
            f'</li>'
        )

    list_markup = "\n".join(rows_html) if rows_html else '<li class="empty">수집된 목록이 없습니다.</li>'

    return f"""<!doctype html>
<html lang="ko">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
{meta_refresh_tag}
<title>예능 최신 목록</title>
<style>
  :root {{
    --bg: #0f172a;
    --card: #1e293b;
    --line: #334155;
    --text: #f1f5f9;
    --muted: #94a3b8;
    --accent: #38bdf8;
    --today: #34d399;
  }}
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0;
    padding: 20px 14px 40px;
    background: var(--bg);
    color: var(--text);
    font-family: -apple-system, BlinkMacSystemFont, "Apple SD Gothic Neo", "Noto Sans KR", sans-serif;
  }}
  .wrap {{ max-width: 920px; margin: 0 auto; }}
  .top {{
    display: flex;
    flex-wrap: wrap;
    justify-content: space-between;
    align-items: center;
    gap: 12px;
    margin-bottom: 14px;
  }}
  h1 {{ margin: 0; font-size: 20px; font-weight: 700; }}
  .meta {{ font-size: 13px; color: var(--muted); }}
  .controls {{
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
    margin-bottom: 14px;
  }}
  input[type="search"] {{
    flex: 1 1 220px;
    padding: 9px 12px;
    border: 1px solid var(--line);
    border-radius: 8px;
    background: var(--card);
    color: var(--text);
    font-size: 14px;
    outline: none;
  }}
  button {{
    padding: 9px 14px;
    border: 1px solid var(--line);
    border-radius: 8px;
    background: var(--card);
    color: var(--text);
    font-size: 13px;
    cursor: pointer;
  }}
  button.active {{
    border-color: var(--accent);
    color: var(--accent);
  }}
  ul.list {{
    list-style: none;
    margin: 0;
    padding: 0;
    background: var(--card);
    border: 1px solid var(--line);
    border-radius: 10px;
    overflow: hidden;
  }}
  li.row {{
    display: flex;
    align-items: center;
    gap: 12px;
    padding: 11px 14px;
    border-bottom: 1px solid var(--line);
  }}
  li.row:last-child {{ border-bottom: none; }}
  li.row:hover {{ background: rgba(255, 255, 255, 0.03); }}
  .num {{
    width: 30px;
    flex-shrink: 0;
    font-size: 12px;
    color: var(--muted);
    font-variant-numeric: tabular-nums;
  }}
  .title {{
    flex: 1;
    color: var(--text);
    text-decoration: none;
    font-size: 14px;
    line-height: 1.45;
    word-break: keep-all;
  }}
  .title:hover {{ color: var(--accent); text-decoration: underline; }}
  .date {{
    flex-shrink: 0;
    font-size: 12px;
    color: var(--muted);
    font-variant-numeric: tabular-nums;
  }}
  .date.today {{ color: var(--today); font-weight: 600; }}
  .empty {{ padding: 28px; text-align: center; color: var(--muted); font-size: 14px; }}
</style>
</head>
<body>
  <div class="wrap">
    <div class="top">
      <h1>예능 최신 목록</h1>
      <div class="meta">갱신: {escape(updated_at)} · 총 <span id="count">{len(items)}</span>건</div>
    </div>
    <div class="controls">
      <input id="q" type="search" placeholder="프로그램명 검색..." oninput="applyFilter()">
      <button id="todayBtn" type="button" onclick="toggleToday()">오늘만 보기</button>
      <button id="refreshBtn" type="button" onclick="refreshNow()">새로고침</button>
    </div>
    <ul class="list" id="list">
      {list_markup}
    </ul>
  </div>
<script>
  let onlyToday = false;
  function applyFilter() {{
    const kw = document.getElementById("q").value.trim().toLowerCase();
    const rows = document.querySelectorAll("#list .row");
    let visible = 0;
    rows.forEach((el) => {{
      const matchKw = !kw || el.dataset.title.includes(kw);
      const matchToday = !onlyToday || el.dataset.today === "1";
      const show = matchKw && matchToday;
      el.style.display = show ? "flex" : "none";
      if (show) visible += 1;
    }});
    document.getElementById("count").textContent = visible;
  }}
  function toggleToday() {{
    onlyToday = !onlyToday;
    document.getElementById("todayBtn").classList.toggle("active", onlyToday);
    applyFilter();
  }}
  async function refreshNow() {{
    const btn = document.getElementById("refreshBtn");
    btn.disabled = true;
    btn.textContent = "갱신 중...";
    try {{
      const res = await fetch("/refresh");
      const msg = await res.text();
      if (res.ok) {{
        location.reload();
      }} else {{
        alert(msg);
      }}
    }} finally {{
      btn.disabled = false;
      btn.textContent = "새로고침";
    }}
  }}
</script>
</body>
</html>
"""


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
        print(f"[{updated_at}] 저장 완료: {output_file} ({len(items)}건)")
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
            print(f"[자동갱신 오류] {exc}", file=sys.stderr)


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    update_once(force=True)
    if OPEN_BROWSER:
        open_in_browser(output_file)

    if os.environ.get("RUN_ONCE") == "1":
        return

    t = threading.Thread(target=scheduler_loop, daemon=True)
    t.start()

    handler = partial(AppHandler, directory=OUT_DIR)
    server = ThreadingHTTPServer(("0.0.0.0", PORT), handler)
    print(f"서버 실행 중: http://{lan_ip()}:{PORT} (종료: Ctrl+C)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
