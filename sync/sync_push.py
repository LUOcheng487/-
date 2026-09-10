#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
把 D:\\Excel 下6个平台禁售xlsx同步到本仓库 data/ 目录并 git push。
由 Windows 任务计划程序每15分钟调一次（pythonw 静默运行），文件无变化时零提交。

用法: pythonw sync_push.py   (在仓库任意位置运行均可，脚本自动定位仓库根目录)
"""
import datetime as dt
import json
import os
import shutil
import subprocess
import sys
import time
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Asia/Shanghai")
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(REPO_ROOT, "data")


def git(*args, check=True):
    r = subprocess.run(["git", "-C", REPO_ROOT, *args],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    if check and r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} 失败:\n{r.stderr.strip()}")
    return r


def copy_with_retry(src, dst, retries=3):
    """Excel可能正被占用，复制到临时文件再原子替换"""
    tmp = dst + ".tmp"
    for i in range(retries):
        try:
            shutil.copy2(src, tmp)
            os.replace(tmp, dst)
            return
        except PermissionError:
            if i == retries - 1:
                raise
            time.sleep(2)


def main():
    os.chdir(REPO_ROOT)
    os.makedirs(DATA_DIR, exist_ok=True)

    with open(os.path.join(REPO_ROOT, "config.json"), encoding="utf-8") as f:
        config = json.load(f)

    failed = []
    for p in config["platforms"]:
        src = p["local_path"]
        dst = os.path.join(DATA_DIR, p["file"])
        try:
            copy_with_retry(src, dst)
        except Exception as e:
            failed.append(f"{p['name']}: {e}")
    if failed:
        print("以下文件同步失败(下个周期重试): " + "; ".join(failed))

    with open(os.path.join(DATA_DIR, "_sync_info.json"), "w", encoding="utf-8") as f:
        json.dump({"synced_at": dt.datetime.now(TZ).strftime("%Y-%m-%d %H:%M")},
                  f, ensure_ascii=False)

    git("add", "data")
    status = git("status", "--porcelain", "--", "data", check=False)
    if not status.stdout.strip():
        print("数据无变化，跳过推送")
        return 0

    ident = ("-c", "user.name=ban-sync", "-c", "user.email=ban-sync@users.noreply.github.com")
    msg = f"data: sync {dt.datetime.now(TZ).strftime('%Y-%m-%d %H:%M')}"
    git(*ident, "commit", "-m", msg)

    if git("push", check=False).returncode != 0:
        # 远端有新提交(如网页上改了config)时先合并再推
        git("pull", "--rebase", "--autostash", check=False)
        if git("push", check=False).returncode != 0:
            print("推送失败，请检查网络/GitHub凭据")
            return 1
    print("已推送:", msg)
    return 0


if __name__ == "__main__":
    sys.exit(main())
