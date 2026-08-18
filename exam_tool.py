#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
LeetCode 周考自动化工具（leetcode.cn 版）

功能：
  pick          每周五自动抽取 5 道题（2 easy + 2 medium + 1 hard），与历史题目不重复
  score         考试截止后统计每位员工是否在考试窗口内 AC 每题（通过 / 未通过）
  report        生成 CSV / Markdown 成绩报告
  notify        发送钉钉机器人通知（考题 / 成绩），需 DINGTALK_WEBHOOK 环境变量
  verify        校验员工账号是否存在
  add-employee  添加员工

依赖：requests（pip install requests）
用法：python exam_tool.py <command> [--date YYYY-MM-DD] [--force]
"""

import argparse
import base64
import csv
import hashlib
import hmac
import json
import os
import random
import re
import sqlite3
import sys
import time
import urllib.parse
from datetime import datetime
from zoneinfo import ZoneInfo

import requests

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
DB_PATH = os.path.join(BASE_DIR, "exams.db")
REPORT_DIR = os.path.join(BASE_DIR, "reports")
CACHE_PATH = os.path.join(BASE_DIR, "problems_cache.json")
USED_PROBLEMS_PATH = os.path.join(BASE_DIR, "used_problems.txt")  # 历次已考题目编号（题号，逗号/顿号分隔）
USERS_MD_PATH = os.path.join(BASE_DIR, "users.md")  # 员工名单 + leetcode 账号主页
CACHE_TTL = 7 * 24 * 3600  # 题库缓存 7 天

CN_GRAPHQL = "https://leetcode.cn/graphql/"
CN_NOJ_GO = "https://leetcode.cn/graphql/noj-go/"

HEADERS = {
    "Content-Type": "application/json",
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    ),
}

PROBLEMSET_QUERY = """query problemsetQuestionList($categorySlug: String, $limit: Int, $skip: Int, $filters: QuestionListFilterInput) {
  problemsetQuestionList(categorySlug: $categorySlug, limit: $limit, skip: $skip, filters: $filters) {
    hasMore
    total
    questions {
      difficulty
      frontendQuestionId
      paidOnly
      title
      titleCn
      titleSlug
    }
  }
}"""

RECENT_AC_QUERY = """query recentAcSubmissions($userSlug: String!) {
  recentACSubmissions(userSlug: $userSlug) {
    submissionId
    submitTime
    question {
      titleSlug
    }
  }
}"""

PROFILE_QUERY = """query userProfilePublicProfile($userSlug: String!) {
  userProfilePublicProfile(userSlug: $userSlug) {
    username
    profile {
      userSlug
      realName
    }
  }
}"""

DIFFICULTY_CN = {"EASY": "简单", "MEDIUM": "中等", "HARD": "困难"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS exams (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  exam_date TEXT NOT NULL UNIQUE,
  start_ts INTEGER NOT NULL,
  end_ts INTEGER NOT NULL,
  created_at TEXT DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS exam_problems (
  exam_id INTEGER NOT NULL REFERENCES exams(id),
  title_slug TEXT NOT NULL,
  title TEXT NOT NULL,
  title_cn TEXT,
  frontend_id TEXT NOT NULL,
  difficulty TEXT NOT NULL,
  url TEXT NOT NULL,
  PRIMARY KEY (exam_id, title_slug)
);
CREATE TABLE IF NOT EXISTS results (
  exam_id INTEGER NOT NULL REFERENCES exams(id),
  employee_name TEXT NOT NULL,
  employee_slug TEXT NOT NULL,
  title_slug TEXT NOT NULL,
  ac INTEGER NOT NULL DEFAULT 0,
  submit_ts INTEGER,
  PRIMARY KEY (exam_id, employee_slug, title_slug)
);
"""


# ---------------------------------------------------------------- 基础工具

def load_config():
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return json.load(f)


