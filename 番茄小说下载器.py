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


SEARCH_API = "https://novel.snssdk.com/api/novel/channel/homepage/search/search/v1/"


def official_search(keyword, limit=40, offset=0, log=None, meta=None):
    """番茄官方轻量搜索接口：不需要签名，支持 offset 翻页，能搜全站。

    每页只给 10 条，翻 6 页如果一串请求排队发，就是 6 秒白等；
    这里把 6 页并发抓下来，通常一秒多就有结果。
    """
    keyword = (keyword or "").strip()
    if not keyword:
        return []
    if meta is not None:
        meta["has_more"] = False
        meta["next_offset"] = offset

    def fetch(off):
        params = {"aid": "1967", "q": keyword, "offset": str(off)}
        url = SEARCH_API + "?" + urllib.parse.urlencode(params)
        payload = json.loads(Http.get(url, referer="https://fanqienovel.com/", timeout=20, retries=2))
        if payload.get("code") != 0:
            raise RuntimeError(f"code={payload.get('code')}")
        data = payload.get("data") or {}
        return (data.get("ret_data") or [], bool(data.get("has_more")),
                int(data.get("offset") or (off + 10)))

    pages = max(1, (limit + 9) // 10)
    got = {}
    with ThreadPoolExecutor(max_workers=min(4, pages)) as pool:
        futs = {i: pool.submit(fetch, offset + i * 10) for i in range(pages)}
        for i in range(pages):
            try:
                got[i] = futs[i].result(timeout=40)
            except Exception as e:
                if log:
                    log(f"[官方搜索] 第 {i + 1} 页失败：{e}")

    out, seen = [], set()
    last_ok = -1
    for i in range(pages):
        if i not in got:
            break
        last_ok = i
        rows, more, next_off = got[i]
        for b in rows:
            bid = str(b.get("book_id") or "")
            if not bid or bid in seen:
                continue
            seen.add(bid)
            out.append({
                "book_id": bid,
                "name": b.get("title") or b.get("book_name") or "",
                "author": b.get("author") or "",
                "abstract": (b.get("abstract") or "").replace("\n", " ").strip(),
                "words": int(b.get("word_number") or 0),
                "status": "完结" if str(b.get("creation_status")) == "0" else "连载中",
                "category": b.get("category") or "",
                "cover": b.get("thumb_url") or "",
            })
        if meta is not None:
            meta["has_more"] = more
            meta["next_offset"] = next_off
        if not rows or not more:
            break
    if meta is not None and last_ok + 1 < pages and not out:
        # 一页都没成：把游标留在原地，用户点"加载更多"还能重试
        meta["next_offset"] = offset
    if log:
        log(f"[官方搜索] 命中 {len(out)} 本（并发 {pages} 页）")
    return out[:limit]


def search_books(keyword, mode="book", limit=60, log=None, auto_index=True, offset=0, meta=None):
    """搜索：官方免签名接口（全站）优先，其次本地索引，最后搜索引擎兜底。"""
    kw = (keyword or "").strip()
    if not kw:
        return []
    if _ID_RE.match(kw):
        return [detail_cached(kw)]
    if "fanqienovel.com" in kw:
        m = _ISBN_RE.search(kw)
        return [detail_cached(m.group(1))] if m else []

    # 1) 官方搜索（全站、免签名）
    try:
        hits = official_search(kw, limit=limit, offset=offset, log=log, meta=meta)
        if hits:
            return hits
    except Exception as e:
        if log:
            log(f"[官方搜索] 失败：{e}")

    # 2) 本地索引
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


# ------------------------------------------------------------- AI 找书
AI_FILE = CACHE_DIR / "ai.json"
AI_DEFAULTS = {"base_url": "https://api.deepseek.com", "model": "deepseek-chat", "api_key": ""}


def ai_config():
    cfg = dict(AI_DEFAULTS)
    try:
        if AI_FILE.exists():
            cfg.update(json.loads(AI_FILE.read_text(encoding="utf-8")))
    except Exception:
        pass
    if os.environ.get("DEEPSEEK_API_KEY"):
        cfg["api_key"] = os.environ["DEEPSEEK_API_KEY"]
    return cfg


def ai_save_config(cfg):
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        AI_FILE.write_text(json.dumps(cfg, ensure_ascii=False, indent=1), encoding="utf-8")
        return True
    except Exception:
        return False


def ai_ready():
    return bool((ai_config().get("api_key") or "").strip())


def ai_chat(messages, temperature=0.3, json_mode=False, timeout=90):
    """调用 DeepSeek（OpenAI 兼容）接口。"""
    cfg = ai_config()
    key = (cfg.get("api_key") or "").strip()
    if not key:
        raise RuntimeError("还没配置 DeepSeek API Key，点右上角 ⚙ 填一下")
    body = {"model": cfg.get("model") or "deepseek-chat", "messages": messages,
            "temperature": temperature, "stream": False}
    if json_mode:
        body["response_format"] = {"type": "json_object"}
    url = (cfg.get("base_url") or AI_DEFAULTS["base_url"]).rstrip("/") + "/chat/completions"
    req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"), headers={
        "Content-Type": "application/json", "Authorization": "Bearer " + key})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:200]
        if e.code == 401:
            raise RuntimeError("DeepSeek API Key 无效，请重新填写")
        raise RuntimeError(f"AI 接口返回 {e.code}：{detail}")
    except Exception as e:
        raise RuntimeError(f"AI 接口连接失败：{e}")
    return (data.get("choices") or [{}])[0].get("message", {}).get("content", "")


