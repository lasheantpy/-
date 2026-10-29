"""Additional official public indexes. No PDF downloads or authentication here."""
from __future__ import annotations
import html,json,re
from urllib.parse import urlencode,urlsplit,quote
from core import paper_id,normalize_doi

SOURCE_NAMES={"openaire":"OpenAIRE","europepmc":"Europe PMC","hal":"HAL"}
SOURCE_PAGE_SIZES={"openaire":100,"europepmc":100,"hal":100}
SOURCE_INTERVALS={"openaire":1.0,"europepmc":1.0,"hal":1.0}

def clean(value):
    return re.sub(r"\s+"," ",html.unescape(re.sub(r"<[^>]*>"," ",str(value or "")))).strip()

def safe_url(value):
    if not isinstance(value,str):return ""
    try:
        p=urlsplit(value)
        return value if p.scheme in ("https","http") and p.hostname and not p.username and not p.password else ""
    except ValueError:return ""

def integer(value):
    try:return int(value)
    except (ValueError,TypeError):return None

def unique(values):
    return list(dict.fromkeys(x for x in values if x))

def finish(p):
    p["id"]=paper_id(p)
    return p

def normalize_openaire(item):
    instances=[x for x in item.get("instances") or [] if isinstance(x,dict)]
    pids=list(item.get("pids") or [])
    for inst in instances:
        pids+=list(inst.get("pids") or [])+list(inst.get("alternateIdentifiers") or [])
    doi=next((normalize_doi(x.get("value")) for x in pids if isinstance(x,dict) and str(x.get("scheme","")).lower()=="doi"),"")
    arxiv=next((clean(x.get("value")) for x in pids if isinstance(x,dict) and str(x.get("scheme","")).lower()=="arxiv"),"")
    oa_urls=[];pdfs=[];locations=[];urls=[]
    for inst in instances:
        links=[safe_url(u) for u in inst.get("urls") or []]
        urls+=links
        is_oa=(inst.get("accessRight") or {}).get("label")=="OPEN"
        if is_oa:
            oa_urls+=links
            for link in links:
                parsed=urlsplit(link)
                if parsed.path.lower().endswith(".pdf") or (parsed.hostname in ("arxiv.org","export.arxiv.org") and parsed.path.startswith("/pdf/")):
                    pdfs.append(link)
                    locations.append({"url":link,"source":"OpenAIRE","is_oa":True,"version":None})
    authors=sorted([x for x in item.get("authors") or [] if isinstance(x,dict)],key=lambda x:integer(x.get("rank")) or 100000)
    date=str(item.get("publicationDate") or "")
    kinds={str(x.get("type") or "").lower() for x in instances}
    container=item.get("container") or {}
    pre=bool(arxiv) and not container or "preprint" in kinds
    version="预印本" if pre else ("期刊论文" if container.get("name") and "article" in kinds else "文献")
    work_type="preprint" if pre else ("article" if version=="期刊论文" else "book" if "book" in kinds else "")
    identifier=str(item.get("id") or "")
    return finish({"title":clean(item.get("mainTitle")) or "（无标题）",
        "authors":[clean(x.get("fullName") or " ".join(filter(None,[x.get("name"),x.get("surname")]))) for x in authors],
        "year":integer(date[:4]),"publication_date":date,"venue":clean(container.get("name")),
        "publisher":clean(item.get("publisher")),
        "issns":unique([clean(container.get("issnPrinted")),clean(container.get("issnOnline"))]),
        "doi":doi,"arxiv_id":arxiv,"abstract":"\n\n".join(clean(x) for x in item.get("descriptions") or [] if x),
        "url":("https://doi.org/"+doi) if doi else next(iter(unique(urls)),"https://explore.openaire.eu/search/publication?articleId="+quote(identifier,safe="")),
        "pdf_urls":unique(pdfs),"pdf_locations":locations,"oa_urls":unique(oa_urls),
        "sources":["OpenAIRE"],"version":version,"work_type":work_type,
        "oa":bool(oa_urls),"citations":integer(((item.get("indicators") or {}).get("citationImpact") or {}).get("citationCount")),
        "openaire_id":identifier})