def parse_used_problems(path=USED_PROBLEMS_PATH):
    """解析 used_problems.txt 中历次已考题目编号（frontend ID），返回 set[int]。

    文件内容为逗号/顿号/换行分隔的题号，如 `3550，3722，3723,...`。
    """
    if not os.path.exists(path):
        return set()
    with open(path, encoding="utf-8") as f:
        return {int(n) for n in re.findall(r"\d+", f.read())}


USER_URL_RE = re.compile(r"https?://leetcode\.cn/u/([^/\s]+)")


def parse_users_md(path=USERS_MD_PATH):
    """解析 users.md 员工名单，返回 [{"name", "slug"}]。

    新格式（每行一条）：`姓名 https://leetcode.cn/u/<slug>/`，姓名与 URL 之间可用空格/逗号/制表符分隔。
    兼容旧格式（每 9 行一组：编号 / 姓名 / 5 个题目标记 / 通过题数 / 账号主页 URL）。
    """
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as f:
        lines = [l.strip() for l in f]

    employees, seen = [], set()

    # 新格式：行内含账号 URL，URL 之前的部分为姓名
    for line in lines:
        m = USER_URL_RE.search(line)
        if not m:
            continue
        slug = m.group(1)
        name = line[: m.start()].strip(" \t,，|")
        if not name:
            name = slug
        if slug not in seen:
            seen.add(slug)
            employees.append({"name": name, "slug": slug})

    # 旧格式回退：URL 独占一行，向前回溯找姓名
    if not employees:
        for i, line in enumerate(lines):
            m = USER_URL_RE.search(line)
            if not m:
                continue
            slug = m.group(1)
            name = None
            for j in range(i - 1, -1, -1):
                prev = lines[j]
                if not prev or prev.isdigit() or (prev.startswith("[") and prev.endswith("]")):
                    continue
                name = prev
                break
            if name is None:
                name = slug
            if slug not in seen:
                seen.add(slug)
                employees.append({"name": name, "slug": slug})
    return employees


def load_employees():
    """员工名单：优先解析 users.md（动态生效），失败/缺失则回退到 config.json。"""
    try:
        emps = parse_users_md()
        if emps:
            return emps
        print("[!] users.md 未解析出员工，回退到 config.json")
    except Exception as e:
        print(f"[!] 解析 users.md 失败（{e}），回退到 config.json")
    return load_config().get("employees", [])


