#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
qz_web —— 秋招投递记录的本地网页界面

用标准库 http.server 起一个只监听本机的服务，前端是单文件 HTML，
数据直接读写 qz.py 那份 SQLite，和命令行共享同一个库。

启动：
    python qz.py serve                 默认 http://127.0.0.1:8765
    python qz.py serve --port 9000
    python qz.py serve --host 0.0.0.0  给手机访问（会提示风险）
    python qz.py serve --no-browser    不自动开浏览器
"""
from __future__ import annotations

import json
import socket
import sqlite3
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import qz
from qz import (
    ALL_STATUS, STATUS_FLOW, STATUS_SIDE, TERMINAL, WEB_DIR,
    days_between, days_until, effective_status, insert_application, log_event,
    now_str, parse_date, split_bulk_line, stall_days, today_str,
)

# 允许通过 PUT 更新的字段白名单
EDITABLE = {
    "company", "department", "position", "batch", "channel", "city", "referrer",
    "jd_url", "portal_url", "status", "applied_at", "next_action",
    "next_action_at", "priority", "salary", "notes",
}


def app_to_dict(r: sqlite3.Row, site_url: str = "") -> dict:
    """把一行投递转成前端要的 JSON，附上算好的天数。"""
    d = {k: r[k] for k in r.keys()}
    st = effective_status(r)
    d["status"] = st               # 前端按四档判定后的状态显示
    d["raw_status"] = r["status"]  # 库内原值（对账用；被天数自动判过时两者不同）
    d["is_terminal"] = st in TERMINAL
    stall = stall_days(r)
    d["stall_days"] = stall if (not d["is_terminal"] and stall is not None) else None
    due = days_until(d["next_action_at"]) if d.get("next_action_at") else None
    d["due_days"] = None if d["is_terminal"] else due
    age = days_between(d.get("applied_at") or "")
    d["age_days"] = age
    # 优先用手填的直达链接，其次用预置的招聘系统入口
    d["site_url"] = d.get("portal_url") or site_url or ""
    return d


class Store:
    """所有数据库操作集中在这里，每次操作开独立连接（HTTP 是多线程的）。"""

    def __init__(self, db_path: Path):
        self.db_path = db_path

    def _conn(self) -> sqlite3.Connection:
        c = sqlite3.connect(self.db_path)
        c.row_factory = sqlite3.Row
        return c

    # ---------------------------------------------------------- 读

    def _site_index(self):
        """招聘系统入口索引：(精确公司名->url, [(站名, url)])"""
        with self._conn() as c:
            rows = c.execute("SELECT company, name, url, login_url FROM sites").fetchall()
        exact, loose = {}, []
        for r in rows:
            u = r["login_url"] or r["url"]
            if r["company"]:
                exact[r["company"]] = u
            if r["name"]:
                loose.append((r["name"], u))
        return exact, loose

    @staticmethod
    def _match_site(company: str, exact: dict, loose: list) -> str:
        if not company:
            return ""
        if company in exact:
            return exact[company]
        for name, url in loose:
            if company in name:
                return url
        return ""

    def list_apps(self, status=None, kw=None, include_archived=True) -> list[dict]:
        sql = "SELECT * FROM applications WHERE 1=1"
        p = []
        if status:
            sql += " AND status = ?"
            p.append(status)
        if kw:
            sql += (" AND (company LIKE ? OR position LIKE ? OR department LIKE ?"
                    " OR notes LIKE ? OR city LIKE ? OR batch LIKE ? OR channel LIKE ?)")
            p += [f"%{kw}%"] * 7
        if not include_archived:
            sql += " AND status != '已归档'"
        sql += " ORDER BY id"
        exact, loose = self._site_index()
        with self._conn() as c:
            return [app_to_dict(r, self._match_site(r["company"], exact, loose))
                    for r in c.execute(sql, p).fetchall()]

    def companies(self) -> list[dict]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT company, COUNT(*) n, MAX(status) st FROM applications"
                " GROUP BY company ORDER BY n DESC, company").fetchall()
            return [{"company": r["company"], "n": r["n"]} for r in rows]

    def find_duplicate(self, company: str, position: str, batch: str):
        uid = qz.make_uid(company, position, batch)
        with self._conn() as c:
            r = c.execute("SELECT id, company, position, batch, status FROM applications"
                          " WHERE uid = ?", (uid,)).fetchone()
            return dict(r) if r else None

    def same_company_other_roles(self, company: str) -> list[dict]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT id, position, batch, status FROM applications"
                " WHERE company = ? ORDER BY id", (company,)).fetchall()
            return [dict(r) for r in rows]

    # ---------------------------------------------------------- 我的资料

    def profile_list(self, kw: str = "", only_todo: bool = False) -> list[dict]:
        """按分组返回资料字段。首次调用会自动建表并灌入种子（老库也能直接用）。"""
        with self._conn() as c:
            try:
                qz.ensure_profile(c)
            except Exception:
                pass
            rows = qz.fetch_profile(c, kw or "", bool(only_todo))
        return [
            {
                "grp": g["grp"],
                "items": [
                    {
                        "id": r["id"],
                        "label": r["label"],
                        "value": r["value"],
                        "note": r["note"],
                        "todo": bool(r["todo"]) or not r["value"],
                    }
                    for r in g["items"]
                ],
            }
            for g in qz.profile_grouped(rows)
        ]

    def profile_set(self, pid: int, value: str):
        with self._conn() as c:
            try:
                qz.ensure_profile(c)
            except Exception:
                pass
            r = c.execute("SELECT id, grp, label FROM profile WHERE id = ?", (pid,)).fetchone()
            if not r:
                return None
            v = (value or "").strip()
            c.execute(
                "UPDATE profile SET value = ?, todo = ?, updated_at = ? WHERE id = ?",
                (v, 0 if v else 1, now_str(), pid),
            )
            return {"id": pid, "grp": r["grp"], "label": r["label"],
                    "value": v, "todo": (not v)}

    def stats(self) -> dict:
        with self._conn() as c:
            rows = c.execute("SELECT * FROM applications").fetchall()
            n = len(rows)
            by_status, by_batch, by_channel = {}, {}, {}
            for r in rows:
                by_status[effective_status(r)] = by_status.get(effective_status(r), 0) + 1
                b = r["batch"] or "未填"
                by_batch[b] = by_batch.get(b, 0) + 1
                ch = r["channel"] or "未填"
                d = by_channel.setdefault(ch, {"n": 0, "iv": 0})
                d["n"] += 1

            # 阶段类统计一律走 qz._ever_reached 整组判定，不要只查单点状态
            for r in rows:
                ch = r["channel"] or "未填"
                if qz._ever_reached(c, r["id"], qz.INTERVIEW_STAGES):
                    by_channel[ch]["iv"] += 1

            upcoming = []
            for r in rows:
                if r["status"] in TERMINAL or not r["next_action_at"]:
                    continue
                dd = days_until(r["next_action_at"])
                if dd is not None and dd <= 14:
                    upcoming.append({
                        "id": r["id"], "company": r["company"], "position": r["position"],
                        "action": r["next_action"], "at": r["next_action_at"], "days": dd,
                    })
            upcoming.sort(key=lambda x: x["days"])

            # 漏斗卡片改为四档分布 —— 与命令行、导出看板的口径保持一致
            eff = [effective_status(r) for r in rows]
            funnel = [{"stage": "投递", "n": n}]
            for s in ALL_STATUS:
                funnel.append({"stage": s, "n": eff.count(s)})

            return {
                "total": n,
                "active": sum(1 for r in rows if r["status"] not in TERMINAL),
                "by_status": by_status, "by_batch": by_batch,
                "by_channel": by_channel, "funnel": funnel, "upcoming": upcoming,
            }

    def get(self, app_id: int) -> dict | None:
        exact, loose = self._site_index()
        with self._conn() as c:
            r = c.execute("SELECT * FROM applications WHERE id = ?", (app_id,)).fetchone()
            if not r:
                return None
            d = app_to_dict(r, self._match_site(r["company"], exact, loose))
            evs = c.execute("SELECT * FROM events WHERE app_id = ? ORDER BY at, id",
                            (app_id,)).fetchall()
            d["events"] = [dict(e) for e in evs]
            return d

    def sites(self, company: str | None = None) -> list[dict]:
        with self._conn() as c:
            if company:
                rows = c.execute("SELECT * FROM sites WHERE company LIKE ? OR name LIKE ?",
                                 (f"%{company}%", f"%{company}%")).fetchall()
            else:
                rows = c.execute("SELECT * FROM sites ORDER BY id").fetchall()
            return [dict(r) for r in rows]

    # ---------------------------------------------------------- 写

    def create(self, row: dict) -> dict:
        """新增或补充。返回 {'id':.., 'created':bool}"""
        with self._conn() as c:
            app_id, created = insert_application(c, row, quiet=True)
            c.commit()
            return {"id": app_id, "created": created}

    def bulk_create(self, lines: list[str]) -> dict:
        keys = ["company", "position", "batch", "channel", "city", "applied_at",
                "status", "next_action", "next_action_at", "notes"]
        created = updated = failed = 0
        problems = []
        with self._conn() as c:
            for i, line in enumerate(lines, 1):
                if not line.strip() or line.strip().startswith("#"):
                    continue
                if line.lstrip().startswith("公司"):
                    continue
                parts = split_bulk_line(line.replace("｜", "|"))
                if len(parts) < 2 or not parts[0] or not parts[1]:
                    problems.append({"line": i, "text": line[:60], "why": "缺公司或岗位"})
                    failed += 1
                    continue
                row = {k: "" for k in keys}   # 先补齐所有键，缺列留空
                for k, v in zip(keys, parts):
                    row[k] = (v or "").strip()
                row["applied_at"] = parse_date(row["applied_at"]) if row["applied_at"] else today_str()
                row["next_action_at"] = parse_date(row["next_action_at"]) if row["next_action_at"] else ""
                if row.get("status") and row["status"] not in ALL_STATUS:
                    problems.append({"line": i, "text": line[:60],
                                     "why": f"状态「{row['status']}」不认识，已置为已投递"})
                    row["status"] = "已投递"
                _, is_new = insert_application(c, row, quiet=True)
                created += is_new
                updated += (not is_new)
            c.commit()
        return {"created": created, "updated": updated, "failed": failed,
                "problems": problems}

    def update(self, app_id: int, patch: dict) -> dict | None:
        fields, values = [], []
        for k, v in patch.items():
            if k not in EDITABLE:
                continue
            if k in ("applied_at", "next_action_at") and v:
                v = parse_date(v)
            if k == "priority":
                v = int(v or 0)
            fields.append(f"{k} = ?")
            values.append(v)
        if not fields:
            return self.get(app_id)
        with self._conn() as c:
            old = c.execute("SELECT * FROM applications WHERE id = ?", (app_id,)).fetchone()
            if not old:
                return None
            fields.append("updated_at = ?")
            values.append(now_str())
            values.append(app_id)
            c.execute(f"UPDATE applications SET {', '.join(fields)} WHERE id = ?", values)
            # 状态变了就补一条流水
            if "status" in patch and patch["status"] != old["status"]:
                c.execute("UPDATE applications SET status_at = ? WHERE id = ?",
                          (today_str(), app_id))
                log_event(c, app_id, "状态推进", old["status"], patch["status"],
                          note="（网页端修改）")
            c.commit()
        return self.get(app_id)

    def advance(self, app_id: int, to_status: str, at: str = "", note: str = "") -> dict | None:
        if to_status not in ALL_STATUS:
            raise ValueError(f"不认识的状态：{to_status}")
        with self._conn() as c:
            old = c.execute("SELECT * FROM applications WHERE id = ?", (app_id,)).fetchone()
            if not old:
                return None
            when = parse_date(at) if at else today_str()
            c.execute("UPDATE applications SET status = ?, status_at = ?, updated_at = ?"
                      " WHERE id = ?", (to_status, when, now_str(), app_id))
            log_event(c, app_id, "状态推进", old["status"], to_status, note=note or "", at=when)
            c.commit()
        return self.get(app_id)

    def add_note(self, app_id: int, text: str) -> dict | None:
        with self._conn() as c:
            r = c.execute("SELECT notes FROM applications WHERE id = ?", (app_id,)).fetchone()
            if not r:
                return None
            log_event(c, app_id, "备注", note=text)
            new = (r["notes"] + "\n" if r["notes"] else "") + f"[{today_str()}] {text}"
            c.execute("UPDATE applications SET notes = ?, updated_at = ? WHERE id = ?",
                      (new, now_str(), app_id))
            c.commit()
        return self.get(app_id)

    def delete(self, app_id: int) -> bool:
        with self._conn() as c:
            r = c.execute("SELECT id FROM applications WHERE id = ?", (app_id,)).fetchone()
            if not r:
                return False
            c.execute("DELETE FROM events WHERE app_id = ?", (app_id,))
            c.execute("DELETE FROM applications WHERE id = ?", (app_id,))
            c.commit()
            return True


# ---------------------------------------------------------------- HTTP

def safe(fn):
    """兜住任何意外异常，返回 500 JSON 而不是把连接掐断。"""
    def wrapper(self, *a, **kw):
        try:
            return fn(self, *a, **kw)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            import traceback
            traceback.print_exc()
            try:
                self._err(f"服务器内部错误：{type(e).__name__}: {e}", 500)
            except Exception:
                pass
    return wrapper


class Handler(BaseHTTPRequestHandler):
    store: Store = None
    server_version = "qz"

    def log_message(self, fmt, *args):
        pass  # 静音，别刷屏

    # ---------- 工具

    def _send(self, code: int, body: bytes, ctype="application/json; charset=utf-8"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, data, code=200):
        self._send(code, json.dumps(data, ensure_ascii=False).encode("utf-8"))

    def _err(self, msg: str, code=400):
        self._json({"error": msg}, code)

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        raw = self.rfile.read(n)
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception:
            return {}

    # ---------- 路由

    @safe
    def do_GET(self):
        u = urlparse(self.path)
        path, q = u.path, parse_qs(u.query)

        if path in ("/", "/index.html"):
            f = WEB_DIR / "index.html"
            if not f.exists():
                return self._send(500, "找不到 web/index.html".encode("utf-8"),
                                  "text/plain; charset=utf-8")
            return self._send(200, f.read_bytes(), "text/html; charset=utf-8")

        if path == "/api/meta":
            return self._json({
                "statuses": ALL_STATUS,
                "flow": STATUS_FLOW,
                "terminal": list(TERMINAL),
                "batches": ["提前批", "正式批", "补录", "秋招"],
                "channels": ["官网", "内推", "Boss", "牛客", "MokaHR", "北森", "实习僧", "邮箱"],
                "today": today_str(),
                "db": str(qz.DB_PATH),
            })

        if path == "/api/apps":
            return self._json(self.store.list_apps(
                status=(q.get("status") or [None])[0],
                kw=(q.get("kw") or [None])[0],
                include_archived=(q.get("archived", ["1"])[0] != "0"),
            ))

        if path == "/api/companies":
            return self._json(self.store.companies())

        if path == "/api/stats":
            return self._json(self.store.stats())

        if path == "/api/sites":
            return self._json(self.store.sites((q.get("company") or [None])[0]))

        if path == "/api/outdir":
            return self._json({"out": str(qz.OUT_DIR)})

        if path == "/api/profile":
            return self._json(self.store.profile_list(
                kw=(q.get("kw") or [""])[0],
                only_todo=(q.get("todo", ["0"])[0] == "1"),
            ))

        if path.startswith("/api/apps/"):
            try:
                app_id = int(path.rsplit("/", 1)[-1])
            except ValueError:
                return self._err("ID 不对")
            d = self.store.get(app_id)
            return self._json(d) if d else self._err("找不到这条记录", 404)

        return self._err("没有这个地址", 404)

    @safe
    def do_POST(self):
        u = urlparse(self.path)
        path = u.path
        body = self._body()

        if path == "/api/apps":
            # 支持两种：单条 dict，或 {"lines": [...]} 批量
            if body.get("lines"):
                return self._json(self.store.bulk_create(body["lines"]))
            company = (body.get("company") or "").strip()
            position = (body.get("position") or "").strip()
            if not company or not position:
                return self._err("公司和岗位必填")
            row = {k: (body.get(k) or "") for k in EDITABLE}
            row["company"], row["position"] = company, position
            row["applied_at"] = parse_date(body.get("applied_at") or today_str())
            if row.get("next_action_at"):
                row["next_action_at"] = parse_date(row["next_action_at"])
            if row.get("status") and row["status"] not in ALL_STATUS:
                row["status"] = "已投递"
            if not row.get("status"):
                row["status"] = "已投递"
            res = self.store.create(row)
            res["app"] = self.store.get(res["id"])
            return self._json(res)

        if path == "/api/check":
            company = (body.get("company") or "").strip()
            dup = self.store.find_duplicate(company, (body.get("position") or "").strip(),
                                            (body.get("batch") or "").strip())
            return self._json({
                "duplicate": dup,
                "same_company": self.store.same_company_other_roles(company) if company else [],
            })

        if path.startswith("/api/apps/") and path.endswith("/advance"):
            app_id = self._id_from(path, "/advance")
            if app_id is None:
                return self._err("ID 不对")
            try:
                d = self.store.advance(app_id, body.get("to") or "", body.get("at") or "",
                                       body.get("note") or "")
            except ValueError as e:
                return self._err(str(e))
            return self._json(d) if d else self._err("找不到这条记录", 404)

        if path.startswith("/api/apps/") and path.endswith("/note"):
            app_id = self._id_from(path, "/note")
            if app_id is None:
                return self._err("ID 不对")
            d = self.store.add_note(app_id, (body.get("text") or "").strip())
            return self._json(d) if d else self._err("找不到这条记录", 404)

        return self._err("没有这个地址", 404)

    @safe
    def do_PUT(self):
        u = urlparse(self.path)
        if u.path.startswith("/api/profile/"):
            try:
                pid = int(u.path.rsplit("/", 1)[-1])
            except ValueError:
                return self._err("ID 不对")
            d = self.store.profile_set(pid, (self._body().get("value") or ""))
            return self._json(d) if d else self._err("找不到这个字段", 404)
        if u.path.startswith("/api/apps/"):
            try:
                app_id = int(u.path.rsplit("/", 1)[-1])
            except ValueError:
                return self._err("ID 不对")
            d = self.store.update(app_id, self._body())
            return self._json(d) if d else self._err("找不到这条记录", 404)
        return self._err("没有这个地址", 404)

    @safe
    def do_DELETE(self):
        u = urlparse(self.path)
        if u.path.startswith("/api/apps/"):
            try:
                app_id = int(u.path.rsplit("/", 1)[-1])
            except ValueError:
                return self._err("ID 不对")
            ok = self.store.delete(app_id)
            return self._json({"ok": ok}) if ok else self._err("找不到这条记录", 404)
        return self._err("没有这个地址", 404)

    def _id_from(self, path: str, suffix: str):
        mid = path[len("/api/apps/"):-len(suffix)]
        try:
            return int(mid)
        except ValueError:
            return None


# ---------------------------------------------------------------- 启动

def free_port(start: int, tries: int = 30) -> int:
    for p in range(start, start + tries):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind(("127.0.0.1", p))
                return p
            except OSError:
                continue
    return start


def run(host="127.0.0.1", port=8765, open_browser=True):
    if not qz.DB_PATH.exists():
        print("数据库还没建，请先在命令行跑：  python qz.py init")
        return 1
    if not (WEB_DIR / "index.html").exists():
        print(f"找不到前端文件：{WEB_DIR / 'index.html'}")
        return 1

    port = free_port(port)
    handler_cls = Handler

    try:
        httpd = ThreadingHTTPServer((host, port), handler_cls)
    except OSError as e:
        print(f"启动失败，端口 {port} 起不来：{e}")
        print("换个端口试试：  python qz.py serve --port 9000")
        return 1

    handler_cls.store = Store(qz.DB_PATH)
    url = f"http://127.0.0.1:{port}/"

    print("=" * 60)
    print("  秋招投递记录 —— 本地网页界面")
    print("=" * 60)
    print(f"  请在浏览器打开： {url}")
    print(f"  数据文件：       {qz.DB_PATH}")
    print()
    headless = sys.stdout is None or "serve.log" in str(getattr(sys.stdout, "name", ""))
    if headless:
        print("  当前是无窗口（后台）启动，日志写在 data\\serve.log。")
        print("  停止服务：双击「停止网页版.vbs」，或跑  qz serve --stop")
    else:
        print("  这个窗口就是服务本身。关掉它，网页就打不开了。")
        print("  停止服务：按 Ctrl+C，或直接关闭这个窗口。")
    if host not in ("127.0.0.1", "localhost", "::1"):
        print()
        print(f"  ! 你用了 --host {host}，同一局域网的其他设备也能打开这个页面，")
        print("    而且这个服务没有密码 —— 对方能看到、也能修改你的投递记录。")
        print("    只在自己家的 WiFi 下这么做，用完立刻关掉。")
    print("=" * 60)
    print()

    if open_browser:
        def _open():
            try:
                if not webbrowser.open(url):
                    print(f"（没能自动打开浏览器，请手动把上面的地址复制到浏览器）")
            except Exception as e:
                print(f"（自动打开浏览器失败：{e}）")
                print(f"  请手动访问：{url}")
        threading.Timer(0.6, _open).start()

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")
    finally:
        httpd.server_close()
    return 0
