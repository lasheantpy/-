"""Desktop entry point with local error reporting."""
import sys,traceback
def run():
    if "--self-check" in sys.argv:
        import diagnostics
        diagnostics.self_check()
        return
    import ctypes,hashlib
    from ctypes import wintypes
    import core
    kernel=ctypes.WinDLL("kernel32",use_last_error=True)
    kernel.CreateMutexW.argtypes=[wintypes.LPVOID,wintypes.BOOL,wintypes.LPCWSTR]
    kernel.CreateMutexW.restype=wintypes.HANDLE
    kernel.CloseHandle.argtypes=[wintypes.HANDLE]
    key=hashlib.sha256(str(core.APP_DIR).encode()).hexdigest()[:16]
    handle=kernel.CreateMutexW(None,False,"Local\\PaperHarbor-"+key)
    if handle and ctypes.get_last_error()==183:
        user=ctypes.windll.user32
        user.FindWindowW.argtypes=[wintypes.LPCWSTR,wintypes.LPCWSTR]
        user.FindWindowW.restype=wintypes.HWND
        user.ShowWindow.argtypes=[wintypes.HWND,ctypes.c_int]
        user.SetForegroundWindow.argtypes=[wintypes.HWND]
        hwnd=user.FindWindowW(None,"文献港 PaperHarbor")
        if hwnd:
            user.ShowWindow(hwnd,9)
            user.SetForegroundWindow(hwnd)
        kernel.CloseHandle(handle)
        return
    try:
        from app import main
        main()
    finally:
        if handle:kernel.CloseHandle(handle)

try:
    run()
except Exception:
    detail=traceback.format_exc()
    try:
        import core
        core.DATA_DIR.mkdir(parents=True,exist_ok=True)
        (core.DATA_DIR/"startup-error.log").write_text(detail,encoding="utf-8")
    except Exception:
        pass
    if "--self-check" in sys.argv:
        if sys.stderr is not None:
            sys.stderr.write(detail)
        raise SystemExit(1)
    import ctypes
    ctypes.windll.user32.MessageBoxW(None,"程序启动未完成。\n请查看程序目录 data/startup-error.log。\n\n"+detail[-1800:],"文献港 PaperHarbor",16)
