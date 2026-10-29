"""Create a portable release from the separately built, verified staging folder."""
from pathlib import Path
import argparse, hashlib, json, shutil, zipfile

def main():
    root = Path(__file__).resolve().parent
    version = json.loads((root / "paperharbor.project.json").read_text(encoding="utf8"))["version"]
    parser = argparse.ArgumentParser()
    parser.add_argument("--prepare", action="store_true", help="Copy release documents before build")
    options = parser.parse_args()
    stage = root / "build" / ("release-v" + version)
    stage.mkdir(parents=True, exist_ok=True)
    for name in ("paperharbor.project.json", "README.md", "使用说明.html", "create_shortcut.ps1"):
        shutil.copy2(root / name, stage / name)
    (stage / "assets").mkdir(exist_ok=True)
    shutil.copy2(root / "assets" / "paperharbor.ico", stage / "assets" / "paperharbor.ico")
    (stage / "启动文献港.cmd").write_bytes(b'@echo off\r\nstart "" "%~dp0dist\\PaperHarbor\\PaperHarbor.exe"\r\n')
    if options.prepare:
        print(stage)
        return
    exe = stage / "dist" / "PaperHarbor" / "PaperHarbor.exe"
    if not exe.is_file():
        raise SystemExit("Build the staging EXE first")
    files = [p for p in (stage / "dist" / "PaperHarbor").rglob("*") if p.is_file()]
    files += [stage / n for n in ("paperharbor.project.json", "README.md", "使用说明.html",
                                  "create_shortcut.ps1", "启动文献港.cmd", "assets/paperharbor.ico")]
    releases = root / "releases"
    releases.mkdir(exist_ok=True)
    archive = releases / ("PaperHarbor-" + version + "-Windows-x64.zip")
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as bundle:
        for path in sorted(files):
            bundle.write(path, "PaperHarbor-" + version + "/" + path.relative_to(stage).as_posix())
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    archive.with_suffix(".sha256").write_text(digest + "  " + archive.name + "\n", encoding="utf8")
    print(json.dumps({"archive": str(archive), "bytes": archive.stat().st_size,
                      "sha256": digest, "files": len(files)}, ensure_ascii=False))

if __name__ == "__main__":
    main()
