"""Sequential, verified open-access downloads for PaperHarbor.

Public metadata identifies candidates; successful HTTP responses alone never count
as PDFs. Supplement discovery is deliberately bounded and reports unrecognised
or inaccessible pages, not an assertion that no supplements exist.
"""
from __future__ import annotations

import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import socket
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from urllib.error import HTTPError, URLError
from urllib.parse import quote, unquote, urljoin, urlsplit, parse_qs
from urllib.request import Request, HTTPRedirectHandler, build_opener
import uuid
import zipfile

from core import paper_id, safe_filename, normalize_doi

TIMEOUT = 14
MAX_PDF_BYTES = 64 * 1024 * 1024
MAX_SUPPLEMENT_BYTES = 32 * 1024 * 1024
MAX_PAGE_BYTES = 2 * 1024 * 1024
MAX_DOWNLOAD_SECONDS = 90
MAX_PAGE_SECONDS = 30
MAX_PDF_CANDIDATES = 4
MAX_LANDING_PAGES = 3
MAX_SUPPLEMENTS = 8
RETRIES = 1
USER_AGENT = "PaperHarbor/0.1 (local open-access research library)"
ALLOWED_EXTENSIONS = {".pdf", ".zip", ".csv", ".txt", ".xlsx", ".docx", ".png", ".jpg", ".jpeg", ".gif", ".tif", ".tiff", ".webp"}
SUPPLEMENT_LABEL = re.compile(r"supplement(?:ary|al)?|supporting\s+(?:information|material|data|file)|additional\s+(?:file|material|data)|append(?:ix|ices)|\bS\d+\s+(?:Text|File|Table|Fig(?:ure)?|Data|Appendix|Protocol|Code)\b|附录|补充(?:材料|信息|数据)|附件", re.I)


class DownloadError(Exception):
    pass


class Cancelled(DownloadError):
    pass


def _check_cancel(cancel_event):
    if cancel_event is not None and cancel_event.is_set():
        raise Cancelled("用户取消")


def _pause(seconds, cancel_event):
    if cancel_event is not None:
        if cancel_event.wait(seconds):
            raise Cancelled("用户取消")
    else:
        time.sleep(seconds)


def validate_url(url, *, allow_local=False):
    """Reject credentials and non-public targets, including every redirect.

    allow_local is an explicit test seam; it permits loopback fixtures only,
    not arbitrary private LAN hosts. The desktop app never enables it.
    """
    if not isinstance(url, str) or len(url) > 8192 or any(ord(c) < 32 for c in url):
        raise DownloadError("无效的网址")
    try:
        parsed = urlsplit(url)
        if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
            raise DownloadError("只接受公开 HTTP/HTTPS 链接")
        if parsed.username is not None or parsed.password is not None:
            raise DownloadError("拒绝含登录凭据的网址")
        host = parsed.hostname.encode("idna").decode("ascii")
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        if not 1 <= port <= 65535:
            raise DownloadError("无效端口")
        if host.lower().rstrip(".") in {"localhost", "localhost.localdomain"} and not allow_local:
            raise DownloadError("拒绝本地地址")
        try:
            addresses = [ipaddress.ip_address(host)]
        except ValueError:
            addresses = [ipaddress.ip_address(item[4][0]) for item in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)]
        if not addresses:
            raise DownloadError("网址无法解析")
        for address in addresses:
            if allow_local and address.is_loopback:
                continue
            if not address.is_global:
                raise DownloadError("拒绝私有、保留或本地网络地址")
        return url
    except DownloadError:
        raise
    except (ValueError, UnicodeError, OSError) as exc:
        raise DownloadError("网址或域名解析失败: " + str(exc)) from exc


