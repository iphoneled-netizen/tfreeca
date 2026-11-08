import os
import re
import sys
import time
import socket
import threading
import subprocess
from functools import partial
from html import escape
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

from bs4 import BeautifulSoup
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

# ===================== 설정 =====================
BASE = "https://www.torrentzoa.com/"
LIST_URL = BASE + "board.php?mode=lists&b_id=tent&page={page}"

PAGES = (1, 2)                 # 수집할 페이지
BEST_COUNT = 10                # BEST 영역 미검출 시 상위 N개를 베스트로
HEADLESS = True                # False면 크롬 창을 띄우고 최소화
INTERVAL = 12 * 60 * 60        # 갱신 주기: 12시간

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

BEST_HINTS = ("best", "hot", "popular", "issue", "rank", "top")
NOTICE_WORDS = ("notice", "공지")

# ===================== 제목 정리 =====================
BRACKET_PREFIX = re.compile(r"^\s*\[(?:방영중|종영|완결|방영|NEW|추천)\]\s*")
RELEASE_TAIL = re.compile(
    r"[.\s_\-]*(?:\d{3,4}p|(?:x|h)\.?26[45]|WEB[-_. ]?DL|HEVC|AAC).*$",
    re.IGNORECASE,
)


def clean_title(title):
    t = BRACKET_PREFIX.sub("", (title or "").strip())
    t = RELEASE_TAIL.sub("", t)
    t = re.sub(r"\s{2,}", " ", t).strip(" ._-")
    return t or (title or "").strip()


# ===================== 공통 유틸 =====================
def abs_url(href):
    href = (href or "").strip()
    if href.startswith("http"):
        return href
    if href.startswith("//"):
        return "https:" + href
    return BASE + href.lstrip("/")


def class_chain(tag, depth=4):
    names, node = [], tag
    for _ in range(depth):
        if node is None or getattr(node, "name", None) is None:
            break
        names.extend(node.get("class") or [])
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
    if HEADLESS:
        options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--disable-notifications")
    options.add_argument("--window-size=1600,1000")
    options.add_argument("--log-level=3")
    options.add_experimental_option("excludeSwitches", ["enable-logging"])
    options.add_argument(
        "user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        " (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
    )
    driver = webdriver.Chrome(options=options)
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
def parse_page(soup):
    items = []
    for a in soup.select("a[href*='mode=view']"):
        href = a.get("href") or ""
        if "b_id=tent" not in href:
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

        link = abs_url(href)
        pid = (parse_qs(urlparse(link).query).get("id") or [""])[0] or link

        items.append({
            "title": title,
            "link": link,
            "pid": pid,
            "date": date_txt,
            "today": is_today,
            "best": any(h in class_chain(row) for h in BEST_HINTS),
        })
    return items


def update_list(driver, test_mode=False):
    print(f"[{time.strftime('%X')}] 수집 시작")
    collected = []

    for page in PAGES:
        try:
            driver.get(LIST_URL.format(page=page))
            WebDriverWait(driver, 15).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, "a[href*='mode=view']"))
            )
            got = parse_page(BeautifulSoup(driver.page_source, "html.parser"))
            print(f"  └ {page}페이지: {len(got)}개")
            if page == PAGES[0] and test_mode:
                try:
                    with open(debug_file, "w", encoding="utf-8") as f:
                        f.write(driver.page_source)
                except OSError:
                    pass
            collected.extend(got)
        except Exception as e:
            print(f"  └ {page}페이지 실패: {e}")

    merged = {}
    for it in collected:
        if it["pid"] in merged:
            merged[it["pid"]]["best"] |= it["best"]
            merged[it["pid"]]["today"] |= it["today"]
        else:
            merged[it["pid"]] = it
    items = list(merged.values())

    if items and not any(i["best"] for i in items):
        print(f"  └ BEST 미검출 → 상위 {BEST_COUNT}개를 베스트로 처리")
        for it in items[:BEST_COUNT]:
            it["best"] = True

    total_best = sum(i["best"] for i in items)
    print(f"총 {len(items)}개 (베스트 {total_best}개, 신규 {sum(i['today'] for i in items)}개)")

    if not items:
        print("  ! 수집된 게시물이 없습니다 (차단 또는 구조 변경 의심)")
        return False

    if test_mode:
        for n, it in enumerate(items[:15], 1):
            print(f"{'★' if it['best'] else ' '} {n:2d}. [{it['date']}] {it['title'][:50]}")
        return True

    rows = []
    for it in items:
        cls = "row best" if it["best"] else "row"
        tag = ('<span class="tag tag-best">BEST</span>' if it["best"]
               else '<span class="tag">방영중</span>')
        new = '<span class="new">NEW</span>' if it["today"] else ""
        rows.append(
            f'<li class="{cls}" data-title="{escape(it["title"], quote=True)}">'
            f'<div class="left">{tag}'
            f'<a href="{it["link"]}" target="_blank" rel="noreferrer">{escape(it["title"])}</a>'
            f'{new}</div>'
            f'<div class="right"><span class="date">{it["date"]}</span>'
            f'<button class="hide-btn" onclick="hideAndRemember(this)">숨김</button></div>'
            f'</li>'
        )

    os.makedirs(OUT_DIR, exist_ok=True)
    html_out = (
        TEMPLATE
        .replace("__REFRESH__", (f'<meta http-equiv="refresh" content="{META_REFRESH}">'
                                 if META_REFRESH else ""))
        .replace("__TIME__", time.strftime("%Y-%m-%d %H:%M"))
        .replace("__NEXT__", time.strftime("%m-%d %H:%M",
                                           time.localtime(time.time() + INTERVAL)))
        .replace("__COUNT__", str(len(items)))
        .replace("__BEST_COUNT__", str(total_best))
        .replace("__ROWS__", "\n".join(rows))
    )
    with open(output_file, "w", encoding="utf-8") as f:
        f.write(html_out)

    print(f"[{time.strftime('%X')}] {OUT_NAME} 갱신 완료")
    return True


