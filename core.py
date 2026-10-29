"""PaperHarbor: shared local storage and reference export. No network access."""
from __future__ import annotations
import csv, hashlib, json, os, re, sys, tempfile, unicodedata, uuid
from datetime import datetime
from pathlib import Path
from urllib.parse import unquote, urlsplit

def _app_dir():
    start = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
    for p in [start, *start.parents]:
        if (p / "paperharbor.project.json").is_file():
            return p
    return start

APP_DIR = _app_dir()
DEFAULT_LIBRARY = APP_DIR / "library"
DATA_DIR = APP_DIR / "data"
SETTINGS_VERSION = 3

def normalize_doi(value):
    value = unquote(str(value or "")).strip()
    value = re.sub(r"^(?:https?://(?:dx\.)?doi\.org/|doi:\s*)", "", value, flags=re.I)
    return value.strip().lower()

def _title_key(title):
    return "".join(ch for ch in unicodedata.normalize("NFKC", str(title or "")).casefold() if ch.isalnum())

def normalize_arxiv(value):
    """Canonical arXiv identity; individual version strings stay in source records."""
    value = unquote(str(value or "")).strip().lower()
    value = re.sub(r"^arxiv:\s*", "", value)
    value = re.sub(r"^10\.48550/arxiv\.", "", value)
    if "://" in value:
        try:
            parts = urlsplit(value)
            if parts.hostname not in ("arxiv.org", "www.arxiv.org", "export.arxiv.org"):
                return ""
            value = re.sub(r"^/(?:abs|pdf)/", "", parts.path)
        except ValueError:
            return ""
    value = re.sub(r"\.pdf$", "", value)
    value = re.sub(r"v\d+$", "", value)
    return value if re.fullmatch(r"(?:\d{4}\.\d{4,5}|[a-z-]+(?:\.[a-z]{2})?/\d{7})", value) else ""


_ID_FIELDS = ("openalex_id", "openaire_id", "pmcid", "pmid", "hal_id")
_UNION_FIELDS = ("sources", "pdf_urls", "oa_urls", "oa_landing_urls", "categories", "issns")
_JOURNAL_FIELDS = ("title", "authors", "year", "venue", "url", "version", "publication_date", "publisher")


def _json_key(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def _fallback_identity(paper):
    # Exact bibliographic repeats are safe to collapse. A title/year similarity is
    # not an identifier: authors, venue, source and URL may describe different works.
    fields = (*_JOURNAL_FIELDS, "sources", "work_type", "abstract", "journal_reference",
              "pdf_urls", "oa_urls", "oa_landing_urls")
    values = {field: paper.get(field) or ([] if field in ("authors", *_UNION_FIELDS) else "")
              for field in fields}
    for field in _UNION_FIELDS:
        if field in values and isinstance(values[field], list):
            values[field] = sorted(values[field])
    return _json_key(values)


def _identity_keys(paper):
    keys = set()
    records = [paper] + [r for r in paper.get("source_records") or [] if isinstance(r, dict)]
    for record in records:
        doi = normalize_doi(record.get("doi"))
        if doi:
            keys.add("doi:" + doi)
            if doi.startswith("10.48550/arxiv.") and (aid := normalize_arxiv(doi)):
                keys.add("arxiv:" + aid)
        if aid := normalize_arxiv(record.get("arxiv_id")):
            keys.add("arxiv:" + aid)
        for field in _ID_FIELDS:
            value = str(record.get(field) or "").strip()
            if field == "openalex_id":
                value = re.sub(r"^https?://openalex\.org/", "", value, flags=re.I).upper()
            elif field == "pmcid":
                value = value.upper()
            elif field == "hal_id":
                value = re.sub(r"v\d+$", "", value.lower())
            if value:
                keys.add(field + ":" + value)
        # Repository links can carry the arXiv identity even if an index omitted
        # its dedicated field. Arbitrary shared landing pages are not identifiers.
        urls = [record.get("url")] + list(record.get("oa_urls") or []) + list(record.get("pdf_urls") or [])
        for url in urls:
            if isinstance(url, str) and "://" in url and (aid := normalize_arxiv(url)):
                keys.add("arxiv:" + aid)
    return keys


def paper_id(paper):
    # Keep the historical DOI hash so existing library sidecars remain usable.
    doi = normalize_doi(paper.get("doi"))
    keys = _identity_keys(paper)
    arxiv = sorted(k for k in keys if k.startswith("arxiv:"))
    stable = sorted(k for k in keys if not k.startswith("doi:"))
    raw = "doi:" + doi if doi else (arxiv[0] if arxiv else stable[0] if stable else "record:" + _fallback_identity(paper))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20]