class _SafeRedirect(HTTPRedirectHandler):
    max_redirections = 5
    max_repeats = 2

    def __init__(self, allow_local=False):
        super().__init__()
        self.allow_local = allow_local

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        validate_url(newurl, allow_local=self.allow_local)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class _Client:
    def __init__(self, cancel_event=None, allow_local=False):
        self.cancel_event = cancel_event
        self.allow_local = allow_local
        self.opener = build_opener(_SafeRedirect(allow_local))
        self.last_request = 0.0
        self.host_requests = {}

    def open(self, url, accept="*/*"):
        validate_url(url, allow_local=self.allow_local)
        for attempt in range(RETRIES + 1):
            _check_cancel(self.cancel_event)
            host = urlsplit(url).hostname
            now = time.monotonic()
            _pause(max(0.0, .15 - (now - self.last_request), .35 - (now - self.host_requests.get(host, 0.0))), self.cancel_event)
            try:
                self.last_request = time.monotonic()
                self.host_requests[host] = self.last_request
                req = Request(url, headers={"User-Agent": USER_AGENT, "Accept": accept, "Accept-Encoding": "identity"})
                response = self.opener.open(req, timeout=TIMEOUT)
                validate_url(response.geturl(), allow_local=self.allow_local)
                return response
            except HTTPError as exc:
                code = exc.code
                retry_after = exc.headers.get("Retry-After", "")
                exc.close()
                if code in {429, 500, 502, 503, 504} and attempt < RETRIES:
                    delay = .6 * (attempt + 1)
                    try:
                        delay = max(delay, float(retry_after))
                    except (ValueError, TypeError):
                        try:
                            delay = max(delay, (parsedate_to_datetime(retry_after) - datetime.now(timezone.utc)).total_seconds())
                        except (ValueError, TypeError, OverflowError):
                            pass
                    _pause(min(delay, 10.0), self.cancel_event)
                    continue
                raise DownloadError(f"HTTP {code}；页面可能需要机构访问或暂不可用") from exc
            except (URLError, TimeoutError, OSError) as exc:
                if attempt < RETRIES:
                    _pause(.6 * (attempt + 1), self.cancel_event)
                    continue
                raise DownloadError("网络请求失败: " + str(exc)) from exc

    def bytes(self, url, max_bytes=MAX_PAGE_BYTES, accept="text/html,application/xhtml+xml"):
        started = time.monotonic()
        with self.open(url, accept) as response:
            data = bytearray()
            while True:
                _check_cancel(self.cancel_event)
                if time.monotonic() - started > MAX_PAGE_SECONDS:
                    raise DownloadError("页面读取超过总时限")
                chunk = response.read1(min(65536, max_bytes + 1 - len(data)))
                if not chunk:
                    break
                data.extend(chunk)
                if len(data) > max_bytes:
                    raise DownloadError("页面超过读取大小限制")
            return bytes(data), response.geturl(), response.headers

    def download(self, url, directory, max_bytes):
        """Write only a random .part; validation and naming happen afterwards."""
        temp = directory / (".paperharbor-" + uuid.uuid4().hex + ".part")
        started = time.monotonic()
        try:
            with self.open(url) as response:
                declared = response.headers.get("Content-Length")
                if declared and declared.isdigit() and int(declared) > max_bytes:
                    raise DownloadError("文件超过本版下载大小限制")
                total = 0
                digest = hashlib.sha256()
                with temp.open("xb") as stream:
                    while True:
                        _check_cancel(self.cancel_event)
                        if time.monotonic() - started > MAX_DOWNLOAD_SECONDS:
                            raise DownloadError("文件下载超过总时限，可稍后重试")
                        chunk = response.read1(65536)
                        if not chunk:
                            break
                        total += len(chunk)
                        if total > max_bytes:
                            raise DownloadError("文件超过本版下载大小限制")
                        stream.write(chunk)
                        digest.update(chunk)
                    stream.flush()
                    os.fsync(stream.fileno())
                if declared and declared.isdigit() and total != int(declared):
                    raise DownloadError("下载未完成：文件长度不符")
                return temp, response.geturl(), response.headers, total, digest.hexdigest()
        except Exception:
            temp.unlink(missing_ok=True)
            raise


def _sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(65536), b""):
            digest.update(block)
    return digest.hexdigest()


