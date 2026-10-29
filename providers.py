"""Public scholarly metadata search. No PDF or supplementary files are downloaded here.

Sources: public scholarly indexes declared in SOURCE_NAMES. Transport uses the standard library;
metadata cache is local to this application. Search results are not access guarantees.
"""
from __future__ import annotations

import concurrent.futures
import email.utils
import hashlib
import html
import json
import re
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Callable

from core import APP_DIR, deduplicate, normalize_doi, paper_id

try:
    import extra_providers
except ImportError:
    extra_providers = None

CACHE_DIR = Path(APP_DIR) / "data" / "cache"
CACHE_TTL = 24 * 60 * 60
REQUEST_TIMEOUT = 12
MAX_ATTEMPTS = 2
MAX_RESPONSE_BYTES = 12 * 1024 * 1024
MAX_ATTEMPT_SECONDS = 25
USER_AGENT = "PaperHarbor/0.2 (desktop academic literature metadata search)"
SOURCE_NAMES = {"openalex": "OpenAlex", "arxiv": "arXiv", "crossref": "Crossref"}
SOURCE_PAGE_SIZES = {"openalex": 100, "arxiv": 100, "crossref": 100}
SOURCE_INTERVALS = {"openalex": 0.3, "arxiv": 3.0, "crossref": 1.0}
if extra_providers is not None:
    SOURCE_NAMES.update(getattr(extra_providers, "SOURCE_NAMES", {}))
    SOURCE_PAGE_SIZES.update(getattr(extra_providers, "SOURCE_PAGE_SIZES", {}))
    SOURCE_INTERVALS.update(getattr(extra_providers, "SOURCE_INTERVALS", {}))
MAX_RESULTS = 500
MAX_PAGES = 20
_THROTTLE_LOCK = threading.Lock()
_SOURCE_LOCKS = {}
_SOURCE_LAST_START = {}
ATOM = "{http://www.w3.org/2005/Atom}"
ARXIV = "{http://arxiv.org/schemas/atom}"
OPENSEARCH = "{http://a9.com/-/spec/opensearch/1.1/}"


class SearchCancelled(Exception):
    pass


class ProviderError(Exception):
    """Safe, user-facing network/metadata error without secrets or response bodies."""

    def __init__(self, message, status_code=None):
        super().__init__(message)
        self.status_code = status_code


def _check_cancel(cancel_event):
    if cancel_event is not None and cancel_event.is_set():
        raise SearchCancelled()


def _wait(seconds, cancel_event):
    if cancel_event is not None:
        if cancel_event.wait(seconds):
            raise SearchCancelled()
    else:
        time.sleep(seconds)


def _notify(progress, message):
    if progress:
        try:
            progress(message)
        except Exception:
            pass  # A view callback must never turn a successful search into failure.


def _safe_url(value):
    if not isinstance(value, str):
        return ""
    value = value.strip()
    try:
        parts = urllib.parse.urlsplit(value)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            return ""
        if parts.username or parts.password:
            return ""
    except ValueError:
        return ""
    return value


def _clean(value):
    if not isinstance(value, str):
        return ""
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", value))).strip()


def _as_int(value):
    try:
        return int(value) if value is not None else None
    except (ValueError, TypeError):
        return None


def _unique(values):
    return list(dict.fromkeys(v for v in values if v))


def _arxiv_id(url):
    """Keep version identifiers; canonical version deduplication belongs to core."""
    if not isinstance(url, str):
        return ""
    try:
        parsed = urllib.parse.urlsplit(url)
        if parsed.hostname not in ("arxiv.org", "www.arxiv.org", "export.arxiv.org"):
            return ""
        path = urllib.parse.unquote(parsed.path)
        match = re.match(r"/(?:abs|pdf)/(.+?)(?:\.pdf)?$", path)
        return match.group(1) if match else ""
    except ValueError:
        return ""


def _abstract_from_index(index):
    if not isinstance(index, dict):
        return ""
    pairs = []
    for word, positions in index.items():
        if not isinstance(word, str) or not isinstance(positions, list):
            continue
        for pos in positions:
            if isinstance(pos, int) and 0 <= pos < 100000:
                pairs.append((pos, word))
    pairs.sort()
    return " ".join(word for _, word in pairs)


def _finish(paper):
    paper["id"] = paper_id(paper)
    return paper


