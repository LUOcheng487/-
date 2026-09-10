#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
航司禁售日报：读取各平台禁售Excel -> 汇总"前一天" -> 推送钉钉群

用法:
  本地试跑(不发送):  python daily_report.py --excel-dir "D:\\Excel" --date 2026-09-09 --dry-run
  本地实发:          设置环境变量 DING_WEBHOOK / DING_SECRET 后去掉 --dry-run
  云端(GitHub Actions): python daily_report.py --data-dir data   (日期默认=北京时间昨天)
"""
import argparse
import base64
import datetime as dt
import glob
import hashlib
import hmac
import json
import os
import sys
import time
import urllib.parse
import pandas as pd
import requests
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Asia/Shanghai")


# ---------------- 各平台适配器：统一输出 [航司, 时间, 时长分钟] ----------------
# 航司为 None 的行：美团=平台级禁售(无航司维度)；其他平台=未指定航司(全店/全部航线)

def _norm_airline(v):
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return None
    s = str(v).strip().upper()
    if s in ("", "*", "NAN", "NONE", "全部", "所有"):
        return None
    return s


def adapter_feizhu(df):
    """飞猪: 下线航司 / 屏蔽开始 / 下线时长(分钟)"""
    t = pd.to_datetime(df["屏蔽开始"], errors="coerce")
    return pd.DataFrame({
        "航司": df["下线航司"].map(_norm_airline),
        "时间": t,
        "时长": pd.to_numeric(df["下线时长(分钟)"], errors="coerce"),
    })


def adapter_qunar(df):
    """去哪儿ttr: 航司代码 / 禁售时间 / (解除禁售时间 - 禁售时间)"""
    t = pd.to_datetime(df["禁售时间"], errors="coerce")
    t2 = pd.to_datetime(df["解除禁售时间"], errors="coerce")
    return pd.DataFrame({
        "航司": df["航司代码"].map(_norm_airline),
        "时间": t,
        "时长": (t2 - t).dt.total_seconds() / 60,
    })


def adapter_meituan(df):
    """美团: 只有站点维度，无航司。禁售开始(下线时间) / 禁售时长(分钟)"""
    return pd.DataFrame({
        "航司": None,
        "时间": pd.to_datetime(df["禁售开始(下线时间)"], errors="coerce"),
        "时长": pd.to_numeric(df["禁售时长(分钟)"], errors="coerce"),
    })


def adapter_zx(df):
    """智行/携程: 出票航司 / 禁售开始时间 / (禁售结束时间-开始)，缺失时用实际解禁时间"""
    t = pd.to_datetime(df["禁售开始时间"], errors="coerce")
    t2 = pd.to_datetime(df["禁售结束时间"], errors="coerce")
    t3 = pd.to_datetime(df["实际解禁时间"], errors="coerce")
    dur = (t2 - t).dt.total_seconds() / 60
    dur = dur.fillna((t3 - t).dt.total_seconds() / 60)
    return pd.DataFrame({"航司": df["出票航司"].map(_norm_airline), "时间": t, "时长": dur})


# schema名: (适配函数, 识别列, 是否有航司维度)
ADAPTERS = {
    "feizhu":    (adapter_feizhu,  {"屏蔽开始"}, True),
    "qunar_ttr": (adapter_qunar,   {"航司代码", "禁售时间"}, True),
    "meituan":   (adapter_meituan, {"禁售开始(下线时间)"}, False),
    "zx":        (adapter_zx,      {"禁售开始时间", "出票航司"}, True),
}


def detect_schema(cols):
    for name, (_, need, _) in ADAPTERS.items():
        if need <= set(cols):
            return name
    return None


# ---------------- 读取与汇总 ----------------

def load_platforms(config, data_dir, excel_dir):
    """返回 (记录表DataFrame, 提示信息列表)。excel_dir优先于data_dir。"""
    frames, notes = [], []
    for p in config["platforms"]:
        name, schema = p["name"], p.get("schema", "auto")
        if excel_dir:
            path = os.path.join(excel_dir, os.path.basename(p["local_path"]))
        else:
            path = os.path.join(data_dir, p["file"])
            # 本地直接运行且尚未执行过数据同步时，回退读取 D:\Excel 原始文件
            # （云端Actions上 local_path 不存在，不受影响）
            if not os.path.exists(path) and os.path.exists(p.get("local_path", "")):
                print(f"[本地回退] {name}: data/{p['file']} 不存在，直接读取 {p['local_path']}")
                path = p["local_path"]
        if not os.path.exists(path):
            notes.append(f"{name}: 文件缺失({path})")
            continue
        try:
            df = pd.read_excel(path)
        except Exception as e:
            notes.append(f"{name}: 读取失败({e})")
            continue
        if df.empty or not len(df.columns):
            notes.append(f"{name}: 文件为空(暂无数据)")
            continue
        df = df.drop_duplicates().reset_index(drop=True)
        if schema == "auto":
            schema = detect_schema(df.columns)
        if schema is None or schema not in ADAPTERS:
            notes.append(f"{name}: 无法识别表结构，列={list(df.columns)[:6]}...")
            continue
        func, _, has_airline = ADAPTERS[schema]
        rec = func(df)
        rec["平台"] = name
        rec["有航司维度"] = has_airline
        frames.append(rec)
    if frames:
        return pd.concat(frames, ignore_index=True), notes
    return pd.DataFrame(), notes


def build_message(df_all, target_date, notes, sync_time):
    """生成钉钉markdown文本。df_all已按日期过滤。"""
    d = target_date.strftime("%Y-%m-%d")
    md = f"**✈ 航司禁售日报 · {d}**"
    md += f"\n\n统计口径：禁售开始时间在 {d} 当天（6平台）"

    if df_all.empty:
        md += "\n\n**昨日无禁售记录 ✓**"
    else:
        total_n, total_min = len(df_all), df_all["时长"].sum()
        md += f"\n\n共 **{total_n} 次** / 累计 **{fmt_min(total_min)} min**\n\n---"

        with_air = df_all[df_all["有航司维度"] & df_all["航司"].notna()]
        g = (with_air.groupby(["航司", "平台"])
             .agg(n=("航司", "size"), m=("时长", "sum")).reset_index())

        # 航司按总时长降序、次数降序
        order = (g.groupby("航司").agg(n=("n", "sum"), m=("m", "sum"))
                 .sort_values(["m", "n"], ascending=False))
        for air, row in order.iterrows():
            md += f"\n\n**{air}** ｜ {int(row.n)}次 ｜ {fmt_min(row.m)}min"
            sub = g[g["航司"] == air].sort_values(["n", "m"], ascending=False)
            for _, r in sub.iterrows():
                md += f"\n\n- {r['平台']}：{int(r.n)}次 ｜ {fmt_min(r.m)}min"

        # 平台级（美团，无航司维度）
        plat = df_all[~df_all["有航司维度"]]
        if not plat.empty:
            md += "\n\n---\n\n**平台级禁售（无航司维度）**"
            for p, r in (plat.groupby("平台").agg(n=("平台", "size"), m=("时长", "sum"))
                         .sort_values("n", ascending=False)).iterrows():
                md += f"\n\n- {p}：{int(r.n)}次 ｜ {fmt_min(r.m)}min"

        # 未指定航司（全店/全部航线）
        unspec = df_all[df_all["有航司维度"] & df_all["航司"].isna()]
        if not unspec.empty:
            md += f"\n\n> 未指定航司(全店/全部航线)：{len(unspec)}次 ｜ {fmt_min(unspec['时长'].sum())}min"

    foot = []
    if notes:
        foot.append("⚠ " + "；".join(notes))
    if sync_time:
        foot.append(f"数据同步时间：{sync_time}")
    if foot:
        md += "\n\n---\n\n" + "\n\n".join(foot)
    return md


def fmt_min(m):
    m = round(float(m), 1)
    return f"{int(m)}" if abs(m - int(m)) < 1e-9 else f"{m:.1f}"


# ---------------- 钉钉推送 ----------------

def send_dingtalk(webhook, secret, title, text):
    url = webhook
    if secret:
        ts = str(round(time.time() * 1000))
        sign = base64.b64encode(hmac.new(
            secret.encode("utf-8"), f"{ts}\n{secret}".encode("utf-8"),
            digestmod=hashlib.sha256).digest())
        url = f"{webhook}&timestamp={ts}&sign={urllib.parse.quote_plus(sign)}"
    payload = {"msgtype": "markdown", "markdown": {"title": title, "text": text}}
    for attempt in (1, 2):
        try:
            r = requests.post(url, json=payload, timeout=10)
            data = r.json()
            if data.get("errcode") == 0:
                print("钉钉推送成功")
                return True
            print(f"钉钉返回错误(第{attempt}次): {data}")
        except Exception as e:
            print(f"钉钉请求异常(第{attempt}次): {e}")
        time.sleep(2)
    return False


# ---------------- 主流程 ----------------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=None, help="config.json路径，默认取脚本上级目录")
    ap.add_argument("--data-dir", default="data", help="云端：各平台xlsx所在目录")
    ap.add_argument("--excel-dir", default=None, help="本地：D:\\Excel 原始目录(优先)")
    ap.add_argument("--date", default=None, help="报告日期YYYY-MM-DD，默认=北京时间昨天")
    ap.add_argument("--dry-run", action="store_true", help="只打印消息不发送")
    args = ap.parse_args()

    cfg_path = args.config or os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config.json")
    with open(cfg_path, encoding="utf-8") as f:
        config = json.load(f)

    date_str = args.date or os.environ.get("REPORT_DATE") or ""
    if date_str:
        target_date = dt.date.fromisoformat(date_str)
    else:
        target_date = dt.datetime.now(TZ).date() - dt.timedelta(days=1)

    df_all, notes = load_platforms(config, args.data_dir, args.excel_dir)
    if not df_all.empty:
        t = pd.to_datetime(df_all["时间"], errors="coerce")
        df_all = df_all[t.dt.date == target_date].copy()

    sync_time = None
    for info in sorted(glob.glob(os.path.join(args.data_dir, "_sync_info*.json")),
                       key=os.path.getmtime):
        try:
            with open(info, encoding="utf-8") as f:
                sync_time = json.load(f).get("synced_at")  # 取最新一台机器的
        except Exception:
            pass

    text = build_message(df_all, target_date, notes, sync_time)
    print("=" * 60)
    print(text.replace("\n\n", "\n"))
    print("=" * 60)

    if args.dry_run:
        print("[dry-run] 未发送")
        return 0

    webhook = os.environ.get("DING_WEBHOOK") or config.get("dingtalk", {}).get("webhook", "")
    secret = os.environ.get("DING_SECRET") or config.get("dingtalk", {}).get("secret", "")
    if not webhook:
        print("错误：未配置 DING_WEBHOOK")
        return 2
    ok = send_dingtalk(webhook, secret, f"禁售日报 {target_date}", text)
    return 0 if ok else 2


if __name__ == "__main__":
    sys.exit(main())