# ===================== HTML 템플릿 =====================
TEMPLATE = r"""<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="icon" href="data:,">
__REFRESH__
<title>예능 리스트</title>
<style>
  body { font-family: 'Malgun Gothic', 'Apple SD Gothic Neo', sans-serif;
         margin: 20px; background: #f5f6f8; color: #333; }
  h2 { border-bottom: 2px solid #007bff; padding-bottom: 10px; color: #007bff;
       display: flex; justify-content: space-between; align-items: flex-start; gap: 12px; }
  .title { font-weight: 700; }
  .meta { font-size: .5em; color: #777; font-weight: normal; text-align: right; line-height: 1.9; }
  .controls { display: inline-flex; gap: 6px; }
  .btn { font-size: .9em; background: #e2e8f0; border: none; padding: 5px 10px;
         border-radius: 4px; cursor: pointer; color: #475569; white-space: nowrap; }
  .btn:hover { background: #cbd5e1; }
  ul { list-style: none; padding: 0; margin: 0; }

  li { background: #fff; margin-bottom: 8px; padding: 12px 15px; border-radius: 6px;
       box-shadow: 0 1px 3px rgba(0,0,0,.08); display: flex; justify-content: space-between;
       align-items: center; gap: 15px; }
  li:hover { background: #f1f1f1; }
  li.best { background: #e7edf8; box-shadow: 0 1px 4px rgba(0,0,0,.14); }
  li.best:hover { background: #dde5f5; }
  body.hide-best li.best { display: none; }

  .left { display: flex; align-items: center; gap: 10px; flex-grow: 1; min-width: 0; }
  .right { display: flex; align-items: center; gap: 10px; flex-shrink: 0; }
  a { text-decoration: none; color: #333; font-weight: 500; white-space: nowrap;
      overflow: hidden; text-overflow: ellipsis; }
  a:hover { color: #007bff; text-decoration: underline; }
  .tag { font-size: .78em; background: #e2e8f0; color: #475569; padding: 2px 6px;
         border-radius: 4px; flex-shrink: 0; }
  .tag-best { background: #4a6fa5; color: #fff; }
  .new { font-size: .72em; background: #ff5252; color: #fff; padding: 2px 5px;
         border-radius: 3px; flex-shrink: 0; }
  .date { font-size: .85em; color: #888; }
  .hide-btn { background: #ff4d4d; color: #fff; border: none; padding: 4px 9px;
              border-radius: 4px; cursor: pointer; font-size: .8em; white-space: nowrap; }
  .hide-btn:hover { background: #cc0000; }

  /* ===== 모바일 ===== */
  @media (max-width: 600px) {
    body { margin: 10px; font-size: 17px; }
    h2 { font-size: 1em; flex-direction: column; align-items: stretch; gap: 8px; }
    .meta { text-align: left; font-size: .62em; line-height: 2; }
    .controls { display: flex; gap: 8px; }
    .btn { flex: 1; padding: 9px 6px; font-size: 1em; }

    li { padding: 12px; gap: 8px; }

    .left { flex-wrap: wrap; gap: 6px; }
    a { white-space: normal; overflow: visible; text-overflow: clip;
        font-size: 1.03em; line-height: 1.35; }

    .tag, .new { font-size: .74em; }
    .date { font-size: .82em; }
    .hide-btn { padding: 7px 12px; font-size: .85em; }
  }
</style>
</head>
<body>
<h2>
  <span class="title">📺 예능 리스트</span>
  <span class="meta">
    총 __COUNT__개 · 갱신 __TIME__ · 다음 __NEXT__<br>
    <span class="controls">
      <button class="btn" id="bestToggle" data-count="__BEST_COUNT__"
              onclick="toggleBest()">BEST 접기 ▲</button>
      <button class="btn" onclick="resetHidden()">숨김 초기화</button>
    </span>
  </span>
</h2>
<ul>
__ROWS__
</ul>
<script>
const BEST_KEY = 'bestCollapsed';
const HIDDEN_KEY = 'hiddenShows';

function bestBtn() { return document.getElementById('bestToggle'); }

function applyBest(collapsed, save) {
  document.body.classList.toggle('hide-best', collapsed);
  const b = bestBtn();
  if (b) {
    const n = b.dataset.count || '';
    b.textContent = (collapsed ? 'BEST 펼치기 ▼' : 'BEST 접기 ▲') + (n ? ' (' + n + ')' : '');
    b.setAttribute('aria-expanded', String(!collapsed));
  }
  if (save) { try { localStorage.setItem(BEST_KEY, collapsed ? '1' : '0'); } catch (e) {} }
}
function toggleBest() {
  applyBest(!document.body.classList.contains('hide-best'), true);
}

function getBaseName(t) {
  return (t || '').split(/\.\s*E\d+/i)[0].split(/\s+E\d+/i)[0].split(' - ')[0].trim();
}
function loadHidden() {
  try { return JSON.parse(localStorage.getItem(HIDDEN_KEY) || '[]'); } catch (e) { return []; }
}

window.addEventListener('DOMContentLoaded', () => {
  applyBest(localStorage.getItem(BEST_KEY) === '1', false);

  if (!document.querySelector('li.best')) {
    const b = bestBtn();
    if (b) b.style.display = 'none';
  }

  const hidden = loadHidden();
  document.querySelectorAll('li').forEach(li => {
    if (hidden.includes(getBaseName(li.getAttribute('data-title')))) li.style.display = 'none';
  });
});

function hideAndRemember(btn) {
  const li = btn.closest('li');
  const key = getBaseName(li.getAttribute('data-title'));
  const hidden = loadHidden();
  if (!hidden.includes(key)) {
    hidden.push(key);
    try { localStorage.setItem(HIDDEN_KEY, JSON.stringify(hidden)); } catch (e) {}
  }
  li.style.display = 'none';
}

function resetHidden() {
  if (!confirm('숨긴 프로그램 목록을 모두 지울까요?')) return;
  try { localStorage.removeItem(HIDDEN_KEY); } catch (e) {}
  location.reload();
}
</script>
</body>
</html>
"""