def _normalize_openalex(item):
    primary = item.get("primary_location") or {}
    source = primary.get("source") or {}
    locations = [item.get("best_oa_location") or {}, primary] + (item.get("locations") or [])
    pdf_urls, oa_urls, location_info = [], [], []
    for loc in locations:
        if not isinstance(loc, dict) or loc.get("is_oa") is not True:
            continue
        pdf = _safe_url(loc.get("pdf_url"))
        landing = _safe_url(loc.get("landing_page_url"))
        if pdf:
            pdf_urls.append(pdf)
            location_info.append({"url": pdf, "source": "OpenAlex", "is_oa": True,
                                  "license": loc.get("license"), "version": loc.get("version")})
        if landing:
            oa_urls.append(landing)
    doi = normalize_doi(item.get("doi") or "")
    title = _clean(item.get("display_name") or item.get("title")) or "（无标题）"
    work_type = item.get("type") or ""
    if work_type == "preprint":
        version = "预印本"
    elif work_type == "article" and source.get("type") == "journal":
        version = "期刊论文"
    else:
        version = "文献"
    arxiv_id = next((aid for url in _unique(oa_urls + pdf_urls)
                     if (aid := _arxiv_id(url))), "")
    authors = [_clean((a.get("author") or {}).get("display_name"))
               for a in item.get("authorships") or [] if isinstance(a, dict)]
    return _finish({
        "title": title, "authors": [a for a in authors if a],
        "year": _as_int(item.get("publication_year")),
        "venue": _clean(source.get("display_name")), "doi": doi, "arxiv_id": arxiv_id,
        "publisher": _clean(source.get("host_organization_name")) if source.get("type") == "journal" else "",
        "issns": _unique([_clean(v) for v in (source.get("issn") or [])] + [_clean(source.get("issn_l"))]),
        "abstract": _abstract_from_index(item.get("abstract_inverted_index")),
        "url": _safe_url(primary.get("landing_page_url")) or
               ("https://doi.org/" + doi if doi else _safe_url(item.get("id"))),
        "pdf_urls": _unique(pdf_urls), "sources": ["OpenAlex"], "version": version,
        "oa": bool((item.get("open_access") or {}).get("is_oa") or pdf_urls or oa_urls),
        "citations": _as_int(item.get("cited_by_count")),
        "oa_urls": _unique(oa_urls), "pdf_locations": location_info,
        "openalex_id": item.get("id") or "", "work_type": work_type,
        "publication_date": item.get("publication_date") or "",
        "is_retracted": bool(item.get("is_retracted")),
    })


def _normalize_arxiv(entry):
    raw_id = _clean(entry.findtext(ATOM + "id"))
    aid = _arxiv_id(raw_id)
    if not aid:
        raise ProviderError("返回了无效的 arXiv 文献标识。")
    # Canonical HTTPS endpoints for the public article identified by Atom metadata.
    landing = "https://arxiv.org/abs/" + aid
    pdf_urls = []
    for link in entry.findall(ATOM + "link"):
        if link.get("title") == "pdf" or link.get("type") == "application/pdf":
            url = _safe_url(link.get("href"))
            if _arxiv_id(url) == aid:
                pdf_urls.append("https://arxiv.org/pdf/" + aid)
    journal = _clean(entry.findtext(ARXIV + "journal_ref"))
    published = _clean(entry.findtext(ATOM + "published"))
    return _finish({
        "title": _clean(entry.findtext(ATOM + "title")) or "（无标题）",
        "authors": [_clean(a.findtext(ATOM + "name")) for a in entry.findall(ATOM + "author")],
        "year": _as_int(published[:4]), "venue": "arXiv", "doi": normalize_doi(
            _clean(entry.findtext(ARXIV + "doi"))), "arxiv_id": aid,
        "abstract": _clean(entry.findtext(ATOM + "summary")), "url": landing,
        "pdf_urls": _unique(pdf_urls), "sources": ["arXiv"], "version": "预印本",
        "oa": True, "citations": None, "oa_urls": [landing],
        "pdf_locations": [{"url": u, "source": "arXiv", "is_oa": True,
                           "version": "submittedVersion"} for u in _unique(pdf_urls)],
        "journal_reference": journal, "publication_date": published[:10],
        "categories": [c.get("term") for c in entry.findall(ATOM + "category") if c.get("term")],
    })


