"""bili_checker.py — 检查 YouTube 频道在 B站是否有同名账号。"""

import difflib
import logging
import time

import requests

logger = logging.getLogger(__name__)

_BILI_SEARCH_URL = "https://api.bilibili.com/x/web-interface/search/type"
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Referer": "https://www.bilibili.com",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9",
}

_CACHE: dict = {}  # key -> {result, _ts}
_CACHE_TTL = 3600  # 1 hour


def _similarity(a: str, b: str) -> float:
    """计算两个字符串的相似度（0~1）。"""
    a, b = a.lower().strip(), b.lower().strip()
    if not a or not b:
        return 0.0
    # 包含匹配直接给高分
    if a in b or b in a:
        return 0.9
    return difflib.SequenceMatcher(None, a, b).ratio()


def check_channel(channel_name: str, channel_id: str = "", threshold: float = 0.75) -> dict:
    """
    查询 YouTube 频道名在 B站是否有疑似同名账号。

    返回:
        {
            'status': 'found' | 'not_found' | 'error',
            'match_name': str,
            'match_mid': int,
            'match_fans': int,
            'similarity': float,
            'bili_url': str,
        }
    """
    if not channel_name:
        return {"status": "error", "match_name": "", "match_mid": 0, "match_fans": 0, "similarity": 0.0, "bili_url": ""}

    cache_key = (channel_id or channel_name).lower()
    cached = _CACHE.get(cache_key)
    if cached and (time.time() - cached.get("_ts", 0)) < _CACHE_TTL:
        result = {k: v for k, v in cached.items() if k != "_ts"}
        logger.debug(f"bili_check cache hit: {channel_name} -> {result['status']}")
        return result

    try:
        resp = requests.get(
            _BILI_SEARCH_URL,
            params={"search_type": "bili_user", "keyword": channel_name},
            headers=_HEADERS,
            timeout=8,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        logger.warning(f"bili_check request failed for '{channel_name}': {exc}")
        return {"status": "error", "match_name": "", "match_mid": 0, "match_fans": 0, "similarity": 0.0, "bili_url": ""}

    code = data.get("code", -1)
    if code != 0:
        logger.warning(f"bili_check API error {code} for '{channel_name}'")
        return {"status": "error", "match_name": "", "match_mid": 0, "match_fans": 0, "similarity": 0.0, "bili_url": ""}

    results = (data.get("data") or {}).get("result") or []

    best_sim = 0.0
    best_user = None
    for user in results[:5]:
        uname = user.get("uname", "")
        sim = _similarity(channel_name, uname)
        if sim > best_sim:
            best_sim = sim
            best_user = user

    if best_user and best_sim >= threshold:
        result = {
            "status": "found",
            "match_name": best_user.get("uname", ""),
            "match_mid": int(best_user.get("mid", 0)),
            "match_fans": int(best_user.get("fans", 0)),
            "similarity": round(best_sim, 3),
            "bili_url": f"https://space.bilibili.com/{best_user.get('mid', '')}",
        }
    else:
        result = {
            "status": "not_found",
            "match_name": "",
            "match_mid": 0,
            "match_fans": 0,
            "similarity": round(best_sim, 3),
            "bili_url": "",
        }

    _CACHE[cache_key] = {**result, "_ts": time.time()}
    logger.info(f"bili_check '{channel_name}' -> {result['status']} (sim={result['similarity']})")
    return result