def _json_of(text, fallback=None):
    text = (text or "").strip()
    m = re.search(r"\{.*\}", text, re.S)
    if m:
        text = m.group(0)
    try:
        return json.loads(text)
    except Exception:
        return fallback if fallback is not None else {}


def ai_search(query, limit=40, log=None):
    """自然语言找书：AI 拆关键词 → 官方搜索 → AI 按简介精排。"""
    query = (query or "").strip()
    if not query:
        raise RuntimeError("先用一句话描述你想看的故事")

    if log:
        log("[AI] 正在理解你的需求…")
    plan = _json_of(ai_chat([
        {"role": "system", "content": "你是网文检索助手。只输出 JSON。"},
        {"role": "user", "content":
            f"读者需求：{query}\n\n"
            "请输出 JSON：{\"keywords\":[3到5个用于站内搜索的关键词，2到6个字，覆盖题材/设定/主角特点],"
            "\"note\":\"一句话概括你理解的需求\"}。关键词要具体，不要用‘小说’这种泛词。"},
    ], temperature=0.2, json_mode=True))

    keywords = [str(k).strip() for k in (plan.get("keywords") or []) if str(k).strip()][:5]
    if not keywords:
        keywords = [query]
    note = plan.get("note") or query
    if log:
        log(f"[AI] 关键词：{'、'.join(keywords)}")

    pool, seen = [], set()
    for kw in keywords:
        try:
            for b in official_search(kw, limit=30):
                if b["book_id"] not in seen:
                    seen.add(b["book_id"])
                    pool.append(b)
        except Exception as e:
            if log:
                log(f"[AI] 关键词「{kw}」检索失败：{e}")
    if not pool:
        return [], note

    if log:
        log(f"[AI] 候选 {len(pool)} 本，正在按简介精排…")
    brief = "\n".join(
        f'{i}. {b["name"]}｜{b.get("author")}｜{(b.get("abstract") or "")[:90]}'
        for i, b in enumerate(pool[:90]))
    ranked = _json_of(ai_chat([
        {"role": "system", "content": "你是网文编辑，只输出 JSON。"},
        {"role": "user", "content":
            f"读者需求：{query}\n\n候选书单：\n{brief}\n\n"
            "挑出最符合的，按契合度从高到低排序，最多 30 本。"
            "只输出 JSON：{\"picks\":[{\"i\":序号,\"why\":\"10字内理由\"}]}"},
    ], temperature=0.2, json_mode=True))

    picks = ranked.get("picks") or []
    out, used = [], set()
    for item in picks:
        try:
            idx = int(item.get("i"))
        except Exception:
            continue
        if idx < 0 or idx >= len(pool) or idx in used:
            continue
        used.add(idx)
        book = dict(pool[idx])
        book["why"] = (item.get("why") or "")[:24]
        out.append(book)
        if len(out) >= limit:
            break
    if not out:
        out = pool[:limit]
    if log:
        log(f"[AI] 选出 {len(out)} 本")
    return out, note


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


# ------------------------------------------------------------ WebView 桥
import webview  # noqa: E402

_stop_flag = threading.Event()
_book_cache = {}
_dl_running = threading.Event()