def _normalize_crossref(item):
    dates = (item.get("published") or item.get("published-print") or
             item.get("published-online") or item.get("issued") or {}).get("date-parts", [])
    year = _as_int(dates[0][0]) if dates and dates[0] else None
    doi = normalize_doi(item.get("DOI") or "")
    authors = []
    for author in item.get("author") or []:
        name = author.get("name") or " ".join(filter(None, [author.get("given"), author.get("family")]))
        if name:
            authors.append(_clean(name))
    kind = item.get("type") or ""
    version = "期刊论文" if kind == "journal-article" else ("预印本" if kind == "posted-content" else "文献")
    titles = item.get("title") or []
    venues = item.get("container-title") or []
    # Crossref links can be paywalled TDM URLs. Do not label these as authorized OA PDFs.
    return _finish({
        "title": _clean(titles[0] if titles else "") or "（无标题）", "authors": authors,
        "year": year, "venue": _clean(venues[0] if venues else ""), "doi": doi,
        "publisher": _clean(item.get("publisher")),
        "issns": _unique([_clean(v) for v in (item.get("ISSN") or [])]),
        "arxiv_id": "", "abstract": _clean(item.get("abstract")),
        "url": _safe_url(item.get("URL")) or ("https://doi.org/" + doi if doi else ""),
        "pdf_urls": [], "sources": ["Crossref"], "version": version, "oa": False,
        "citations": _as_int(item.get("is-referenced-by-count")), "oa_urls": [],
        "work_type": kind, "oa_status": "unknown",
    })


def _throttle_source(source, cancel_event):
    """Space request starts per source, including retries and successive searches."""
    with _THROTTLE_LOCK:
        lock = _SOURCE_LOCKS.setdefault(source, threading.Lock())
    while not lock.acquire(timeout=0.1):
        _check_cancel(cancel_event)
    try:
        _check_cancel(cancel_event)
        delay = max(0.0, SOURCE_INTERVALS.get(source, 1.0) -
                    (time.monotonic() - _SOURCE_LAST_START.get(source, 0.0)))
        if delay:
            _wait(delay, cancel_event)
        _SOURCE_LAST_START[source] = time.monotonic()
    finally:
        lock.release()


def _throttle_arxiv(cancel_event):
    _throttle_source("arxiv", cancel_event)


def _request(url, source, cancel_event):
    for attempt in range(MAX_ATTEMPTS):
        _check_cancel(cancel_event)
        _throttle_source(source, cancel_event)
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT,
                "Accept": "application/atom+xml, application/json;q=0.9, */*;q=0.1"})
            attempt_started = time.monotonic()
            with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as response:
                chunks, size = [], 0
                read_chunk = getattr(response, "read1", response.read)
                while True:
                    _check_cancel(cancel_event)
                    if time.monotonic() - attempt_started > MAX_ATTEMPT_SECONDS:
                        raise ProviderError("读取元数据超过时间限制，已停止本次请求。")
                    chunk = read_chunk(65536)
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > MAX_RESPONSE_BYTES:
                        raise ProviderError("元数据响应过大，已停止读取；请减少结果数量。")
                    chunks.append(chunk)
            _check_cancel(cancel_event)
            return b"".join(chunks)
        except urllib.error.HTTPError as exc:
            status = exc.code
            retry_after = exc.headers.get("Retry-After", "") if exc.headers else ""
            exc.close()
            delay = 1.5 * (attempt + 1)
            try:
                delay = max(delay, float(retry_after))
            except (ValueError, TypeError):
                try:
                    retry_date = email.utils.parsedate_to_datetime(retry_after)
                    delay = max(delay, retry_date.timestamp() - time.time())
                except (ValueError, TypeError, OverflowError):
                    pass
            if status in (429, 500, 502, 503, 504) and attempt + 1 < MAX_ATTEMPTS and delay <= 6:
                _wait(delay, cancel_event)
                continue
            if status == 429:
                raise ProviderError("服务限流或每日额度已用尽（HTTP 429），请稍后重试。") from exc
            if status in (401, 403):
                raise ProviderError(f"服务拒绝访问（HTTP {status}），可能需要调整网络或服务访问设置。") from exc
            raise ProviderError(f"服务暂不可用（HTTP {status}）。", status_code=status) from exc
        except (urllib.error.URLError, TimeoutError, socket.timeout, ConnectionError, OSError) as exc:
            if attempt + 1 < MAX_ATTEMPTS:
                _wait(1.5, cancel_event)
                continue
            reason = "请求超时" if isinstance(exc, (TimeoutError, socket.timeout)) or "timed out" in str(exc).lower() else "网络连接失败"
            raise ProviderError(reason + "；请检查网络后重试。") from exc
    raise ProviderError("请求失败。")