def normalize_europepmc(item):
    doi=normalize_doi(item.get("doi"))
    pmcid=clean(item.get("pmcid"))
    is_oa=item.get("isOpenAccess")=="Y"
    oa_urls=[];pdfs=[]
    for link in (item.get("fullTextUrlList") or {}).get("fullTextUrl") or []:
        url=safe_url(link.get("url"))
        if url and link.get("availabilityCode")=="OA":
            oa_urls.append(url)
            if str(link.get("documentStyle","")).lower()=="pdf" or urlsplit(url).path.lower().endswith(".pdf"):
                pdfs.append(url)
    pre=item.get("source")=="PPR" or any("preprint" in str(x).lower() for x in (item.get("pubTypeList") or {}).get("pubType",[]))
    journal=((item.get("journalInfo") or {}).get("journal") or {}).get("title") or item.get("journalTitle")
    types=(item.get("pubTypeList") or {}).get("pubType") or []
    is_journal=bool(journal) and any("journal" in str(t).lower() for t in types)
    landing=("https://europepmc.org/articles/"+quote(pmcid,safe="")) if pmcid else "https://europepmc.org/article/"+quote(str(item.get("source") or "MED"),safe="")+"/"+quote(str(item.get("id") or ""),safe="")
    if is_oa:oa_urls.append(landing)
    authors=[clean(x.get("fullName") or x.get("collectiveName")) for x in (item.get("authorList") or {}).get("author") or []]
    return finish({"title":clean(item.get("title")) or "（无标题）","authors":[a for a in authors if a],
        "year":integer(item.get("pubYear")),"publication_date":str(item.get("firstPublicationDate") or ""),
        "venue":clean(journal) or ("预印本仓储" if pre else "Europe PMC"),
        "publisher":"", "issns":unique([clean(((item.get("journalInfo") or {}).get("journal") or {}).get(k)) for k in ("issn","essn")]),
        "doi":doi,"arxiv_id":"","abstract":clean(item.get("abstractText")),"url":landing,
        "pdf_urls":unique(pdfs),"oa_urls":unique(oa_urls),
        "pdf_locations":[{"url":u,"source":"Europe PMC","is_oa":True,"version":None} for u in unique(pdfs)],
        "sources":["Europe PMC"],"version":"预印本" if pre else ("期刊论文" if is_journal else "文献"),
        "work_type":"preprint" if pre else ("article" if is_journal else ""),
        "oa":bool(oa_urls),"citations":integer(item.get("citedByCount")),"pmcid":pmcid})

HAL_FIELDS = (
    "halId_s,title_s,authFullName_s,producedDateY_i,producedDate_s,journalTitle_s,"
    "journalPublisher_s,publisher_s,journalIssn_s,journalEissn_s,doiId_s,arxivId_s,"
    "abstract_s,uri_s,fileMain_s,docType_s,openAccess_bool,licence_s"
)


def _list(value):
    return value if isinstance(value, list) else [value] if value else []


def _first(value):
    return next((clean(x) for x in _list(value) if clean(x)), "")


def normalize_hal(item):
    """Normalize stored HAL metadata without promoting metadata-only deposits to OA."""
    doi = normalize_doi(_first(item.get("doiId_s")))
    arxiv = _first(item.get("arxivId_s"))
    identifier = _first(item.get("halId_s"))
    landing = safe_url(_first(item.get("uri_s")))
    if not landing and identifier:
        landing = "https://hal.science/" + quote(identifier, safe="")
    file_url = safe_url(_first(item.get("fileMain_s")))
    # HAL explicitly reports OA; fileMain is a direct-document redirect whose
    # content type and signature are still checked by the downloader.
    is_oa = item.get("openAccess_bool") is True
    pdfs = [file_url] if file_url and is_oa else []
    kind = _first(item.get("docType_s"))
    preprint = kind == "UNDEFINED"
    journal = _first(item.get("journalTitle_s"))
    return finish({
        "title": _first(item.get("title_s")) or "（无标题）",
        "authors": [clean(x) for x in _list(item.get("authFullName_s")) if clean(x)],
        "year": integer(item.get("producedDateY_i")),
        "publication_date": _first(item.get("producedDate_s")),
        "venue": journal or "HAL", "publisher": _first(item.get("journalPublisher_s")) or _first(item.get("publisher_s")),
        "issns": unique([clean(x) for key in ("journalIssn_s", "journalEissn_s") for x in _list(item.get(key))]),
        "doi": doi, "arxiv_id": arxiv, "hal_id": identifier,
        "abstract": "\n\n".join(clean(x) for x in _list(item.get("abstract_s")) if clean(x)),
        "url": landing or ("https://doi.org/" + doi if doi else ""),
        "pdf_urls": pdfs, "oa_urls": [landing] if landing and is_oa else [],
        "pdf_locations": [{"url": u, "source": "HAL", "is_oa": True,
                           "license": _first(item.get("licence_s")), "version": None} for u in pdfs],
        "sources": ["HAL"], "oa": is_oa, "citations": None,
        "version": "期刊论文" if kind == "ART" else "预印本" if preprint else "文献",
        "work_type": "article" if kind == "ART" else "preprint" if preprint else "book" if kind == "OUV" else "",
    })


