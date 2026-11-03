# -*- coding: utf-8 -*-
"""
番茄小说下载器（图形界面版）

功能：搜索书籍 / 预览章节正文 / 下载全书并保存为 txt

运行：双击本文件，或 `python 番茄小说下载器.py`
依赖：只用 Python 标准库 + tkinter（Windows 自带 Python 即满足）。
      仅当番茄更换了字体混淆表时，才需要 Pillow / fontTools / numpy 来自动重建映射（程序会提示）。

自测（无界面）：python 番茄小说下载器.py --selftest 十日终焉
"""

import base64
import gzip
import hashlib
import html as html_lib
import json
import os
import queue
import re
import sys
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
import zlib
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
ASSET_DIR = APP_DIR / "assets"
CACHE_DIR = APP_DIR / ".cache"
FONTMAP_FILE = ASSET_DIR / "fontmap.json"
OUT_DIR = APP_DIR / "下载"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

PAGE_URL = "https://fanqienovel.com/page/{book_id}"
READER_URL = "https://fanqienovel.com/reader/{item_id}"
DIRECTORY_API = "https://fanqienovel.com/api/reader/directory/detail?bookId={book_id}"

# ---------------------------------------------------------------- 字体映射表
# 番茄把正文里的字换成私有区编码(U+E000~U+F8FF)，再用一个随机字体渲染回来。
# 下表是当前字体(sha1 eb1f85ce...)的还原表，压进脚本里，离线也能用。
PACKED_MAP = (
    "eNrdVMluIlcU/ZUW614EPCa7zFHmOZ2OsvAA6cyddJJWFEUqjIEqpoKYyVTZgKEwNlNhY6jBRf3MO/e9+ouQfRatrNItndWV"
    "zr33nDv8EXoYeuHOFxvba+vrd++EnwyRfwuuPTH9v+V/uvAMSPj/uxF5Rhfm6ZPz5d07ob3VIwm9Ar3PLJcrNsZzpDR+OBft"
    "PnRnXfTiUC9/w1+5TcgD4de/4gUThz6u4jTqimmbdHkNaYfkOhYG16wfeXnBtRm6U2ZVXwsSHuxrqqaZK336HswU15sfBwcd"
    "eEfstsEHWeZov1J9gnwcty2MemSW0T3n8ordRclDso9igVk6SeeB1BBngzDGCWYXqWAIKUeHTeH1MJ0iOWROCpbFXIN5FagT"
    "MTlBUn74NdSB8Nx30Bk85icS1DpWFfzBd8y6FJMZzJJQroJ0DmNFZKbC17eCxhKWTOXlL5QxxLgbnJwF8aOPkMxCN+H0mCXB"
    "8Eh3PiGzhNQxZTLMSqOYI3NGVZOu2szu8fINigkkVKpONpjl/M6PPaRbVDOocsz1LPNSfDhEpgXpmHk6szKUVZinkq7QdeVn"
    "5mThlJmXh3zBvBY6pnAG0LygNg4uhjCumVv9nCZlLs/vvfjGu4/uiaXNlwVMbFKkIF2imQvtlA5VyOZ95ha+4U0DqoxUElqL"
    "H9gfMEvBuLH94S6NOiRX0R2gWMJygeKQVsNb1lDKwJ6TPvxJ+GkxNt8W4wtSjmLMcWBOhZREwUWhSW6beUeIN7irviVuTPQO"
    "MLrYe527K1k+Vy5Il/iwFCT6KNX5SIFa+1a0cysP6aRNpzoyfSSVoNZHdybO41hKIn0pzlc9FdE9RPdCJDzmFFDMIzmHWiLJ"
    "pUmeNw5p5JKSRdIQi4nwNfgJOjEeUG2OjIZuA7cSFldibO2L9DUvz7GUeSX3A0yVWYX7lFgZ2GG2DDnFy02oHaTy6MmkazRs"
    "C8MPOgV+nofVexPjU0okSckL//hVkkvcOKXWbVDxxfmq7RnsGddvqdZiVp45V0hec/foOahZ5reZlUV+FqTzWKwMi0eC5jVG"
    "te9ROqN6gdlZKPlgUH//Ja6N0b2MPi9uFryvw7CZ3fyMl/tUtTEqiuyUJiovptjSh6ZRxSS9yazRy+hWqbXYoZsFc4fMdUOr"
    "Y370YCf8zz1Hd8Ox7Y29aCwWDq/tRre2N3eisXB0Z3cvFols7exvbO5t70cjm6E//wavZ2Dh"
)

# 搜索引擎兜底：番茄自己的搜索接口需要浏览器签名，直接调用会被返回空。
SEARCH_ENGINES = [
    ("DuckDuckGo", "https://html.duckduckgo.com/html/?q={q}"),
    ("Bing", "https://cn.bing.com/search?q={q}&count=30"),
    ("搜狗", "https://www.sogou.com/web?query={q}"),
]

_ISBN_RE = re.compile(r'fanqienovel\.com/page/(\d{15,})')

try:  # 图形界面所需（Windows 自带 Python 均包含）
    import tkinter as tk
    from tkinter import ttk, filedialog, messagebox
    from tkinter import font as tkfont
    HAS_TK = True
except Exception:  # 无 tkinter 时仍可用 --selftest
    tk = ttk = filedialog = messagebox = tkfont = None
    HAS_TK = False

_ID_RE = re.compile(r'^\d{15,}$')


# --------------------------------------------------------------------- 网络
class Http:
    @staticmethod
    def get(url, referer="https://fanqienovel.com/", timeout=25, retries=3, binary=False):
        last = None
        for i in range(retries):
            try:
                req = urllib.request.Request(url, headers={
                    "User-Agent": UA,
                    "Accept": "text/html,application/json,*/*;q=0.8",
                    "Accept-Language": "zh-CN,zh;q=0.9",
                    "Accept-Encoding": "gzip, deflate",
                    "Referer": referer,
                })
                with urllib.request.urlopen(req, timeout=timeout) as r:
                    data = r.read()
                    enc = (r.headers.get("Content-Encoding") or "").lower()
                    if enc == "gzip":
                        data = gzip.decompress(data)
                    elif enc == "deflate":
                        try:
                            data = zlib.decompress(data)
                        except zlib.error:
                            data = zlib.decompress(data, -zlib.MAX_WBITS)
                    return data if binary else data.decode("utf-8", "replace")
            except urllib.error.HTTPError as e:
                last = f"HTTP {e.code}"
            except Exception as e:  # 网络抖动/超时
                last = repr(e)
            time.sleep(0.6 * (i + 1))
        raise RuntimeError(f"请求失败 {url} :: {last}")


def extract_state(html_text):
    """从页面里取出 window.__INITIAL_STATE__ 的 JSON。"""
    m = re.search(r"window\.__INITIAL_STATE__\s*=\s*", html_text)
    if not m:
        return {}
    try:
        obj, _ = json.JSONDecoder().raw_decode(html_text[m.end():])
        return obj
    except Exception:
        return {}


# --------------------------------------------------------------- 字体解码
import sqlite3
import customtkinter as ctk

# -*- coding: utf-8 -*-
# 供拼装使用的后端增量（替换 FontDecoder / search_books，新增索引）

# ---------------------------------------------------------------- 字体映射表
# 番茄有两套混淆字体：正文(reader)和书库页面(library)，码点范围一样但映射完全不同。
PACKED_MAP_LIB = (
    "eNrlVltvGmcQ/SuRn5M4xsSO89Z71Wt6T1P1wSbQNE1CGjuNoqoSBAO7YFioMWBYGwgsxua22Bj24mX/zHfm+/ZflPyEVEof"
    "UmmeRnNGR+fMjObPhccLNy/9dP3Gst9/+dLS64TvNevfjvC9GYjvTQru+08c/H/Ow78W4a2S6+fLlxYC80OywJzd58hEeVaH"
    "22UzVwwPkBmTKnH5mPQxr2xTYcZrGpdGzEiKRsdrZnku8aWIRJlZewzDQDwN8wzJOqkRZtjQzE3oCTrbo1g8jLi8TlIe+Zef"
    "IF9G64SGCqlVKCXa238G6RiJKqan3zHLQvsFlDJmJdGOQs9TceiVOrxw/pAXpnTaeOdTVB0qTZjRF+c6MyXm7EFpQleQy3ul"
    "gXDsb+9y7ZAZaa80Fk479BGs9vtQJLSOvIOXwlWhj5DYv8/tfS9SEedTDA4/wCCGrP0NJN2L7tKejlyM2T2k6reoPKRSnVIp"
    "ZqWF5qJV4Xs7UObEdLSKNLbFURP9YygnzFEhZ36EXuC28hRuDPkUlya8L39Feh7xjnD31+hQxUD+lYaZG0/EcMxsG3EJf+9A"
    "1d/DRZ2kMlTrF8SUwO/MqTPHpfqUIjavDm5T5Aitba92htYYVoGZOcTPRMwRqdEDOLvIZUhO+71kBv0SmjpSc33TfHvCCzVm"
    "N5GIe8kdUnuoHnK1hrgGQxKjxjX+wvyaWaff86MMUh2RHiHv3Ll3G9EKl00xM71umVT5XWZYpNZE8gTm2DvuiUELSYuymkie"
    "fUz9JhVN2q6RHKFyVkTivKN+yBwF/bZwy5AStK1QdYz45BFzEjQsiCOZ2RovTKhvYzARg2O0Rmh2P/MxI+Il81C6wupSSkOu"
    "9wMSGWRrGI3ofMr3HcwkcRS9zqtj5mR4r4epxowTXERwGoXm8ELHiznMykIZIrezTL0GqlUqacxtMCML7Yz38mQ3YE68WIfZ"
    "82nNMDtLUpFUi/otZiXEQGeGHGRWdeMLtLrsokJyBtsuVy+Y0eKzrHjZnfNEvEcHGu+m73h7LoYmpiOupn8TAwPVOq8aQj5F"
    "LotZhORdbu9S/UK4SWYUSS+sop+jgwbUDjPbzExjUPmD0jJJOeFWYbT5wZyWyowUxeZ2HzO7iLa0RMWkiOyI6dB70YTUxWy6"
    "8vmWaOwgsu9VZgvzZd68t770ap+XV1eWgsv++X+wcveGzx8Ira9vrAVC86QvsBq86792bXk5sBq4/grz9MmDV5B7W1uPN28u"
    "Lj4IrVxZfxYMXd14vhXcCm9eDYQfLoY37i/Ok5vhh8ErofCjrcXAYtC3Elzzz9tv+AKBjdWrz8KhkG/hr38A9np5+w=="
)