# ===================== 크롤링 요청 관리 =====================
_crawl_lock = threading.Lock()
_last_crawl = 0.0


def request_crawl(force=False, test_mode=False):
    """크롤링 1회 실행. (성공여부, 메시지) 반환."""
    global _last_crawl
    now = time.time()
    if not force and now - _last_crawl < REFRESH_COOLDOWN:
        wait = int(REFRESH_COOLDOWN - (now - _last_crawl))
        return False, f"너무 잦은 요청입니다. {wait}초 후 다시 시도하세요."
    if not _crawl_lock.acquire(blocking=False):
        return False, "이미 갱신 중입니다."

    try:
        driver = make_driver()
        try:
            ok = update_list(driver, test_mode=test_mode)
        finally:
            try:
                driver.quit()
            except Exception:
                pass
        if ok:
            _last_crawl = time.time()
        return ok, ("갱신 완료" if ok else "갱신 실패 (콘솔 로그 확인)")
    except Exception as e:
        return False, f"오류: {e}"
    finally:
        _crawl_lock.release()


# ===================== 웹서버 =====================
def make_handler(directory):
    class Handler(SimpleHTTPRequestHandler):
        def end_headers(self):
            # 폰이 예전 파일을 캐시하지 않도록
            self.send_header("Cache-Control", "no-store, max-age=0")
            self.send_header("Pragma", "no-cache")
            super().end_headers()

        def do_GET(self):
            path = urlparse(self.path).path
            if path in ("/refresh", "/crawl"):
                ok, msg = request_crawl(force=False)
                body = (msg + "\n").encode("utf-8")
                self.send_response(200 if ok else 429)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                print(f"[{time.strftime('%X')}] /refresh → {msg}")
                return
            super().do_GET()

        def log_message(self, fmt, *args):
            if SERVER_LOG:
                super().log_message(fmt, *args)

    return partial(Handler, directory=directory)


def start_server(directory, port=PORT):
    server = ThreadingHTTPServer(("0.0.0.0", port), make_handler(directory))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


# ===================== 메인 =====================
if __name__ == "__main__":
    os.makedirs(OUT_DIR, exist_ok=True)
    test_mode = "--test" in sys.argv

    if "--github-action" in sys.argv:
        ok, msg = request_crawl(force=True)
        print(msg)
        sys.exit(0 if ok else 1)

    print("=== 예능 리스트 봇 시작 ===")
    ok, msg = request_crawl(force=True)
    print(msg)

    if OPEN_BROWSER and ok:
        open_in_browser(output_file)

    try:
        start_server(OUT_DIR)
        print(f"[서버] http://{lan_ip()}:{PORT}/  (같은 WiFi에서 접속)")
    except OSError as e:
        print(f"[서버] 시작 실패: {e} (포트 {PORT} 사용 중?)")

    print(f"[예약] {INTERVAL // 3600}시간마다 자동 갱신 · 종료는 Ctrl+C")
    try:
        while True:
            time.sleep(INTERVAL)
            ok, msg = request_crawl(force=True)
            print(f"[{time.strftime('%X')}] {msg}")
    except KeyboardInterrupt:
        print("종료합니다.")