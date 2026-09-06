"""把使用者提交的「文章網址」抓成純文字，給研究提交分析用。

為什麼需要這一支：提交研究頁一直有「貼連結網址」這個模式，但後端從來沒有
去抓那個網址——`llm.analyze_research_submission()` 是直接把使用者輸入當成
「原文」塞進 prompt，所以貼連結時 LLM 收到的原文就只有一行網址，什麼都分析
不出來（使用者實際回報：「感覺只有貼文章的會成功」）。

安全性：這裡抓的是**使用者提供的任意網址**，抓回來的內容之後會進 LLM prompt，
所以一律當成不可信的資料：
- 只允許 http/https，擋掉 file:// 這類 scheme
- 擋掉指向內網／本機的位址（SSRF）
- 限制下載大小與逾時，避免被一個巨大檔案拖死
- 只回純文字，不回 HTML，也不執行任何東西
抓回來的內容仍然要走既有那套嚴格驗證（verified／conflicting／unverified），
這一支只負責「把文字拿到手」，不負責判斷真偽。
"""
from __future__ import annotations

import ipaddress
import re
import socket
from urllib.parse import urlparse

import requests

TIMEOUT = 20
MAX_BYTES = 2_000_000          # 2MB，正常文章遠小於此
MAX_CHARS = 20_000             # 交給 LLM 的上限（analyze 那邊還會再截到 8000）
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36",
    "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.8",
}

URL_RE = re.compile(r"^\s*(https?://\S+)\s*$", re.I)


def looks_like_url(text: str) -> str:
    """整段輸入就是一個網址時回傳該網址，否則回空字串。

    刻意只認「整段就是一個網址」：使用者貼一整篇文章、裡面剛好含有連結時，
    要分析的是那篇文章本身，不是去抓文章裡的某個連結。
    """
    m = URL_RE.match(text or "")
    return m.group(1) if m else ""


def _is_public_host(host: str) -> bool:
    """擋掉 localhost／內網／保留位址（SSRF 防護）。"""
    if not host:
        return False
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return False
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except ValueError:
            return False
        if (ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_reserved or ip.is_multicast or ip.is_unspecified):
            return False
    return True


def _html_to_text(html: str) -> str:
    """抽出正文純文字。優先用 BeautifulSoup，沒有就退回粗略的去標籤。"""
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        text = re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>", " ", html)
        text = re.sub(r"(?s)<[^>]+>", " ", text)
        return re.sub(r"\s+", " ", text).strip()

    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript", "nav", "header", "footer",
                     "form", "aside", "iframe", "svg"]):
        tag.decompose()
    # 有 <article> / <main> 就以它為主，通常能濾掉導覽列與頁尾雜訊
    node = soup.find("article") or soup.find("main") or soup.body or soup
    text = node.get_text("\n", strip=True)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def fetch_article(url: str) -> dict:
    """抓一個網址 → {"ok", "url", "title", "text", "error"}。

    失敗一律回 ok=False ＋ error 說明，不丟例外也不回半套內容——呼叫端要能
    據此誠實告訴使用者「這個連結抓不到」，而不是拿空字串去硬分析。
    """
    parsed = urlparse(url or "")
    if parsed.scheme not in ("http", "https"):
        return {"ok": False, "url": url, "title": "", "text": "",
                "error": "只支援 http／https 網址"}
    if not _is_public_host(parsed.hostname or ""):
        return {"ok": False, "url": url, "title": "", "text": "",
                "error": "網址指向內網或無法解析的主機，基於安全不抓取"}

    try:
        resp = requests.get(url, headers=HEADERS, timeout=TIMEOUT, stream=True)
        resp.raise_for_status()
        ctype = (resp.headers.get("Content-Type") or "").lower()
        if not any(t in ctype for t in ("text/html", "text/plain", "application/xhtml")):
            return {"ok": False, "url": url, "title": "", "text": "",
                    "error": f"不是可讀的網頁內容（Content-Type: {ctype or '未知'}）"}

        chunks, total = [], 0
        for chunk in resp.iter_content(8192):
            chunks.append(chunk)
            total += len(chunk)
            if total >= MAX_BYTES:
                break
        raw = b"".join(chunks)
        resp.close()
        encoding = resp.encoding or "utf-8"
        html = raw.decode(encoding, errors="replace")
    except Exception as exc:
        return {"ok": False, "url": url, "title": "", "text": "",
                "error": f"抓取失敗：{exc}"}

    title = ""
    m = re.search(r"(?is)<title[^>]*>(.*?)</title>", html)
    if m:
        title = re.sub(r"\s+", " ", m.group(1)).strip()[:200]

    text = _html_to_text(html)
    if len(text) < 120:
        return {"ok": False, "url": url, "title": title, "text": text,
                "error": "這個網址抓不到足夠的文字內容（可能是需要登入、"
                         "或內容由 JavaScript 動態載入）"}
    return {"ok": True, "url": url, "title": title,
            "text": text[:MAX_CHARS], "error": ""}
