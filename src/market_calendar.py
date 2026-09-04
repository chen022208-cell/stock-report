"""開休市判斷。

重點：不要用「星期幾」去猜今天有沒有開盤，會漏掉國定假日與補行交易日。
一律查 TWSE 的開休市日期資料集，查不到再退回保守推論。
"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from pathlib import Path

import requests

from .config import DATA_DIR, DRY_RUN

HOLIDAY_CACHE = DATA_DIR / "holidays.json"
TWSE_HOLIDAY_URL = "https://openapi.twse.com.tw/v1/holidaySchedule/holidaySchedule"


def _load_cache() -> dict:
    if HOLIDAY_CACHE.exists():
        return json.loads(HOLIDAY_CACHE.read_text(encoding="utf-8"))
    return {}


def _save_cache(data: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    HOLIDAY_CACHE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def refresh_holidays(year: int | None = None) -> dict:
    """抓 TWSE 休市表並快取。一年更新一次就夠（通常 12 月公布隔年）。"""
    if DRY_RUN:
        return _load_cache()
    try:
        resp = requests.get(TWSE_HOLIDAY_URL, timeout=20)
        resp.raise_for_status()
        raw = resp.json()
    except Exception as exc:  # 抓不到就沿用快取，不要讓整個排程掛掉
        print(f"[calendar] 休市表更新失敗，改用快取：{exc}")
        return _load_cache()

    cache = _load_cache()
    for item in raw:
        # 欄位名稱歷年略有差異，逐一嘗試
        raw_date = item.get("Date") or item.get("date") or ""
        name = item.get("Name") or item.get("name") or "休市"
        iso = _normalize_date(raw_date, year or datetime.now().year)
        if iso:
            cache[iso] = name
    _save_cache(cache)
    return cache


def _normalize_date(raw: str, default_year: int) -> str | None:
    """TWSE 日期格式不統一，可能是 1150101 / 115/01/01 / 20260101。"""
    raw = raw.strip().replace("/", "").replace("-", "")
    if not raw.isdigit():
        return None
    try:
        if len(raw) == 8:                      # 西元 20260101
            return f"{raw[:4]}-{raw[4:6]}-{raw[6:]}"
        if len(raw) == 7:                      # 民國 1150101
            return f"{int(raw[:3]) + 1911}-{raw[3:5]}-{raw[5:]}"
        if len(raw) == 4:                      # 只有月日 0101
            return f"{default_year}-{raw[:2]}-{raw[2:]}"
    except ValueError:
        return None
    return None


def is_tw_trading_day(d: date | None = None) -> bool:
    d = d or date.today()
    if d.weekday() >= 5:                       # 週六日
        return False
    return d.isoformat() not in _load_cache()


def is_us_trading_day(d: date | None = None) -> bool:
    """美股粗略判斷：只排除週末。美股國定假日不多，漏抓時資料源會回空值，
    下游有容錯，不值得為此再維護一份美股行事曆。"""
    d = d or date.today()
    return d.weekday() < 5


def classify_day(d: date | None = None) -> str:
    """回傳今天屬於哪一種情境，決定跑哪個分支。

    trading          台股有開 → 正常流程
    tw_closed_only   台股休市但國際盤照跑 → 只發國際盤，台股報告跳過
    full_holiday     兩邊都休 → 發假日功課
    """
    d = d or date.today()
    tw, us = is_tw_trading_day(d), is_us_trading_day(d)
    if tw:
        return "trading"
    # 台股休市時，看的是「昨晚」的美股（台北時間的前一天）
    if us or is_us_trading_day(d - timedelta(days=1)):
        return "tw_closed_only"
    return "full_holiday"


def consecutive_closed_days(d: date | None = None) -> int:
    """往回數連續休市天數，用來判斷是否為農曆年這種長假。"""
    d = d or date.today()
    n, cursor = 0, d
    while not is_tw_trading_day(cursor) and n < 15:
        n += 1
        cursor -= timedelta(days=1)
    return n


def is_last_day_before_reopen(d: date | None = None) -> bool:
    """長假最後一天 → 該產出「假期彙整報告」，避免開紅盤被跳空嚇到。"""
    d = d or date.today()
    if is_tw_trading_day(d):
        return False
    tomorrow_open = is_tw_trading_day(d + timedelta(days=1))
    return tomorrow_open and consecutive_closed_days(d) > 2


def next_trading_day(d: date | None = None) -> date:
    cursor = (d or date.today()) + timedelta(days=1)
    for _ in range(15):
        if is_tw_trading_day(cursor):
            return cursor
        cursor += timedelta(days=1)
    return cursor