def verify_pdf(path):
    """Return validation level, or raise. Never regard an HTML login as PDF."""
    path = Path(path)
    if path.stat().st_size > MAX_PDF_BYTES:
        raise DownloadError("既有 PDF 超过验证大小限制")
    with path.open("rb") as stream:
        first = stream.read(1024)
        offset = first.find(b"%PDF-")
        if offset < 0 or first[:offset].strip(b"\xef\xbb\xbf \t\r\n"):
            raise DownloadError("响应不是 PDF，可能是登录页或反爬页面")
        stream.seek(max(0, path.stat().st_size - 65536))
        if b"%%EOF" not in stream.read():
            raise DownloadError("PDF 缺少结束标记，可能下载不完整")
    try:
        from pypdf import PdfReader
    except ImportError:
        return "signature+eof (pypdf unavailable)"
    try:
        reader = PdfReader(str(path), strict=False)
        if reader.is_encrypted and not reader.decrypt(""):
            raise DownloadError("PDF 需要密码，未计入下载成功")
        if len(reader.pages) < 1:
            raise DownloadError("PDF 不包含页面")
    except DownloadError:
        raise
    except Exception as exc:
        raise DownloadError("PDF 解析验证失败: " + str(exc)) from exc
    return "signature+eof+pypdf"