def _chapters_flat(info):
    out = []
    for vol in info.get("volumes", []):
        for ch in vol["chapters"]:
            out.append({"id": ch["id"], "title": ch.get("title", ""),
                        "pay": ch.get("pay", 0), "vol": vol["name"]})
    return out


def _fix_cover(url):
    """封面地址收拾成能直接用的：换掉有防盗链的域名，并让 CDN 只给缩略图尺寸。

    列表里封面只显示 52x70，却拉 640 宽的图（一张 150KB、解码占 1MB 内存），
    60 本就要 90MB 内存。改成 160 宽：一张 12KB，内存省一个数量级。
    """
    u = str(url or "")
    u = re.sub(r"https?://p\d+-tt\.bytecdn\.cn", "https://p3-novel.byteimg.com", u)
    u = re.sub(r"~tplv-shrink:\d+:", "~tplv-shrink:160:", u)
    return u


def _fix_covers(items):
    for b in items or []:
        if b.get("cover"):
            b["cover"] = _fix_cover(b["cover"])
    return items


def default_out_dir():
    """默认保存目录：优先用户下载文件夹，不可写就退回项目内 下载/。"""
    candidates = [Path.home() / "Downloads" / "番茄小说", OUT_DIR]
    for d in candidates:
        try:
            d.mkdir(parents=True, exist_ok=True)
            probe = d / ".write_test"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
            return str(d)
        except Exception:
            continue
    return str(OUT_DIR)


def _default_threads():
    """默认下载线程数：按核数给，别把 CPU 全占死（界面还要渲染）。"""
    cores = os.cpu_count() or 2
    return max(2, min(8, cores * 2))