def _arxiv_query(query, year_from, year_to):
    # Explicit arXiv field syntax is passed through. Plain words keep Boolean
    # operators and grouping instead of silently converting OR into AND.
    if re.search(r"\b(?:all|ti|au|abs|cat|id|submittedDate|lastUpdatedDate):", query, re.I):
        term = query
    else:
        if query.count('"') % 2:
            raise ProviderError("arXiv：检索词的双引号未配对，请检查短语。")
        tokens = re.findall(r'"[^"\n]+"|[()]|[^\s()]+', query)
        terms, expected, depth = [], True, 0
        for token in tokens:
            upper = token.upper()
            if upper in ("AND", "OR", "ANDNOT", "NOT"):
                if expected:
                    raise ProviderError("arXiv：AND、OR、ANDNOT 必须放在两个检索词之间。")
                terms.append("ANDNOT" if upper == "NOT" else upper)
                expected = True
            elif token == "(":
                if not expected:
                    terms.append("AND")
                terms.append("(")
                depth += 1
                expected = True
            elif token == ")":
                if expected or not depth:
                    raise ProviderError("arXiv：检索词的括号或布尔表达式不完整。")
                terms.append(")")
                depth -= 1
                expected = False
            else:
                if not expected:
                    terms.append("AND")
                value = token.replace("\\", "").strip('"')
                terms.append('all:"' + value.replace('"', '') + '"')
                expected = False
        if expected or depth:
            raise ProviderError("arXiv：检索词的括号或布尔表达式不完整。")
        term = " ".join(terms)
    if year_from is not None or year_to is not None:
        start = f"{year_from or 1:04d}01010000"
        end = f"{year_to or 9999:04d}12312359"
        term = f"({term}) AND submittedDate:[{start} TO {end}]"
    return term

def _query_doi(query):
    """Recognize a complete DOI identifier, not a title that happens to mention one."""
    query = str(query or "").strip()
    if re.match(r"^https?://", query, re.I):
        try:
            parsed = urllib.parse.urlsplit(query)
            if parsed.hostname not in ("doi.org", "dx.doi.org") or parsed.username or parsed.password:
                return ""
            value = normalize_doi(parsed.path.lstrip("/"))
        except ValueError:
            return ""
    else:
        value = normalize_doi(query)
    return value if re.fullmatch(r"10\.\d{4,9}/\S+", value) else ""


def _fetch_crossref_doi(doi, year_from, year_to, cancel_event):
    """Use Crossref's singleton endpoint; local date filters never broaden a DOI."""
    warnings = ["Crossref：按 DOI 精确查询；此来源主要提供书目信息，普通全文/TDM 链接不等于开放 PDF。"]
    try:
        data = json.loads(_request("https://api.crossref.org/works/" + urllib.parse.quote(doi, safe=""),
                                   "crossref", cancel_event))
    except ProviderError as exc:
        if exc.status_code != 404:
            raise
        return {"papers": [], "total": 0, "raw_count": 0, "next_cursor": None, "exhausted": True,
                "warnings": ["Crossref：未找到该 DOI 的登记记录（HTTP 404）；其他索引仍可继续检索。"]}
    item = data.get("message") if isinstance(data, dict) else None
    if not isinstance(item, dict) or normalize_doi(item.get("DOI")) != doi:
        raise ProviderError("Crossref：DOI 精确查询返回的记录格式或标识不符，未将其加入结果。")
    paper = _normalize_crossref(item)
    year = paper.get("year")
    papers = [paper]
    if year_from is not None or year_to is not None:
        if year is None:
            papers = []
            warnings.append("Crossref：该 DOI 记录缺少年份，无法确认是否符合年份筛选，本次未列入结果。")
        elif (year_from is not None and year < year_from) or (year_to is not None and year > year_to):
            papers = []
            warnings.append("Crossref：已找到该 DOI，但出版年份不在所选范围内，本次未列入结果。")
    return {"papers": papers, "total": len(papers), "raw_count": 1,
            "next_cursor": None, "exhausted": True, "warnings": warnings}