LIB_FONT_URL = "https://lf6-awef.bytetos.com/obj/awesome-font/c/e26e946d8b2ccb7.woff2"


def _unpack_map(packed, kind, default_url=""):
    raw = json.loads(zlib.decompress(base64.b64decode(packed)).decode("utf-8"))
    puas, run = [], 0
    for i, d in enumerate(raw["p"]):
        run = d if i == 0 else run + d
        puas.append(run)
    return {raw["sha1"]: {"chars": raw["c"], "puas": puas, "kind": kind,
                          "url": raw.get("url") or default_url}}


class FontDecoder:
    """把私有区编码还原成真字；两套字体表分开存，按用途选表。"""

    def __init__(self):
        self._lock = threading.RLock()
        self.store = self._load_store()
        self._cache = {}

    # ---------- 存取
    def _load_store(self):
        store = {}
        try:
            if FONTMAP_FILE.exists():
                raw = json.loads(FONTMAP_FILE.read_text(encoding="utf-8"))
                store = raw.get("fonts", {})
        except Exception:
            store = {}
        for item in store.values():
            item.setdefault("kind", "reader")
        store.update(_unpack_map(PACKED_MAP, "reader"))
        store.update(_unpack_map(PACKED_MAP_LIB, "library", LIB_FONT_URL))
        return store

    def _save_store(self):
        try:
            ASSET_DIR.mkdir(parents=True, exist_ok=True)
            FONTMAP_FILE.write_text(json.dumps({"fonts": self.store}, ensure_ascii=False, indent=1),
                                    encoding="utf-8")
        except Exception:
            pass

    @staticmethod
    def _maps(item):
        return {p: c for p, c in zip(item.get("puas") or [], item.get("chars") or "")}

    # ---------- 对外
    def decode(self, text, kind="reader", font_url=None):
        pua = {ord(c) for c in text if 0xE000 <= ord(c) <= 0xF8FF}
        if not pua:
            return text
        table = self._table_for(pua, kind, font_url)
        if not table:
            return text
        return "".join(table.get(ord(c), c) if 0xE000 <= ord(c) <= 0xF8FF else c for c in text)

    def decode_library(self, text):
        return self.decode(text, kind="library")

    def _table_for(self, pua, kind, font_url):
        # 1) 同用途、能全覆盖的表
        for item in self.store.values():
            if item.get("kind") != kind:
                continue
            table = self._maps(item)
            if pua <= set(table):
                return table
        # 2) 任意能全覆盖的表
        for item in self.store.values():
            table = self._maps(item)
            if pua <= set(table):
                return table
        # 3) 下载字体现场重建
        with self._lock:
            urls = []
            if font_url:
                urls.append(font_url)
            urls += [it.get("url") for it in self.store.values()
                     if it.get("kind") == kind and it.get("url")]
            if kind == "library":
                urls.append(LIB_FONT_URL)
            for url in dict.fromkeys(u for u in urls if u):
                try:
                    data = Http.get(url, binary=True, referer="https://fanqienovel.com/")
                except Exception:
                    continue
                sha = hashlib.sha1(data).hexdigest()
                if sha in self.store:
                    table = self._maps(self.store[sha])
                    if pua <= set(table):
                        return table
                try:
                    table = self.rebuild(data, pua)
                except Exception:
                    table = None
                if table:
                    puas = sorted(table)
                    self.store[sha] = {"puas": puas, "chars": "".join(table[p] for p in puas),
                                       "kind": kind, "url": url, "size": len(data)}
                    self._save_store()
                    return table
        return {}

    @staticmethod
    def parse_font_url(reader_html):
        m = re.search(r"src:url\((https://[^)]+?\.woff2)\)", reader_html)
        if m:
            return m.group(1)
        m = re.search(r"url\((https://[^)]+?\.(?:woff2|woff|otf|ttf))\)", reader_html)
        return m.group(1) if m else None

    # ---------- 字体换了以后，用字形比对重建（需要 Pillow / fontTools / numpy）
    def rebuild(self, font_bytes, pua_set):
        try:
            import numpy as np
            from PIL import Image, ImageDraw, ImageFont
            from fontTools.ttLib import TTFont
        except Exception:
            raise RuntimeError(
                "站点换了字体表，自动重建需要：pip install pillow fonttools brotli numpy"
            )

        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        woff = CACHE_DIR / "current_font.woff2"
        woff.write_bytes(font_bytes)
        ttf_path = CACHE_DIR / "current_font.ttf"
        font = TTFont(str(woff))
        font.flavor = None
        font.save(str(ttf_path))
        cmap = TTFont(str(ttf_path)).getBestCmap()

        S = 32

        def bmp(path, ch, size=140):
            f = ImageFont.truetype(str(path), size)
            img = Image.new("L", (size * 2, size * 2), 0)
            ImageDraw.Draw(img).text((size // 2, size // 3), ch, font=f, fill=255)
            bb = img.getbbox()
            if not bb:
                return None
            img = img.crop(bb).resize((S, S), Image.BILINEAR)
            return (np.asarray(img, dtype=np.float32) / 255.0 > 0.35).astype(np.float32)

        glyphs = [cp for cp in pua_set if cp in cmap]
        A = []
        for cp in glyphs:
            b = bmp(ttf_path, chr(cp))
            A.append(b.ravel() if b is not None else np.zeros(S * S, dtype=np.float32))
        A = np.array(A, dtype=np.float32)
        if not len(A):
            return {}

        chars = [chr(c) for c in range(0x4E00, 0x9FA6)] + [chr(c) for c in range(0x20, 0x7F)]
        refs = []
        cached_ref = CACHE_DIR / "SourceHanSansSC-Regular.otf"
        if not cached_ref.exists():
            try:
                data = Http.get(
                    "https://raw.githubusercontent.com/adobe-fonts/source-han-sans/release/"
                    "OTF/SimplifiedChinese/SourceHanSansSC-Regular.otf",
                    binary=True, timeout=180, retries=1)
                if len(data) > 5_000_000:
                    cached_ref.write_bytes(data)
            except Exception:
                pass
        if cached_ref.exists():
            refs.append(cached_ref)
        refs += [Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts" / n
                 for n in ("msyh.ttc", "simhei.ttf", "Deng.ttf", "simsun.ttc")]

        known = sorted({c for it in self.store.values() for c in (it.get("chars") or "") if c.strip()})
        plans = [p for p in (known, chars) if p]

        def match_against(ref, cand):
            scores = np.full(A.shape[0], -1.0, dtype=np.float32)
            idxs = np.full(A.shape[0], -1, dtype=np.int32)
            for start in range(0, len(cand), 1200):
                part = cand[start:start + 1200]
                R, keep = [], []
                for i, ch in enumerate(part):
                    b = bmp(ref, ch)
                    if b is None:
                        continue
                    R.append(b.ravel())
                    keep.append(start + i)
                if not R:
                    continue
                R = np.array(R, dtype=np.float32)
                inter = R @ A.T
                union = R.sum(axis=1)[:, None] + A.sum(axis=1)[None, :] - inter
                iou = inter / np.maximum(union, 1e-6)
                value = iou.max(axis=0)
                local = iou.argmax(axis=0)
                better = value > scores
                scores[better] = value[better]
                idxs[better] = np.array(keep, dtype=np.int32)[local[better]]
            return scores, idxs, int(np.count_nonzero(scores >= 0.80))

        best = {}
        for cand in plans:
            done = False
            for ref in refs:
                if not ref.exists():
                    continue
                scores, idxs, cover = match_against(ref, cand)
                for j, cp in enumerate(glyphs):
                    if idxs[j] >= 0 and scores[j] > best.get(cp, (-1, ""))[0]:
                        best[cp] = (float(scores[j]), cand[idxs[j]])
                if cover >= 0.95 * A.shape[0]:
                    done = True
                    break
            if done:
                break
        table = {cp: c for cp, (s, c) in best.items() if s >= 0.65}
        if len(table) < 0.9 * len(glyphs):
            raise RuntimeError(f"字体映射重建失败（{len(table)}/{len(glyphs)}）")
        return table


FONT = FontDecoder()


# ------------------------------------------------------------ 本地书籍索引
INDEX_DB = CACHE_DIR / "library.db"
_index_lock = threading.Lock()


def _index_conn():
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(INDEX_DB, check_same_thread=False, timeout=20)
    conn.execute("""CREATE TABLE IF NOT EXISTS books(
        book_id TEXT PRIMARY KEY, name TEXT, author TEXT, abstract TEXT,
        words INTEGER DEFAULT 0, status TEXT DEFAULT '', ts REAL)""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_books_name ON books(name)")
    conn.commit()
    return conn


def index_count():
    try:
        conn = _index_conn()
        n = conn.execute("SELECT COUNT(*) FROM books").fetchone()[0]
        conn.close()
        return n
    except Exception:
        return 0


def _parse_words(text):
    """把「82.9万字」这类文本解析成整数。"""
    m = re.search(r"([\d.]+)\s*万", str(text or ""))
    if m:
        try:
            return int(float(m.group(1)) * 10000)
        except Exception:
            return 0
    m = re.search(r"(\d+)", str(text or ""))
    return int(m.group(1)) if m else 0


def _fetch_lib_page(page, category_id=-1, retries=3):
    """抓一页书库。空响应说明被限流，要退避重试。"""
    params = {"page_count": 18, "page_index": page, "gender": -1,
              "category_id": category_id, "creation_status": -1,
              "word_count": -1, "book_type": -1, "sort": 0}
    url = LIB_API + "?" + urllib.parse.urlencode(params)
    for attempt in range(retries):
        try:
            txt = Http.get(url, referer="https://fanqienovel.com/library", timeout=25, retries=1)
            if txt.strip().startswith("{"):
                payload = json.loads(txt)
                if payload.get("code") == 0:
                    return (payload.get("data") or {}).get("book_list") or []
        except Exception:
            pass
        time.sleep([5, 15, 30][min(attempt, 2)])
    raise RuntimeError("书库接口无响应（可能被限流）")


def _meta_get(conn, key, default=""):
    try:
        row = conn.execute("SELECT v FROM meta WHERE k=?", (key,)).fetchone()
        return row[0] if row else default
    except Exception:
        return default


def _meta_set(conn, key, value):
    try:
        conn.execute("INSERT OR REPLACE INTO meta(k,v) VALUES(?,?)", (key, str(value)))
        conn.commit()
    except Exception:
        pass


def build_index(progress=None, should_stop=None, deep=False, log=None, workers=1, start_page=None):
    """抓番茄书库 → 解码 → 存本地索引。

    限速 + 退避 + 断点续传：接口被限流时会自己停下来，下次接着抓。
    """
    conn = _index_conn()
    conn.execute("CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT)")
    conn.commit()

    cats = []
    if deep:
        try:
            cats = [c["category_id"] for c in library_categories().get("主分类", [])]
        except Exception:
            cats = []

    tasks = [(-1, p) for p in range(556)]
    if deep:
        for cid in cats:
            tasks += [(cid, p) for p in range(556)]

    if start_page is None:
        start_page = int(_meta_get(conn, "next_task", "0") or 0)
    tasks = [t for i, t in enumerate(tasks) if i >= start_page]
    total = len(tasks)
    done = [0]
    written = [0]
    fails = [0]
    stop_all = [False]

    def work(idx_task):
        i, (cid, page) = idx_task
        if stop_all[0] or (should_stop and should_stop()):
            return
        time.sleep(0.9)                        # 温和限速，避免触发风控
        try:
            rows = _fetch_lib_page(page, cid)
            fails[0] = 0
        except Exception:
            fails[0] += 1
            if fails[0] >= 3:
                stop_all[0] = True
            done[0] += 1
            return
        batch = []
        for b in rows:
            try:
                name = FONT.decode_library(b.get("book_name") or "")
                author = FONT.decode_library(b.get("author") or "")
                abstract = FONT.decode_library(b.get("abstract") or "")
                status = "完结" if str(b.get("creation_status")) == "0" else "连载中"
                batch.append((b.get("book_id"), name, author, abstract,
                              _parse_words(FONT.decode_library(b.get("word_count") or "")),
                              status, time.time()))
            except Exception:
                continue
        if batch:
            with _index_lock:
                conn.executemany(
                    "INSERT OR REPLACE INTO books(book_id,name,author,abstract,words,status,ts) "
                    "VALUES(?,?,?,?,?,?,?)", batch)
                conn.commit()
                written[0] += len(batch)
                _meta_set(conn, "next_task", start_page + i + 1)
                _meta_set(conn, "updated", time.time())
        done[0] += 1
        if progress and done[0] % 5 == 0:
            progress(done[0], total, written[0])

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        list(pool.map(work, list(enumerate(tasks))))

    if progress:
        progress(done[0], total, written[0])
    if should_stop and should_stop():
        with _index_lock:
            _meta_set(conn, "next_task", start_page + done[0])
    if log:
        note = "（连续失败，已暂停；再点一次会从断点继续）" if stop_all[0] else ""
        log(f"[索引] 抓 {done[0]} 页，写入 {written[0]} 条{note}")
    conn.close()
    return written[0]



def index_search(keyword, limit=80):
    kw = f"%{(keyword or '').strip()}%"
    if kw == "%%":
        return []
    try:
        conn = _index_conn()
        rows = conn.execute(
            "SELECT book_id,name,author,abstract,words,status FROM books "
            "WHERE name LIKE ? OR author LIKE ? OR abstract LIKE ? "
            "ORDER BY CASE WHEN name = ? THEN 0 WHEN name LIKE ? THEN 1 "
            "WHEN author LIKE ? THEN 2 ELSE 3 END, words DESC LIMIT ?",
            (kw, kw, kw, keyword.strip(), kw, kw, limit)).fetchall()
        conn.close()
    except Exception:
        return []
    return [{"book_id": r[0], "name": r[1], "author": r[2], "abstract": r[3],
             "words": r[4] or 0, "status": r[5] or ""} for r in rows]


def search_books(keyword, mode="book", limit=60, log=None, auto_index=True):
    """搜索：优先本地索引（覆盖书库全量），没有索引就先建一次，最后才用搜索引擎兜底。"""
    kw = (keyword or "").strip()
    if not kw:
        return []
    if _ID_RE.match(kw):
        return [detail_cached(kw)]
    if "fanqienovel.com" in kw:
        m = _ISBN_RE.search(kw)
        return [detail_cached(m.group(1))] if m else []

    hits = index_search(kw, limit)
    if hits:
        if log:
            log(f"[索引] 命中 {len(hits)} 本")
        return hits

    if auto_index and index_count() == 0:
        if log:
            log("[索引] 本地还没有索引，正在抓书库（约 20 秒）…")
        try:
            build_index(log=log)
        except Exception as e:
            if log:
                log(f"[索引] 建立失败：{e}")
        hits = index_search(kw, limit)
        if hits:
            if log:
                log(f"[索引] 命中 {len(hits)} 本")
            return hits

    # 索引里没有 → 搜索引擎兜底
    if log:
        log("[索引] 未命中，改用搜索引擎…")
    if mode == "content":
        queries = ['site:fanqienovel.com "%s"' % kw]
    else:
        queries = ['site:fanqienovel.com "%s"' % kw, "fanqienovel %s" % kw]

    ids, seen = [], set()
    for q in queries:
        for name, tpl, ref in SEARCH_ENGINES:
            try:
                page = Http.get(tpl.format(q=urllib.parse.quote(q)), referer=ref, retries=2)
            except Exception:
                time.sleep(0.6)
                continue
            found = _ISBN_RE.findall(page)
            if not found:
                reads = re.findall(r"fanqienovel\.com/reader/(\d{15,})", page)
                found = [b for b in (reader_to_book(x) for x in reads[:5]) if b]
            for i in found:
                if i not in seen:
                    seen.add(i)
                    ids.append(i)
            if len(ids) >= limit:
                break
            time.sleep(0.8)
        if len(ids) >= limit:
            break
    items = hydrate_books(ids[:limit])
    items.sort(key=_rank_key(kw))
    return items


LIB_API = "https://fanqienovel.com/api/author/library/book_list/v0/"
CAT_API = "https://fanqienovel.com/api/author/book/category_list/v0/"
CAT_FILE = ASSET_DIR / "categories.json"
DETAIL_FILE = CACHE_DIR / "details.json"

_detail_cache = {}
_detail_cache_lock = threading.Lock()


def library_categories():
    """番茄书库的标签体系：主分类 / 情节 / 角色 / 主题。"""
    try:
        if CAT_FILE.exists():
            data = json.loads(CAT_FILE.read_text(encoding="utf-8"))
            if time.time() - float(data.get("ts", 0)) < 7 * 86400:
                return data["groups"]
    except Exception:
        pass
    txt = Http.get(CAT_API, referer="https://fanqienovel.com/library")
    payload = json.loads(txt)
    groups = {}
    for c in payload.get("data") or []:
        label = c.get("label") or "其他"
        groups.setdefault(label, []).append({
            "category_id": c.get("category_id"),
            "name": c.get("name") or "",
            "description": c.get("description") or "",
        })
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        CAT_FILE.write_text(json.dumps({"ts": time.time(), "groups": groups},
                                       ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception:
        pass
    return groups


def library_list(page_index=0, gender=-1, category_id=-1, creation_status=-1,
                 word_count=-1, sort=0, page_count=18):
    """书库列表：返回 (原始条目, 总数)。注意条目文本是字体混淆的，书名要另取。"""
    params = {
        "page_count": page_count, "page_index": page_index, "gender": gender,
        "category_id": category_id, "creation_status": creation_status,
        "word_count": word_count, "book_type": -1, "sort": sort,
    }
    url = LIB_API + "?" + urllib.parse.urlencode(params)
    txt = Http.get(url, referer="https://fanqienovel.com/library")
    if not txt.strip().startswith("{"):
        raise RuntimeError("书库接口被限流了，等几分钟再试；搜索走本地索引，不受影响")
    payload = json.loads(txt)
    if payload.get("code") != 0:
        raise RuntimeError(payload.get("message") or "书库接口返回异常")
    data = payload.get("data") or {}
    return data.get("book_list") or [], int(data.get("total_count") or 0)


def _load_detail_cache():
    if _detail_cache:
        return
    try:
        if DETAIL_FILE.exists():
            raw = json.loads(DETAIL_FILE.read_text(encoding="utf-8"))
            now = time.time()
            for bid, item in raw.items():
                if now - float(item.get("ts", 0)) < 7 * 86400:
                    _detail_cache[bid] = item["info"]
    except Exception:
        pass


def _save_detail_cache():
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        raw = {bid: {"ts": time.time(), "info": info} for bid, info in _detail_cache.items()}
        DETAIL_FILE.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


def detail_cached(book_id):
    """书籍信息（无目录），带内存 + 磁盘缓存，避免重复请求。"""
    with _detail_cache_lock:
        _load_detail_cache()
        hit = _detail_cache.get(book_id)
    if hit:
        return hit
    info = book_detail(book_id, with_chapters=False)
    with _detail_cache_lock:
        _detail_cache[book_id] = info
        if len(_detail_cache) % 6 == 0:
            _save_detail_cache()
    return info


def hydrate_books(ids, workers=8):
    """并发把 book_id 换成明文书籍信息（书库接口给的书名是混淆的）。"""
    out, lock = [], threading.Lock()

    def one(bid):
        try:
            info = detail_cached(bid)
        except Exception:
            return
        with lock:
            out.append(info)

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        list(pool.map(one, ids))
    order = {b: i for i, b in enumerate(ids)}
    out.sort(key=lambda x: order.get(x["book_id"], 999))
    return out


FONT = FontDecoder()


# ------------------------------------------------------------------- API


def http_json(url):
    txt = Http.get(url, referer="https://fanqienovel.com/")
    if not txt.strip():
        raise RuntimeError("接口返回空内容（可能被站点风控拦截）")
    return json.loads(txt)


SEARCH_ENGINES = [
    ("搜狗", "https://www.sogou.com/web?query={q}", "https://www.sogou.com/"),
    ("Bing", "https://cn.bing.com/search?q={q}&count=30", "https://cn.bing.com/"),
    ("DDG", "https://lite.duckduckgo.com/lite/?q={q}", "https://lite.duckduckgo.com/"),
]


def _rank_key(word):
    """书名精确匹配排最前，其次是书名包含、作者包含。"""
    def key(info):
        name = info.get("name") or ""
        if name == word:
            return 0
        if word and word in name:
            return 1
        if word and word in (info.get("author") or ""):
            return 2
        return 3
    return key


def reader_to_book(chapter_id):
    """从章节页反查书籍 id（用于正文关键词检索）。"""
    try:
        page = Http.get(READER_URL.format(item_id=chapter_id), retries=2)
    except Exception:
        return None
    m = re.search(r'"bookId"\s*:\s*"(\d{10,})"', page)
    return m.group(1) if m else None


def book_detail(book_id, with_chapters=True):
    page_html = Http.get(PAGE_URL.format(book_id=book_id))
    state = extract_state(page_html)
    pg = state.get("page") or {}
    if not pg.get("bookName"):
        raise RuntimeError(f"打不开书籍页 {book_id}")
    info = {
        "book_id": book_id,
        "name": pg.get("bookName", ""),
        "author": pg.get("author") or pg.get("authorName") or "",
        "status": "完结" if str(pg.get("creationStatus")) == "0" else "连载中",
        "words": int(pg.get("wordNumber") or 0),
        "abstract": (pg.get("abstract") or "").strip(),
        "cover": pg.get("thumbUri") or "",
        "last": pg.get("lastChapterTitle") or "",
        "total": int(pg.get("chapterTotal") or 0),
        "volumes": [],
    }
    if not with_chapters:
        return info

    volumes = []
    raw = pg.get("chapterListWithVolume") or []
    for vol in raw:
        chapters = [{"id": c["itemId"], "title": c.get("title", ""), "pay": int(c.get("needPay") or 0)}
                    for c in vol]
        volumes.append(chapters)
    if not volumes:
        data = http_json(DIRECTORY_API.format(book_id=book_id)).get("data") or {}
        flat = []
        for vol in (data.get("chapterListWithVolume") or []):
            flat.append([{"id": c["itemId"], "title": c.get("title", ""), "pay": int(c.get("needPay") or 0)}
                         for c in vol])
        volumes = flat
    names = pg.get("volumeNameList") or []
    info["volumes"] = [{"name": names[i] if i < len(names) else f"第{i + 1}卷", "chapters": v}
                       for i, v in enumerate(volumes)]
    info["total"] = sum(len(v["chapters"]) for v in info["volumes"])
    return info


def chapter_text(item_id):
    """取一章正文，返回 {'title','text','words','pay'}。"""
    if not item_id:
        raise RuntimeError("空章节号")
    page = Http.get(READER_URL.format(item_id=item_id))
    state = extract_state(page)
    data = ((state.get("reader") or {}).get("chapterData")) or {}
    if not data:
        data = ((state.get("preview") or {}).get("chapterData")) or {}
    title = data.get("title") or ""
    content = data.get("content") or ""
    if not content:
        raise RuntimeError("本章拿不到正文（可能需付费/登录）")
    content = FONT.decode(content, kind="reader", font_url=page)
    return {
        "title": title,
        "text": content_to_text(content),
        "words": data.get("chapterWordNumber") or "",
        "pay": data.get("needPay") or 0,
    }


def content_to_text(content):
    content = content.replace("{{image_domain}}", "")
    content = re.sub(r"<img[^>]*>", "\n[图片]\n", content)
    content = re.sub(r"(?i)</?h\d[^>]*>", "\n", content)
    parts = re.split(r"(?i)</p\s*>|<br\s*/?>", content)
    lines = []
    for part in parts:
        t = html_lib.unescape(re.sub(r"<[^>]+>", "", part))
        t = t.replace("\u3000", " ").strip()
        if t:
            lines.append("　　" + t)
    return "\n".join(lines)


# ------------------------------------------------------------- 下载与保存


def safe_name(name):
    name = re.sub(r'[\\/:*?"<>|\r\n\t]', "_", name).strip()
    return name[:80] or "未命名"


def download_book(book, chapters, out_dir, threads=8, on_log=None, on_progress=None, should_stop=None):
    """并发抓章节，边下边缓存，最后合成一个 txt。中途停止会保留缓存，下次自动续传。"""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    cache = CACHE_DIR / f"book_{book['book_id']}"
    cache.mkdir(parents=True, exist_ok=True)

    total = len(chapters)
    done = [0]
    lock = threading.Lock()
    results = {}
    failed = []
    locked_chapters = []

    def work(idx_ch):
        idx, ch = idx_ch
        if should_stop and should_stop():
            return
        if ch.get("pay"):
            with lock:
                locked_chapters.append(ch.get("title", ""))
                done[0] += 1
                if on_progress:
                    on_progress(done[0], total)
            return
        cf = cache / f"{ch['id']}.json"
        if cf.exists():
            try:
                item = json.loads(cf.read_text(encoding="utf-8"))
                with lock:
                    results[idx] = item
                    done[0] += 1
                    if on_progress:
                        on_progress(done[0], total)
                return
            except Exception:
                pass
        for attempt in range(3):
            try:
                info = chapter_text(ch["id"])
                item = {"idx": idx, "title": info["title"] or ch["title"], "text": info["text"]}
                cf.write_text(json.dumps(item, ensure_ascii=False), encoding="utf-8")
                with lock:
                    results[idx] = item
                    done[0] += 1
                    if on_progress:
                        on_progress(done[0], total)
                return
            except Exception as e:
                if attempt == 2:
                    with lock:
                        failed.append((ch.get("title"), str(e)[:80]))
                        done[0] += 1
                        if on_progress:
                            on_progress(done[0], total)
                else:
                    time.sleep(1.2 * (attempt + 1))

    with ThreadPoolExecutor(max_workers=max(1, threads)) as pool:
        list(pool.map(work, list(enumerate(chapters))))

    stopped = bool(should_stop and should_stop())

    # 合成 txt
    lines = [book["name"], f"作者：{book['author']}", f"字数：{book['words']}", ""]
    if book.get("abstract"):
        lines += ["【简介】", book["abstract"], ""]
    if stopped:
        lines.insert(0, f"（未下载完：{len(results)}/{total} 章，重新点“下载全书”会自动续传）\n")
    vol_names = {id(c): v["name"] for v in book.get("volumes", []) for c in v["chapters"]}
    for idx, ch in enumerate(chapters):
        item = results.get(idx)
        if not item:
            continue
        vn = vol_names.get(ch["id"])
        if vn and (idx == 0 or vol_names.get(chapters[idx - 1]["id"]) != vn):
            lines += ["", f"=== {vn} ===", ""]
        lines += ["", item["title"], "", item["text"], ""]
    txt = "\n".join(lines).strip() + "\n"

    out_file = out_dir / f"{safe_name(book['name'])}.txt"
    out_file.write_text(txt, encoding="utf-8")
    if not stopped:
        try:
            import shutil
            shutil.rmtree(cache, ignore_errors=True)
        except Exception:
            pass
    if on_log:
        if locked_chapters:
            on_log(f"[下载] {len(locked_chapters)} 章是付费/锁定章节，已跳过")
        if failed:
            on_log(f"[下载] 有 {len(failed)} 章抓取失败（可重试）：{failed[:3]}")
        if stopped:
            on_log("[下载] 已手动停止，缓存已保留，下次继续下载会自动接着下")
    return out_file, len(results), failed


# -*- coding: utf-8 -*-
"""新版界面（现代扁平风）：搜索 / 标签找书 / 预览 / 下载。"""

BG = "#F4F5F7"
CARD = "#FFFFFF"
BORDER = "#E9EBEF"
TEXT = "#1F2329"
MUTED = "#8A9099"
PRIMARY = "#FF5A1F"
PRIMARY_HOVER = "#FF7A45"
PRIMARY_SOFT = "#FFF1EA"
SEL_BG = "#FFF3EC"
FONT_UI = "Microsoft YaHei UI"

WORD_TIERS = [("全部", -1), ("<30万", 0), ("30-50万", 1), ("50-100万", 2),
              ("100-200万", 3), (">200万", 4)]
STATUS_OPTS = [("全部", -1), ("完结", 0), ("连载", 1)]
SORT_OPTS = [("默认", 0), ("最新", 1), ("字数最多", 2)]
GENDER_OPTS = [("全部", -1), ("男频", 0), ("女频", 1)]


# -*- coding: utf-8 -*-
"""CustomTkinter 界面：现代浅色/深色，三栏布局。"""

# (浅色, 深色)
C_BG = ("#F2F3F5", "#13151A")
C_CARD = ("#FFFFFF", "#1B1E24")
C_CARD2 = ("#F7F8FA", "#22262E")
C_LINE = ("#E6E9ED", "#2A2F38")
C_TEXT = ("#1A1D21", "#E8EAED")
C_MUTED = ("#7C848E", "#8A93A0")
C_PRIMARY = ("#FF5A1F", "#FF6B35")
C_PRIMARY_H = ("#FF7A45", "#FF8352")
C_SOFT = ("#FFF1EA", "#3A2418")
C_SOFT_H = ("#FFE4D8", "#4A2C1C")
C_SEL = ("#FFF3EC", "#2A2118")

UI_FONT = "Microsoft YaHei UI"


def ftc(size=11, bold=False):
    return ctk.CTkFont(family=UI_FONT, size=size, weight="bold" if bold else "normal")


class BookCards(ctk.CTkScrollableFrame):
    """书籍卡片列表"""

    def __init__(self, master, on_select=None):
        super().__init__(master, fg_color="transparent", corner_radius=0)
        self.on_select = on_select
        self.items = []
        self.selected = None
        self._cards = []
        self.empty = ctk.CTkLabel(self, text="这里还没有内容", font=ftc(11), text_color=C_MUTED)
        self.empty.pack(pady=40)

    def _bind_recursive(self, widget, idx):
        widget.bind("<Button-1>", lambda e, i=idx: self._click(i))
        for child in widget.winfo_children():
            self._bind_recursive(child, idx)

    def _click(self, idx):
        self.selected = idx
        self._paint()
        if self.on_select:
            self.on_select(self.items[idx])

    def _paint(self):
        for i, card in enumerate(self._cards):
            card.configure(fg_color=C_SEL if i == self.selected else C_CARD2,
                           border_color=C_PRIMARY if i == self.selected else C_LINE)

    def set_items(self, items, empty_text="没有找到书"):
        for c in self._cards:
            c.destroy()
        self._cards = []
        self.items = items or []
        self.selected = None
        self.empty.configure(text=empty_text)
        self.empty.pack(pady=40)
        if not self.items:
            return
        self.empty.pack_forget()
        for i, b in enumerate(self.items):
            card = ctk.CTkFrame(self, fg_color=C_CARD2, corner_radius=10,
                                border_width=1, border_color=C_LINE)
            card.pack(fill="x", pady=(0, 6), padx=2)
            inner = ctk.CTkFrame(card, fg_color="transparent")
            inner.pack(fill="x", padx=10, pady=7)

            name = (b.get("name") or "未知书名")
            avatar = ctk.CTkFrame(inner, width=34, height=34, corner_radius=17, fg_color=C_SOFT)
            avatar.pack(side="left", padx=(0, 10))
            avatar.pack_propagate(False)
            ctk.CTkLabel(avatar, text=name[:1], font=ftc(14, True),
                         text_color=C_PRIMARY).place(relx=0.5, rely=0.5, anchor="center")

            body = ctk.CTkFrame(inner, fg_color="transparent")
            body.pack(side="left", fill="x", expand=True)
            ctk.CTkLabel(body, text=name, font=ftc(12, True), text_color=C_TEXT,
                         anchor="w").pack(fill="x")
            meta = "  ·  ".join(x for x in [
                b.get("author") or "未知作者",
                (f"{b['words'] / 10000:.1f} 万字" if b.get("words") else ""),
                b.get("status") or "",
            ] if x)
            ctk.CTkLabel(body, text=meta, font=ftc(9), text_color=C_MUTED, anchor="w").pack(fill="x")
            abstract = (b.get("abstract") or "").replace("\n", " ").strip()
            if abstract:
                ctk.CTkLabel(body, text=abstract[:52] + ("…" if len(abstract) > 52 else ""),
                             font=ftc(9), text_color=C_MUTED, anchor="w").pack(fill="x", pady=(2, 0))

            self._bind_recursive(card, i)
            self._cards.append(card)
        self._paint()


class App:
    def __init__(self, root):
        self.root = root
        self.root.title("番茄小说下载器")
        self.root.geometry("1420x900")
        self.root.minsize(1180, 760)

        self.books = []
        self.book = None
        self.chapters = []
        self.ui_q = queue.Queue()
        self.busy = False
        self.stop_flag = False
        self.page = 0
        self.page_total = 1
        self.mode = "书名/作者"
        self.cats = {}
        self.main_opts = [("全部", -1)]
        self.tag_opts = [("全部", -1)]

        self._build()
        self.root.after(80, self._pump)
        self.bg(self._boot)

    # ---------------- 构建 ----------------
    def _build(self):
        self.root.grid_columnconfigure(0, weight=1)
        self.root.grid_rowconfigure(1, weight=1)
        self._build_top()
        self._build_body()
        self._build_bottom()

    def _build_top(self):
        bar = ctk.CTkFrame(self.root, fg_color=C_CARD, corner_radius=14)
        bar.grid(row=0, column=0, sticky="ew", padx=16, pady=(14, 0))
        bar.grid_columnconfigure(3, weight=1)

        left = ctk.CTkFrame(bar, fg_color="transparent")
        left.grid(row=0, column=0, sticky="w", padx=(16, 12), pady=12)
        ctk.CTkLabel(left, text="番茄小说下载器", font=ftc(17, True),
                     text_color=C_TEXT).pack(anchor="w")
        ctk.CTkLabel(left, text="免费章节 · 搜索 · 预览 · 存 txt", font=ftc(9),
                     text_color=C_MUTED).pack(anchor="w")

        self.seg_mode = ctk.CTkSegmentedButton(
            bar, values=["书名/作者", "正文关键词"], command=self._switch_mode,
            font=ftc(10), height=32, corner_radius=8,
            selected_color=C_PRIMARY, selected_hover_color=C_PRIMARY_H,
            fg_color=C_CARD2, text_color=C_TEXT, unselected_color=C_CARD2)
        self.seg_mode.set("书名/作者")
        self.seg_mode.grid(row=0, column=1, padx=(0, 10), pady=12)

        self.entry_kw = ctk.CTkEntry(bar, placeholder_text="输入书名 / 作者 / 关键词，回车搜索",
                                     height=38, corner_radius=19, font=ftc(11),
                                     fg_color=C_CARD2, border_color=C_LINE, text_color=C_TEXT)
        self.entry_kw.grid(row=0, column=3, sticky="ew", padx=(0, 10), pady=12)
        self.entry_kw.bind("<Return>", lambda e: self.do_search())

        self.btn_search = ctk.CTkButton(bar, text="搜索", width=86, height=38, corner_radius=19,
                                        font=ftc(11, True), fg_color=C_PRIMARY,
                                        hover_color=C_PRIMARY_H, command=self.do_search)
        self.btn_search.grid(row=0, column=4, padx=(0, 10), pady=12)

        self.sw_dark = ctk.CTkSwitch(bar, text="深色", font=ftc(9), command=self._toggle_theme,
                                     progress_color=C_PRIMARY, text_color=C_MUTED)
        self.sw_dark.grid(row=0, column=5, padx=(0, 16), pady=12)

    def _build_body(self):
        body = ctk.CTkFrame(self.root, fg_color="transparent")
        body.grid(row=1, column=0, sticky="nsew", padx=16, pady=12)
        body.grid_columnconfigure(1, weight=1)
        body.grid_rowconfigure(0, weight=1)
        self._build_left(body)
        self._build_center(body)
        self._build_right(body)

    def _build_left(self, master):
        panel = ctk.CTkFrame(master, fg_color=C_CARD, corner_radius=14, width=268)
        panel.grid(row=0, column=0, sticky="nsw", padx=(0, 12))
        panel.grid_propagate(False)
        panel.grid_rowconfigure(0, weight=1)
        panel.grid_columnconfigure(0, weight=1)

        box = ctk.CTkScrollableFrame(panel, fg_color="transparent", corner_radius=0)
        box.grid(row=0, column=0, sticky="nsew", padx=2, pady=(4, 0))

        ctk.CTkLabel(box, text="按标签找书", font=ftc(12, True), text_color=C_TEXT,
                     anchor="w").pack(fill="x", padx=12, pady=(10, 0))
        ctk.CTkLabel(box, text="从番茄书库筛选", font=ftc(9), text_color=C_MUTED,
                     anchor="w").pack(fill="x", padx=12)

        def section(t):
            ctk.CTkLabel(box, text=t, font=ftc(9), text_color=C_MUTED,
                         anchor="w").pack(fill="x", padx=12, pady=(7, 2))

        def dropdown(values, default="全部"):
            m = ctk.CTkOptionMenu(box, values=values, font=ftc(10), height=28, corner_radius=8,
                                  fg_color=C_CARD2, button_color=C_CARD2,
                                  button_hover_color=C_SOFT, text_color=C_TEXT,
                                  dropdown_fg_color=C_CARD, dropdown_text_color=C_TEXT,
                                  dropdown_hover_color=C_SOFT)
            m.set(default)
            m.pack(fill="x", padx=12)
            return m

        section("频道")
        self.opt_gender = dropdown(["全部", "男频", "女频"])

        section("分类")
        self.opt_cat = dropdown(["全部"])

        section("标签（情节/角色/主题）")
        self.opt_tag = dropdown(["全部"])

        section("状态")
        self.opt_status = dropdown(["全部", "完结", "连载"])

        section("字数")
        self.opt_words = dropdown([n for n, _ in WORD_TIERS])

        section("排序")
        self.opt_sort = dropdown([n for n, _ in SORT_OPTS])

        self.btn_lib = ctk.CTkButton(box, text="查询书库", height=34, corner_radius=9,
                                     font=ftc(10, True), fg_color=C_SOFT, hover_color=C_SOFT_H,
                                     text_color=C_PRIMARY, command=self.do_library)
        self.btn_lib.pack(fill="x", padx=12, pady=(12, 6))

        idx = ctk.CTkFrame(panel, fg_color="transparent")
        idx.grid(row=1, column=0, sticky="ew", padx=14, pady=(0, 12))
        ctk.CTkFrame(idx, height=1, fg_color=C_LINE).pack(fill="x", pady=(0, 10))
        ctk.CTkLabel(idx, text="本地索引", font=ftc(11, True), text_color=C_TEXT,
                     anchor="w").pack(fill="x")
        ctk.CTkLabel(idx, text="把书库书单存本地，搜索秒出", font=ftc(9), text_color=C_MUTED,
                     anchor="w").pack(fill="x", pady=(0, 6))
        self.lbl_index = ctk.CTkLabel(idx, text="索引：0 本", font=ftc(10, True),
                                      text_color=C_TEXT, anchor="w")
        self.lbl_index.pack(fill="x")
        self.pb_index = ctk.CTkProgressBar(idx, height=6, corner_radius=3, progress_color=C_PRIMARY,
                                           fg_color=C_CARD2)
        self.pb_index.set(0)
        self.pb_index.pack(fill="x", pady=6)
        self.btn_index = ctk.CTkButton(idx, text="建立索引", height=32, corner_radius=8,
                                       font=ftc(10), fg_color=C_SOFT, hover_color=C_SOFT_H,
                                       text_color=C_PRIMARY, command=self.do_index)
        self.btn_index.pack(fill="x")
        self.btn_index_deep = ctk.CTkButton(idx, text="深度索引（按分类）", height=28,
                                            corner_radius=8, font=ftc(9), fg_color=C_CARD2,
                                            hover_color=C_SOFT, text_color=C_MUTED,
                                            command=self.do_index_deep)
        self.btn_index_deep.pack(fill="x", pady=(6, 0))

    def _build_center(self, master):
        panel = ctk.CTkFrame(master, fg_color=C_CARD, corner_radius=14)
        panel.grid(row=0, column=1, sticky="nsew")
        panel.grid_rowconfigure(1, weight=1)
        panel.grid_columnconfigure(0, weight=1)

        head = ctk.CTkFrame(panel, fg_color="transparent")
        head.grid(row=0, column=0, sticky="ew", padx=16, pady=(12, 4))
        self.lbl_list_title = ctk.CTkLabel(head, text="搜索结果", font=ftc(13, True),
                                           text_color=C_TEXT)
        self.lbl_list_title.pack(side="left")
        self.lbl_hint = ctk.CTkLabel(head, text="", font=ftc(9), text_color=C_MUTED)
        self.lbl_hint.pack(side="left", padx=8)

        self.cards = BookCards(panel, on_select=self.pick_book)
        self.cards.grid(row=1, column=0, sticky="nsew", padx=12)

        pager = ctk.CTkFrame(panel, fg_color="transparent")
        pager.grid(row=2, column=0, sticky="ew", padx=16, pady=(4, 12))
        self.btn_prev = ctk.CTkButton(pager, text="上一页", width=76, height=30, corner_radius=8,
                                      font=ftc(9), fg_color=C_CARD2, hover_color=C_SOFT,
                                      text_color=C_TEXT, command=lambda: self.turn_page(-1))
        self.btn_prev.pack(side="left")
        self.lbl_page = ctk.CTkLabel(pager, text="第 1 页", font=ftc(9), text_color=C_MUTED)
        self.lbl_page.pack(side="left", padx=10)
        self.btn_next = ctk.CTkButton(pager, text="下一页", width=76, height=30, corner_radius=8,
                                      font=ftc(9), fg_color=C_CARD2, hover_color=C_SOFT,
                                      text_color=C_TEXT, command=lambda: self.turn_page(1))
        self.btn_next.pack(side="left")

    def _build_right(self, master):
        panel = ctk.CTkFrame(master, fg_color=C_CARD, corner_radius=14, width=452)
        panel.grid(row=0, column=2, sticky="nsew", padx=(12, 0))
        panel.grid_propagate(False)

        self.lbl_book = ctk.CTkLabel(panel, text="未选择书籍", font=ftc(14, True),
                                     text_color=C_TEXT, anchor="w")
        self.lbl_book.pack(fill="x", padx=16, pady=(14, 2))
        self.lbl_book_meta = ctk.CTkLabel(panel, text="", font=ftc(9), text_color=C_MUTED,
                                          anchor="w", justify="left", wraplength=410)
        self.lbl_book_meta.pack(fill="x", padx=16)

        ctk.CTkLabel(panel, text="章节目录（双击预览）", font=ftc(9), text_color=C_MUTED,
                     anchor="w").pack(fill="x", padx=16, pady=(10, 4))

        tree_wrap = tk.Frame(panel, bg="#FFFFFF", highlightthickness=0)
        tree_wrap.pack(fill="both", expand=True, padx=14)
        self.tree = ttk.Treeview(tree_wrap, columns=("t",), show="tree")
        self.tree.column("#0", width=390, minwidth=200, stretch=True)
        sb = ttk.Scrollbar(tree_wrap, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.tree.bind("<Double-1>", lambda e: self.preview_selected())
        self._style_tree()

        prev_head = ctk.CTkFrame(panel, fg_color="transparent")
        prev_head.pack(fill="x", padx=16, pady=(10, 4))
        ctk.CTkLabel(prev_head, text="正文预览", font=ftc(9), text_color=C_MUTED).pack(side="left")
        self.lbl_ptitle = ctk.CTkLabel(prev_head, text="", font=ftc(9), text_color=C_PRIMARY)
        self.lbl_ptitle.pack(side="left", padx=8)

        text_wrap = ctk.CTkFrame(panel, fg_color=C_CARD2, corner_radius=10)
        text_wrap.pack(fill="both", expand=True, padx=14, pady=(0, 14))
        self.txt = tk.Text(text_wrap, wrap="word", bd=0, relief="flat", bg="#FBFBFC",
                           fg="#1A1D21", insertbackground="#1A1D21", font=(UI_FONT, 10),
                           padx=14, pady=12, spacing1=2, spacing3=4, height=8)
        tsb = ttk.Scrollbar(text_wrap, orient="vertical", command=self.txt.yview)
        self.txt.configure(yscrollcommand=tsb.set)
        self.txt.pack(side="left", fill="both", expand=True, padx=(2, 0), pady=2)
        tsb.pack(side="right", fill="y")

    def _style_tree(self):
        st = ttk.Style()
        st.configure("Treeview", background="#FFFFFF", fieldbackground="#FFFFFF",
                     foreground="#1A1D21", rowheight=29, borderwidth=0, font=(UI_FONT, 10))
        st.configure("Treeview.Heading", background="#FFFFFF", foreground="#7C848E",
                     relief="flat", font=(UI_FONT, 9))
        st.map("Treeview", background=[("selected", "#FFF3EC")],
               foreground=[("selected", "#1A1D21")])
        st.configure("Vertical.TScrollbar", background="#DDE0E4", troughcolor="#FFFFFF",
                     borderwidth=0, arrowsize=12)

    def _build_bottom(self):
        bar = ctk.CTkFrame(self.root, fg_color=C_CARD, corner_radius=14)
        bar.grid(row=2, column=0, sticky="ew", padx=16, pady=(0, 14))
        bar.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(bar, text="保存到", font=ftc(9), text_color=C_MUTED).grid(
            row=0, column=0, padx=(16, 6), pady=(12, 4), sticky="w")
        self.var_dir = tk.StringVar(value=str(Path.home() / "Downloads" / "番茄小说"))
        self.entry_dir = ctk.CTkEntry(bar, textvariable=self.var_dir, height=32, corner_radius=8,
                                      font=ftc(10), fg_color=C_CARD2, border_color=C_LINE,
                                      text_color=C_TEXT)
        self.entry_dir.grid(row=0, column=1, columnspan=2, sticky="ew", pady=(12, 4))
        ctk.CTkButton(bar, text="浏览", width=64, height=32, corner_radius=8, font=ftc(10),
                      fg_color=C_CARD2, hover_color=C_SOFT, text_color=C_TEXT,
                      command=self.pick_dir).grid(row=0, column=3, padx=8, pady=(12, 4))

        ctk.CTkLabel(bar, text="线程", font=ftc(9), text_color=C_MUTED).grid(
            row=0, column=4, padx=(4, 4), pady=(12, 4))
        self.var_th = tk.IntVar(value=8)
        ctk.CTkEntry(bar, textvariable=self.var_th, width=52, height=32, corner_radius=8,
                     font=ftc(10), fg_color=C_CARD2, border_color=C_LINE,
                     text_color=C_TEXT).grid(row=0, column=5, padx=(0, 10), pady=(12, 4))

        self.btn_dl_all = ctk.CTkButton(bar, text="下载全书", width=104, height=32, corner_radius=8,
                                        font=ftc(10, True), fg_color=C_PRIMARY,
                                        hover_color=C_PRIMARY_H, command=lambda: self.start_download(False))
        self.btn_dl_all.grid(row=0, column=6, padx=(0, 8), pady=(12, 4))
        self.btn_dl_sel = ctk.CTkButton(bar, text="下载选中", width=96, height=32, corner_radius=8,
                                        font=ftc(10), fg_color=C_SOFT, hover_color=C_SOFT_H,
                                        text_color=C_PRIMARY, command=lambda: self.start_download(True))
        self.btn_dl_sel.grid(row=0, column=7, padx=(0, 8), pady=(12, 4))
        self.btn_stop = ctk.CTkButton(bar, text="停止", width=64, height=32, corner_radius=8,
                                      font=ftc(10), fg_color=C_CARD2, hover_color=C_SOFT,
                                      text_color=C_MUTED, command=self.stop_download)
        self.btn_stop.grid(row=0, column=8, padx=(0, 16), pady=(12, 4))

        self.pb = ctk.CTkProgressBar(bar, height=8, corner_radius=4, progress_color=C_PRIMARY,
                                     fg_color=C_CARD2)
        self.pb.grid(row=1, column=0, columnspan=9, sticky="ew", padx=16, pady=(0, 4))
        self.pb.set(0)
        self.lbl_status = ctk.CTkLabel(bar, text="就绪", font=ftc(9), text_color=C_MUTED, anchor="w")
        self.lbl_status.grid(row=2, column=0, columnspan=9, sticky="ew", padx=16, pady=(0, 10))

    # ---------------- 主题 ----------------
    def _toggle_theme(self):
        dark = bool(self.sw_dark.get())
        ctk.set_appearance_mode("dark" if dark else "light")
        bg = "#0E1014" if dark else "#FBFBFC"
        fg = "#D7DBE0" if dark else "#1A1D21"
        self.txt.configure(bg=bg, fg=fg, insertbackground=fg)
        for child in self.root.winfo_children():
            if isinstance(child, tk.Frame):
                child.configure(bg=bg)

    # ---------------- 队列 ----------------
    def log(self, msg):
        self.ui_q.put(("status", msg))

    def status(self, msg):
        self.ui_q.put(("status", msg))

    def _pump(self):
        try:
            while True:
                kind, payload = self.ui_q.get_nowait()
                if kind == "status":
                    self.lbl_status.configure(text=payload)
                elif kind == "progress":
                    cur, total = payload
                    self.pb.set(min(1.0, cur / max(1, total)))
                    self.lbl_status.configure(text=f"下载中 {cur}/{total} 章（{cur * 100 // max(1, total)}%）")
                elif kind == "index_progress":
                    cur, total, written = payload
                    self.pb.set(min(1.0, cur / max(1, total)))
                    self.lbl_status.configure(text=f"建立索引 {cur}/{total} 页，已写入 {written} 本")
                elif kind == "index_count":
                    self.lbl_index.configure(text=f"索引：{payload} 本")
                    self.pb.set(0)
                elif kind == "books":
                    items, title, hint = payload
                    self.books = items
                    self.lbl_list_title.configure(text=title)
                    self.lbl_hint.configure(text=hint)
                    self.cards.set_items(items)
                    if items:
                        self.cards.selected = 0
                        self.cards._paint()
                        self.pick_book(items[0])
                elif kind == "chapters":
                    self.fill_chapters(payload)
                elif kind == "preview":
                    self.txt.delete("1.0", "end")
                    self.txt.insert("1.0", payload[1])
                    self.lbl_ptitle.configure(text=payload[0])
                elif kind == "busy":
                    self._set_busy(payload)
                elif kind == "page":
                    self.lbl_page.configure(text=f"第 {self.page + 1} / {max(1, self.page_total)} 页")
                elif kind == "done":
                    messagebox.showinfo("完成", payload)
                    self._set_busy(False)
        except queue.Empty:
            pass
        self.root.after(80, self._pump)

    def _set_busy(self, flag):
        self.busy = flag
        state = "disabled" if flag else "normal"
        for b in (self.btn_dl_all, self.btn_dl_sel, self.btn_search, self.btn_lib,
                  self.btn_index, self.btn_index_deep):
            b.configure(state=state)
        self.btn_stop.configure(state="normal" if flag else "disabled")

    def bg(self, fn, *a, **kw):
        def runner():
            try:
                fn(*a, **kw)
            except Exception as e:
                self.ui_q.put(("status", f"出错：{e}"))
                self.ui_q.put(("busy", False))
                if os.environ.get("FANQIE_DEBUG"):
                    traceback.print_exc()
        threading.Thread(target=runner, daemon=True).start()

    # ---------------- 初始化 ----------------
    def _boot(self):
        self.ui_q.put(("index_count", index_count()))
        try:
            cats = library_categories()
        except Exception as e:
            self.ui_q.put(("status", f"分类加载失败：{e}"))
            return
        self.ui_q.put(("cats", cats))

    def _fill_cats(self, cats):
        self.cats = cats
        mains = cats.get("主分类", [])
        self.main_opts = [("全部", -1)] + [(c["name"], c["category_id"]) for c in mains]
        self.opt_cat.configure(values=[n for n, _ in self.main_opts])
        self.opt_cat.set("全部")
        tags = []
        for label in ("情节", "角色", "主题"):
            for c in cats.get(label, []):
                tags.append((f"{label}·{c['name']}", c["category_id"]))
        self.tag_opts = [("全部", -1)] + tags
        self.opt_tag.configure(values=[n for n, _ in self.tag_opts])
        self.opt_tag.set("全部")

    # ---------------- 动作 ----------------
    def _switch_mode(self, value):
        self.mode = value

    def _current_filter(self):
        gender = {"全部": -1, "男频": 0, "女频": 1}[self.opt_gender.get()]
        status = {"全部": -1, "完结": 0, "连载": 1}[self.opt_status.get()]
        cat = -1
        name = self.opt_cat.get()
        for n, v in self.main_opts:
            if n == name:
                cat = v
                break
        if cat == -1:
            tname = self.opt_tag.get()
            for n, v in self.tag_opts:
                if n == tname:
                    cat = v
                    break
        words = -1
        for n, v in WORD_TIERS:
            if n == self.opt_words.get():
                words = v
        sort = 0
        for n, v in SORT_OPTS:
            if n == self.opt_sort.get():
                sort = v
        return {"gender": gender, "category_id": cat, "creation_status": status,
                "word_count": words, "sort": sort}

    def do_search(self):
        kw = self.entry_kw.get().strip()
        if not kw:
            self.status("先输入书名、作者或关键词")
            return
        if self.busy:
            return
        self._set_busy(True)
        self.lbl_list_title.configure(text="搜索结果")
        self.lbl_hint.configure(text=f"正在搜索「{kw}」…")
        mode = "content" if self.mode == "正文关键词" else "book"
        self.bg(self._search_worker, kw, mode)

    def _search_worker(self, kw, mode):
        try:
            items = search_books(kw, mode=mode, log=self.log)
            self.ui_q.put(("index_count", index_count()))
            hint = f"找到 {len(items)} 本" if items else "没找到；可先建立索引再搜"
            self.ui_q.put(("books", (items, "搜索结果", hint)))
        finally:
            self.ui_q.put(("busy", False))

    def do_library(self):
        if self.busy:
            return
        self.page = 0
        self._set_busy(True)
        self._run_library()

    def _run_library(self):
        self.lbl_hint.configure(text="加载中…")
        self.bg(self._library_worker, self._current_filter(), self.page)

    def _library_worker(self, f, page):
        try:
            rows, total = library_list(page_index=page, **f)
            items = hydrate_books([r["book_id"] for r in rows])
            self.page_total = min(556, max(1, (total + 17) // 18))
            self.ui_q.put(("books", (items, "书库结果", f"共约 {total} 本")))
            self.ui_q.put(("page", None))
        finally:
            self.ui_q.put(("busy", False))

    def turn_page(self, delta):
        if self.busy:
            return
        new = self.page + delta
        if new < 0 or new >= self.page_total:
            return
        self.page = new
        self._set_busy(True)
        self._run_library()

    def do_index(self):
        self._index(deep=False)

    def do_index_deep(self):
        self._index(deep=True)

    def _index(self, deep):
        if self.busy:
            return
        self._set_busy(True)
        self.lbl_status.configure(text="开始建立索引…")
        self.bg(self._index_worker, deep)

    def _index_worker(self, deep):
        try:
            build_index(progress=lambda c, t, w: self.ui_q.put(("index_progress", (c, t, w))),
                        should_stop=lambda: self.stop_flag, deep=deep, log=self.log)
            self.ui_q.put(("index_count", index_count()))
            self.ui_q.put(("status", f"索引完成，共 {index_count()} 本"))
        finally:
            self.ui_q.put(("busy", False))

    def pick_book(self, book):
        self.book = book
        self.lbl_book.configure(text=book.get("name") or "未选择书籍")
        meta = "  ·  ".join(x for x in [
            book.get("author"), (f"{book['words'] / 10000:.1f} 万字" if book.get("words") else ""),
            book.get("status")] if x)
        abstract = (book.get("abstract") or "")[:80]
        self.lbl_book_meta.configure(text=(meta + ("\n" + abstract if abstract else "")))
        self.bg(self._chapters_worker, book)

    def _chapters_worker(self, book):
        try:
            info = book_detail(book["book_id"], with_chapters=True)
            self.ui_q.put(("chapters", info))
        except Exception as e:
            self.ui_q.put(("status", f"目录获取失败：{e}"))

    def fill_chapters(self, info):
        self.book = info
        self.chapters = []
        self.tree.delete(*self.tree.get_children())
        n = 0
        for vi, vol in enumerate(info["volumes"]):
            node = self.tree.insert("", "end", text=f"  {vol['name']}（{len(vol['chapters'])}章）",
                                    open=(vi == 0))
            for ch in vol["chapters"]:
                n += 1
                self.tree.insert(node, "end", iid=ch["id"],
                                 text=f"   {n}. {ch['title']}" + ("  🔒" if ch.get("pay") else ""))
                self.chapters.append(ch)
        self.lbl_status.configure(text=f"《{info['name']}》共 {len(self.chapters)} 章")

    def preview_selected(self):
        sel = self.tree.selection()
        if not sel or not _ID_RE.match(sel[0]):
            self.status("先选中一章（不是卷名）")
            return
        item_id = sel[0]
        title = self.tree.item(item_id, "text").strip()
        self.lbl_status.configure(text=f"正在取正文：{title}")
        self.bg(self._preview_worker, item_id, title)

    def _preview_worker(self, item_id, title):
        info = chapter_text(item_id)
        self.ui_q.put(("preview", (f"{info['title'] or title}　{info['words']}字", info["text"])))
        self.ui_q.put(("status", "预览完成"))

    def pick_dir(self):
        d = filedialog.askdirectory(initialdir=self.var_dir.get() or str(Path.home()))
        if d:
            self.var_dir.set(d)

    # ---------------- 下载 ----------------
    def start_download(self, selected_only):
        if not self.book:
            messagebox.showwarning("提示", "先点一本书，加载它的目录")
            return
        if self.busy:
            return
        if selected_only:
            sel = [i for i in self.tree.selection() if _ID_RE.match(i)]
            chapters = [c for c in self.chapters if c["id"] in set(sel)]
            if not chapters:
                messagebox.showwarning("提示", "没选中章节（Ctrl 可多选）")
                return
        else:
            chapters = list(self.chapters)
        if not chapters:
            return
        self.stop_flag = False
        self._set_busy(True)
        out_dir = self.var_dir.get()
        try:
            threads = max(1, int(self.var_th.get()))
        except Exception:
            threads = 8
        self.lbl_status.configure(text=f"开始下载，共 {len(chapters)} 章…")
        self.bg(self._download_worker, self.book, chapters, out_dir, threads)

    def _download_worker(self, book, chapters, out_dir, threads):
        try:
            out, ok, failed = download_book(
                book, chapters, out_dir, threads=threads, on_log=self.log,
                on_progress=lambda c, t: self.ui_q.put(("progress", (c, t))),
                should_stop=lambda: self.stop_flag)
            msg = f"完成：{ok}/{len(chapters)} 章\n已保存到：{out}"
            if failed:
                msg += f"\n失败 {len(failed)} 章（可重试）"
            self.ui_q.put(("status", "下载完成"))
            self.ui_q.put(("done", msg))
        except Exception as e:
            self.ui_q.put(("status", f"下载失败：{e}"))
            self.ui_q.put(("busy", False))

    def stop_download(self):
        self.stop_flag = True
        self.lbl_status.configure(text="正在停止（已下载的保留）…")


WORD_TIERS = [("全部", -1), ("30万以下", 0), ("30-50万", 1), ("50-100万", 2),
              ("100-200万", 3), ("200万以上", 4)]
SORT_OPTS = [("默认", 0), ("最新", 1), ("字数最多", 2)]


def run_gui():
    ctk.set_appearance_mode("light")
    ctk.set_default_color_theme("blue")
    root = ctk.CTk()
    App(root)
    root.mainloop()


# ------------------------------------------------------------------ 自测
def selftest(keyword="十日终焉"):
    print("== 1) 本地索引 ==")
    print("   当前索引:", index_count(), "本")
    if index_count() == 0:
        print("   首次建索引（书库接口若被限流会自动停下）…")
        t0 = time.time()
        build_index(log=print)
        print(f"   用时 {time.time()-t0:.1f}s，索引 {index_count()} 本")

    print("\n== 2) 搜索 ==")
    for kw in (keyword, "土豆", "恐怖"):
        items = search_books(kw, log=lambda s: None)
        print(f"   「{kw}」→ {len(items)} 本：{[b['name'] for b in items[:3]]}")

    info = book_detail("7143038691944959011", with_chapters=True)
    print(f"\n== 3) 《{info['name']}》{info['total']} 章 ==")
    got = chapter_text(info["volumes"][0]["chapters"][0]["id"])
    print("   ", got["title"], got["words"], "字")
    print("   ", got["text"][:100].replace("\n", " "))
    out, ok, failed = download_book(info, info["volumes"][0]["chapters"][:2],
                                    OUT_DIR, threads=4, on_log=print)
    print(f"   写出 {out}（{ok} 章，失败 {len(failed)}）")
    return 0


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--selftest":
        sys.exit(selftest(sys.argv[2] if len(sys.argv) > 2 else "十日终焉"))
    run_gui()
