"""Self-contained package diagnostics; never write into the user's saved settings."""
def self_check():
    import ctypes, gc, json, ssl, sys, tempfile, time
    from pathlib import Path
    from ctypes import wintypes
    from unittest.mock import patch
    from PIL import ImageGrab
    import pypdf, core, app, providers, downloader
    root = core.APP_DIR / "validation"
    root.mkdir(parents=True, exist_ok=True)
    offline = "--offline" in sys.argv
    probe = {"openssl": ssl.OPENSSL_VERSION, "offline": offline}
    network = {"papers": [], "sources": {}, "warnings": []}
    with tempfile.TemporaryDirectory(prefix="self-check-", dir=root) as td:
        temp = Path(td)
        with patch.object(core, "DATA_DIR", temp / "data"), patch.object(providers, "CACHE_DIR", temp / "cache"):
            if not offline:
                network = providers.search("10.1371/journal.pone.0301505", limit=1, sources=["crossref"])
                probe["crossref_ok"] = network["sources"].get("crossref", {}).get("status") == "ok"
                probe["exact_doi_match"] = len(network["papers"]) == 1 and network["papers"][0].get("doi") == "10.1371/journal.pone.0301505"
            writer = pypdf.PdfWriter()
            writer.add_blank_page(width=200, height=200)
            sample = temp / "parser-check.pdf"
            with sample.open("wb") as stream:
                writer.write(stream)
            parser_check = downloader.verify_pdf(sample)
            verified = []
            for name in ("live-download", "live-supplements"):
                for path in (root / name).glob("*.pdf"):
                    verified.append({"file": path.name, "verification": downloader.verify_pdf(path),
                                     "pages": len(pypdf.PdfReader(str(path)).pages)})
            try:
                ctypes.windll.shcore.SetProcessDpiAwareness(1)
            except Exception:
                pass
            window = app.PaperHarborApp()
            try:
                window.papers = core.deduplicate(network["papers"])
                window.by_id = {p["id"]: p for p in window.papers}
                window.render_results()
                if window.papers:
                    window.tree.selection_set(window.papers[0]["id"])
                    window._show_details()
                window.source_status_var.set("打包自检：" + ("离线界面验证" if offline else "本次真实 Crossref 检索"))
                window.update()
                until = time.monotonic() + .25
                while time.monotonic() < until:
                    window.update()
                    time.sleep(.02)
                user = ctypes.windll.user32
                user.GetParent.argtypes = [wintypes.HWND]
                user.GetParent.restype = wintypes.HWND
                hwnd = user.GetParent(window.winfo_id())
                screenshot_error = None
                try:
                    ImageGrab.grab(window=hwnd).save(root / "packaged-ui.png")
                except Exception as exc:
                    screenshot_error = str(exc)
                record = {
                    "frozen": bool(getattr(sys, "frozen", False)),
                    "app_dir": str(core.APP_DIR),
                    "version": json.loads((core.APP_DIR / "paperharbor.project.json").read_text(encoding="utf8"))["version"],
                    "pypdf": pypdf.__version__, "tk": window.tk.call("info", "patchlevel"),
                    "visible_download_button": bool(window.download_button.winfo_ismapped()),
                    "results": len(window.papers),
                    "window_size": [window.winfo_width(), window.winfo_height()],
                    "network_probe": probe, "network_sources": network["sources"],
                    "network_warnings": network["warnings"], "pdf_parser_check": parser_check,
                    "verified_pdfs": verified, "automatic_sources": list(providers.SOURCE_NAMES),
                    "screenshot_error": screenshot_error, "isolated_settings": True,
                }
                try:
                    import publisher_catalog
                    record["publisher_catalog_loaded"] = True
                except ImportError:
                    record["publisher_catalog_loaded"] = False
                core.atomic_json(root / "packaged-check.json", record)
            finally:
                window._close()
                del window
                gc.collect()