def _prepare_paper(original):
    p = dict(original)
    p["doi"] = normalize_doi(p.get("doi"))
    for field in (*_UNION_FIELDS, "authors"):
        value = p.get(field) or []
        p[field] = list(dict.fromkeys(value if isinstance(value, list) else [str(value)]))
    p["pdf_locations"] = [dict(x) for x in p.get("pdf_locations") or [] if isinstance(x, dict)]
    p["source_records"] = [dict(x) for x in p.get("source_records") or [] if isinstance(x, dict)] or [{
        k: p.get(k) for k in ("sources", *_JOURNAL_FIELDS, "doi", "arxiv_id", "journal_reference", "issns", *_ID_FIELDS)
    }]
    p["oa"] = bool(p.get("oa"))
    p["is_retracted"] = bool(p.get("is_retracted"))
    return p


def _merge_group(papers):
    # Bibliographic preference is independent of provider completion/input order.
    def preference(p):
        return (p.get("version") != "期刊论文",
                -sum(bool(p.get(f)) for f in _JOURNAL_FIELDS),
                _json_key({k: v for k, v in p.items() if k != "id"}))
    ordered = sorted(papers, key=preference)
    result = dict(ordered[0])
    for field in _UNION_FIELDS:
        result[field] = sorted({value for p in ordered for value in p.get(field, [])})
    for field in ("pdf_locations", "source_records"):
        unique = {_json_key(value): value for p in ordered for value in p.get(field, [])}
        result[field] = [unique[key] for key in sorted(unique)]
    for p in ordered[1:]:
        for field in (*_JOURNAL_FIELDS, "arxiv_id", "abstract", "journal_reference", *_ID_FIELDS,
                      "oa_url", "oa_landing_url", "pdf_version", "work_type"):
            if not result.get(field) and p.get(field):
                result[field] = p[field]
    dois = sorted({p["doi"] for p in ordered if p.get("doi")})
    real_dois = [doi for doi in dois if not (doi.startswith("10.48550/arxiv.") and normalize_arxiv(doi))]
    result["doi"] = next(iter(real_dois or dois), "")
    result["oa"] = any(p.get("oa") for p in ordered)
    result["is_retracted"] = any(p.get("is_retracted") for p in ordered)
    citations = [p.get("citations") for p in ordered if isinstance(p.get("citations"), (int, float))]
    if citations:
        result["citations"] = max(citations)
    result["id"] = paper_id(result)
    return result