def fetch_page(source,query,*,page_size,offset,cursor,year_from,year_to,sort,cancel_event,request):
    if source=="openaire":
        filters=["type:publication"]
        if year_from is not None:filters.append("from_publication_year:"+str(year_from))
        if year_to is not None:filters.append("to_publication_year:"+str(year_to))
        params={"search":query,"filter":",".join(filters),"page":offset//page_size+1,"page_size":page_size}
        if sort=="year":params["sort"]="publication_date:desc"
        if sort=="citations":params["sort"]="citation_count:desc"
        data=json.loads(request("https://api.openaire.eu/graph/v4/research-products?"+urlencode(params),source,cancel_event))
        raw=data.get("results")
        if not isinstance(raw,list):raise RuntimeError("OpenAIRE 返回格式异常。")
        total=integer((data.get("header") or {}).get("numFound"))
        return {"papers":[normalize_openaire(x) for x in raw if isinstance(x,dict)],"raw_count":len(raw),"total":total,
            "next_cursor":None,"exhausted":len(raw)<page_size or (total is not None and offset+len(raw)>=total),"warnings":[]}
    if source=="europepmc":
        term=query
        if year_from is not None or year_to is not None:
            term="("+query+") AND FIRST_PDATE:["+str(year_from or 1000)+"-01-01 TO "+str(year_to or 9999)+"-12-31]"
        params={"query":term,"format":"json","resultType":"core","pageSize":page_size,"cursorMark":cursor or "*"}
        if sort=="year":params["sort"]="P_PDATE_D desc"
        if sort=="citations":params["sort"]="CITED desc"
        data=json.loads(request("https://www.ebi.ac.uk/europepmc/webservices/rest/search?"+urlencode(params),source,cancel_event))
        raw=(data.get("resultList") or {}).get("result")
        if not isinstance(raw,list):raise RuntimeError("Europe PMC 返回格式异常。")
        total=integer(data.get("hitCount"));next_cursor=data.get("nextCursorMark")
        exhausted=not raw or (total is not None and offset+len(raw)>=total)
        pagination_error=""
        if not exhausted and (not isinstance(next_cursor,str) or not next_cursor or next_cursor==(cursor or "*")):
            pagination_error="分页游标缺失或未推进，无法继续读取。"
        return {"papers":[normalize_europepmc(x) for x in raw if isinstance(x,dict)],"raw_count":len(raw),"total":total,
            "next_cursor":next_cursor,"exhausted":exhausted,"pagination_error":pagination_error,
            "warnings":["Europe PMC：主要覆盖生命科学及交叉研究，用于补充数学建模、数值方法应用文献。"]}
    if source == "hal":
        params = {"q": query, "wt": "json", "fl": HAL_FIELDS,
                  "rows": page_size, "start": offset}
        if year_from is not None or year_to is not None:
            params["fq"] = f"producedDateY_i:[{year_from or '*'} TO {year_to or '*'}]"
        if sort == "year":
            params["sort"] = "producedDate_tdate desc,docid asc"
        else:
            params["sort"] = "score desc,docid asc"
        data = json.loads(request("https://api.archives-ouvertes.fr/search/?" + urlencode(params), source, cancel_event))
        response = data.get("response") if isinstance(data, dict) else None
        raw = response.get("docs") if isinstance(response, dict) else None
        if not isinstance(raw, list):
            raise RuntimeError("HAL 返回格式异常。")
        total = integer(response.get("numFound"))
        warnings = []
        if sort == "citations":
            warnings.append("HAL：接口不提供引用次数，先按相关性检索；引用数未知的记录排在末尾。")
        return {"papers": [normalize_hal(x) for x in raw if isinstance(x, dict)],
                "raw_count": len(raw), "total": total, "next_cursor": None,
                "exhausted": len(raw) < page_size or (total is not None and offset + len(raw) >= total),
                "warnings": warnings}
    raise ValueError("未知检索来源")
