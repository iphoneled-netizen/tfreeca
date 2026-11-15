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
BRACKET_PREFIX = re.compile(r"^\s*(?:\[(?:방영중|종영|완결|방영|NEW|추천)\]|방영중)\s*")
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