def deduplicate(papers):
    """Match stable identities conservatively, preserving versions and provenance.

    A DOI/arXiv bridge may join records regardless of arrival order. If one arXiv
    identity points at conflicting publication DOIs, keep those DOI groups and
    the ambiguous unidentified group separate instead of guessing a publication.
    """
    records = [_prepare_paper(p) for p in papers]
    parents = list(range(len(records)))
    keys = [_identity_keys(p) for p in records]
    real_dois = [p["doi"] if p["doi"] and not (
        p["doi"].startswith("10.48550/arxiv.") and normalize_arxiv(p["doi"])) else "" for p in records]

    def root(i):
        while parents[i] != i:
            parents[i] = parents[parents[i]]
            i = parents[i]
        return i

    def join(a, b):
        a, b = root(a), root(b)
        if a != b:
            parents[max(a, b)] = min(a, b)

    doi_groups, unidentified_keys = {}, {}
    for i, doi in enumerate(real_dois):
        if doi:
            if doi in doi_groups:
                join(i, doi_groups[doi])
            else:
                doi_groups[doi] = i
        else:
            for key in keys[i] or {"record:" + _fallback_identity(records[i])}:
                if key in unidentified_keys:
                    join(i, unidentified_keys[key])
                else:
                    unidentified_keys[key] = i

    key_dois = {}
    for i, doi in enumerate(real_dois):
        if doi:
            for key in keys[i]:
                key_dois.setdefault(key, set()).add(doi)
    candidates = {}
    for i, doi in enumerate(real_dois):
        if not doi:
            group = root(i)
            candidates.setdefault(group, set()).update(d for key in keys[i] for d in key_dois.get(key, ()))
    for group, matches in candidates.items():
        if len(matches) == 1:
            join(group, doi_groups[next(iter(matches))])

    groups = {}
    for i, p in enumerate(records):
        groups.setdefault(root(i), []).append(p)
    results = [_merge_group(group) for group in groups.values()]
    # Stable provider identities may themselves be inconsistent in upstream data.
    # Conflict groups normally have DOI-based IDs; guard all remaining UI IDs too.
    by_id = {}
    for result in results:
        by_id.setdefault(result["id"], []).append(result)
    for identity, conflicts in by_id.items():
        if len(conflicts) > 1:
            for p in conflicts:
                if not p.get("doi"):
                    p["id"] = identity + "-" + hashlib.sha256(_fallback_identity(p).encode("utf-8")).hexdigest()[:10]
    return results

def safe_filename(title, max_length=110):
    text = unicodedata.normalize("NFKC", str(title or "untitled"))
    text = re.sub(r'[\x00-\x1f<>:"/\\|?*]', "_", text)
    text = re.sub(r"\s+", " ", text).strip(" .")
    text = text[:max_length].rstrip(" .") or "untitled"
    if text.split(".")[0].upper() in {"CON", "PRN", "AUX", "NUL", *("COM"+str(n) for n in range(1,10)), *("LPT"+str(n) for n in range(1,10))}:
        text = "_" + text
    return text

def atomic_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)

def load_settings():
    defaults = {"download_dir": str(DEFAULT_LIBRARY), "query": "Volterra integral equations",
                "limit": 100, "sort": "relevance", "sources": ["openalex", "arxiv", "crossref", "openaire"], "settings_version":SETTINGS_VERSION,
                "year_from": "", "year_to": "", "oa_only": False}
    try:
        obj = json.loads((DATA_DIR / "settings.json").read_text(encoding="utf-8"))
        if isinstance(obj, dict):
            defaults.update({k:v for k,v in obj.items() if k in defaults})
            if obj.get("settings_version",1)<2:
                defaults["sources"]=list(dict.fromkeys(defaults["sources"]+["openaire"]))
            if obj.get("settings_version",1)<SETTINGS_VERSION:
                defaults["sources"]=list(dict.fromkeys(defaults["sources"]+["crossref"]))
            defaults["settings_version"] = SETTINGS_VERSION
    except (OSError, ValueError, TypeError):
        pass
    return defaults

def save_settings(settings):
    atomic_json(DATA_DIR / "settings.json", settings)

def _single(value):
    return re.sub(r"[\r\n\t]+", " ", str(value or "")).strip()

def _csv_safe(value):
    s = _single(value)
    return "'" + s if s.startswith(("=", "+", "-", "@")) else s

def _tex(value):
    table = {"\\": r"\textbackslash{}", "{": r"\{", "}": r"\}", "&": r"\&", "%": r"\%", "$": r"\$", "#": r"\#", "_": r"\_", "^": r"\textasciicircum{}", "~": r"\textasciitilde{}"}
    return "".join(table.get(ch,ch) for ch in _single(value))

