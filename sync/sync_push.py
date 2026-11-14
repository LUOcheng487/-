#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
把各平台禁售 xlsx 同步到本仓库 data/ 目录并 git push。
由 Windows 任务计划程序每15分钟调一次（pythonw 静默运行），文件无变化时零提交。

用法: pythonw sync_push.py   (在仓库任意位置运行均可，脚本自动定位仓库根目录)

网络说明：本机直连 GitHub 时偶发 DNS 漂移 / 连接被重置 / 建连超时，
本脚本对 pull、push 等网络类失败做指数退避重试；认证、冲突、证书等业务错误不重试。
"""
import datetime as dt
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Asia/Shanghai")
HOST = socket.gethostname()  # 多台电脑同步时区分来源
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(REPO_ROOT, "data")

# pythonw 静默运行时 stdout/stderr 可能为 None，避免 print 直接崩溃
if sys.stdout is None:
    sys.stdout = open(os.devnull, "w", encoding="utf-8", errors="replace")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w", encoding="utf-8", errors="replace")

# 网络类错误关键词：偶发抖动，值得重试
_NETWORK_RE = re.compile(
    r"connection was reset|could not connect|failed to connect|couldn't connect|"
    r"timed out|timeout|recv failure|early eof|network is down|unexpected eof|"
    r"connection refused|operation now in progress|failed to receive|broken pipe|"
    r"handshake failure|connection closed by peer|reset by peer|"
    r"operation too slow|less than \d+ bytes|low speed|slow transfer|"
    r"connection timed out",
    re.I,
)
# 业务/致命错误：重试无意义，直接返回
_FATAL_RE = re.compile(
    r"authentication failed|could not read username|permission denied|403|401|"
    r"invalid username|repository not found|ssl certificate problem|certificate verify failed",
    re.I,
)

# 网络参数：10s 内必须完成建连；带宽低于 100B/s 持续 30s 判定断链，快速失败进入重试
_GIT_NET_OPTS = [
    "-c", "http.connectTimeout=10",
    "-c", "http.lowSpeedLimit=100",
    "-c", "http.lowSpeedTime=30",
]

RETRY_DELAYS = (5, 10, 20, 30)  # 指数退避（秒），对应各次重试
GIT_TIMEOUT = 60               # 单条 git 命令整体超时（秒）


def _resolve_git() -> str:
    """定位 git 可执行文件：优先 PATH，其次常见安装目录，避免任务计划环境 PATH 缺 Git。"""
    found = shutil.which("git")
    if found:
        return found
    candidates = [
        r"C:\Program Files\Git\cmd\git.exe",
        r"C:\Program Files (x86)\Git\cmd\git.exe",
        os.path.expandvars(r"%LOCALAPPDATA%\Programs\Git\cmd\git.exe"),
    ]
    for c in candidates:
        if os.path.exists(c):
            return c
    return "git"


GIT_BIN = _resolve_git()


def _is_network_error(stderr: str) -> bool:
    if not stderr:
        return False
    if _FATAL_RE.search(stderr):
        return False
    return bool(_NETWORK_RE.search(stderr))


def git(*args, check=True, retries=4, timeout=GIT_TIMEOUT):
    """
    调用 git。网络类失败按 RETRY_DELAYS 退避重试；其余失败原样返回。
    check=True 且最终失败时抛 RuntimeError；check=False 时返回进程结果供调用方判断。
    """
    last = None
    for attempt in range(1, retries + 1):
        try:
            r = subprocess.run(
                [GIT_BIN, "-C", REPO_ROOT, *_GIT_NET_OPTS, *args],
                capture_output=True, text=True, encoding="utf-8",
                errors="replace", timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            last = None
            if attempt < retries:
                wait = RETRY_DELAYS[min(attempt - 1, len(RETRY_DELAYS) - 1)]
                print(f"git {' '.join(args)} 超时({timeout}s)，{wait}s 后重试({attempt}/{retries - 1})")
                time.sleep(wait)
                continue
            if check:
                raise RuntimeError(f"git {' '.join(args)} 超时({timeout}s)")
            return None

        last = r
        if r.returncode == 0:
            return r

        # 仅网络类错误才重试；认证/冲突/证书等直接跳出
        if attempt < retries and _is_network_error(r.stderr):
            wait = RETRY_DELAYS[min(attempt - 1, len(RETRY_DELAYS) - 1)]
            print(f"git {' '.join(args)} 网络抖动，{wait}s 后重试({attempt}/{retries - 1}): "
                  f"{r.stderr.strip()[:160]}")
            time.sleep(wait)
            continue
        break

    if check and last is not None and last.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} 失败:\n{last.stderr.strip()}")
    return last


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


def write_sync_log(status: str, message: str):
    """把本次同步结果按机器名落到 jsonl，便于事后排查是哪次网络抖动。"""
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        log_path = os.path.join(DATA_DIR, f"_sync_log_{HOST}.jsonl")
        rec = {
            "synced_at": dt.datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S"),
            "host": HOST,
            "status": status,
            "message": message[:300],
        }
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception as e:
        print(f"写入同步日志失败(不影响主流程): {e}")


def main():
    os.chdir(REPO_ROOT)
    os.makedirs(DATA_DIR, exist_ok=True)

    # 先拉取远端最新代码和数据（多台电脑时自动获得脚本更新和其他机器的数据）
    p = git("pull", "--rebase", "--autostash", check=False)
    if p is None or p.returncode != 0:
        git("rebase", "--abort", check=False)
        err = (p.stderr.strip()[:200] if p is not None else "命令超时")
        print(f"拉取远端失败(下个周期重试): {err}")
        write_sync_log("pull_failed", err)
        # 拉取失败不中断本地流程：本地先整理好，下个周期网络恢复后仍可推送

    with open(os.path.join(REPO_ROOT, "config.json"), encoding="utf-8") as f:
        config = json.load(f)

    failed = []
    for plat in config["platforms"]:
        src = plat["local_path"]
        dst = os.path.join(DATA_DIR, plat["file"])
        try:
            copy_with_retry(src, dst)
        except Exception as e:
            failed.append(f"{plat['name']}: {e}")
    if failed:
        print("以下文件同步失败(下个周期重试): " + "; ".join(failed))

    # 同步信息按机器名分文件，避免多台电脑互推时冲突
    info_path = os.path.join(DATA_DIR, f"_sync_info_{HOST}.json")
    with open(info_path, "w", encoding="utf-8") as f:
        json.dump({"synced_at": dt.datetime.now(TZ).strftime("%Y-%m-%d %H:%M"),
                   "host": HOST}, f, ensure_ascii=False)

    git("add", "data")
    status = git("status", "--porcelain", "--", "data", check=False)
    status_out = status.stdout.strip() if status is not None else ""
    # 本地是否有未推送的提交（如上次推送失败遗留），有则仍需推送
    ahead_r = git("rev-list", "--count", "@{u}..HEAD", check=False)
    ahead = ahead_r.stdout.strip() if ahead_r is not None and ahead_r.returncode == 0 else "unknown"
    if not status_out and ahead == "0":
        print("数据无变化，跳过推送")
        write_sync_log("skipped", "数据无变化")
        return 0

    ident = ("-c", "user.name=ban-sync", "-c", "user.email=ban-sync@users.noreply.github.com")
    msg = f"data: sync {HOST} {dt.datetime.now(TZ).strftime('%Y-%m-%d %H:%M')}"
    if status_out:
        git(*ident, "commit", "-m", msg)

    r = git("push", check=False)
    if r is None or r.returncode != 0:
        # 远端有新提交(如另一台电脑先推了)时，先变基合并再推
        p = git("pull", "--rebase", "--autostash", check=False)
        if p is None or p.returncode != 0:
            git("rebase", "--abort", check=False)  # 解除卡在中间的rebase状态，下个周期重试
            err = (p.stderr.strip()[:200] if p is not None else "命令超时")
            print(f"合并远端失败(下个周期自动重试): {err}")
            write_sync_log("merge_failed", err)
            return 1
        r2 = git("push", check=False)
        if r2 is None or r2.returncode != 0:
            err = (r2.stderr.strip()[:200] if r2 is not None else "命令超时")
            print(f"推送失败，请检查网络/GitHub凭据: {err}")
            write_sync_log("push_failed", err)
            return 1

    note = "; ".join(failed) if failed else "ok"
    print("已推送:", msg)
    write_sync_log("pushed", note)
    return 0


if __name__ == "__main__":
    sys.exit(main())