def init_db():
    os.makedirs(REPORT_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.executescript(SCHEMA)
    return conn


def gql(endpoint, query, variables, operation_name, referer, retries=4):
    """POST GraphQL 查询，带重试（应对 Cloudflare 偶发 403/429）。

    可选环境变量（用于 GitHub Actions 等机房 IP 绕过 Cloudflare 风控）：
      LEETCODE_SESSION / LEETCODE_CSRF —— 从登录后的浏览器 Cookie 复制，约两周过期需更新。
    """
    headers = dict(HEADERS)
    headers["Referer"] = referer
    headers["Origin"] = "https://leetcode.cn"
    session_cookie = os.environ.get("LEETCODE_SESSION")
    if session_cookie:
        csrf = os.environ.get("LEETCODE_CSRF", "")
        headers["Cookie"] = f"LEETCODE_SESSION={session_cookie};" + (f" csrftoken={csrf};" if csrf else "")
        if csrf:
            headers["X-CSRFToken"] = csrf
    payload = {"query": query, "variables": variables, "operationName": operation_name}
    last_err = None
    for attempt in range(retries):
        try:
            resp = requests.post(endpoint, headers=headers, json=payload, timeout=20)
            if resp.status_code == 200:
                data = resp.json()
                if "errors" in data:
                    raise RuntimeError(data["errors"][0].get("message", "GraphQL 错误"))
                return data
            last_err = RuntimeError(f"HTTP {resp.status_code}: {resp.text[:200]}")
        except Exception as e:  # 网络错误 / 超时 / Cloudflare 页
            last_err = e
        time.sleep(2 ** attempt)
    raise RuntimeError(f"请求失败 {endpoint}: {last_err}")


def local_now_str(cfg):
    return datetime.now(ZoneInfo(cfg["exam"]["timezone"])).strftime("%Y-%m-%d %H:%M")


def resolve_exam(conn, date=None):
    """按日期（或最近一次）定位考试，返回 (exam_id, exam_date, start_ts, end_ts) 或 None。"""
    if date:
        row = conn.execute(
            "SELECT id, exam_date, start_ts, end_ts FROM exams WHERE exam_date = ?",
            (date,),
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT id, exam_date, start_ts, end_ts FROM exams ORDER BY exam_date DESC LIMIT 1"
        ).fetchone()
    return row


# ---------------------------------------------------------------- 题库

def load_cache():
    if not os.path.exists(CACHE_PATH):
        return {}
    try:
        with open(CACHE_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def save_cache(cache):
    with open(CACHE_PATH, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False)


def fetch_difficulty_pool(difficulty, force=False):
    """拉取某难度全部非会员题目（带 7 天缓存）。返回题目 dict 列表。"""
    cache = load_cache()
    entry = cache.get(difficulty)
    if not force and entry and time.time() - entry.get("fetched_at", 0) < CACHE_TTL:
        return entry["problems"]

    problems, skip, limit = [], 0, 100
    while True:
        data = gql(
            CN_GRAPHQL,
            PROBLEMSET_QUERY,
            {"categorySlug": "", "skip": skip, "limit": limit, "filters": {"difficulty": difficulty}},
            "problemsetQuestionList",
            referer="https://leetcode.cn/problemset/",
        )
        node = data["data"]["problemsetQuestionList"]
        for q in node["questions"]:
            if q.get("paidOnly") or not q.get("titleSlug"):
                continue
            problems.append({
                "slug": q["titleSlug"],
                "title": q.get("title", ""),
                "titleCn": q.get("titleCn") or q.get("title", ""),
                "frontendId": q.get("frontendQuestionId", ""),
                "difficulty": q.get("difficulty", difficulty),
            })
        if not node.get("hasMore"):
            break
        skip += limit
        time.sleep(0.2)

    cache[difficulty] = {"fetched_at": time.time(), "problems": problems}
    save_cache(cache)
    return problems


# ---------------------------------------------------------------- 抽题

def cmd_pick(args):
    cfg = load_config()
    tz = ZoneInfo(cfg["exam"]["timezone"])
    exam_date = args.date or datetime.now(tz).strftime("%Y-%m-%d")

    # 非周五抽题给出提示（不阻断，便于补抽）
    if datetime.strptime(exam_date, "%Y-%m-%d").weekday() != cfg["exam"]["day_of_week"]:
        print(f"[!] 注意：{exam_date} 不是每周考试日（周{cfg['exam']['day_of_week'] + 1}），如属误操作请中止")

    start_ts = int(datetime.fromisoformat(f"{exam_date} {cfg['exam']['start_time']}").replace(tzinfo=tz).timestamp())
    end_ts = int(datetime.fromisoformat(f"{exam_date} {cfg['exam']['end_time']}").replace(tzinfo=tz).timestamp())

    conn = init_db()
    existing = resolve_exam(conn, exam_date)
    if existing and not args.force:
        print(f"[!] {exam_date} 已有考试（id={existing[0]}），如需重新抽题请加 --force")
        conn.close()
        return 1
    if existing:  # --force：删除旧考试及关联数据
        conn.execute("DELETE FROM results WHERE exam_id = ?", (existing[0],))
        conn.execute("DELETE FROM exam_problems WHERE exam_id = ?", (existing[0],))
        conn.execute("DELETE FROM exams WHERE id = ?", (existing[0],))

    # 排除来源①：本工具历次已抽题目（slug）
    used_slugs = {r[0] for r in conn.execute("SELECT DISTINCT title_slug FROM exam_problems")}
    # 排除来源②：used_problems.txt 历次已考题目编号（frontend ID）
    used_ids = parse_used_problems()
    if used_ids:
        print(f"[i] used_problems.txt 已考题目：{len(used_ids)} 个题号，抽题时将排除")

    def is_used(p):
        return (p["slug"] in used_slugs
                or (p["frontendId"].isdigit() and int(p["frontendId"]) in used_ids))

    plan = {}
    for difficulty, count in cfg["difficulties"].items():
        all_pool = fetch_difficulty_pool(difficulty)
        pool = [p for p in all_pool if not is_used(p)]
        if len(pool) < count:
            print(f"[!] {DIFFICULTY_CN[difficulty]} 未使用题目不足（{len(pool)} < {count}），从全部题目中补充（可能出现与历史重复）")
            pool = all_pool
        plan[difficulty] = random.sample(pool, count)

    cur = conn.execute("INSERT INTO exams (exam_date, start_ts, end_ts) VALUES (?, ?, ?)",
                       (exam_date, start_ts, end_ts))
    exam_id = cur.lastrowid
    for difficulty, items in plan.items():
        for p in items:
            conn.execute(
                "INSERT INTO exam_problems (exam_id, title_slug, title, title_cn, frontend_id, difficulty, url) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (exam_id, p["slug"], p["title"], p["titleCn"], p["frontendId"], p["difficulty"], f"https://leetcode.cn/problems/{p['slug']}/"),
            )
    conn.commit()
    conn.close()

    print(f"\n===== {exam_date} 周考题目（共 {sum(len(v) for v in plan.values())} 道）=====")
    print(f"考试窗口：{cfg['exam']['start_time']} ~ {cfg['exam']['end_time']}（{cfg['exam']['timezone']}）")
    for diff in cfg["difficulties"]:
        for p in plan[diff]:
            print(f"  [{DIFFICULTY_CN[p['difficulty']]}] {p['frontendId']}. {p['titleCn']}（{p['title']}）")
            print(f"      https://leetcode.cn/problems/{p['slug']}/")
    print("\n请把上面的链接发给员工，员工须用自己账号在考试窗口内提交并 AC。")


# ---------------------------------------------------------------- 判分

def fetch_window_ac(user_slug, start_ts, end_ts):
    """返回 {title_slug: submit_ts}，仅统计 [start_ts, end_ts] 内的 AC 提交（判分时传入考试当天 00:00~23:59）。"""
    data = gql(CN_NOJ_GO, RECENT_AC_QUERY, {"userSlug": user_slug}, "recentAcSubmissions",
               referer=f"https://leetcode.cn/u/{user_slug}/")
    result = {}
    for s in (data.get("data") or {}).get("recentACSubmissions", []) or []:
        slug = (s.get("question") or {}).get("titleSlug")
        ts = s.get("submitTime")
        if slug and ts and start_ts <= ts <= end_ts:
            result.setdefault(slug, ts)  # 同一题多次 AC 取最早一次
    return result


def cmd_score(args):
    cfg = load_config()
    conn = init_db()
    exam = resolve_exam(conn, args.date)
    if not exam:
        print("[!] 未找到考试，请先运行 pick")
        conn.close()
        return 1
    exam_id, exam_date, start_ts, end_ts = exam

    # 判分规则：考试当天（00:00~23:59）内的 AC 提交均计入，不限定考试窗口
    tz = ZoneInfo(cfg["exam"]["timezone"])
    day_start = int(datetime.fromisoformat(f"{exam_date} 00:00:00").replace(tzinfo=tz).timestamp())
    day_end = int(datetime.fromisoformat(f"{exam_date} 23:59:59").replace(tzinfo=tz).timestamp())

    now_ts = int(time.time())
    if now_ts < day_end and not args.force:
        print(f"[!] 现在（{datetime.fromtimestamp(now_ts, tz).strftime('%Y-%m-%d %H:%M:%S')} {cfg['exam']['timezone']}）早于考试当天结束"
              f"（{datetime.fromtimestamp(day_end, tz).strftime('%Y-%m-%d %H:%M:%S')} {cfg['exam']['timezone']}），成绩不完整，如需强制执行请加 --force")
        conn.close()
        return 1

    problem_slugs = [r[0] for r in conn.execute("SELECT title_slug FROM exam_problems WHERE exam_id = ?", (exam_id,))]
    print(f"\n===== {exam_date} 成绩统计（当天 AC 判定）=====")

    for emp in load_employees():
        name, slug = emp["name"], emp["slug"]
        try:
            ac_map = fetch_window_ac(slug, day_start, day_end)
        except Exception as e:
            print(f"  [!!] {name}({slug}) 拉取失败：{e}（按未通过计）")
            ac_map = {}
        for pslug in problem_slugs:
            ts = ac_map.get(pslug)
            conn.execute(
                "INSERT OR REPLACE INTO results (exam_id, employee_name, employee_slug, title_slug, ac, submit_ts) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (exam_id, name, slug, pslug, 1 if ts is not None else 0, ts),
            )
        n_ac = sum(1 for p in problem_slugs if p in ac_map)
        print(f"  {name:<12} {slug:<24} 通过 {n_ac}/{len(problem_slugs)}")
        time.sleep(0.3)  # 避免过快请求

    conn.commit()
    conn.close()
    print("\n完成。运行 `python exam_tool.py report` 生成报告。")


# ---------------------------------------------------------------- 报告

def cmd_report(args):
    cfg = load_config()
    conn = init_db()
    exam = resolve_exam(conn, args.date)
    if not exam:
        print("[!] 未找到考试，请先运行 pick")
        conn.close()
        return 1
    exam_id, exam_date, start_ts, end_ts = exam

    problems = conn.execute(
        "SELECT title_slug, title, title_cn, frontend_id, difficulty, url FROM exam_problems WHERE exam_id = ? "
        "ORDER BY CASE difficulty WHEN 'EASY' THEN 1 WHEN 'MEDIUM' THEN 2 ELSE 3 END, frontend_id",
        (exam_id,),
    ).fetchall()
    res_rows = conn.execute(
        "SELECT employee_name, employee_slug, title_slug, ac FROM results WHERE exam_id = ?", (exam_id,)
    ).fetchall()
    conn.close()

    if not problems:
        print("[!] 该考试没有题目记录")
        return 1
    if not res_rows:
        print("[!] 暂无成绩，请先运行 score")

    res_map = {(r[1], r[2]): r[3] for r in res_rows}
    # 以员工名单顺序为基准（users.md），未统计到的员工按 0 分补齐
    order = [(e["name"], e["slug"]) for e in load_employees()]

    header = ["姓名", "账号"] + [f"{p[3]}.{p[2]}" for p in problems] + ["通过数"]
    rows = []
    for name, slug in order:
        acs = [res_map.get((slug, p[0]), 0) for p in problems]
        rows.append([name, slug] + ["✓" if a else "—" for a in acs] + [sum(acs)])
    rows.sort(key=lambda r: -r[-1])

    # CSV
    os.makedirs(REPORT_DIR, exist_ok=True)
    csv_path = os.path.join(REPORT_DIR, f"{exam_date}_成绩.csv")
    with open(csv_path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(rows)

    # Markdown（带题目链接）
    md_lines = [f"# {exam_date} 编程周考成绩", "", "| " + " | ".join(header) + " |", "|" + "|".join(["---"] * len(header)) + "|"]
    for r in rows:
        cells = r[:2]
        for i, p in enumerate(problems):
            mark = "✓" if r[2 + i] == "✓" else "—"
            cells.append(f"[{mark}]({p[5]})" if mark == "✓" else mark)
        cells.append(str(r[-1]))
        md_lines.append("| " + " | ".join(cells) + " |")
    md_path = os.path.join(REPORT_DIR, f"{exam_date}_成绩.md")
    with open(md_path, "w", encoding="utf-8") as f:
        f.write("\n".join(md_lines) + "\n")

    print(f"\n===== {exam_date} 成绩报告 =====")
    print("\n".join(md_lines[: 6 + len(rows)]))
    print(f"\n已生成：\n  {csv_path}\n  {md_path}")


# ---------------------------------------------------------------- 钉钉通知

DIFF_EMOJI = {"EASY": "🟢", "MEDIUM": "🟡", "HARD": "🔴"}


def get_notify_env(key, cfg_key):
    """优先取环境变量（CI 中用 secrets 注入），其次取 config.json。"""
    cfg = load_config()
    val = os.environ.get(key)
    if val:
        return val
    return (cfg.get("notify") or {}).get("dingtalk") or {}.get(cfg_key)


def build_problems_markdown(cfg, exam_date, problems):
    lines = [f"### 📝 {exam_date} 编程周考", ""]
    lines.append(f"考试窗口：{cfg['exam']['start_time']} ~ {cfg['exam']['end_time']}（{cfg['exam']['timezone']}）")
    lines.append("")
    for i, p in enumerate(problems, 1):
        slug, title, title_cn, frontend_id, difficulty, url = p
        lines.append(f"{i}. {DIFF_EMOJI.get(difficulty, '')} [{frontend_id}. {title_cn}（{title}）]({url})")
    lines.append("")
    lines.append("请在考试窗口内，用**自己的账号**打开上方链接提交并 AC。")
    return "\n".join(lines)


def build_results_markdown(employees, exam_date, problems, results, dashboard_url=None):
    lines = [f"### 🏆 {exam_date} 周考成绩", ""]
    if not results:
        lines.append("暂无成绩（可能尚未判分）。")
        return "\n".join(lines)
    order = [(e["name"], e["slug"]) for e in employees]
    res_map = {(r[1], r[2]): r[3] for r in results}
    rows = []
    for name, slug in order:
        n = sum(1 for p in problems if res_map.get((slug, p[0]), 0))
        rows.append((n, name, slug))
    rows.sort(key=lambda r: -r[0])
    medals = ["🥇", "🥈", "🥉"]
    for idx, (n, name, slug) in enumerate(rows):
        medal = medals[idx] if idx < len(medals) else "  "
        lines.append(f"{medal} {name}（{slug}）：**{n}/{len(problems)}** 通过")
    lines.append("")
    lines.append(f"共 {len(problems)} 题，仅统计考试窗口内 AC。")
    if dashboard_url:
        lines.append(f"\n📊 [查看完整看板]({dashboard_url})")
    return "\n".join(lines)


def send_dingtalk(webhook, secret, title, text):
    """发送钉钉自定义机器人 markdown 消息，支持加签。"""
    url = webhook
    if secret:
        ts = str(round(time.time() * 1000))
        string_to_sign = f"{ts}\n{secret}"
        hmac_code = hmac.new(secret.encode("utf-8"), string_to_sign.encode("utf-8"),
                             digestmod=hashlib.sha256).digest()
        sign = urllib.parse.quote_plus(base64.b64encode(hmac_code))
        url += f"&timestamp={ts}&sign={sign}"
    resp = requests.post(url, json={"msgtype": "markdown",
                                    "markdown": {"title": title, "text": text}}, timeout=10)
    data = resp.json()
    if data.get("errcode") != 0:
        raise RuntimeError(f"钉钉返回错误：{data}")
    print(f"[+] 钉钉通知已发送：{title}")


def cmd_notify(args):
    cfg = load_config()
    webhook = get_notify_env("DINGTALK_WEBHOOK", "webhook")
    if not webhook:
        print("[!] 未配置 DINGTALK_WEBHOOK（环境变量或 config.json 的 notify.dingtalk.webhook），跳过通知")
        return 0
    secret = get_notify_env("DINGTALK_SECRET", "secret")
    dashboard_url = (cfg.get("notify") or {}).get("dashboard_url")

    conn = init_db()
    exam = resolve_exam(conn, args.date)
    if not exam:
        print("[!] 未找到考试，请先运行 pick")
        conn.close()
        return 1
    exam_id, exam_date, start_ts, end_ts = exam
    problems = conn.execute(
        "SELECT title_slug, title, title_cn, frontend_id, difficulty, url FROM exam_problems WHERE exam_id = ? "
        "ORDER BY CASE difficulty WHEN 'EASY' THEN 1 WHEN 'MEDIUM' THEN 2 ELSE 3 END, frontend_id",
        (exam_id,),
    ).fetchall()
    conn.close()

    if args.type == "problems":
        text = build_problems_markdown(cfg, exam_date, problems)
        title = f"{exam_date} 周考题目"
    else:
        conn = init_db()
        results = conn.execute("SELECT employee_name, employee_slug, title_slug, ac FROM results WHERE exam_id = ?",
                               (exam_id,)).fetchall()
        conn.close()
        text = build_results_markdown(load_employees(), exam_date, problems, results, dashboard_url)
        title = f"{exam_date} 周考成绩"

    if args.dry_run:
        print("---- 模拟发送内容 ----")
        print(text)
        return 0
    send_dingtalk(webhook, secret, title, text)


# ---------------------------------------------------------------- 员工管理

def cmd_verify(args):
    employees = load_employees()
    print(f"校验员工账号（userProfilePublicProfile），共 {len(employees)} 人...")
    for emp in employees:
        try:
            data = gql(CN_GRAPHQL, PROFILE_QUERY, {"userSlug": emp["slug"]}, "userProfilePublicProfile",
                       referer=f"https://leetcode.cn/u/{emp['slug']}/")
            ok = (data.get("data") or {}).get("userProfilePublicProfile") is not None
        except Exception:
            ok = False
        print(f"  {'OK  ' if ok else 'FAIL'} {emp['name']:<12} {emp['slug']}")
        time.sleep(0.2)


def cmd_add_employee(args):
    cfg = load_config()
    if any(e["slug"] == args.slug for e in cfg["employees"]):
        print(f"[!] 账号 {args.slug} 已存在")
        return 1
    cfg["employees"].append({"name": args.name, "slug": args.slug})
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    print(f"[+] 已添加：{args.name}（{args.slug}）")


# ---------------------------------------------------------------- 入口

def main():
    parser = argparse.ArgumentParser(description="LeetCode 周考自动化工具（leetcode.cn）")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("pick", help="抽取本周考试题目（2 easy + 2 medium + 1 hard，不与历史重复）")
    p.add_argument("--date", help="考试日期 YYYY-MM-DD（默认今天）")
    p.add_argument("--force", action="store_true", help="同日期已有考试时强制重新抽题")

    p = sub.add_parser("score", help="统计考试成绩（请在考试截止后运行）")
    p.add_argument("--date", help="考试日期（默认最近一次）")
    p.add_argument("--force", action="store_true", help="截止时间未到也强制执行")

    p = sub.add_parser("report", help="生成 CSV / Markdown 成绩报告")
    p.add_argument("--date", help="考试日期（默认最近一次）")

    p = sub.add_parser("notify", help="发送钉钉通知（题目或成绩），需配置 DINGTALK_WEBHOOK")
    p.add_argument("--type", choices=["problems", "results"], required=True, help="通知内容：考题 or 成绩")
    p.add_argument("--date", help="考试日期（默认最近一次）")
    p.add_argument("--dry-run", action="store_true", help="只打印消息内容，不发送")

    p = sub.add_parser("verify", help="校验 config.json 中员工账号是否存在")
    p = sub.add_parser("add-employee", help="添加员工")
    p.add_argument("--name", required=True, help="员工姓名")
    p.add_argument("--slug", required=True, help="LeetCode 用户名（URL 中的 userSlug）")

    args = parser.parse_args()

    handlers = {
        "pick": cmd_pick,
        "score": cmd_score,
        "report": cmd_report,
        "notify": cmd_notify,
        "verify": cmd_verify,
        "add-employee": cmd_add_employee,
    }
    try:
        rc = handlers[args.command](args)
    except Exception as e:
        print(f"[!!] 错误：{e}")
        rc = 1
    sys.exit(rc or 0)


if __name__ == "__main__":
    main()