def _supplement_extension(url, headers):
    disposition = headers.get("Content-Disposition", "")
    match = re.search(r"filename\*?\s*=\s*(?:UTF-8''|\")?([^\";]+)", disposition, re.I)
    name = unquote(match.group(1).strip()) if match else unquote(urlsplit(url).path)
    ext = Path(name).suffix.lower()
    if ext in ALLOWED_EXTENSIONS:
        return ext
    # Unknown executable/code names must never be relabelled from MIME.
    if ext in {".exe", ".dll", ".bat", ".cmd", ".ps1", ".js", ".vbs", ".msi", ".scr", ".com", ".sh", ".py"}:
        raise DownloadError("不下载可执行文件或脚本")
    mime = headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
    mapping = {"application/pdf": ".pdf", "application/zip": ".zip", "application/x-zip-compressed": ".zip", "text/csv": ".csv", "text/plain": ".txt", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx", "image/png": ".png", "image/jpeg": ".jpg", "image/gif": ".gif", "image/tiff": ".tif", "image/webp": ".webp"}
    if mime in mapping:
        return mapping[mime]
    raise DownloadError("补充材料不是本版支持的直接文件格式")


def verify_supplement(path, extension, content_type=""):
    path = Path(path)
    if path.stat().st_size == 0 or path.stat().st_size > MAX_SUPPLEMENT_BYTES:
        raise DownloadError("补充文件为空或超过大小限制")
    with path.open("rb") as stream:
        head = stream.read(8192)
    stripped = head.lstrip().lower()
    if "text/html" in content_type.lower() or stripped.startswith((b"<!doctype html", b"<html", b"<head", b"<body")):
        raise DownloadError("补充材料链接返回 HTML 页面，未当作文件保存")
    if extension == ".pdf":
        return verify_pdf(path)
    if extension in {".zip", ".xlsx", ".docx"}:
        try:
            with zipfile.ZipFile(path) as archive:
                entries = archive.infolist()
                if not entries or len(entries) > 10000 or sum(x.file_size for x in entries) > 64 * 1024 * 1024:
                    raise DownloadError("压缩包目录异常或解压体积过大")
                names = {x.filename for x in entries}
                if extension == ".xlsx" and not {"[Content_Types].xml", "xl/workbook.xml"}.issubset(names):
                    raise DownloadError("文件不是有效 XLSX 容器")
                if extension == ".docx" and not {"[Content_Types].xml", "word/document.xml"}.issubset(names):
                    raise DownloadError("文件不是有效 DOCX 容器")
                # ZIP is never extracted or executed. Check CRC under a strict budget.
                for item in entries:
                    if item.flag_bits & 1:
                        raise DownloadError("加密压缩包无法验证")
                if archive.testzip() is not None:
                    raise DownloadError("压缩文件校验失败")
        except (zipfile.BadZipFile, RuntimeError, OSError) as exc:
            raise DownloadError("压缩文件验证失败") from exc
        return "zip-directory+crc (not extracted)"
    if extension in {".txt", ".csv"}:
        raw = path.read_bytes()
        try:
            value = raw.decode("utf-16" if raw.startswith((b"\xff\xfe", b"\xfe\xff")) else "utf-8-sig")
        except UnicodeError as exc:
            raise DownloadError("文本编码未识别，仅自动保存 UTF-8/UTF-16 文本") from exc
        if "\x00" in value or value.lstrip().lower().startswith(("<!doctype html", "<html")):
            raise DownloadError("文件不是可识别文本")
        if extension == ".csv" and not any(x in value for x in (",", ";", "\t")):
            raise DownloadError("CSV 未识别到分隔符")
        return "text-encoding"
    signatures = {".png": head.startswith(b"\x89PNG\r\n\x1a\n"), ".jpg": head.startswith(b"\xff\xd8\xff"), ".jpeg": head.startswith(b"\xff\xd8\xff"), ".gif": head.startswith((b"GIF87a", b"GIF89a")), ".tif": head.startswith((b"II*\x00", b"MM\x00*")), ".tiff": head.startswith((b"II*\x00", b"MM\x00*")), ".webp": head.startswith(b"RIFF") and head[8:12] == b"WEBP"}
    if not signatures.get(extension):
        raise DownloadError("补充文件格式标记与扩展名不符")
    try:
        from PIL import Image
        with Image.open(path) as picture:
            picture.verify()
    except ImportError:
        return "image-signature"
    except Exception as exc:
        raise DownloadError("图片解析失败") from exc
    return "image-signature+parser"


class _LandingParser(HTMLParser):
    def __init__(self, base_url):
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.pdf_urls = []
        self.supplements = []
        self.manual_supplements = []
        self.anchor = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "meta" and attrs.get("name", "").lower() == "citation_pdf_url":
            value = attrs.get("content", "")
            if value:
                self.pdf_urls.append(urljoin(self.base_url, value))
        if tag == "a":
            self.anchor = {"href": attrs.get("href", ""), "text": " ".join(attrs.get(k, "") for k in ("title", "aria-label", "download"))}
        if tag == "img" and self.anchor:
            self.anchor["text"] += " " + attrs.get("alt", "")

    def handle_data(self, data):
        if self.anchor is not None:
            self.anchor["text"] += " " + data

    def handle_endtag(self, tag):
        if tag == "a" and self.anchor is not None:
            href, label = self.anchor["href"], " ".join(self.anchor["text"].split())
            self.anchor = None
            if not href or href.startswith(("#", "javascript:", "mailto:")) or not SUPPLEMENT_LABEL.search(label):
                return
            url = urljoin(self.base_url, href)
            parsed = urlsplit(url)
            path = unquote(parsed.path).lower().rstrip("/")
            # PLOS site-wide author guidance is navigation, not this article's SI.
            if re.search(r"/s/(?:supporting-information|submission-guidelines)$", path):
                return
            ext = Path(path).suffix.lower()
            query = parse_qs(parsed.query)
            plos_attachment = (path.endswith("/article/file")
                               and query.get("type", [""])[0].lower() == "supplementary"
                               and re.search(r"\.s\d+$", query.get("id", [""])[0], re.I))
            # Explicit attachment label plus a file extension or recognised route.
            direct = bool(plos_attachment) or ext in ALLOWED_EXTENSIONS or any(term in url.lower() for term in ("/attachment", "/suppinfo", "/suppl_file/", "/suppl/", "download=", "/download", "/bin/", "/media/"))
            if direct:
                self.supplements.append({"url": url, "label": label[:160]})
            elif urlsplit(url).scheme in {"http", "https"}:
                self.manual_supplements.append({"url": url, "label": label[:160], "reason": "附件页面或格式未支持自动下载，请手动核查"})


def _ordered_unique(values):
    return list(dict.fromkeys(x for x in values if isinstance(x, str) and x.strip()))


def _read_landing(client, url):
    raw, final, headers = client.bytes(url)
    if raw.lstrip().startswith(b"%PDF-"):
        return None, final
    charset = headers.get_content_charset() or "utf-8"
    try:
        html = raw.decode(charset, errors="replace")
    except LookupError:
        html = raw.decode("utf-8", errors="replace")
    # A successful HTTP status can still be an access challenge.
    lower = html.lower()
    if any(token in lower for token in ("cf-chl-", "verify you are human", "just a moment...", "checking your browser")):
        raise DownloadError("页面需要浏览器验证，自动检查未完成")
    parser = _LandingParser(final)
    parser.feed(html)
    return parser, final


def _resolve_openalex(paper, client):
    doi = normalize_doi(str(paper.get("doi") or ""))
    if not doi or not re.fullmatch(r"10\.\d{4,9}/\S+", doi):
        return [], [], []
    endpoint = "https://api.openalex.org/works/https://doi.org/" + quote(doi, safe="/")
    try:
        raw, _, _ = client.bytes(endpoint, max_bytes=3 * 1024 * 1024, accept="application/json")
        data = json.loads(raw)
        locations = [data.get("best_oa_location") or {}] + [x for x in data.get("locations", []) if isinstance(x, dict) and x.get("is_oa")]
        pdfs, landings = [], []
        for location in locations:
            if location.get("is_oa"):
                if location.get("pdf_url"):
                    pdfs.append(location["pdf_url"])
                if location.get("landing_page_url"):
                    landings.append(location["landing_page_url"])
        oa_url = (data.get("open_access") or {}).get("oa_url")
        if oa_url:
            if Path(urlsplit(oa_url).path).suffix.lower() == ".pdf":
                pdfs.append(oa_url)
            else:
                landings.append(oa_url)
        return _ordered_unique(pdfs), _ordered_unique(landings), []
    except Cancelled:
        raise
    except Exception as exc:
        return [], [], ["OpenAlex 开放全文解析未完成: " + str(exc)]


def _atomic_json(path, data):
    temp = path.with_name(path.name + "." + uuid.uuid4().hex + ".part")
    try:
        with temp.open("x", encoding="utf-8") as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def _inside(path, root):
    try:
        Path(path).resolve().relative_to(root.resolve())
        return True
    except (ValueError, OSError):
        return False


def _available_name(path):
    if not path.exists():
        return path
    for index in range(2, 10000):
        candidate = path.with_name(f"{path.stem}-{index}{path.suffix}")
        if not candidate.exists():
            return candidate
    raise DownloadError("文件名冲突过多")


def _emit(progress, result, current, total, message, status=None, **extra):
    if progress:
        event = {"id": result["id"], "status": status or result["status"], "message": message, "current": current, "total": total, **extra}
        try:
            progress(event)
        except Exception:
            # A UI reporting failure must not discard downloaded files or manifest.
            pass


def _supplements(paper, result, old, client, directory, stem, landings, landing_cache, progress, current, total):
    result["supplements"] = []
    links, errors, checked, manual_links = [], [], [], []
    for url in _ordered_unique(landings)[:MAX_LANDING_PAGES]:
        _check_cancel(client.cancel_event)
        _emit(progress, result, current, total, "检查公开页面中的补充材料链接", "supplements", url=url)
        try:
            if url in landing_cache:
                parser, final = landing_cache[url]
            else:
                parser, final = _read_landing(client, url)
                landing_cache[url] = (parser, final)
            checked.append(final)
            if parser:
                links.extend(parser.supplements)
                manual_links.extend(parser.manual_supplements)
        except Cancelled:
            raise
        except Exception as exc:
            errors.append({"url": url, "error": str(exc)})
    result["si_checked_urls"] = checked
    result["si_page_errors"] = errors
    result["si_manual_links"] = list({x["url"]: x for x in manual_links}.values())
    result["manual_urls"] = _ordered_unique(result["manual_urls"] + [x["url"] for x in manual_links])
    unique_links = {x["url"]: x for x in links}
    candidates = list(unique_links.values())[:MAX_SUPPLEMENTS]
    prior = {x.get("source_url"): x for x in old.get("supplements", []) if isinstance(x, dict)}
    for index, link in enumerate(candidates, 1):
        _check_cancel(client.cancel_event)
        entry = {"source_url": link["url"], "label": link["label"], "status": "failed", "path": "", "error": "", "sha256": "", "bytes": 0}
        temp = None
        try:
            known = prior.get(link["url"], {})
            oldpath = Path(known.get("path") or directory / "__missing__")
            if oldpath.is_file() and _inside(oldpath, directory):
                try:
                    validation = verify_supplement(oldpath, oldpath.suffix.lower())
                    digest = _sha256(oldpath)
                    if not known.get("sha256") or digest == known["sha256"]:
                        entry.update(status="existing", path=str(oldpath), sha256=digest, bytes=oldpath.stat().st_size, validation=validation)
                        result["supplements"].append(entry)
                        continue
                    entry["existing_file_warning"] = "既有补充文件摘要不符，将保留并重新下载"
                except Exception as exc:
                    entry["existing_file_warning"] = str(exc)
            temp, final, headers, size, digest = client.download(link["url"], directory, MAX_SUPPLEMENT_BYTES)
            ext = _supplement_extension(final, headers)
            validation = verify_supplement(temp, ext, headers.get("Content-Type", ""))
            target = _available_name(directory / f"{stem}-supplement-{index}{ext}")
            os.replace(temp, target)
            temp = None
            entry.update(status="downloaded", path=str(target), final_url=final, sha256=digest, bytes=size, validation=validation)
        except Cancelled:
            raise
        except Exception as exc:
            entry["error"] = str(exc)
        finally:
            if temp is not None:
                temp.unlink(missing_ok=True)
        result["supplements"].append(entry)
        _emit(progress, result, current, total, f"补充材料 {index}/{len(candidates)}：" + ("已验证保存" if entry["status"] == "downloaded" else entry["error"]), "supplements", path=entry["path"], url=link["url"])
    successful = [x for x in result["supplements"] if x["status"] in {"downloaded", "existing"}]
    failed = [x for x in result["supplements"] if x["status"] == "failed"]
    limited = len(unique_links) > MAX_SUPPLEMENTS or len(_ordered_unique(landings)) > MAX_LANDING_PAGES
    result["si_limits_reached"] = limited
    if successful:
        result["si_status"] = "partial" if failed or errors or limited or manual_links else ("downloaded" if any(x["status"] == "downloaded" for x in successful) else "existing")
        result["si_message"] = f"已验证 {len(successful)} 个补充文件；只检查公开页面的明确附件链接，未证明补充材料已穷尽。"
        if failed or errors or limited or manual_links:
            result["si_message"] += " 部分链接需手动查看、检查失败或达到上限；请查看清单和原文入口。"
    elif candidates:
        result["si_status"] = "failed"
        result["si_message"] = "识别到了补充材料候选，但均未通过下载或格式验证；请从原文入口手动查看。"
    elif manual_links:
        result["si_status"] = "incomplete"
        result["si_message"] = "已发现补充材料入口，但不是支持的直接文件；入口已写入清单，请手动查看。"
    elif errors:
        result["si_status"] = "incomplete" if checked else "failed"
        result["si_message"] = "补充材料页面检查未完成；不能据此判断论文没有补充材料。请从原文入口查看。"
    else:
        result["si_status"] = "not_identified"
        result["si_message"] = "公开页面中未识别到可直接下载的补充链接；不代表没有补充材料，请从原文入口核查。"


def download_batch(papers, out_dir, cancel_event=None, progress=None, include_supplements=True, *, allow_local=False):
    """Download only the explicit selection; return a manifest even on failures.

    include_supplements defaults to True, as requested by the user. Downloads
    are sequential, cancellable between chunks, size limited and verified.
    Existing valid files are retained; invalid files are not overwritten.
    """
    directory = Path(out_dir).expanduser().resolve()
    directory.mkdir(parents=True, exist_ok=True)
    selected = []
    seen = set()
    for source in papers:
        paper = dict(source)
        identity = str(paper.get("id") or paper_id(paper))
        if identity not in seen:
            seen.add(identity)
            paper["id"] = identity
            selected.append(paper)
    batch_id = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    manifest_path = directory / f"manifest-{batch_id}.json"
    manifest = {"schema_version": 1, "created_at": datetime.now().astimezone().isoformat(), "include_supplements": bool(include_supplements), "selection_count": len(selected), "results": [], "summary": {}, "limits": {"pdf_bytes": MAX_PDF_BYTES, "supplement_bytes": MAX_SUPPLEMENT_BYTES, "supplements_per_paper": MAX_SUPPLEMENTS, "landing_pages_per_paper": MAX_LANDING_PAGES}, "note": "仅公开链接；未登录、未绕过付费或访问控制。补充材料为有限自动识别，未识别不等于不存在。"}
    client = _Client(cancel_event, allow_local)
    for current, paper in enumerate(selected, 1):
        identity = paper["id"]
        stem = f"{paper.get('year') or 'undated'}-{safe_filename(str(paper.get('title') or 'untitled'), max_length=85)}-{hashlib.sha256(identity.encode()).hexdigest()[:10]}"
        sidecar = directory / (stem + ".json")
        result = {"id": identity, "title": paper.get("title", ""), "status": "unavailable", "path": "", "error": "", "source_url": "", "sha256": "", "bytes": 0, "si_status": "pending" if include_supplements else "skipped", "si_message": "", "supplements": [], "paper": paper, "manual_urls": _ordered_unique([paper.get("url"), "https://doi.org/" + str(paper["doi"]) if paper.get("doi") else None]), "sidecar_path": str(sidecar)}
        old = {}
        try:
            if sidecar.is_file():
                value = json.loads(sidecar.read_text(encoding="utf-8"))
                if isinstance(value, dict) and value.get("id") == identity:
                    old = value
        except (OSError, ValueError):
            pass
        try:
            _check_cancel(cancel_event)
            _emit(progress, result, current, len(selected), "检查已下载文件和公开全文候选", "resolving")
            known_path = Path(old.get("path") or directory / (stem + ".pdf"))
            if known_path.is_file() and _inside(known_path, directory):
                try:
                    validation = verify_pdf(known_path)
                    digest = _sha256(known_path)
                    if old.get("sha256") and digest != old["sha256"]:
                        raise DownloadError("既有 PDF 的 SHA256 与清单不符；保留旧文件并重新下载")
                    result.update(status="existing", path=str(known_path), source_url=old.get("source_url", ""), sha256=digest, bytes=known_path.stat().st_size, validation=validation, pdf_location=old.get("pdf_location", {}))
                except Exception as exc:
                    result["existing_file_warning"] = str(exc)
            candidates = _ordered_unique(paper.get("pdf_urls") or [])
            oa_landings = _ordered_unique((paper.get("oa_landing_urls") or []) + (paper.get("oa_urls") or []) + [paper.get("oa_landing_url")])
            warnings = []
            landing_cache = {}
            if result["status"] != "existing" and not candidates:
                found, resolved_landings, warnings = _resolve_openalex(paper, client)
                candidates.extend(found)
                oa_landings.extend(resolved_landings)
            # citation_pdf_url is considered only on OA-labelled metadata pages.
            if paper.get("oa") and paper.get("url"):
                oa_landings.append(paper["url"])
            if result["status"] != "existing" and not candidates:
                for landing in _ordered_unique(oa_landings)[:MAX_LANDING_PAGES]:
                    try:
                        parser, final = _read_landing(client, landing)
                        landing_cache[landing] = (parser, final)
                        if parser:
                            candidates.extend(parser.pdf_urls)
                    except Cancelled:
                        raise
                    except Exception as exc:
                        warnings.append("开放落地页检查失败: " + str(exc))
            failures = []
            if result["status"] != "existing":
                for url in _ordered_unique(candidates)[:MAX_PDF_CANDIDATES]:
                    _check_cancel(cancel_event)
                    temp = None
                    try:
                        _emit(progress, result, current, len(selected), "下载并验证正文 PDF", "downloading", url=url)
                        temp, final, headers, size, digest = client.download(url, directory, MAX_PDF_BYTES)
                        if "text/html" in headers.get("Content-Type", "").lower():
                            raise DownloadError("全文链接返回 HTML 页面，而不是 PDF")
                        validation = verify_pdf(temp)
                        target = _available_name(directory / (stem + ".pdf"))
                        os.replace(temp, target)
                        temp = None
                        result.update(status="downloaded", path=str(target), source_url=final, sha256=digest, bytes=size, validation=validation)
                        result["pdf_location"] = next((dict(x) for x in paper.get("pdf_locations", []) if isinstance(x, dict) and x.get("url") == url), {"url": url, "source": "public metadata"})
                        break
                    except Cancelled:
                        raise
                    except Exception as exc:
                        failures.append({"url": url, "error": str(exc)})
                    finally:
                        if temp is not None:
                            temp.unlink(missing_ok=True)
                if result["status"] != "downloaded":
                    result["status"] = "failed" if failures else "unavailable"
                    result["error"] = failures[-1]["error"] if failures else "未找到已知的公开全文直链；请使用原文或 DOI 入口。"
            result["attempts"] = failures
            result["warnings"] = warnings
            landings = _ordered_unique(oa_landings + [paper.get("url")] + result["manual_urls"])
            result["manual_urls"] = _ordered_unique(result["manual_urls"] + oa_landings)
            if include_supplements:
                _supplements(paper, result, old, client, directory, stem, landings, landing_cache, progress, current, len(selected))
            else:
                result["si_message"] = "本批次未启用补充材料下载。"
        except Cancelled:
            if result["status"] not in {"downloaded", "existing"}:
                result["status"] = "cancelled"
                result["error"] = "用户取消；已完成文件保留。"
            if include_supplements and result["si_status"] == "pending":
                result["si_status"] = "cancelled"
                result["si_message"] = "补充材料检查或下载已取消；已完成文件保留。"
        except Exception as exc:
            if result["status"] not in {"downloaded", "existing"}:
                result["status"] = "failed"
                result["error"] = str(exc)
            if include_supplements and result["si_status"] == "pending":
                result["si_status"] = "failed"
                result["si_message"] = "补充材料检查未完成: " + str(exc)
        result["updated_at"] = datetime.now().astimezone().isoformat()
        try:
            if old and result["status"] == "cancelled" and not result["path"]:
                result["previous_sidecar_preserved"] = True
            else:
                cache_record = dict(result)
                # A cancelled/incomplete scan must not erase prior file provenance.
                present = {x.get("source_url") for x in result["supplements"]}
                retained = [dict(x, status="cached_unchecked", cached_from_previous_run=True)
                            for x in old.get("supplements", [])
                            if isinstance(x, dict) and x.get("path") and x.get("source_url") not in present]
                cache_record["supplements"] = list(result["supplements"]) + retained
                _atomic_json(sidecar, cache_record)
        except OSError as exc:
            result["sidecar_error"] = str(exc)
        manifest["results"].append(result)
        summary = {key: sum(x["status"] == key for x in manifest["results"]) for key in ("downloaded", "existing", "failed", "unavailable", "cancelled")}
        summary.update(total=len(selected), processed=len(manifest["results"]), supplements_downloaded=sum(s["status"] == "downloaded" for x in manifest["results"] for s in x["supplements"]), supplements_existing=sum(s["status"] == "existing" for x in manifest["results"] for s in x["supplements"]), supplements_failed=sum(s["status"] == "failed" for x in manifest["results"] for s in x["supplements"]))
        summary["si_statuses"] = {key: sum(x["si_status"] == key for x in manifest["results"]) for key in ("downloaded", "existing", "partial", "not_identified", "incomplete", "failed", "cancelled", "skipped")}
        manifest["summary"] = summary
        _atomic_json(manifest_path, manifest)
        _emit(progress, result, current, len(selected), ("正文已验证保存" if result["status"] == "downloaded" else "已有正文已复验" if result["status"] == "existing" else result["error"]) + ("；" + result["si_message"] if include_supplements else ""), path=result["path"], manual_urls=result["manual_urls"])
    if not selected:
        manifest["summary"] = {"total": 0, "processed": 0, "downloaded": 0, "existing": 0, "failed": 0, "unavailable": 0, "cancelled": 0, "supplements_downloaded": 0, "supplements_existing": 0, "supplements_failed": 0, "si_statuses": {}}
        _atomic_json(manifest_path, manifest)
    return {"results": manifest["results"], "manifest_path": str(manifest_path), "summary": manifest["summary"]}
