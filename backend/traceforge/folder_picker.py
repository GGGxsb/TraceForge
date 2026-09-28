from __future__ import annotations

import asyncio
import subprocess
import sys


_DIALOG_SCRIPT = """
from tkinter import Tk, filedialog

root = Tk()
root.withdraw()
root.attributes('-topmost', True)
try:
    selected = filedialog.askdirectory(parent=root, title='选择 TraceForge 工作区', mustexist=True)
    print(selected, end='')
finally:
    root.destroy()
"""


async def pick_directory() -> str | None:
    """Open a folder dialog on the machine running the local FastAPI server."""
    creationflags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        _DIALOG_SCRIPT,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        creationflags=creationflags,
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=600)
    except (TimeoutError, asyncio.CancelledError):
        process.kill()
        await process.communicate()
        raise
    if process.returncode:
        detail = stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(
            "无法打开本机文件夹选择器。请确认后端在图形桌面中运行，且 Python 已安装 tkinter。"
            + (f" 详细信息：{detail[-500:]}" if detail else "")
        )
    return stdout.decode("utf-8", errors="replace").strip() or None
