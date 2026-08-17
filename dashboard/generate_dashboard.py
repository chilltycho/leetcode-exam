#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""从 exams.db 生成静态看板 index.html（供 GitHub Pages 部署，无需构建工具/外部依赖）。"""
import html
import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
REPO_DIR = BASE_DIR.parent
DB_PATH = REPO_DIR / "exams.db"
CONFIG_PATH = REPO_DIR / "config.json"
OUT_PATH = BASE_DIR / "index.html"

sys.path.insert(0, str(REPO_DIR))
from exam_tool import parse_users_md  # noqa: E402

DIFF = {"EASY": ("简单", "#16a34a"), "MEDIUM": ("中等", "#d97706"), "HARD": ("困难", "#dc2626")}


def esc(s):
    return html.escape(str(s or ""))


def fmt_ts(ts, tz):
    try:
        return datetime.fromtimestamp(ts, tz).strftime("%m-%d %H:%M")
    except (OSError, ValueError):
        return str(ts)


def main():
    try:
        cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8")) if CONFIG_PATH.exists() else {}
    except (json.JSONDecodeError, OSError):
        cfg = {}
    tz_name = (cfg.get("exam") or {}).get("timezone", "Asia/Shanghai")
    from zoneinfo import ZoneInfo
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        from datetime import timezone
        tz = timezone.utc
    # 员工名单：优先 users.md，回退 config.json
    employees = parse_users_md()
    if not employees:
        try:
            employees = json.loads(CONFIG_PATH.read_text(encoding="utf-8")).get("employees", [])
        except (json.JSONDecodeError, OSError):
            employees = []
    dashboard_url = (cfg.get("notify") or {}).get("dashboard_url", "")

    if DB_PATH.exists():
        conn = sqlite3.connect(DB_PATH)
        exams = []
        for eid, edate, sts, ets in conn.execute(
            "SELECT id, exam_date, start_ts, end_ts FROM exams ORDER BY exam_date DESC"
        ):
            problems = conn.execute(
                "SELECT title_slug, title, title_cn, frontend_id, difficulty, url "
                "FROM exam_problems WHERE exam_id=? ORDER BY "
                "CASE difficulty WHEN 'EASY' THEN 1 WHEN 'MEDIUM' THEN 2 ELSE 3 END, frontend_id",
                (eid,),
            ).fetchall()
            results = conn.execute(
                "SELECT employee_name, employee_slug, title_slug, ac FROM results WHERE exam_id=?",
                (eid,),
            ).fetchall()
            exams.append({"id": eid, "date": edate, "start": sts, "end": ets,
                          "problems": problems, "results": results})
        conn.close()
    else:
        exams = []

    # ---- 员工累计统计 ----
    stats = {e["slug"]: {"name": e["name"], "exams": 0, "total": 0, "attempted": 0} for e in employees}
    for ex in exams:
        n_prob = len(ex["problems"])
        present = {r[1] for r in ex["results"]}
        for slug in present:
            s = stats.setdefault(slug, {"name": slug, "exams": 0, "total": 0, "attempted": 0})
            s["exams"] += 1
            s["attempted"] += n_prob
            s["total"] += sum(1 for r in ex["results"] if r[1] == slug and r[3])

    # ---- HTML 组装 ----
    cards = []
    for slug, s in sorted(stats.items(), key=lambda kv: -kv[1]["total"]):
        avg = (s["total"] / s["attempted"] * 5) if s["attempted"] else 0
        cards.append(f'''
      <div class="card">
        <div class="card-name">{esc(s["name"])}</div>
        <div class="card-slug">{esc(slug)}</div>
        <div class="card-num">{s["total"]}</div>
        <div class="card-label">累计通过 · {s["exams"]} 场参考 · 场均 {avg:.1f}/5</div>
      </div>''')
    cards_html = "\n".join(cards) if cards else '<div class="empty">暂无员工数据，请先在 config.json 配置员工。</div>'

    exam_sections = []
    for ex in exams:
        chips = []
        for slug, title, title_cn, fid, diff, url in ex["problems"]:
            name, color = DIFF.get(diff, (diff, "#64748b"))
            chips.append(
                f'<a class="chip" style="--c:{color}" href="{esc(url)}" target="_blank" rel="noopener">'
                f'<span class="dot" style="background:{color}"></span>{esc(name)} · {esc(fid)}. {esc(title_cn)}</a>'
            )
        probs = len(ex["problems"])

        # 结果表
        res_map = {(r[1], r[2]): r[3] for r in ex["results"]}
        order = [(e["name"], e["slug"]) for e in employees]
        present = {r[1] for r in ex["results"]}
        order += [(slug, slug) for slug in present if slug not in {s for _, s in order}]
        rows = []
        total_ac = 0
        for name, slug in order:
            marks = []
            for p in ex["problems"]:
                ac = res_map.get((slug, p[0]), 0)
                marks.append('<td class="ok">✓</td>' if ac else '<td class="no">—</td>')
                total_ac += ac
            n_ac = sum(1 for p in ex["problems"] if res_map.get((slug, p[0]), 0))
            rows.append(
                f'<tr><td class="emp">{esc(name)}</td><td class="emp-slug">{esc(slug)}</td>'
                + "".join(marks) + f'<td class="sum">{n_ac}/{probs}</td></tr>'
            )
        rows_html = "\n".join(rows) if rows else '<tr><td colspan="8" class="empty">尚未判分</td></tr>'
        total_cells = probs + 3
        rate = total_ac / (probs * max(len(order), 1)) if probs and order else 0

        exam_sections.append(f'''
    <section class="exam">
      <div class="exam-head">
        <h2>📅 {esc(ex["date"])}</h2>
        <span class="window">{esc(fmt_ts(ex["start"], tz))} ~ {esc(fmt_ts(ex["end"], tz))}</span>
        <span class="rate">通过率 <b>{rate * 100:.0f}%</b></span>
      </div>
      <div class="bar"><i style="width:{rate * 100:.1f}%"></i></div>
      <div class="chips">{"".join(chips)}</div>
      <div class="table-wrap">
        <table>
          <thead><tr><th>姓名</th><th>账号</th>{"".join(f"<th>{esc(p[3])}</th>" for p in ex["problems"])}<th>通过</th></tr></thead>
          <tbody>{rows_html}</tbody>
        </table>
      </div>
    </section>''')
    exams_html = "\n".join(exam_sections) if exam_sections else '<div class="empty">还没有考试记录，周五运行 pick 后自动生成。</div>'

    generated = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S")
    html_doc = f'''<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>编程周考看板</title>
<style>
:root {{ --bg:#f6f7fb; --card:#fff; --line:#e5e7eb; --text:#1f2937; --muted:#6b7280; --accent:#4f46e5; }}
* {{ box-sizing:border-box; margin:0; padding:0; }}
body {{ font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif;
       background:var(--bg); color:var(--text); line-height:1.6; }}
header {{ background:linear-gradient(135deg,#312e81,#4f46e5); color:#fff; padding:32px 24px; }}
header h1 {{ font-size:22px; font-weight:700; }}
header p {{ opacity:.85; font-size:13px; margin-top:4px; }}
main {{ max-width:960px; margin:0 auto; padding:24px 16px 64px; }}
.cards {{ display:grid; grid-template-columns:repeat(auto-fill,minmax(180px,1fr)); gap:12px; margin:20px 0 28px; }}
.card {{ background:var(--card); border:1px solid var(--line); border-radius:12px; padding:16px; }}
.card-name {{ font-weight:700; font-size:15px; }}
.card-slug {{ color:var(--muted); font-size:12px; }}
.card-num {{ font-size:30px; font-weight:800; color:var(--accent); margin:4px 0; }}
.card-label {{ color:var(--muted); font-size:12px; }}
.exam {{ background:var(--card); border:1px solid var(--line); border-radius:12px; padding:20px; margin-bottom:20px; }}
.exam-head {{ display:flex; align-items:baseline; gap:12px; flex-wrap:wrap; }}
.exam-head h2 {{ font-size:17px; }}
.window {{ color:var(--muted); font-size:12px; }}
.rate {{ margin-left:auto; font-size:13px; color:var(--muted); }}
.rate b {{ color:var(--accent); }}
.bar {{ background:#eef0f5; border-radius:99px; height:8px; margin:10px 0 14px; overflow:hidden; }}
.bar i {{ display:block; height:100%; background:linear-gradient(90deg,#4f46e5,#8b5cf6); }}
.chips {{ display:flex; flex-wrap:wrap; gap:8px; margin-bottom:14px; }}
.chip {{ display:inline-flex; align-items:center; gap:6px; border:1px solid var(--line); border-radius:99px;
        padding:4px 12px; font-size:12px; color:var(--text); text-decoration:none; background:#fafbfc; }}
.chip:hover {{ border-color:var(--accent); color:var(--accent); }}
.dot {{ width:8px; height:8px; border-radius:50%; }}
.table-wrap {{ overflow-x:auto; }}
table {{ width:100%; border-collapse:collapse; font-size:13px; }}
th, td {{ padding:8px 10px; border-bottom:1px solid var(--line); text-align:center; }}
th {{ color:var(--muted); font-weight:600; font-size:12px; background:#fafbfc; }}
td.emp {{ text-align:left; font-weight:600; }}
td.emp-slug {{ color:var(--muted); font-size:12px; }}
td.ok {{ color:#16a34a; font-weight:700; }}
td.no {{ color:#d1d5db; }}
td.sum {{ font-weight:700; }}
.empty {{ color:var(--muted); text-align:center; padding:24px; }}
footer {{ text-align:center; color:var(--muted); font-size:12px; padding:16px; }}
</style>
</head>
<body>
<header>
  <h1>📊 编程周考看板</h1>
  <p>LeetCode 周考 · 每周五 · 自动抽题 / 判分 / 统计 · 更新于 {esc(generated)}</p>
</header>
<main>
  <div class="cards">{cards_html}</div>
  {exams_html}
</main>
<footer>由 leetcode-exam 自动生成 · GitHub Pages 托管</footer>
</body>
</html>'''
    OUT_PATH.write_text(html_doc, encoding="utf-8")
    print(f"[+] 看板已生成：{OUT_PATH}（{len(exams)} 场考试，{len(stats)} 名员工）")


if __name__ == "__main__":
    main()
