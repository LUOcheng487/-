#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""主动触发云端 workflow（daily-report.yml），绕开 GitHub 自带定时的排队延迟。

用法（计划任务 / 手动都一样）：
    python trigger/dispatch_report.py                 # 正常触发：报告日=昨天，有数据才发
    python trigger/dispatch_report.py --date 2026-12-31   # 连通性自检：该日无数据则不发
    python trigger/dispatch_report.py --force 1       # 强制发送

凭据：运行时从 Git Credential Manager 现取 GitHub token，不落盘。
"""

import argparse
import datetime as dt
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request

GIT = r"C:\Program Files\Git\cmd\git.exe"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOG_DIR = os.path.join(ROOT, "logs")


def log(msg):
    line = "[{}] {}".format(dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"), msg)
    print(line)
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        with open(os.path.join(LOG_DIR, "dispatch_{}.log".format(dt.date.today())),
                  "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def get_token():
    env = dict(os.environ)
    env["GCM_INTERACTIVE"] = "false"
    p = subprocess.run(
        [GIT, "-c", "credential.helper=manager", "credential", "fill"],
        input="protocol=https\nhost=github.com\n\n",
        capture_output=True, text=True, env=env, timeout=60,
    )
    for line in p.stdout.splitlines():
        if line.startswith("password="):
            tok = line[len("password="):].strip()
            if tok:
                return tok
    raise RuntimeError("Git Credential Manager 未返回凭据: "
                       + (p.stderr.strip() or "empty output"))


def _api_get(url, token):
    req = urllib.request.Request(url, headers={
        "Authorization": "Bearer " + token,
        "Accept": "application/vnd.github+json",
        "User-Agent": "sync-trigger",
    })
    with urllib.request.urlopen(req, timeout=40) as r:
        return json.loads(r.read().decode("utf-8"))


def already_triggered_today(repo, token):
    """今天(北京时间)是否已有 workflow_dispatch 运行：排队中/进行中/已成功 -> True（本次跳过）。

    已失败或已取消 -> False，允许本次补一次。
    """
    tz = dt.timezone(dt.timedelta(hours=8))
    today = dt.datetime.now(tz).date()
    try:
        data = _api_get("https://api.github.com/repos/{}/actions/runs?per_page=10".format(repo), token)
    except Exception as e:
        log("check today's runs failed ({}), continue to dispatch".format(e))
        return False
    for run in data.get("workflow_runs", []):
        if run.get("event") != "workflow_dispatch":
            continue
        created = dt.datetime.fromisoformat(run["created_at"].replace("Z", "+00:00"))
        if created.astimezone(tz).date() != today:
            continue
        status, concl = run.get("status"), run.get("conclusion")
        if status in ("queued", "in_progress") or concl == "success":
            log("today already has a dispatch run ({} / {}), skip this trigger".format(status, concl))
            return True
    return False


def dispatch(repo, workflow, ref, date, force, token):
    url = "https://api.github.com/repos/{}/actions/workflows/{}/dispatches".format(repo, workflow)
    body = json.dumps({"ref": ref, "inputs": {"date": date, "force": force}}).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST", headers={
        "Authorization": "Bearer " + token,
        "Accept": "application/vnd.github+json",
        "Content-Type": "application/json",
        "User-Agent": "sync-trigger",
    })
    with urllib.request.urlopen(req, timeout=40) as r:
        return r.status


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", default="LUOcheng487/-")
    ap.add_argument("--workflow", default="daily-report.yml")
    ap.add_argument("--ref", default="main")
    ap.add_argument("--date", default="", help="报告日期 YYYY-MM-DD，留空=昨天")
    ap.add_argument("--force", default="", help="填 1 强制发送")
    ap.add_argument("--skip-if-run-today", action="store_true",
                    help="今天已有成功/进行中的触发则跳过（给兜底任务防重复用）")
    args = ap.parse_args()

    try:
        token = get_token()
        if args.skip_if_run_today and already_triggered_today(args.repo, token):
            return 0
        status = dispatch(args.repo, args.workflow, args.ref, args.date, args.force, token)
        log("dispatch ok, HTTP {} (date={}, force={})".format(
            status, args.date or "yesterday", "yes" if args.force else "no"))
        return 0
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "ignore")[:300]
        log("dispatch FAILED: HTTP {} : {}".format(e.code, detail))
        return 1
    except Exception as e:
        log("dispatch FAILED: {}".format(e))
        return 1


if __name__ == "__main__":
    sys.exit(main())
