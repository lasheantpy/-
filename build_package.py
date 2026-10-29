"""Build a self-contained Windows app; use --wait in CI."""
from pathlib import Path
import argparse,importlib.util,os,subprocess,sys,ssl

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--wait",action="store_true",help="Wait and return the builder's exit code")
    parser.add_argument("--dist-dir", type=Path, help="Build into a staging directory before replacing the installed version")
    options=parser.parse_args()
    root=Path(__file__).resolve().parent
    (root/"build"/"temp").mkdir(parents=True,exist_ok=True)
    env=os.environ.copy()
    env["TEMP"]=env["TMP"]=str(root/"build"/"temp")
    if importlib.util.find_spec("PyInstaller") is None:
        tools=root/".build-tools"
        if not (tools/"PyInstaller").is_dir():
            raise SystemExit("Install build dependencies: python -m pip install -r requirements-build.txt")
        env["PYTHONPATH"]=str(tools)+(os.pathsep+env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    env["PYINSTALLER_CONFIG_DIR"]=str(root/"build"/"pyinstaller-cache")
    args=[sys.executable,"-m","PyInstaller","--onedir","--noconfirm","--windowed",
          "--name","PaperHarbor","--icon",str(root/"assets"/"paperharbor.ico"),
          "--distpath",str(options.dist_dir.resolve() if options.dist_dir else root/"dist"),"--workpath",str(root/"build"/"pyinstaller"),
          "--specpath",str(root/"build"),"--paths",str(root),"--hidden-import","pypdf","--hidden-import","PIL.Image"]
    for module in ["numpy","scipy","pandas","matplotlib","IPython","pytest","torch","tensorflow","cv2","PyQt5","PySide6"]:
        args+=["--exclude-module",module]
    # Some Python distributions require this public trust-policy data at SSL initialization.
    # Include it unchanged when present; never relax certificate verification.
    pem=Path(ssl.__file__).resolve().parent/"codex-rejected-cas.pem"
    if pem.is_file():args+=["--add-data",str(pem)+":."]
    args+=[str(root/"launch.pyw")]
    with (root/"build"/"package.log").open("w",encoding="utf8") as log:
        process=subprocess.Popen(args,cwd=root,env=env,stdout=log,stderr=subprocess.STDOUT,
                                  creationflags=getattr(subprocess,"CREATE_NO_WINDOW",0))
        print("Build PID:",process.pid,"; log:",root/"build"/"package.log")
        if options.wait:raise SystemExit(process.wait())
if __name__=="__main__":main()