class Api:
    """暴露给前端的接口（js_api）"""

    def __init__(self):
        self.window = None

    # ---------- 搜索 ----------
    def search(self, kw, mode="book"):
        try:
            meta = {}
            if mode == "ai":
                items, note = ai_search(kw, limit=40, log=lambda s: print(s))
                return {"items": _fix_covers(items), "has_more": False, "next_offset": 0, "note": note}
            items = search_books(kw, mode=mode, limit=60, meta=meta, log=lambda s: print(s))
            return {"items": _fix_covers(items), "has_more": bool(meta.get("has_more")),
                    "next_offset": meta.get("next_offset", len(items))}
        except Exception as e:
            return {"error": str(e)}

    def more(self, kw, mode="book", offset=0):
        try:
            meta = {}
            items = search_books(kw, mode=mode, limit=30, offset=int(offset or 0), meta=meta)
            return {"items": _fix_covers(items), "has_more": bool(meta.get("has_more")),
                    "next_offset": meta.get("next_offset", 0)}
        except Exception as e:
            return {"error": str(e)}

    # ---------- 书库 ----------
    def library(self, filters, page=0):
        try:
            rows, total = library_list(page_index=int(page or 0), **(filters or {}))
            items = hydrate_books([r["book_id"] for r in rows])
            return {"items": _fix_covers(items), "total": total,
                    "page_total": max(1, min(556, (total + 17) // 18))}
        except Exception as e:
            return {"error": str(e)}

    def meta(self):
        try:
            cats = library_categories()
        except Exception as e:
            print("[meta] 分类失败:", e, flush=True)
            cats = {}
        n = index_count()
        print(f"[meta] index_count={n} 分类组={list(cats.keys())}", flush=True)
        return {"index_count": n, "categories": cats, "out_dir": default_out_dir(),
                "threads": _default_threads()}

    # ---------- 详情 / 章节 ----------
    def detail(self, book_id):
        try:
            info = book_detail(book_id, with_chapters=True)
            _book_cache[book_id] = info
            book = {k: info[k] for k in ("book_id", "name", "author", "status",
                                         "words", "abstract", "cover") if k in info}
            book["cover"] = _fix_cover(book.get("cover"))
            return {"book": book, "chapters": _chapters_flat(info)}
        except Exception:
            return {"error": "这本书打不开（可能已下架或仅 App 可读）"}

    def chapter(self, item_id):
        try:
            info = chapter_text(item_id)
            return {"title": info["title"], "text": info["text"], "words": info["words"]}
        except Exception as e:
            return {"error": f"正文取不到：{e}"}

    # ---------- 下载 ----------
    def download(self, book_id, ids=None, threads=8, out_dir=None):
        info = _book_cache.get(book_id)
        if not info:
            try:
                info = book_detail(book_id, with_chapters=True)
                _book_cache[book_id] = info
            except Exception as e:
                return {"error": str(e)}
        chapters = [c for c in _chapters_flat(info)]
        if ids:
            wanted = set(ids)
            chapters = [c for c in chapters if c["id"] in wanted]
        if not chapters:
            return {"error": "没有可下载的章节"}
        out_dir = (out_dir or "").strip() or default_out_dir()
        # 防御：前端可能传来无效/无权限的路径（例如 C:\Users\Downloads）
        try:
            Path(out_dir).mkdir(parents=True, exist_ok=True)
            probe = Path(out_dir) / ".write_test"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
        except Exception:
            out_dir = default_out_dir()
        _stop_flag.clear()
        _dl_running.set()

        def on_progress(cur, total):
            _progress["download"] = [cur, total]

        try:
            out, ok, failed = download_book(
                info, chapters, out_dir, threads=max(1, int(threads or 8)),
                on_log=lambda s: print(s), on_progress=on_progress,
                should_stop=lambda: _stop_flag.is_set())
            return {"ok": ok, "path": str(out), "failed": len(failed)}
        except Exception as e:
            return {"error": str(e)}
        finally:
            _dl_running.clear()

    def progress(self):
        return dict(_progress)

    def stop(self):
        _stop_flag.set()
        return {"ok": True}

    # ---------- 杂项 ----------
    def pick_dir(self):
        try:
            res = self.window.create_file_dialog(webview.FOLDER_DIALOG)
            if res:
                return {"path": res[0] if isinstance(res, (list, tuple)) else res}
        except Exception:
            pass
        return {"path": str(Path.home() / "Downloads" / "番茄小说")}

    def get_ai_config(self):
        cfg = ai_config()
        cfg = dict(cfg)
        cfg["api_key"] = cfg.get("api_key") or ""
        return cfg

    def save_ai_config(self, key, base_url, model):
        ai_save_config({"api_key": key or "", "base_url": base_url or "https://api.deepseek.com",
                        "model": model or "deepseek-chat"})
        return {"ok": True}

    def build_index(self):
        def run():
            try:
                build_index(
                    progress=lambda c, t, w: _progress.__setitem__("index", [c, t, w]),
                    should_stop=lambda: _stop_flag.is_set(), log=lambda s: print(s))
            except Exception as e:
                print("索引失败", e)
            finally:
                _progress["index_done"] = index_count()

        threading.Thread(target=run, daemon=True).start()
        return {"started": True}


# --------------------------------------------------------- 本机 HTTP 服务
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler  # noqa: E402

_progress = {"download": [0, 0], "index": [0, 0, 0], "index_done": 0}

LOG_FILE = CACHE_DIR / "app.log"


def _log(msg):
    """把关键事件写进 .cache/app.log。界面出问题时看这个文件就够了。"""
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%m-%d %H:%M:%S')} {msg}\n")
    except Exception:
        pass


class _Server(ThreadingHTTPServer):
    """加固过的 HTTP 服务：

    - 队列加大，慢请求不会把连接堆在门外
    - 出错只记一行日志，绝不走 traceback.print_exc()（它会去读源文件，
      异常时曾把处理线程卡死，表现就是整个界面失去响应）
    """

    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 128

    def handle_error(self, request, client_address):
        etype, evalue, _ = sys.exc_info()
        _log(f"[http] {getattr(etype, '__name__', etype)}: {evalue} <- {client_address}")


class _Handler(BaseHTTPRequestHandler):
    api = None
    ui_file = None
    protocol_version = "HTTP/1.0"   # 短连接：一次请求一条连接，线程用完就退，不会越积越多
    timeout = 20                    # 客户端不发请求就断开，不让线程无限等

    def log_message(self, *args):
        pass

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?")[0]
        if path in ("/", "/index.html"):
            data = self.ui_file.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)
        elif path == "/api/progress":
            self._json(_progress)
        elif path == "/api/ping":
            self._json({"ok": True})
        else:
            self._json({"error": "not found"}, 404)

    def do_POST(self):
        path = self.path.split("?")[0]
        if not path.startswith("/api/"):
            return self._json({"error": "not found"}, 404)
        name = path[5:]
        t0 = time.perf_counter()
        try:
            length = int(self.headers.get("Content-Length") or 0)
            args = json.loads(self.rfile.read(length) or b"[]") if length else []
            fn = getattr(self.api, name, None)
            if not callable(fn):
                return self._json({"error": f"未知接口 {name}"}, 400)
            result = fn(*args)
            self._json(result if isinstance(result, dict) else {"ok": True, "data": result})
        except Exception as e:
            _log(f"[api] {name} 失败：{type(e).__name__}: {e}")
            self._json({"error": str(e)}, 500)
        finally:
            cost = time.perf_counter() - t0
            if cost > 1.5:
                _log(f"[api] {name} 用了 {cost:.1f}s")


def _serve_forever(httpd):
    """serve_forever 万一抛异常（例如系统资源耗尽），自动重启，不留下半死的服务。"""
    while True:
        try:
            httpd.serve_forever(poll_interval=0.5)
            return
        except Exception as e:
            _log(f"[http] serve_forever 异常，2 秒后重启：{type(e).__name__}: {e}")
            setattr(httpd, "_BaseServer__shutdown_request", False)
            time.sleep(2)


def start_server(api):
    _Handler.api = api
    _Handler.ui_file = APP_DIR / "ui" / "index.html"
    httpd = None
    for port in (8731, 0):
        try:
            httpd = _Server(("127.0.0.1", port), _Handler)
            break
        except OSError as e:
            _log(f"[http] 端口 {port} 打不开（{e}），换随机端口")
    if httpd is None:
        raise RuntimeError("本机 HTTP 服务启动失败")
    threading.Thread(target=_serve_forever, args=(httpd,), daemon=True).start()
    return httpd, httpd.server_address[1]


# WebView2 默认会开一堆子进程；这台机器核少内存紧，把它压到最小：
# 一个渲染进程、JS 堆上限 64M、不用 GPU 进程（集显还要抢同一份内存）、关掉后台联网。
WEBVIEW2_ARGS = " ".join([
    "--disable-gpu",
    "--renderer-process-limit=1",
    "--js-flags=--max-old-space-size=64",
    "--disable-background-networking",
    "--disable-features=CalculateNativeWinOcclusion,msEdgeMemorySaver",
    "--disable-extensions",
    "--disable-sync",
    "--no-first-run",
])


def _ui_url(port):
    """界面地址。设了环境变量 FANQIE_DEBUG_Q 就自动搜这个词（截图/自测用）。"""
    url = f"http://127.0.0.1:{port}/"
    q = (os.environ.get("FANQIE_DEBUG_Q") or "").strip()
    if q:
        url += "?q=" + urllib.parse.quote(q)
    return url


def _screen_size():
    """屏幕工作区尺寸（像素）。取不到就按 1366x768 算。"""
    try:
        import ctypes
        from ctypes import wintypes
        rect = wintypes.RECT()
        if ctypes.windll.user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(rect), 0):
            return rect.right - rect.left, rect.bottom - rect.top
    except Exception:
        pass
    return 1366, 768


def _already_running(port=8731):
    """已经有一个实例在跑？（能 ping 通才算，光占着端口不算）"""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/ping", timeout=1.0) as r:
            return json.loads(r.read().decode("utf-8")).get("ok") is True
    except Exception:
        return False


def run_gui():
    os.environ.setdefault("WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS", WEBVIEW2_ARGS)
    _log("======== 启动界面 ========")
    if _already_running():
        _log("[app] 已经有实例在运行，本次启动退出")
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(
                None, "番茄小说下载器已经在运行了，去任务栏找一下它。",
                "番茄小说下载器", 0x40)
        except Exception:
            pass
        return
    api = Api()
    httpd, port = start_server(api)
    _log(f"[http] 监听 http://127.0.0.1:{port}/")
    sw, sh = _screen_size()
    w, h = min(1380, max(1000, sw - 40)), min(880, max(680, sh - 40))
    window = webview.create_window(
        "番茄小说下载器", url=_ui_url(port),
        width=w, height=h, min_size=(980, 640),
        background_color="#0A0B10", text_select=True,
    )
    api.window = window
    try:
        webview.start(debug=False)
    finally:
        _log("======== 退出界面 ========")
        httpd.shutdown()


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--selftest":
        sys.exit(selftest(sys.argv[2] if len(sys.argv) > 2 else "十日终焉"))
    run_gui()
