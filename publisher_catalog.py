"""Official publisher entry points for manual browser searches.

These links are not metadata providers: following them never adds papers to the
application. Query parameters come from publisher search forms; sites may require
cookies, a browser challenge or institutional access. AMS does not expose a query
link verified in this environment, so its journal portal is an explicit manual
fallback. Reviewed 2026-10-26; Springer, Cambridge and De Gruyter forms fetched
successfully, other platforms can reject automated HTTP checks (403).
"""
from __future__ import annotations

from urllib.parse import urlencode

PUBLISHERS = {
    "elsevier": {"name": "Elsevier · ScienceDirect", "url": "https://www.sciencedirect.com/search", "query_key": "qs"},
    "springer": {"name": "Springer Nature Link", "url": "https://link.springer.com/search", "query_key": "query"},
    "wiley": {"name": "Wiley Online Library", "url": "https://onlinelibrary.wiley.com/action/doSearch", "query_key": "AllField"},
    "siam": {"name": "SIAM Publications", "url": "https://epubs.siam.org/action/doSearch", "query_key": "AllField"},
    "ams": {"name": "AMS · 美国数学会", "url": "https://www.ams.org/journals/", "query_key": None},
    "cambridge": {"name": "Cambridge Core", "url": "https://www.cambridge.org/core/search", "query_key": "q"},
    "oxford": {"name": "Oxford Academic", "url": "https://academic.oup.com/search-results", "query_key": "q"},
    "taylor_francis": {"name": "Taylor & Francis", "url": "https://www.tandfonline.com/action/doSearch", "query_key": "AllField"},
    "degruyter": {"name": "De Gruyter Brill", "url": "https://www.degruyterbrill.com/search", "query_key": "query"},
}


def search_link(publisher: str, query: str) -> str:
    """Encode the complete query as one value; never accept an arbitrary URL."""
    entry = PUBLISHERS[publisher]
    query = str(query or "").strip()
    if not query:
        raise ValueError("请输入关键词或论文标题。")
    key = entry["query_key"]
    return entry["url"] + ("?" + urlencode({key: query}) if key else "")


def publisher_note(publisher: str) -> str:
    if not PUBLISHERS[publisher]["query_key"]:
        return "AMS：打开期刊入口；先复制关键词，再在网站内检索。"
    return "携带当前关键词打开。年份、排序请在网站内设置；检索语法以网站为准。"