def export_references(papers, out_dir):
    papers = deduplicate(papers)
    if not papers:
        raise ValueError("请先勾选需要导出的论文。")
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:4]
    prefix = out / ("papers-" + stamp)
    csv_path, bib_path, ris_path = (prefix.with_suffix(s) for s in (".csv", ".bib", ".ris"))
    with csv_path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["Title", "Authors", "Year", "Journal / Repository", "DOI", "URL", "Version", "Sources", "Public PDF links"])
        for p in papers:
            writer.writerow([_csv_safe(x) for x in [p.get("title"), "; ".join(p.get("authors",[])), p.get("year"), p.get("venue"), p.get("doi"), p.get("url"), p.get("version"), "; ".join(p.get("sources",[])), "; ".join(p.get("pdf_urls",[]))]])
    bib, ris = [], []
    for p in papers:
        work_type=p.get("work_type","")
        if p.get("version")=="预印本":
            kind,ris_kind="misc","RPRT"
        elif work_type in ("book","monograph","edited-book"):
            kind,ris_kind="book","BOOK"
        elif work_type in ("book-chapter","book-part","book-section"):
            kind,ris_kind="incollection","CHAP"
        elif work_type in ("proceedings-article","proceedings"):
            kind,ris_kind="inproceedings","CPAPER"
        elif work_type=="report":
            kind,ris_kind="techreport","RPRT"
        elif p.get("version")=="期刊论文":
            kind,ris_kind="article","JOUR"
        else:
            kind,ris_kind="misc","GEN"
        fields = {"title": p.get("title"), "author": " and ".join(p.get("authors",[])),
                  "year": p.get("year"), "doi": p.get("doi"),
                  "url": p.get("url"), "note": p.get("version")}
        if kind=="article":
            fields["journal"]=p.get("venue")
        elif kind in ("incollection","inproceedings"):
            fields["booktitle"]=p.get("venue")
        if p.get("arxiv_id") and p.get("version")=="预印本":
            fields["eprint"]=p["arxiv_id"]
            fields["archivePrefix"]="arXiv"
        lines = ["@" + kind + "{ph" + paper_id(p) + ","]
        lines += ["  "+k+" = {"+_tex(v)+"}," for k,v in fields.items() if v]
        bib.append("\n".join(lines+["}"]))
        entry = ["TY  - " + ris_kind, "TI  - " + _single(p.get("title"))]
        entry += ["AU  - "+_single(a) for a in p.get("authors",[])]
        for key, value in [("PY",p.get("year")),("JO" if kind=="article" else "T2",p.get("venue")),("DO",p.get("doi")),("UR",p.get("url")),("N1",p.get("version"))]:
            if value:
                entry.append(key+"  - "+_single(value))
        ris.append("\n".join(entry+["ER  -"]))
    bib_path.write_text("\n\n".join(bib)+"\n", encoding="utf-8")
    ris_path.write_text("\n\n".join(ris)+"\n", encoding="utf-8")
    json_path = prefix.with_suffix(".json")
    atomic_json(json_path, {"exported_at":datetime.now().astimezone().isoformat(),"count":len(papers),"papers":papers})
    return {"csv":str(csv_path),"bibtex":str(bib_path),"ris":str(ris_path),"json":str(json_path)}