def _fetch_page(source, query, *, page_size, offset, cursor, year_from,
                year_to, sort, cancel_event):
    """Fetch one page; offsets count raw source records, not deduplicated results."""
    warnings = []
    if source == "openalex":
        # Keep per-page fixed throughout a search: changing it changes page offsets.
        params = {"search": query, "per-page": page_size, "page": offset // page_size + 1}
        filters = []
        if year_from is not None:
            filters.append(f"from_publication_date:{year_from:04d}-01-01")
        if year_to is not None:
            filters.append(f"to_publication_date:{year_to:04d}-12-31")
        if filters:
            params["filter"] = ",".join(filters)
        if sort == "year":
            params["sort"] = "publication_date:desc"
        elif sort == "citations":
            params["sort"] = "cited_by_count:desc"
        data = json.loads(_request("https://api.openalex.org/works?" + urllib.parse.urlencode(params), source, cancel_event))
        if not isinstance(data, dict) or not isinstance(data.get("results"), list):
            raise ProviderError("返回的元数据格式异常。")
        raw = data["results"]
        papers = [_normalize_openalex(x) for x in raw if isinstance(x, dict)]
        total = _as_int((data.get("meta") or {}).get("count"))
        if (data.get("meta") or {}).get("notice"):
            warnings.append("OpenAlex：当前未配置 API 密钥，共享当前网络 IP 的免费每日额度；额度用尽时可继续使用其他来源。")
    elif source == "arxiv":
        params = {"search_query": _arxiv_query(query, year_from, year_to), "start": offset,
                  "max_results": page_size, "sortBy": "submittedDate" if sort == "year" else "relevance",
                  "sortOrder": "descending"}
        payload = _request("https://export.arxiv.org/api/query?" + urllib.parse.urlencode(params), source, cancel_event)
        if b"<!DOCTYPE" in payload.upper() or b"<!ENTITY" in payload.upper():
            raise ProviderError("返回的 XML 不符合预期，已停止解析。")
        feed = ET.fromstring(payload)
        if feed.tag != ATOM + "feed":
            raise ProviderError("返回的元数据不是 arXiv Atom 格式。")
        raw = feed.findall(ATOM + "entry")
        for entry in raw:
            if "api/errors" in (entry.findtext(ATOM + "id") or ""):
                raise ProviderError("检索表达式被 arXiv 拒绝，请使用英文关键词或正确的 arXiv 检索语法。")
        papers = [_normalize_arxiv(e) for e in raw]
        total = _as_int(feed.findtext(OPENSEARCH + "totalResults"))
        if sort == "citations":
            warnings.append("arXiv：接口不提供引用次数，先按相关性检索；合并列表中引用数未知的记录排在末尾。")
    elif source == "crossref":
        exact_doi = _query_doi(query)
        if exact_doi:
            return _fetch_crossref_doi(exact_doi, year_from, year_to, cancel_event)
        params = {"query.bibliographic": query, "rows": page_size, "offset": offset}
        filters = []
        if year_from is not None:
            filters.append(f"from-pub-date:{year_from:04d}-01-01")
        if year_to is not None:
            filters.append(f"until-pub-date:{year_to:04d}-12-31")
        if filters:
            params["filter"] = ",".join(filters)
        if sort == "year":
            params.update(sort="published", order="desc")
        elif sort == "citations":
            params.update(sort="is-referenced-by-count", order="desc")
        data = json.loads(_request("https://api.crossref.org/works?" + urllib.parse.urlencode(params), source, cancel_event))
        message = data.get("message") if isinstance(data, dict) else None
        if not isinstance(message, dict) or not isinstance(message.get("items"), list):
            raise ProviderError("返回的元数据格式异常。")
        raw = message["items"]
        papers = [_normalize_crossref(x) for x in raw if isinstance(x, dict)]
        total = _as_int(message.get("total-results"))
        warnings.append("Crossref：此来源主要补全书目信息；未将普通全文/TDM 链接标成开放 PDF，下载时需另行解析合法开放版本。")
    elif extra_providers is not None and source in getattr(extra_providers, "SOURCE_NAMES", {}):
        return extra_providers.fetch_page(source, query, page_size=page_size, offset=offset,
            cursor=cursor, year_from=year_from, year_to=year_to, sort=sort,
            cancel_event=cancel_event, request=_request)
    else:
        raise ProviderError("此检索来源尚未安装或不可用。")
    return {"papers": papers, "total": total, "warnings": warnings,
            "raw_count": len(raw), "next_cursor": None,
            "exhausted": not raw or (total is not None and offset + len(raw) >= total)
                         or len(raw) < page_size}


def _error_reason(exc):
    if isinstance(exc, (ProviderError, RuntimeError)):
        return str(exc)[:400] or "来源返回错误。"
    return "元数据格式异常，暂时无法读取。"


def _fetch_source(source, query, limit, year_from, year_to, sort, cancel_event,
                  progress=None, on_page=None):
    """Read multiple pages, preserving every completed page if later work fails."""
    name = SOURCE_NAMES[source]
    page_size = min(limit, max(1, min(100, SOURCE_PAGE_SIZES.get(source, 100))))
    papers, warnings, offset, pages, total, cursor = [], [], 0, 0, None, None
    complete, end_reason, error, exhausted = False, "page_cap", "", False
    no_progress = 0

    def snapshot():
        return {"papers": list(papers[:limit]), "warnings": _unique(warnings), "total": total,
                "retrieved": len(papers[:limit]), "raw_retrieved": offset, "pages": pages,
                "requested": limit, "complete": complete, "exhausted": exhausted,
                "end_reason": end_reason, "error": error, "source": source}

    for page_number in range(1, MAX_PAGES + 1):
        try:
            _check_cancel(cancel_event)
            _notify(progress, f"{name}：正在读取第 {page_number} 页，已保留 {len(papers)} / {limit} 条。")
            page = _fetch_page(source, query, page_size=page_size, offset=offset, cursor=cursor,
                year_from=year_from, year_to=year_to, sort=sort, cancel_event=cancel_event)
            _check_cancel(cancel_event)
            if not isinstance(page, dict) or not isinstance(page.get("papers"), list):
                raise ProviderError("分页元数据格式异常。")
            raw_count = page.get("raw_count", len(page["papers"]))
            if not isinstance(raw_count, int) or raw_count < 0:
                raise ProviderError("分页计数无效。")
            pages += 1
            offset += raw_count
            reported_total = _as_int(page.get("total"))
            if reported_total is not None and reported_total >= 0:
                total = reported_total
            warnings.extend(page.get("warnings") or [])
            before = len(papers)
            papers = deduplicate(papers + page["papers"])[:limit]
            no_progress = no_progress + 1 if len(papers) == before else 0
            exhausted = bool(page.get("exhausted")) or raw_count == 0
            next_cursor = page.get("next_cursor")
            if len(papers) >= limit:
                complete, end_reason = True, "limit"
            elif exhausted:
                complete, end_reason = True, "exhausted"
            elif page.get("pagination_error"):
                error = str(page["pagination_error"])[:400]
                end_reason = "no_progress"
                warnings.append(f"{name}：{error} 已保留 {len(papers)} 条；此来源结果未取全。")
            elif no_progress >= 2 or (cursor is not None and next_cursor == cursor):
                end_reason = "no_progress"
                warnings.append(f"{name}：后续页面没有新增文献或游标未推进，已保留 {len(papers)} 条并停止翻页。")
            elif page_number == MAX_PAGES:
                end_reason = "page_cap"
                warnings.append(f"{name}：已达到 {MAX_PAGES} 页的本次请求保护上限，保留 {len(papers)} 条；此来源结果未取全。")
            else:
                end_reason = "fetching"
            cursor = next_cursor
            if on_page:
                on_page(snapshot())
            _notify(progress, f"{name}：第 {page_number} 页完成，已保留 {len(papers)} 条" +
                    (f"，索引匹配约 {total} 条。" if total is not None else "。"))
            if complete or end_reason in ("no_progress", "page_cap"):
                break
        except SearchCancelled:
            end_reason = "cancelled"
            warnings.append(f"{name}：检索已取消，保留已完成的 {pages} 页、{len(papers)} 条结果。")
            break
        except Exception as exc:
            if cancel_event is not None and cancel_event.is_set():
                end_reason = "cancelled"
                warnings.append(f"{name}：检索已取消，保留已完成的 {pages} 页、{len(papers)} 条结果。")
            elif not papers:
                raise ProviderError(_error_reason(exc)) from exc
            else:
                error = _error_reason(exc)
                end_reason = "page_failed"
                warnings.append(f"{name}：第 {page_number} 页失败（{error}），已保留前 {pages} 页、{len(papers)} 条；此来源结果未取全。")
            break
    return snapshot()


def _cache_path(source, query, limit, year_from, year_to, sort):
    key = json.dumps([6, source, query, limit, year_from, year_to, sort], ensure_ascii=False)
    return CACHE_DIR / (source + "-" + hashlib.sha256(key.encode("utf-8")).hexdigest() + ".json")


def _read_cache(path):
    try:
        if path.stat().st_size > MAX_RESPONSE_BYTES:
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        if (isinstance(data, dict) and isinstance(data.get("papers"), list) and
                isinstance(data.get("saved_at"), (int, float)) and
                all(isinstance(p, dict) and p.get("id") and p.get("title") for p in data["papers"])):
            return data
    except (OSError, ValueError, TypeError):
        pass
    return None


def _write_cache(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        tmp.write_text(json.dumps({**data, "saved_at": time.time()}, ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


def _source_status(data, status, *, from_cache=False):
    papers = data.get("papers") or []
    result = {"status": status, "count": len(papers), "retrieved": len(papers),
        "raw_retrieved": data.get("raw_retrieved", len(papers)), "total": data.get("total"),
        "pages": data.get("pages", 0), "requested": data.get("requested", len(papers)),
        "complete": data.get("complete", True), "exhausted": data.get("exhausted", False),
        "end_reason": data.get("end_reason", "cache" if from_cache else "limit"),
        "from_cache": from_cache}
    if data.get("saved_at") is not None:
        result["cached_at"] = data["saved_at"]
    if data.get("error"):
        result["error"] = data["error"]
    return result


def _search_source(source, query, limit, year_from, year_to, sort, cancel_event, progress,
                   on_update=None):
    name = SOURCE_NAMES[source]
    _check_cancel(cancel_event)
    path = _cache_path(source, query, limit, year_from, year_to, sort)
    cached = _read_cache(path)
    # A recent interrupted cache is useful as fallback, never as a completed search.
    if (cached and cached.get("complete", True) and
            0 <= time.time() - cached["saved_at"] < CACHE_TTL):
        _notify(progress, f"{name}：读取 24 小时内的检索缓存。")
        warnings = list(cached.get("warnings") or []) + [f"{name}：使用 24 小时内的元数据缓存；结果不是本次实时获取。"]
        return cached["papers"], warnings, _source_status(cached, "cached", from_cache=True)

    def update(data):
        if on_update:
            on_update(data["papers"], data["warnings"], _source_status(data, "fetching"))

    _notify(progress, f"{name}：正在检索……")
    try:
        data = _fetch_source(source, query, limit, year_from, year_to, sort, cancel_event,
                             progress=progress, on_page=update)
        data.setdefault("complete", True)
        data.setdefault("requested", limit)
        data.setdefault("source", source)
        # Do not destroy an older complete cache when a new attempt only got a prefix.
        if data.get("papers") or data.get("complete"):
            if data["complete"] or not cached or not cached.get("complete", True):
                try:
                    _write_cache(path, data)
                except OSError:
                    data["warnings"].append(f"{name}：本次结果已获取，但本地缓存写入失败。")
        status = "ok" if data["complete"] else ("cancelled" if data.get("end_reason") == "cancelled" else "partial")
        _notify(progress, f"{name}：本次保留 {len(data['papers'])} 条候选文献" +
                ("。" if data["complete"] else "（未完成全部分页）。"))
        return data["papers"], data["warnings"], _source_status(data, status)
    except SearchCancelled:
        raise
    except Exception as exc:
        if cancel_event is not None and cancel_event.is_set():
            raise SearchCancelled() from exc
        reason = _error_reason(exc)
        if cached:
            incomplete = "且该缓存本身未完成全部分页" if not cached.get("complete", True) else ""
            warning = f"{name}：{reason} 已回退到历史元数据缓存；结果可能过期{incomplete}。"
            _notify(progress, warning)
            status = _source_status(cached, "stale_cache", from_cache=True)
            status["error"] = reason
            return cached["papers"], [warning] + list(cached.get("warnings") or []), status
        warning = f"{name}：{reason} 本来源未返回结果，其他来源继续。"
        _notify(progress, warning)
        return [], [warning], {"status": "failed", "count": 0, "retrieved": 0,
            "raw_retrieved": 0, "total": None, "pages": 0, "requested": limit,
            "complete": False, "exhausted": False, "end_reason": "page_failed",
            "from_cache": False, "error": reason}


def search(query, limit=30, year_from=None, year_to=None, sort="relevance",
           sources=None, cancel_event=None, progress: Callable[[str], None] | None = None):
    """Return up to 500 merged records; source counts and completeness remain visible.

    Every selected source may fetch up to the requested limit before cross-source
    deduplication. Cancellation returns completed pages without waiting on an active
    socket; that bounded request stops before starting another page.
    """
    query = str(query or "").strip()
    if not query:
        raise ValueError("请输入论文题名、DOI 或英文关键词。")
    if len(query) > 2000:
        raise ValueError("检索词过长，请缩短到 2000 个字符以内。")
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        raise ValueError(f"结果数量应为 1 到 {MAX_RESULTS} 的整数。") from None
    if not 1 <= limit <= MAX_RESULTS:
        raise ValueError(f"结果数量应为 1 到 {MAX_RESULTS} 的整数。")
    for year in (year_from, year_to):
        if year is not None and (not isinstance(year, int) or not 1 <= year <= 9999):
            raise ValueError("年份应为 1 到 9999 的整数，或留空。")
    if year_from is not None and year_to is not None and year_from > year_to:
        raise ValueError("起始年份不能晚于结束年份。")
    sort = {"newest": "year", "date": "year", "publication_date": "year", "cited_by_count": "citations"}.get(sort, sort)
    if sort not in ("relevance", "year", "citations"):
        raise ValueError("不支持的排序方式。")
    selected = list(dict.fromkeys(sources if sources is not None else ["openalex", "arxiv", "crossref", "openaire"]))
    if not selected:
        raise ValueError("请至少选择一个检索来源。")
    if any(s not in SOURCE_NAMES for s in selected):
        raise ValueError("存在不可用的检索来源，请重新选择。")
    event = cancel_event if cancel_event is not None else threading.Event()
    empty_status = {"status": "cancelled", "count": 0, "retrieved": 0, "raw_retrieved": 0,
        "pages": 0, "total": None, "requested": limit, "complete": False,
        "exhausted": False, "end_reason": "cancelled", "from_cache": False}
    if event.is_set():
        return {"papers": [], "warnings": ["检索已取消。"], "sources": {
            s: dict(empty_status) for s in selected}, "cancelled": True, "requested": limit}
    warnings, source_info, results = [], {}, {}
    snapshots, snapshot_lock = {}, threading.Lock()

    def update(source, papers, messages, status):
        with snapshot_lock:
            snapshots[source] = (list(papers), list(messages), dict(status))

    if re.search(r"[\u4e00-\u9fff]", query):
        warnings.append("当前来源主要收录英文文献；建议使用英文主题词或论文英文题名，中文检索可能漏检。")
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=len(selected), thread_name_prefix="paper-search")
    jobs = {pool.submit(_search_source, source, query, limit, year_from, year_to, sort, event, progress,
                       lambda papers, messages, status, s=source: update(s, papers, messages, status)): source
            for source in selected}
    pending = set(jobs)
    try:
        while pending and not event.is_set():
            done, pending = concurrent.futures.wait(pending, timeout=0.1,
                return_when=concurrent.futures.FIRST_COMPLETED)
            for future in done:
                source = jobs[future]
                try:
                    papers, messages, status = future.result()
                except SearchCancelled:
                    with snapshot_lock:
                        papers, messages, status = snapshots.get(source, ([], [], dict(empty_status)))
                    status = {**status, "status": "cancelled", "complete": False, "end_reason": "cancelled"}
                except Exception:
                    with snapshot_lock:
                        papers, messages, status = snapshots.get(source, ([], [], dict(empty_status)))
                    messages = list(messages) + [f"{SOURCE_NAMES[source]}：读取失败，保留已完成页面；其他来源继续。"]
                    status = {**status, "status": "partial" if papers else "failed",
                              "complete": False, "end_reason": "page_failed", "error": "读取失败"}
                results[source], source_info[source] = papers, status
                warnings.extend(messages)
        if event.is_set():
            warnings.append("检索已取消；各来源已完成页面的结果予以保留。")
            for future in pending:
                source = jobs[future]
                if future.done() and not future.cancelled():
                    try:
                        papers, messages, status = future.result()
                    except Exception:
                        with snapshot_lock:
                            papers, messages, status = snapshots.get(source, ([], [], dict(empty_status)))
                else:
                    future.cancel()
                    with snapshot_lock:
                        papers, messages, status = snapshots.get(source, ([], [], dict(empty_status)))
                if not status.get("complete"):
                    status = {**status, "status": "cancelled", "end_reason": "cancelled"}
                results[source], source_info[source] = papers, status
                warnings.extend(messages)
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
    # Round robin preserves each source's rank without comparing incompatible scores.
    merged = []
    for index in range(max((len(r) for r in results.values()), default=0)):
        for source in selected:
            records = results.get(source, [])
            if index < len(records):
                merged.append(records[index])
    merged = deduplicate(merged)
    merged_total = len(merged)
    if sort == "year":
        merged.sort(key=lambda p: (p.get("year") or 0, p.get("publication_date") or ""), reverse=True)
    elif sort == "citations":
        merged.sort(key=lambda p: p.get("citations") if isinstance(p.get("citations"), int) else -1, reverse=True)
    return {"papers": merged[:limit], "warnings": _unique(warnings), "sources": source_info,
            "cancelled": event.is_set(), "requested": limit, "retrieved": len(merged[:limit]),
            "unique_candidates": merged_total}