def write_download_report(batch):
    """Create a readable result page; metadata and stale paths are not PDFs."""
    import html
    esc = lambda value: html.escape(str(value or ""), quote=True)
    manifest = Path(batch["manifest_path"]).resolve()
    dest = manifest.with_suffix(".html")
    results = batch.get("results", [])
    successful = ("downloaded", "existing")
    allowed_supplements = {".pdf", ".zip", ".csv", ".txt", ".xlsx", ".docx", ".png",
                           ".jpg", ".jpeg", ".gif", ".tif", ".tiff", ".webp"}

    def local_file(value, *, pdf=False):
        if not isinstance(value, str) or not value.strip():
            return None
        try:
            path = Path(value)
            if not path.is_absolute():
                path = manifest.parent / path
            path = path.resolve()
            if not path.is_file() or (pdf and path.suffix.lower() != ".pdf"):
                return None
            if pdf:
                with path.open("rb") as stream:
                    if stream.read(5) != b"%PDF-":
                        return None
            return path
        except (OSError, ValueError, RuntimeError):
            return None

    def manual_link(value):
        if not isinstance(value, str) or any(ord(ch) < 32 for ch in value):
            return ""
        try:
            parts = urlsplit(value)
            if parts.scheme not in ("https", "http") or not parts.hostname or parts.username or parts.password:
                return ""
            parts.port  # Reject malformed ports instead of emitting a broken link.
        except ValueError:
            return ""
        return '<a href="' + esc(value) + '">文章 / 附件入口</a>'

    count, rows = 0, []
    status = {"downloaded": "正文已下载", "existing": "正文已存在", "failed": "正文下载失败",
              "unavailable": "未找到公开正文", "cancelled": "已取消", "queued": "等待处理"}
    for result in results:
        state = result.get("status")
        path = local_file(result.get("path"), pdf=True) if state in successful else None
        if path:
            count += 1
            body = esc(status[state]) + ' · <a href="' + esc(path.as_uri()) + '">打开正文 PDF</a>'
        elif state in successful:
            body = "记录曾完成正文下载，但本地文件已缺失、无法读取或不是 PDF；未计入可打开的正文。"
        else:
            body = esc(status.get(state, "尚未完成"))
        if result.get("error"):
            body += "<p>" + esc(result["error"]) + "</p>"
        supplements = []
        missing_supplements = 0
        for item in result.get("supplements", []):
            path = local_file(item.get("path")) if item.get("status") in successful else None
            if path and path.suffix.lower() in allowed_supplements:
                supplements.append('<a href="' + esc(path.as_uri()) + '">' + esc(path.name) + '</a>')
            elif item.get("status") in successful:
                missing_supplements += 1
                supplements.append("记录中的补充文件已缺失、无法读取或格式不支持：" + esc(Path(item.get("path") or "未提供路径").name))
            elif item.get("error"):
                supplements.append(esc(item["error"]))
        si_message = esc(result.get("si_message") or "未完成")
        if missing_supplements:
            si_message += " 当前有 " + str(missing_supplements) + " 个记录中的补充文件不可打开。"
        manual = list(dict.fromkeys(link for url in result.get("manual_urls", []) if (link := manual_link(url))))
        rows.append('<section><h2>' + esc(result.get("title")) + '</h2><div>' + body + '</div><p>补充材料：' + si_message +
                    '</p><div>' + "<br>".join(supplements) + '</div><p>' + " · ".join(manual) + '</p></section>')
    total = (batch.get("summary") or {}).get("total", len(results))
    total = max(total, len(results)) if isinstance(total, int) else len(results)
    pending = '<p>已处理 ' + str(len(results)) + ' / ' + str(total) + ' 篇；其余尚未处理。</p>' if total > len(results) else ""
    content = '''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>文献港 · 下载结果</title><style>
body{background:#edf2f5;color:#203643;font:16px/1.8 "Microsoft YaHei",sans-serif;margin:0}main{max-width:1000px;margin:35px auto;padding:30px;background:white;border-radius:12px}h1{color:#15394a}h2{font-size:19px}section{border-top:1px solid #dbe4e8;padding:14px 0}a{color:#087f8c;overflow-wrap:anywhere}p{margin:8px 0}.notice{padding:14px;background:#eaf5f5}
</style><main><h1>下载结果：可打开的正文 PDF ''' + str(count) + ' / ' + str(total) + ''' 篇</h1><p class="notice">论文阅读请打开 PDF。JSON 保存的是来源、状态和校验信息；只有 JSON 不代表论文正文已经下载。本页按生成时的文件状态统计；文件内容的下载校验记录请查看 JSON 清单。</p>''' + pending + ''.join(rows) + '</main></html>'
    dest.write_text(content, encoding="utf8")
    return str(dest)
