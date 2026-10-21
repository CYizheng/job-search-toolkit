#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
qz - 秋招投递记录本地工具
单文件 CLI，标准库实现（无第三方依赖），数据存 SQLite。

用法速查：
  qz init
  qz add --bulk              # 批量粘贴，一行一条（未收到邮件时最快的录入方式）
  qz add                     # 交互式新增
  qz ls                      # 看板
  qz ls --due 3              # 3 天内到期的 DDL
  qz up 12 --to 已挂          # 四档：流程中 / 泡池子 / 已挂 / 已放弃
  qz note 12 "面试官问XXX"
  qz stall                   # 谁卡住 ≥21 天了（--apply 落库为泡池子）
  qz baseline                # 哪几家该催了（只读）
  qz stats
  qz export --html
  qz sites                   # 列出官网入口
  qz open 12                 # 打开该投递的招聘系统页面
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import html as _html
import json
import os
import re
import sqlite3
import subprocess
import sys
import webbrowser
from datetime import date, datetime, timedelta
from pathlib import Path

# ---------------------------------------------------------------- 基础设施

APP_DIR = Path(__file__).resolve().parent
DB_PATH = APP_DIR / "data" / "qiuzhao.db"
OUT_DIR = APP_DIR / "out"
WEB_DIR = APP_DIR / "web"

IS_WIN = os.name == "nt"

# 状态机：2026-10-16 按实际使用需要精简为四档
#
# 为什么砍掉细流程状态（13 档 → 4 档）：
#   各企业的流程顺序并不统一——有的先测评再筛简历，有的先筛再测，同一家
#   不同岗位也可能不一样。按「测评待做 / 测评完成 / 笔试待做 / 一面 / 二面」
#   这类节点维护状态，既费力又常常和实际对不上；更关键的是，它对「这条该
#   不该催、还值不值得等」没有指导意义。
#   真正有决策价值的信息只有一个：投出去之后，流程停了多久没动。
#
# 因此：
#   · 只保留四档：流程中 / 泡池子 / 已挂 / 已放弃
#   · 具体情况（走到哪一轮、是测评还是笔试）改为写进备注
#   · 完整历史仍在 events 表里，随时可查、可回溯
#   · 「泡池子」既可由人手工标，也可由卡住天数自动判定（见 STALL_DAYS）
STATUS_FLOW = ["流程中"]
STATUS_SIDE = ["泡池子", "已挂", "已放弃"]
ALL_STATUS = STATUS_FLOW + STATUS_SIDE

# 卡住多少自然日算「泡池子」。自最后一次状态变化（status_at）起算，
# 没有 status_at 就退回投递日。21 天 = 三个自然周——越过这个点，
# 「筛完还没轮到通知」已经不太解释得通了。（2026-10-16 定）
STALL_DAYS = 21

# 旧枚举 → 四档。保证历史数据、手工输入、邮件识别结果都能落到有效状态。
# 迁移时这张表也是逐条改写状态的依据。
_OLD_TO_NEW = {
    "已投递": "流程中", "简历筛选中": "流程中",
    "测评待做": "流程中", "测评完成": "流程中", "待结果": "流程中",
    "笔试待做": "流程中", "笔试完成": "流程中",
    "一面": "流程中", "二面": "流程中", "三面": "流程中", "HR面": "流程中",
    "OC": "流程中", "Offer": "流程中",
    "泡池子": "泡池子", "已挂": "已挂", "已放弃": "已放弃",
    "已归档": "已放弃",
}
STATUS_ALIAS = {
    **_OLD_TO_NEW,
    # 口语与近义说法
    "简历筛选": "流程中", "筛选中": "流程中", "评估中": "流程中",
    "笔试": "流程中", "测评": "流程中", "面试": "流程中", "面试中": "流程中",
    "等通知": "流程中", "等结果": "流程中", "初筛": "流程中",
    "无消息": "泡池子", "没消息": "泡池子", "停滞": "泡池子", "卡住": "泡池子",
    "简历挂": "已挂", "被拒": "已挂", "拒绝": "已挂", "挂了": "已挂",
    "放弃": "已放弃", "归档": "已放弃",
}

# 终端里状态用纯文字标记（避免 Windows CMD 编码问题）
STATUS_MARK = {
    "流程中": "· ", "泡池子": "… ", "已挂": "x ", "已放弃": "x ",
}

# 终态：不再需要跟进。只有「已挂」「已放弃」——用户目前不考虑 Offer 档
# （2026-10-16 明确：现在没有 offer，出来时再议），所以别把 Offer 塞进这里。
TERMINAL = {"已挂", "已放弃"}

# 静默期状态：只有「流程中」是等对方回音的阶段；泡池子已单独成档，不必再催。
SILENT_STATUS = {"流程中"}


# ---------------------------------------------------------------- 工作日 / 节假日
# 出处：国务院办公厅《关于 2026 年部分节假日安排的通知》
#      国办发明电〔2025〕7 号，2025-11-04
#      https://www.gov.cn/gongbao/2025/issue_12406/202511/content_7048922.html
HOLIDAYS_2026 = {
    "2026-01-01": "元旦", "2026-01-02": "元旦", "2026-01-03": "元旦",
    "2026-02-15": "春节", "2026-02-16": "春节", "2026-02-17": "春节",
    "2026-02-18": "春节", "2026-02-19": "春节", "2026-02-20": "春节",
    "2026-02-21": "春节", "2026-02-22": "春节", "2026-02-23": "春节",
    "2026-04-04": "清明", "2026-04-05": "清明", "2026-04-06": "清明",
    "2026-05-01": "劳动节", "2026-05-02": "劳动节", "2026-05-03": "劳动节",
    "2026-05-04": "劳动节", "2026-05-05": "劳动节",
    "2026-06-19": "端午", "2026-06-20": "端午", "2026-06-21": "端午",
    "2026-09-25": "中秋", "2026-09-26": "中秋", "2026-09-27": "中秋",
    "2026-10-01": "国庆", "2026-10-02": "国庆", "2026-10-03": "国庆",
    "2026-10-04": "国庆", "2026-10-05": "国庆", "2026-10-06": "国庆",
    "2026-10-07": "国庆",
}
# 周末但调休上班的日子（这两天算工作日）
MAKEUP_WORKDAYS = {
    "2026-01-04", "2026-02-14", "2026-02-28", "2026-05-09",
    "2026-09-20", "2026-10-10",
}
HOLIDAYS = dict(HOLIDAYS_2026)
# ⚠️ 2027 年的放假安排一般要到 2026 年 11 月才公布。公布后把 2027 的
#    日期补进 HOLIDAYS / MAKEUP_WORKDAYS 即可，其余逻辑不用动。


def is_workday(d: date) -> bool:
    """是否工作日：排除法定节假日与周末，但调休上班的周末算工作日。"""
    s = d.isoformat()
    if s in HOLIDAYS:
        return False
    if s in MAKEUP_WORKDAYS:
        return True
    return d.weekday() < 5


def workdays_elapsed(start_str: str, end: date | None = None) -> int | None:
    """从 start_str 次日起算到 end（默认今天）之间过了几个**工作日**。

    投递当天不计入（当天投完就催没意义）。返回 None 表示 start 解析不了。
    """
    try:
        d0 = date.fromisoformat((start_str or "")[:10])
    except ValueError:
        return None
    d1 = end or date.today()
    if d1 <= d0:
        return 0
    n = 0
    cur = d0 + timedelta(days=1)
    while cur <= d1:
        if is_workday(cur):
            n += 1
        cur += timedelta(days=1)
    return n


# ---------------------------------------------------------------- 响应基线
# ⚠️ 诚实说明：公开渠道**没有**任何一家公司公布「平均响应时间」这种统计量。
#    下面这张表是两类数据的组合：
#      ① 有公开统一笔试日期的单位 —— 用**公告里的真实日期**（高置信，可核验）；
#      ② 没有公开日期的单位 —— 用**类别经验窗口**（央国企 1~4 周、互联网 3~7 天
#         这类行业共识），属于估计值，不是实测均值。
#    用途是「什么时候该主动出击」的提醒线，不是对公司的准确预测。
RESPONSE_BASELINE = [
    # kw: 匹配公司/岗位/批次的正则；wd: 经验响应窗口（工作日）
    # node: 公开的统一笔试/节点日期（有则优先用它判）；src: 出处；conf: 置信度
    {"kw": r"中国移动|中移物联网|咪咕|在线营销服务中心",
     "cat": "中国移动系", "wd": 20,
     "node": "2026-10-24", "node_note": "集团全国统一在线笔试（成绩各单位通用）",
     "src": "高校就业网公告（中山大学 2027 届移动宁波分公司）；超级简历校招日历",
     "conf": "高（日期来自公开公告）"},

    {"kw": r"国家电力投资|国家电投|中电国际|国核湛江|电能成套",
     "cat": "国家电投系", "wd": 20,
     "node": None, "node_note": "集团统一线上笔试定在 2026 年 10 月内（两次机会取最高分）",
     "src": "中公教育转载国家电投 2027 届秋招公告",
     "conf": "中（月份明确、具体日期未定）"},

    {"kw": r"南方电网|海南电网",
     "cat": "南方电网系", "wd": 25,
     "node": None, "node_note": "正式批公告 10 月中旬发布，全国统一笔试预计 11 月下旬~12 月上旬",
     "src": "上岸鸭（依据往年规律整理，非官方公告）",
     "conf": "低（往年规律推算）"},

    {"kw": r"中远海运",
     "cat": "中远海运系", "wd": 25,
     "node": "2026-12-15", "node_note": "集团统一笔试安排在 2026 年 12 月中旬",
     "src": "今日头条转载中远海运 2027 秋招公告解读",
     "conf": "中（转载信息，日期为月中旬）"},

    {"kw": r"中国机械工业集团|国机集团",
     "cat": "国机集团", "wd": 20,
     "node": "2026-11-05", "node_note": "集团统一笔试 2026 年 11 月上旬；网申截至 10-31",
     "src": "今日头条转载国机集团 27 秋招；爱企查转载国机人才招聘网",
     "conf": "中（转载信息，日期为月上旬）"},

    {"kw": r"华润",
     "cat": "华润集团", "wd": 20,
     "node": "2026-10-25", "node_note": "笔试安排在 2026 年 10 月下旬~11 月上旬",
     "src": "今日头条转载中国华润 2027 届校招信息",
     "conf": "中（转载信息，日期为旬级）"},

    {"kw": r"中国航空|中国航发|航发|贵州黎阳|燃气涡轮|航发精密|发动机设计研究所",
     "cat": "航空/航发系（单位自主）", "wd": 20,
     "node": None, "node_note": "无集团统一笔试，由各院所/厂自行组织（有单位宣讲次日即笔试）",
     "src": "高校就业网（大连海事大学 航发成发 2027 届宣讲安排）",
     "conf": "中（结构明确，节奏因单位差异大）"},

    {"kw": r"联通",
     "cat": "中国联通系", "wd": 20,
     "node": None, "node_note": "无全国统考，各省自主，笔试集中 10 月下旬~11 月上旬",
     "src": "上岸鸭（三大运营商 2027 秋招时间整理）",
     "conf": "中（省份差异大）"},

    {"kw": r"中航通飞",
     "cat": "中国航空工业系", "wd": 20, "node": None, "node_note": "",
     "src": "—", "conf": "低（无公开日期，用央国企通用窗口）"},

    {"kw": r"腾讯|字节|阿里|百度|美团|京东|网易|小米|滴滴|理想汽车|蚂蚁|去哪儿|"
           r"作业帮|用友|高途|创维|掌趣|巨人网络|盛趣|莉莉丝|米哈游|哔哩哔哩|"
           r"库洛|沐瞳|4399|点点互动|友塔|拓竹|龙湖|蜜雪冰城|Garena|搜狐|"
           r"巽风|灵犀|完美世界|三七互娱",
     "cat": "互联网/科技/游戏", "wd": 8, "node": None, "node_note": "",
     "src": "行业经验：互联网/科技公司简历通过后 3~7 天内安排笔试或面试（爱企查校招答疑整理）",
     "conf": "低（行业经验，非该公司实测）"},

    {"kw": r"广州港|海南矿业|海南领航低空|海南省出版发行|海南顶津|中电国际新能源",
     "cat": "地方国企/央企子公司", "wd": 15,
     "node": None, "node_note": "", "src": "—",
     "conf": "低（无公开日期，用央国企通用窗口）"},
]

DEFAULT_BASELINE = {
    "kw": "", "cat": "未分类（默认）", "wd": 15,
    "node": None, "node_note": "",
    "src": "央国企通用经验值：投递后 1~4 周有反馈，网申结束后 1~2 周发笔试通知",
    "conf": "低（行业经验，非该公司实测）",
}

# 判「超期」时先给这段宽限（工作日），避免把「公告刚截止还没筛」误判成泡池子
BASELINE_GRACE_WORKDAYS = 5


def match_baseline(company: str, position: str = "", batch: str = "") -> dict:
    """按 公司+岗位+批次 文本匹配基线规则，未命中返回默认规则。"""
    text = f"{company or ''} {position or ''} {batch or ''}"
    for rule in RESPONSE_BASELINE:
        if re.search(rule["kw"], text):
            return rule
    return DEFAULT_BASELINE


def baseline_verdict(row, today: date | None = None):
    """返回 (判定, 说明, 静默工作日, 规则)。

    判定取值：
      wait     —— 公开节点日还没到，静默是正常的，不要判泡池子
      ok       —— 在经验窗口内
      overdue  —— 超过窗口，建议标记泡池子
    """
    today = today or date.today()
    rule = match_baseline(row["company"], row["position"], row["batch"])
    base = row["status_at"] or row["applied_at"] or ""
    wd = workdays_elapsed(base, today)
    if wd is None:
        return "ok", "投递日期无法解析", None, rule

    node = rule.get("node")
    if node:
        node_d = date.fromisoformat(node)
        if today < node_d:
            return ("wait",
                    f"公开节点 {node}（{rule['node_note']}）未到，静默属正常",
                    wd, rule)
        after = workdays_elapsed(node, today) or 0
        if after <= rule["wd"] + BASELINE_GRACE_WORKDAYS:
            return ("ok",
                    f"节点 {node} 已过 {after} 个工作日，仍在窗口内", wd, rule)
        return ("overdue",
                f"节点 {node} 已过 {after} 个工作日仍无回音", wd, rule)

    if wd > rule["wd"] + BASELINE_GRACE_WORKDAYS:
        return ("overdue",
                f"静默 {wd} 个工作日 > 窗口 {rule['wd']}（无公开节点）", wd, rule)
    return ("ok", f"静默 {wd} 个工作日，窗口 {rule['wd']}", wd, rule)


# 邮件分类：关键词 → 台账状态。顺序即优先级，先命中先算。
MAIL_RULES = [
    ("已挂", r"未通过|不合适|不予录用|遗憾|感谢信|未进入|未通过筛选|简历未通过|很遗憾"),
    ("Offer", r"录用通知|录用意向|发放 ?offer|offer ?letter|拟录用|签约通知|三方协议"),
    ("一面", r"面试邀请|邀您面试|面试通知|视频面试|初面|一面|复试|终面|面谈"),
    ("笔试待做", r"笔试通知|在线笔试|统一笔试|机考|考试通知|笔试链接|准考证"),
    ("测评待做", r"在线测评|能力测评|性格测评|心理测评|素质测评|测评通知|在线考试链接"),
    ("简历筛选中", r"简历投递成功|网申成功|报名成功|申请成功|投递成功|简历已收到|已收到您的简历"),
]


def die(msg: str, code: int = 1):
    print(f"[错误] {msg}", file=sys.stderr)
    sys.exit(code)


def now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M")


def today_str() -> str:
    return date.today().isoformat()


def parse_date(s: str) -> str:
    """把各种常见写法归一成 YYYY-MM-DD。带时间的只取日期部分。失败返回原串。"""
    s = (s or "").strip()
    if not s:
        return ""
    # 带年份：2026-09-28 / 2026-9-8 / 2026-09-28 14:00 / 2026-09-28T14:00
    m = re.match(r"^(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})(?:[ T]|$)", s)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3))).isoformat()
        except ValueError:
            return s
    # 无年份：09-28，按今年算（自己补年份，避开 strptime 的歧义告警）
    m = re.fullmatch(r"(\d{1,2})[-/.](\d{1,2})", s)
    if m:
        try:
            return date(date.today().year, int(m.group(1)), int(m.group(2))).isoformat()
        except ValueError:
            return s
    # 紧凑写法：20260928
    m = re.fullmatch(r"(\d{4})(\d{2})(\d{2})", s)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3))).isoformat()
        except ValueError:
            return s
    return s


def days_until(target: str) -> int | None:
    """距离目标日期还有几天。正数=将来，0=今天，负数=已逾期。"""
    try:
        d = datetime.strptime(parse_date(target), "%Y-%m-%d").date()
    except Exception:
        return None
    return (d - date.today()).days


def days_between(a: str, b: str | None = None) -> int | None:
    """a 到 b（默认今天）的天数。任一侧无法解析返回 None。"""
    try:
        da = datetime.strptime(parse_date(a), "%Y-%m-%d").date()
    except Exception:
        return None
    if b is None:
        db = date.today()
    else:
        try:
            db = datetime.strptime(parse_date(b), "%Y-%m-%d").date()
        except Exception:
            return None
    return (db - da).days


def stall_days(r) -> int | None:
    """这条投递「卡住」了多少天：最后一次状态变化 → 今天。

    没有 status_at 就退回投递日（老记录、补录时可能没填状态日）。
    这个数是四档体系里最重要的一个指标——它回答的是「还值不值得等」。
    """
    d = (r["status_at"] or r["applied_at"] or "")
    if not d:
        return None
    return days_between(d[:10])


def effective_status(r) -> str:
    """展示与实际判档用的状态：库内「流程中」但卡够 STALL_DAYS 天 → 泡池子。

    两条规则：
      ① 人工标的「泡池子 / 已挂 / 已放弃」优先——人比天数更清楚实情；
      ② 只有「流程中」才可能被天数自动升级，且不会自动降级回去
         （想撤销得手工 qz up --to 流程中）。

    注意：本函数**不改库**。要让天数判定真正落库，用 qz stall --apply。
    """
    st = r["status"]
    if st != "流程中":
        return st
    d = stall_days(r)
    if d is not None and d >= STALL_DAYS:
        return "泡池子"
    return st


def needs_stall_flip(r) -> bool:
    """库内还是「流程中」、但按天数已该是泡池子 —— 供 qz stall 回写时筛选。"""
    return r["status"] == "流程中" and effective_status(r) == "泡池子"


def norm_status(s: str) -> str:
    """把任意状态写法归一为四档。

    吃的输入有三类：旧细状态（历史数据 / 邮件识别结果）、口语说法、
    以及四档本身。认不出来的一律算「流程中」——总比塞个不存在的状态好。
    """
    s = (s or "").strip()
    if s in ALL_STATUS:
        return s
    return STATUS_ALIAS.get(s, "流程中")


def make_uid(company: str, position: str, batch: str, department: str = "") -> str:
    """去重键 = 公司 + 岗位 + 批次（+ 部门，仅当部门非空时）。

    为什么要带上部门：同一家公司常在同一批次投多个岗位，岗位名还可能完全相同
    （如阿里一次投 5 个事业部，其中 3 个都叫「数据科学家」）。若不含部门，
    这 3 条会算出同一个 uid 而互相覆盖，只剩 1 条。

    为什么只在部门非空时纳入：保证历史数据（部门为空）算出的 uid 完全不变，
    无需迁移。空部门 = 旧算法，非空 = 新算法，两种并存互不干扰。
    """
    parts = [company or "", position or "", batch or ""]
    if (department or "").strip():
        parts.append(department)
    raw = "|".join(p.strip() for p in parts)
    return hashlib.md5(raw.encode("utf-8")).hexdigest()[:12]


def disp_width(s: str) -> int:
    """按终端显示宽度算长度：中日韩字符算 2。"""
    w = 0
    for ch in s:
        w += 2 if ord(ch) > 0x1100 and (
            0x4E00 <= ord(ch) <= 0x9FFF or 0x3000 <= ord(ch) <= 0x303F
            or 0x3040 <= ord(ch) <= 0x30FF or 0xFF00 <= ord(ch) <= 0xFF60
            or 0xAC00 <= ord(ch) <= 0xD7A3
        ) else 1
    return w


def pad(s: str, width: int) -> str:
    s = s or ""
    return s + " " * max(0, width - disp_width(s))


def trunc(s: str, width: int) -> str:
    s = (s or "").replace("\n", " ")
    out, w = "", 0
    for ch in s:
        cw = 2 if ord(ch) > 0x1100 and (
            0x4E00 <= ord(ch) <= 0x9FFF or 0x3000 <= ord(ch) <= 0x303F
            or 0x3040 <= ord(ch) <= 0x30FF or 0xFF00 <= ord(ch) <= 0xFF60
        ) else 1
        if w + cw > width:
            return out + "…"
        out += ch
        w += cw
    return out


# ---------------------------------------------------------------- 数据库

PROFILE_SCHEMA = """
CREATE TABLE IF NOT EXISTS profile (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  grp        TEXT NOT NULL,
  grp_order  INTEGER NOT NULL DEFAULT 0,
  label      TEXT NOT NULL,
  value      TEXT DEFAULT '',
  note       TEXT DEFAULT '',
  todo       INTEGER DEFAULT 0,
  ord        INTEGER NOT NULL DEFAULT 0,
  updated_at TEXT DEFAULT '',
  UNIQUE(grp, label)
);
"""

SCHEMA = """
CREATE TABLE IF NOT EXISTS applications (
  id             INTEGER PRIMARY KEY AUTOINCREMENT,
  uid            TEXT UNIQUE,
  company        TEXT NOT NULL,
  department     TEXT DEFAULT '',
  position       TEXT NOT NULL,
  batch          TEXT DEFAULT '',
  channel        TEXT DEFAULT '',
  city           TEXT DEFAULT '',
  referrer       TEXT DEFAULT '',
  jd_url         TEXT DEFAULT '',
  portal_url     TEXT DEFAULT '',
  status         TEXT NOT NULL DEFAULT '已投递',
  status_at      TEXT DEFAULT '',
  applied_at     TEXT DEFAULT '',
  next_action    TEXT DEFAULT '',
  next_action_at TEXT DEFAULT '',
  priority       INTEGER DEFAULT 0,
  salary         TEXT DEFAULT '',
  notes          TEXT DEFAULT '',
  created_at     TEXT DEFAULT '',
  updated_at     TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS events (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  app_id      INTEGER NOT NULL,
  kind        TEXT NOT NULL,
  from_status TEXT DEFAULT '',
  to_status   TEXT DEFAULT '',
  at          TEXT NOT NULL,
  note        TEXT DEFAULT '',
  source      TEXT DEFAULT 'manual'
);

CREATE INDEX IF NOT EXISTS idx_events_app ON events(app_id);

CREATE TABLE IF NOT EXISTS sites (
  id       INTEGER PRIMARY KEY AUTOINCREMENT,
  company  TEXT,
  name     TEXT NOT NULL,
  url      TEXT NOT NULL,
  login_url TEXT DEFAULT '',
  notes    TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS sync_log (
  id       INTEGER PRIMARY KEY AUTOINCREMENT,
  at       TEXT NOT NULL,
  kind     TEXT,
  payload  TEXT,
  applied  INTEGER DEFAULT 0
);
""" + PROFILE_SCHEMA


def connect() -> sqlite3.Connection:
    if not DB_PATH.exists():
        die("数据库不存在，请先运行：  qz init")
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def ensure_dirs():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_DIR.mkdir(parents=True, exist_ok=True)


def log_event(conn, app_id, kind, from_status="", to_status="", note="", at=None, source="manual"):
    conn.execute(
        "INSERT INTO events (app_id, kind, from_status, to_status, at, note, source)"
        " VALUES (?,?,?,?,?,?,?)",
        (app_id, kind, from_status, to_status, at or now_str(), note, source),
    )


# ---------------------------------------------------------------- init

# 预置常用招聘系统入口。login_url 留空则用 url。
DEFAULT_SITES = [
    ("腾讯", "腾讯校招官网", "https://join.qq.com/", "https://join.qq.com/", "腾讯自研系统"),
    ("字节跳动", "字节跳动校园招聘", "https://jobs.bytedance.com/campus", "https://jobs.bytedance.com/campus", "自研系统"),
    ("阿里巴巴", "阿里校园招聘", "https://talent.alibaba.com/campus/home", "https://talent.alibaba.com/campus/home", "自研系统"),
    ("美团", "美团校园招聘", "https://zhaopin.meituan.com/web/campus", "https://zhaopin.meituan.com/web/campus", "自研系统"),
    ("百度", "百度校园招聘", "https://talent.baidu.com/jobs/campus", "https://talent.baidu.com/jobs/campus", "自研系统"),
    ("京东", "京东校园招聘", "https://campus.jd.com/", "https://campus.jd.com/", "自研系统"),
    ("网易", "网易校园招聘", "https://campus.163.com/", "https://campus.163.com/", "自研系统"),
    ("快手", "快手校园招聘", "https://campus.kuaishou.cn/", "https://campus.kuaishou.cn/", "自研系统"),
    ("华为", "华为招聘", "https://career.huawei.com/", "https://career.huawei.com/", "自研系统"),
    ("小米", "小米校园招聘", "https://hr.xiaomi.com/campus", "https://hr.xiaomi.com/campus", "自研系统"),
    ("拼多多", "拼多多校园招聘", "https://careers.pinduoduo.com/campus", "https://careers.pinduoduo.com/campus", "自研系统"),
    ("大疆", "大疆校园招聘", "https://we.dji.com/campus.html", "https://we.dji.com/campus.html", "自研系统"),
    ("招商银行", "招商银行招聘", "https://career.cmbchina.com/", "https://career.cmbchina.com/", "自研系统"),
    ("中国移动", "中国移动招聘", "https://job.10086.cn/", "https://job.10086.cn/", "自研系统"),
    ("国家电网", "国家电网招聘", "https://zhaopin.sgcc.com.cn/", "https://zhaopin.sgcc.com.cn/", "自研系统"),
    ("MokaHR", "Moka 招聘系统（SaaS，多家公司使用）", "https://app.mokahr.com/", "", "各公司 Moka 子站相互独立（app.mokahr.com/campus_apply/<公司>），投递记录在各自站内"),
    ("北森", "北森 iTalent / zhiye（SaaS，多家公司使用）", "https://www.italent.cn/", "", "各公司子站独立（如 mxbc1997.zhiye.com），投递记录在各自站内"),
    ("牛客", "牛客网 我的投递", "https://www.nowcoder.com/", "https://www.nowcoder.com/", "聚合部分企业内推投递"),
    ("飞书招聘", "飞书招聘（SaaS，多家公司使用）", "https://jobs.feishu.cn/", "https://jobs.feishu.cn/", "各公司子站独立（如 kurogame.jobs / nio.jobs.feishu.cn），投递记录在各自站内"),
    # ---- 2027 届已投递（2026-09-28 从微信链接整理）----
    ("科大讯飞", "科大讯飞招聘官网", "https://campus.iflytek.com/", "https://campus.iflytek.com/", "内推码 EVKT98"),
    ("网易游戏", "网易游戏（互娱）校招", "https://game.campus.163.com/", "https://game.campus.163.com/", "内推码 B35yEl"),
    ("OPPO", "OPPO 校园招聘", "https://careers.oppo.com/campus", "https://careers.oppo.com/campus", "内推码 X3285180"),
    ("vivo", "vivo 校园招聘", "https://hr-campus.vivo.com/campus/jobs", "https://hr-campus.vivo.com/campus/jobs", "内推码 EV1AV3"),
    ("沐瞳科技", "沐瞳科技招聘官网", "https://moonton.jobs.feishu.cn/", "https://moonton.jobs.feishu.cn/", "内推码 8KUY57R（大使推荐）"),
    ("搜狐畅游", "搜狐畅游招聘", "https://app.mokahr.com/campus-recruitment/cyou-inc", "https://app.mokahr.com/campus-recruitment/cyou-inc", "内推码 DS1rnJSd"),
    ("库洛游戏", "库洛游戏招聘（飞书系统）", "https://kurogame.jobs.feishu.cn/", "https://kurogame.jobs.feishu.cn/", "内推码 SKAPEFP"),
    ("蔚来", "蔚来校园招聘（飞书系统）", "https://nio.jobs.feishu.cn/", "https://nio.jobs.feishu.cn/", "内推码 BSB3WU7"),
    ("4399游戏", "4399 校园招聘", "http://web.4399.com/campus/", "http://web.4399.com/campus/", "内推码 pv6pt"),
    ("联想", "联想校园招聘", "https://talent.lenovo.com.cn/", "https://talent.lenovo.com.cn/", "实习入口 tempkey 链接会失效"),
    # ---- 其他常见秋招公司 ----
    ("腾讯音乐", "腾讯音乐校园招聘", "https://join.tencentmusic.com/", "https://join.tencentmusic.com/", "TME 独立系统"),
    ("蚂蚁集团", "蚂蚁集团校园招聘", "https://talent.antgroup.com/campus", "https://talent.antgroup.com/campus", ""),
    ("滴滴", "滴滴校园招聘", "https://campus.didiglobal.com/", "https://campus.didiglobal.com/", ""),
    ("小红书", "小红书校园招聘", "https://job.xiaohongshu.com/campus", "https://job.xiaohongshu.com/campus", ""),
    ("哔哩哔哩", "B站校园招聘", "https://jobs.bilibili.com/campus", "https://jobs.bilibili.com/campus", ""),
    ("中兴通讯", "中兴通讯校园招聘", "https://job.zte.com.cn/", "https://job.zte.com.cn/", ""),
    ("海康威视", "海康威视校园招聘", "https://campushr.hikvision.com/", "https://campushr.hikvision.com/", ""),
    ("大华", "大华股份校园招聘", "https://dahua.zhiye.com/", "https://dahua.zhiye.com/", "北森系统"),
    ("宁德时代", "宁德时代校园招聘", "https://catl.zhiye.com/", "https://catl.zhiye.com/", "北森系统"),
    ("比亚迪", "比亚迪校园招聘", "https://job.byd.com/", "https://job.byd.com/", ""),
    ("中国电信", "中国电信招聘", "https://job.chinatelecom.com.cn/", "https://job.chinatelecom.com.cn/", ""),
    ("中国联通", "中国联通招聘", "https://zglt2023.zhaopin.com/", "https://zglt2023.zhaopin.com/", "智联系统"),
    ("工商银行", "中国工商银行招聘", "https://job.icbc.com.cn/", "https://job.icbc.com.cn/", ""),
    ("建设银行", "中国建设银行招聘", "https://job.ccb.com/", "https://job.ccb.com/", ""),
    ("中国银行", "中国银行招聘", "https://campus.chinahr.com/", "https://campus.chinahr.com/", ""),
    ("农业银行", "中国农业银行招聘", "https://career.abchina.com/", "https://career.abchina.com/", ""),
    ("普华永道", "PwC 校园招聘", "https://www.pwccn.com/zh/careers/students.html", "https://www.pwccn.com/zh/careers/students.html", ""),
    ("德勤", "Deloitte 校园招聘", "https://www.deloitte.com/cn/zh/careers/students.html", "https://www.deloitte.com/cn/zh/careers/students.html", ""),
    ("安永", "EY 校园招聘", "https://www.ey.com/zh_cn/careers/students", "https://www.ey.com/zh_cn/careers/students", ""),
    ("毕马威", "KPMG 校园招聘", "https://kpmg.com/cn/zh/home/careers/graduates.html", "https://kpmg.com/cn/zh/home/careers/graduates.html", ""),
    ("迈瑞医疗", "迈瑞医疗校园招聘", "https://mindray.zhiye.com/", "https://mindray.zhiye.com/", "北森系统"),
    ("联影医疗", "联影医疗校园招聘", "https://united-imaging.zhiye.com/", "https://united-imaging.zhiye.com/", "北森系统"),
    ("药明康德", "药明康德校园招聘", "https://wuxiapptec.zhiye.com/", "https://wuxiapptec.zhiye.com/", "北森系统"),
    # ---- 2027 秋招第二批（2026-09-29 补充，URL 均已联网核实）----
    ("拓竹科技", "拓竹科技校园招聘", "https://bambulab.cn/join-us", "https://bambulab.cn/join-us", "深圳/上海；3D打印，R&D与数据岗"),
    ("蜜雪冰城", "蜜雪冰城校园招聘", "https://careers.mxbc.com/campus/jobs", "https://careers.mxbc.com/campus/jobs", "北森 iTalent 系统，测评通过 iTalent 助手发放"),
    ("创维集团", "创维集团校园招聘", "https://skyworth.hotjob.cn/", "https://skyworth.hotjob.cn/", "每账号限投 2 个志愿"),
    ("Garena", "Garena 校园招聘（Moka）", "https://app.mokahr.com/campus-recruitment/garena/148076", "https://app.mokahr.com/m/campus_apply/garena/148076", "Moka 系统；每人限投 2 岗，按志愿顺序推进；策划岗需附作品集；内推码首次投递时填写"),
    ("作业帮", "作业帮校园招聘（Moka）", "https://app.mokahr.com/campus-recruitment/zuoyebang/39595", "https://app.mokahr.com/campus-recruitment/zuoyebang/39595", "Moka 系统；每人最多投 2 岗；技术/产品类投递后 7 个工作日发笔试通知"),
    ("理想汽车", "理想汽车校园招聘", "https://www.lixiang.com/employ/campus/list.html", "https://www.lixiang.com/employ/campus/list.html", "自研系统"),
    ("盛趣游戏", "盛趣游戏校园招聘", "https://www.shengqu.com/campus/", "https://www.shengqu.com/campus/", "自研系统；公众号「盛趣游戏招聘」可查进度"),
    ("去哪儿旅行", "去哪儿旅行校园招聘", "https://campus.qunar.com/", "https://app.mokahr.com/campus_apply/qunar/4207", "Moka 系统；⚠每人只能投 1 个岗位，进流程后不可撤销"),
    ("友塔游戏", "友塔游戏校园招聘", "https://www.yotta-hr.com/zh/campus", "https://www.yotta-hr.com/zh/campus", "⚠进度查询入口不明显，可邮件问 hr@yottastudios.com"),
    ("龙湖集团", "龙湖集团仕官生招聘", "https://www.longfor.com/", "https://app.mokahr.com/campus-recruitment/longfor/74084", "仕官生项目；官网「加入龙湖-我要应聘-校园招聘」；⚠进度查询入口不明显"),
    ("灵犀互娱", "灵犀互娱校园招聘", "https://www.lingxigames.com/", "https://www.lingxigames.com/", "阿里系游戏业务，招聘走阿里校招系统"),
    # ---- 2027 秋招第三批（2026-09-29 补充，URL 均已联网核实）----
    ("米哈游", "米哈游 miHoYo 校园招聘", "https://join.mihoyo.com/", "https://join.mihoyo.com/", "⚠应届生职位仅能投 1 个；应届生截止 10月31日"),
    ("用友集团", "用友招聘官网", "http://career.yonyou.com/", "http://career.yonyou.com/", "测评涉及 offer，务必认真完成"),
    ("点点互动", "点点互动 Century Games 校招（北森）", "https://centurygame.zhiye.com/campus", "https://centurygame.zhiye.com/campus", "进度：登录 ddhd.cn 后移到头像→投递记录；每个项目可投 3 个岗位"),
    ("掌趣科技", "掌趣科技校园招聘", "https://campus.ourpalm.com/", "https://campus.ourpalm.com/", "另有 Moka 入口 campus-recruitment/ourpalm/43628"),
    ("莉莉丝游戏", "莉莉丝游戏校招（飞书）", "https://lilithgames.jobs.feishu.cn/campus", "https://lilithgames.jobs.feishu.cn/campus", "⚠每次最多投 2 岗，内推码首次投递必填"),
    ("巨人网络", "巨人网络校园招聘", "https://hr.ztgame.com/campus/", "https://hr.ztgame.com/campus/", "上海"),
    ("高途", "高途招聘官网", "https://job.gaotu.cn/", "https://job.gaotu.cn/", "可同时投多个岗位，但同一时间只能进行一个岗位的面试流程"),
    ("搜狐畅游", "搜狐畅游校园招聘", "https://campus.changyou.com/", "https://campus.changyou.com/", "内推码 DS1rnJSd"),
]


def cmd_init(args):
    ensure_dirs()
    if DB_PATH.exists() and not args.force:
        print(f"数据库已存在：{DB_PATH}")
        print("如需重建请加 --force（会覆盖，原有数据丢失）")
        return
    conn = sqlite3.connect(DB_PATH)
    conn.executescript(SCHEMA)
    conn.execute("DELETE FROM sites")
    conn.executemany(
        "INSERT INTO sites (company, name, url, login_url, notes) VALUES (?,?,?,?,?)",
        DEFAULT_SITES,
    )
    conn.commit()
    seeded = seed_profile(conn)
    conn.close()
    print(f"已初始化： {DB_PATH}")
    print(f"招聘系统入口已预置 {len(DEFAULT_SITES)} 条，用  qz sites  查看。")
    if seeded:
        print(f"资料字段已预置 {seeded} 条，用  qz info  查看、 qz info --todo  看待补项。")


# ---------------------------------------------------------------- add

BULK_HELP = """
批量录入 —— 一行一条，用 | 或 Tab 分隔。列的顺序：

  公司 | 岗位 | 批次 | 渠道 | 城市 | 投递日期 | 状态 | 下一步 | 下一步时间 | 备注 | 状态时间 | 部门

只有「公司」和「岗位」是必填，其余留空即可（连续 || 表示空列）。
日期支持 2026-08-15 / 08-15 / 20260815。

第 11 列「状态时间」= 当前状态实际发生的那天（默认今天）。
补录历史数据时建议填，否则「卡了多少天」会从今天起算而失真。

第 12 列「部门」= 同一公司投多个岗位时的区分依据（如「淘宝闪购」「高德地图」）。
同一公司同批次投了同名岗位（如阿里三个「数据科学家」）时**必须填**，
否则会因去重键相同而互相覆盖。

示例：
  腾讯 | 后端开发 | 提前批 | 官网 | 深圳 | 08-15
  字节跳动 | 数据科学 | 正式批 | 内推 | 北京 | 08-20 | 笔试待做 | 在线笔试 | 2026-09-30
  美团 | 算法 | 正式批 | 官网 | 北京 ||| 简历筛选中
  小米 | 数据运营 | 2027秋招 | 内推 | 北京 | 09-24 | 测评完成 | 等待面试 | | 已交测评 | 09-26
  阿里巴巴 | 数据科学家 | 2027秋招 | 官网 | 杭州 | 09-28 | 简历筛选中 | | | | | 淘宝闪购
  阿里巴巴 | 数据科学家 | 2027秋招 | 官网 | 北京 | 09-29 | 已投递 | | | | | 高德地图

输入完按 Ctrl+Z 回车（Windows）或 Ctrl+D（Mac/Linux）结束；也可以粘贴后直接回车两次。
""".strip()


def split_bulk_line(line: str):
    """按 | 或 Tab 或连续两空格拆列。"""
    if "\t" in line:
        parts = line.split("\t")
    elif "|" in line:
        parts = line.split("|")
    else:
        # 退化：按两个以上空格拆
        parts = re.split(r"\s{2,}", line)
    return [p.strip() for p in parts]


def read_bulk_lines(args) -> list[str]:
    """从文件或标准输入读取。"""
    if args.file:
        p = Path(args.file)
        if not p.exists():
            die(f"文件不存在：{p}")
        text = p.read_text(encoding="utf-8-sig")
        return [ln for ln in text.splitlines()]

    if getattr(args, "quick", False):
        # quick 模式不刷帮助文本，只留一行极简提示
        print("quick 模式：一行一个公司名即可（可写「公司 | 岗位」覆盖），"
              "结束按 Ctrl+Z 回车：")
    else:
        print(BULK_HELP)
        print("-" * 60)
    lines = []
    for raw in sys.stdin:
        s = raw.rstrip("\n").rstrip("\r")
        if s.strip() == "":
            if lines:
                break
            continue
        lines.append(s)
    return lines


def insert_application(conn, row: dict, quiet=False):
    """插入一条；若 uid 已存在则更新（保留已有事件）。返回 (id, created)。"""
    uid = make_uid(row["company"], row["position"], row.get("batch", ""),
                   row.get("department", ""))
    row["uid"] = uid
    cur = conn.execute("SELECT id, status FROM applications WHERE uid = ?", (uid,))
    hit = cur.fetchone()
    ts = now_str()

    if hit:
        # 已存在：只补充非空字段，不覆盖已有值（避免误清）
        app_id = hit["id"]
        fields, values = [], []
        for k, v in row.items():
            if k in ("uid", "company", "position", "batch", "status", "applied_at", "status_at"):
                continue
            if v not in ("", None, 0):
                fields.append(f"{k} = ?")
                values.append(v)
        if fields:
            fields.append("updated_at = ?")
            values.append(ts)
            values.append(app_id)
            conn.execute(f"UPDATE applications SET {', '.join(fields)} WHERE id = ?", values)
        if not quiet:
            print(f"  已存在，已更新补充：# {app_id}  {row['company']} · {row['position']}")
        return app_id, False

    cols = ["uid", "company", "department", "position", "batch", "channel", "city",
            "referrer", "jd_url", "portal_url", "status", "status_at", "applied_at",
            "next_action", "next_action_at", "priority", "salary", "notes",
            "created_at", "updated_at"]
    # status_at 语义 = 「状态实际发生的时间」，不是「记录写入时间」。
    # 批量补录历史数据时必须用调用方给的时间，否则所有历史行都会被写成今天，
    # 停滞天数（days_between(status_at)）直接归零，看板失去意义。
    status_at = row.get("status_at") or ts
    vals = [
        uid, row["company"], row.get("department", ""), row["position"],
        row.get("batch", ""), row.get("channel", ""), row.get("city", ""),
        row.get("referrer", ""), row.get("jd_url", ""), row.get("portal_url", ""),
        row.get("status") or "流程中", status_at, row.get("applied_at", "") or today_str(),
        row.get("next_action", ""), row.get("next_action_at", ""),
        int(row.get("priority") or 0), row.get("salary", ""), row.get("notes", ""),
        ts, ts,
    ]
    cur = conn.execute(
        f"INSERT INTO applications ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
        vals,
    )
    app_id = cur.lastrowid
    log_event(conn, app_id, "创建", "", row.get("status") or "流程中",
              note=f"投递渠道：{row.get('channel') or '未填'}", at=status_at)
    if not quiet:
        print(f"  + # {app_id}  {row['company']} · {row['position']}"
              f"{' · ' + row['batch'] if row.get('batch') else ''}")
    return app_id, True


# ------------------------------------------------- 上次录入值（减少重复输入）

LAST_FILE = APP_DIR / "data" / "last_input.json"


def load_last() -> dict:
    """读取上次录入时用过的批次/渠道/城市等，作为下次默认值。"""
    try:
        return json.loads(LAST_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_last(row: dict):
    """记住这次填的「可复用」字段。岗位也记，因为同一批投递常投同一个岗位名。"""
    keep = ("batch", "channel", "city", "position")
    data = {k: row.get(k, "") for k in keep if row.get(k)}
    if not data:
        return
    try:
        ensure_dirs()
        LAST_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                             encoding="utf-8")
    except Exception:
        pass


def cmd_add(args):
    """新增：--bulk 读多行，或交互式单条。

    --quick 模式：只写「公司」或「公司 | 岗位」，其余字段用 --batch/--channel/
    --city/--status 等预设值补齐，投递日期默认今天。适合投递后连续快速录入。
    """
    conn = connect()
    created = updated = failed = 0

    if args.bulk or args.file:
        lines = read_bulk_lines(args)
        if not lines:
            die("没有读到任何内容。")
        print()
        quick = getattr(args, "quick", False)
        last = load_last() if quick else {}
        last_used = {}
        # 统计本轮各字段出现次数，最后存「最常用」的那个（而非最后一行）
        _counter = {k: {} for k in ("batch", "channel", "city", "position")}
        for i, line in enumerate(lines, 1):
            if not line.strip() or line.strip().startswith("#"):
                continue
            if re.match(r"^\s*公司\s*[|｜]", line):  # 跳过表头
                continue
            parts = split_bulk_line(line.replace("｜", "|"))
            if not parts or not parts[0]:
                print(f"  ! 第 {i} 行没有公司名，已跳过：{trunc(line, 40)}")
                failed += 1
                continue
            # quick 模式：只给公司时，岗位用 --position 预设，再退回「上次岗位」
            if quick and len(parts) < 2:
                pos = getattr(args, "position", "") or last.get("position", "")
                parts = [parts[0], pos]
            if len(parts) < 2 or not parts[1]:
                hint = ("（quick 模式只写公司名时，需配 --position 给默认岗位；"
                        "或写成「公司 | 岗位」）" if quick else
                        "（只写公司名请加 --quick 并配 --position）")
                print(f"  ! 第 {i} 行缺岗位，已跳过：{trunc(line, 40)}\n    {hint}")
                failed += 1
                continue
            keys = ["company", "position", "batch", "channel", "city", "applied_at",
                    "status", "next_action", "next_action_at", "notes", "status_at",
                    "department"]
            row = {}
            for k, v in zip(keys, parts):
                row[k] = v.strip() if v else ""
            # quick 模式：空字段用命令行预设补齐，命令行没给就用「上次录入值」
            if quick:
                for k, attr in (("batch", "batch"), ("channel", "channel"),
                                ("city", "city"), ("status", "status"),
                                ("applied_at", "at")):
                    if not row.get(k):
                        row[k] = getattr(args, attr, "") or last.get(k, "")
                # 记录本轮用到的值，循环结束后存下来供下次用
                for k in ("batch", "channel", "city", "position"):
                    if row.get(k):
                        last_used[k] = row[k]
                        _counter[k][row[k]] = _counter[k].get(row[k], 0) + 1
            row["applied_at"] = parse_date(row.get("applied_at", "")) if row.get("applied_at") else today_str()
            row["next_action_at"] = parse_date(row.get("next_action_at", "")) if row.get("next_action_at") else ""
            # 第 11 列（可选）：状态实际发生时间。补录历史时必须给，
            # 否则停滞天数会全部从今天起算而失真。
            row["status_at"] = parse_date(row.get("status_at", "")) if row.get("status_at") else ""
            if row.get("status") and row["status"] not in ALL_STATUS:
                fixed = STATUS_ALIAS.get(row["status"])
                if fixed:
                    print(f"  · 第 {i} 行状态「{row['status']}」已归一为「{fixed}」")
                    row["status"] = fixed
                else:
                    print(f"  ! 第 {i} 行状态「{row['status']}」不认识，已置为「流程中」")
                    row["status"] = "流程中"
            _, is_new = insert_application(conn, row)
            created += is_new
            updated += (not is_new)
        conn.commit()
        # 取每个字段出现最多的值（并列时取先出现的），避免被最后一行带偏
        if quick and last_used:
            for k, cnt in _counter.items():
                if cnt:
                    last_used[k] = max(cnt.items(), key=lambda x: x[1])[0]
            save_last(last_used)
        print(f"\n完成：新增 {created} 条，更新 {updated} 条"
              f"{f'，跳过 {failed} 条' if failed else ''}")
        if quick and last_used:
            print(f"（已记住：{'、'.join(f'{k}={v}' for k, v in last_used.items())}"
                  f"，下次可省略）")
        conn.close()
        return

    # 交互式
    def ask(prompt, default=""):
        tip = f" [{default}]" if default else ""
        v = input(f"{prompt}{tip}: ").strip()
        return v or default

    print("=== 新增投递（直接回车用默认值，Ctrl+C 取消）===")
    last = load_last()
    if last:
        print(f"（已记住上次：{'、'.join(f'{k}={v}' for k, v in last.items())}）")
    row = {}
    row["company"] = ask("公司（必填）")
    if not row["company"]:
        die("公司不能为空")
    row["position"] = ask("岗位（必填）", last.get("position", ""))
    if not row["position"]:
        die("岗位不能为空")
    row["batch"] = ask("批次（提前批/正式批/补录）", last.get("batch", ""))
    row["channel"] = ask("渠道（官网/内推/Boss/牛客/邮箱）", last.get("channel", ""))
    row["city"] = ask("城市", last.get("city", ""))
    row["applied_at"] = parse_date(ask("投递日期", today_str()))
    print(f"  状态可选：{' / '.join(ALL_STATUS)}")
    row["status"] = ask("当前状态", "流程中")
    row["next_action"] = ask("下一步动作（如 在线笔试）")
    row["next_action_at"] = parse_date(ask("下一步时间（如 09-30）"))
    row["notes"] = ask("备注")
    app_id, _ = insert_application(conn, row)
    conn.commit()
    conn.close()
    save_last(row)
    print(f"已记录 # {app_id}")


# ---------------------------------------------------------------- ls / 看板

def fetch_apps(conn, status=None, batch=None, keyword=None, include_closed=False, since=None):
    sql = "SELECT * FROM applications WHERE 1=1"
    params = []
    if since:
        sql += " AND applied_at >= ?"
        params.append(since)
    if status:
        sql += " AND status = ?"
        params.append(status)
    if batch:
        sql += " AND batch LIKE ?"
        params.append(f"%{batch}%")
    if keyword:
        sql += " AND (company LIKE ? OR position LIKE ? OR department LIKE ? OR notes LIKE ?)"
        params += [f"%{keyword}%"] * 4
    if not include_closed:
        sql += " AND status NOT IN ('已放弃')"
    sql += " ORDER BY id"
    return conn.execute(sql, params).fetchall()


def render_row(r, show_stall=True) -> str:
    st = effective_status(r)
    mark = STATUS_MARK.get(st, "  ")
    stall = stall_days(r)
    # 卡住天数只在「还在等」时有意义；已挂/已放弃不再显示。
    stall_s = (f"{stall}d" if (show_stall and stall is not None
                               and st not in TERMINAL) else "")
    line = (f"  {mark}#{pad(str(r['id']), 4)} {pad(trunc(r['company'], 18), 20)}"
            f"{pad(trunc(r['position'], 22), 24)}")
    meta = []
    if r["batch"]:
        meta.append(r["batch"])
    if r["city"]:
        meta.append(r["city"])
    if r["channel"]:
        meta.append(r["channel"])
    meta_s = " · ".join(meta)
    line += pad(meta_s, 26)
    if stall_s:
        # meta 超过 26 列时 pad 不会再补空格，这里显式加一个，别让两栏粘住
        if disp_width(meta_s) >= 26:
            line += " "
        line += pad(stall_s, 6)
    if r["next_action"]:
        due = ""
        if r["next_action_at"]:
            dd = days_until(r["next_action_at"])
            if dd is not None:
                due = f"({dd}d)" if dd >= 0 else f"(逾期{abs(dd)}d)"
        line += f"→ {r['next_action']}{due}"
    return line


def cmd_ls(args):
    conn = connect()

    if args.due is not None:
        cmd_due(conn, args.due, args.all)
        conn.close()
        return

    # 时间窗：--days N（最近 N 天）或 --since 日期，两者取其一
    since = None
    if getattr(args, "days", None):
        since = (date.today() - timedelta(days=args.days)).isoformat()
    elif getattr(args, "since", None):
        since = parse_date(args.since)

    rows = fetch_apps(conn, args.status, args.batch, args.kw, args.all, since=since)
    if not rows:
        print("没有匹配记录。")
        conn.close()
        return
    if since:
        print(f"（只看 {since} 及以后投递的）")

    # 按状态分组输出。注意用 effective_status：库内是「流程中」但卡够
    # STALL_DAYS 天的，这里直接显示在 [泡池子] 下，不必先手工改库。
    groups: dict[str, list] = {}
    for r in rows:
        groups.setdefault(effective_status(r), []).append(r)

    order = [s for s in STATUS_FLOW if s in groups] + [s for s in STATUS_SIDE if s in groups]
    order += [s for s in groups if s not in order]

    total = len(rows)
    active = sum(1 for r in rows if r["status"] not in TERMINAL)
    print(f"\n秋招投递看板 —— 共 {total} 条，进行中 {active} 条")

    # 顶部提示条：把「需要你动手的事」前置。
    # 原先到期提醒只在看板末尾出现，条数一多就被刷屏埋掉 —— 这是真实教训。
    dues = []
    for r in rows:
        if r["status"] in TERMINAL or not r["next_action_at"]:
            continue
        d = days_until(r["next_action_at"])
        if d is not None:
            dues.append((d, r))
    dues.sort(key=lambda x: x[0])
    n_incomplete = sum(1 for r in rows if "待补" in (r["position"] or ""))

    tips = []
    for d, r in dues[:2]:
        when = f"{d} 天后" if d > 0 else ("今天" if d == 0 else f"逾期 {-d} 天")
        what = r["next_action"] or "待办"
        tips.append(f"! {r['company']} {what} {when}")
    if len(dues) > 2:
        tips.append(f"另 {len(dues) - 2} 项待办")
    n_incomplete = sum(1 for r in rows if "待补" in (r["position"] or ""))
    n_stall = len(groups.get("泡池子", []))
    if n_stall:
        tips.append(f"{n_stall} 条卡住 ≥{STALL_DAYS} 天（泡池子）")
    if n_incomplete:
        tips.append(f"{n_incomplete} 条岗位/日期待补 → qz doctor")
    if active:
        tips.append(f"{active} 家待跟进 → qz check")
    if tips:
        print("   " + "  |  ".join(tips))
    print()

    # 组内按卡住天数降序：最久没动静的排最前，第一眼就看到该处理的那几条。
    for st in order:
        items = sorted(groups[st], key=lambda r: -(stall_days(r) or 0))
        print(f"[{st}] {len(items)} 条")
        for r in items:
            print(render_row(r))
        print()

    # 四档汇总
    tally = [(st, len(groups.get(st, []))) for st in ALL_STATUS]
    tally = [f"{st} {n}" for st, n in tally if n]
    print("四档：" + "  ".join(tally))

    due_soon = [r for r in rows
                if r["next_action_at"] and r["status"] not in TERMINAL
                and (days_until(r["next_action_at"]) is not None)
                and days_until(r["next_action_at"]) <= 3]
    if due_soon:
        due_soon.sort(key=lambda r: days_until(r["next_action_at"]))
        print(f"\n! 3 天内到期 {len(due_soon)} 项：")
        for r in due_soon:
            d = days_until(r["next_action_at"])
            tag = "今天" if d == 0 else (f"{d} 天后" if d > 0 else f"已逾期 {abs(d)} 天")
            print(f"  #{r['id']} {r['company']} · {r['next_action']} —— {tag}（{r['next_action_at']}）")
    conn.close()


def cmd_due(conn, days: int, show_all=False):
    rows = [r for r in fetch_apps(conn, None, None, None, show_all)
            if r["status"] not in TERMINAL and r["next_action_at"]]
    hits = []
    for r in rows:
        d = days_until(r["next_action_at"])
        if d is None:
            continue
        if d <= days:
            hits.append((d, r))
    if not hits:
        print(f"未来 {days} 天内没有到期的动作。")
        return
    hits.sort(key=lambda x: x[0])
    print(f"\n到期与逾期（阈值 {days} 天）\n")
    for d, r in hits:
        if d < 0:
            tag = f"逾期 {-d} 天"
        elif d == 0:
            tag = "今天"
        elif d == 1:
            tag = "明天"
        else:
            tag = f"{d} 天后"
        print(f"  [{tag:>8}] #{pad(str(r['id']), 4)} {pad(trunc(r['company'], 16), 18)}"
              f"{pad(trunc(r['position'], 18), 20)} {r['next_action']}"
              f"  ({r['next_action_at']})")
    print()


# ---------------------------------------------------------------- up / note

def cmd_up(args):
    conn = connect()
    cur = conn.execute("SELECT * FROM applications WHERE id = ?", (args.id,))
    r = cur.fetchone()
    if not r:
        die(f"找不到 # {args.id}")

    to_status = args.to
    if not to_status:
        print(f"当前：#{r['id']} {r['company']} · {r['position']}  ——  {r['status']}")
        print(f"可选：{' / '.join(ALL_STATUS)}")
        to_status = input("推进到: ").strip()
    if to_status not in ALL_STATUS:
        fixed = STATUS_ALIAS.get(to_status)
        if fixed:
            print(f"（「{to_status}」已归一为「{fixed}」）")
            to_status = fixed
        else:
            die(f"不认识的状态「{to_status}」，可选：{' / '.join(ALL_STATUS)}")

    at = args.at or today_str()
    at = parse_date(at)
    if args.time:
        at = f"{at} {args.time}"

    # 四档之间没有严格先后，不做「回退」拦截。
    # 只有一处值得提醒：把「泡池子」拉回「流程中」= 撤销自动判定。
    if r["status"] == "泡池子" and to_status == "流程中":
        print(f"[提示] #{r['id']} 从泡池子拉回流程中——之前若被天数自动判过，"
              f"起算点会跟着新的状态时间走。")

    conn.execute(
        "UPDATE applications SET status = ?, status_at = ?, updated_at = ? WHERE id = ?",
        (to_status, at, now_str(), args.id),
    )
    log_event(conn, args.id, "状态推进", r["status"], to_status,
              note=args.note or "", at=at)

    if args.next is not None:
        conn.execute("UPDATE applications SET next_action = ?, next_action_at = ? WHERE id = ?",
                     (args.next, parse_date(args.at2 or ""), args.id))
        if args.next:
            print(f"  下一步已设：{args.next}"
                  + (f"（{parse_date(args.at2)}）" if args.at2 else "（无日期）"))
        else:
            print("  下一步已清空")
    else:
        old_next, old_at = r["next_action"] or "", r["next_action_at"] or ""
        if old_next or old_at:
            if args.keep_next:
                print(f"  （保留下一步：{old_next or old_at}）")
            else:
                conn.execute(
                    "UPDATE applications SET next_action = '', next_action_at = '' WHERE id = ?",
                    (args.id,))
                print(f"  （已清除过期的下一步：{old_next or old_at}"
                      f"　想保留请加 --keep-next）")
    conn.commit()
    print(f"#{r['id']} {r['company']}：{r['status']} → {to_status}   ({at})")
    conn.close()


def cmd_next(args):
    """单独设置 / 清除某条投递的下一步动作（推进状态时也会自动清，这里用于补设）。"""
    conn = connect()
    r = conn.execute("SELECT * FROM applications WHERE id = ?", (args.id,)).fetchone()
    if not r:
        die(f"找不到 # {args.id}")

    if args.text is None:
        old = r["next_action"] or r["next_action_at"] or "（空）"
        conn.execute("UPDATE applications SET next_action = '', next_action_at = '',"
                     " updated_at = ? WHERE id = ?", (now_str(), args.id))
        log_event(conn, args.id, "清除待办", note=f"原：{old}")
        conn.commit()
        print(f"#{r['id']} {r['company']}：已清除下一步（原：{old}）")
    else:
        at = parse_date(args.at or "")
        conn.execute("UPDATE applications SET next_action = ?, next_action_at = ?,"
                     " updated_at = ? WHERE id = ?", (args.text, at, now_str(), args.id))
        log_event(conn, args.id, "设置待办", note=args.text, at=at or None)
        conn.commit()
        print(f"#{r['id']} {r['company']}：下一步 = {args.text}"
              + (f"（{at}）" if at else "（无日期，不会进倒计时）"))
    conn.close()


def cmd_note(args):
    conn = connect()
    cur = conn.execute("SELECT company, position FROM applications WHERE id = ?", (args.id,))
    r = cur.fetchone()
    if not r:
        die(f"找不到 # {args.id}")
    text = " ".join(args.text)
    log_event(conn, args.id, "备注", note=text)
    old = conn.execute("SELECT notes FROM applications WHERE id = ?", (args.id,)).fetchone()["notes"]
    new = (old + "\n" if old else "") + f"[{today_str()}] {text}"
    conn.execute("UPDATE applications SET notes = ?, updated_at = ? WHERE id = ?",
                 (new, now_str(), args.id))
    conn.commit()
    print(f"已记到 #{args.id} {r['company']}：{text}")
    conn.close()


# ---------------------------------------------------------------- follow / stats

def cmd_check(args):
    """按招聘系统分组，列出「该去哪里查进度」。

    解决的真实痛点：投递散落在十几个互不相通的招聘系统里（自研 / Moka /
    北森 / 飞书），逐个记「这家该去哪查」非常费劲，也容易漏。本命令按系统
    聚合 —— 同一个系统登录一次就能把多家一起查完，把 N 次登录压成 1 次。

    多公司共用的系统（Moka / 北森 / 飞书）会排在最前面，因为效率最高。
    """
    conn = connect()
    rows = [r for r in fetch_apps(conn, None, None, None, args.all)
            if r["status"] not in TERMINAL]
    if getattr(args, "stale", None):
        rows = [r for r in rows
                if (days_between(r["status_at"] or r["applied_at"]) or 0) >= args.stale]
    if not rows:
        print("没有需要跟进的投递（或都被过滤掉了）。")
        conn.close()
        return

    def stall(r):
        return days_between(r["status_at"] or r["applied_at"]) or 0

    # 聚合成：系统 -> 该公司列表
    groups = {}
    for r in rows:
        sites = _match_sites(conn, r["company"])
        if sites:
            s = sites[0]
            key = (s["name"], s["login_url"] or s["url"], s["notes"] or "")
        else:
            key = ("未收录 —— 需手动补入口", "", "")
        groups.setdefault(key, []).append(r)

    ordered = sorted(groups.items(), key=lambda kv: (-len(kv[1]), kv[0][0]))
    multi = [(k, v) for k, v in ordered if len(v) > 1]
    singles = [(k, v) for k, v in ordered if len(v) == 1]

    def line(r):
        return (f"    #{pad(str(r['id']), 4)}{pad(trunc(r['company'], 14), 16)}"
                f"{pad(trunc(r['position'], 20), 22)}{pad(r['status'], 11)}{stall(r)}d")

    print(f"\n该去查进度了 —— 按招聘系统分组")
    print(f"共 {len(rows)} 家，分布在 {len(groups)} 个系统\n")

    if multi:
        print("=" * 64)
        print("★ 同一系统多家 —— 一次登录可一起查，先做这些")
        print("=" * 64)
        for (name, url, notes), apps in multi:
            print(f"\n【{name}】{len(apps)} 家")
            if url:
                print(f"  {url}")
            if notes:
                print(f"  · {notes}")
            for r in sorted(apps, key=lambda x: -stall(x)):
                print(line(r))

    if singles:
        print(f"\n{'=' * 64}")
        print(f"单独登录查的 {len(singles)} 家")
        print("=" * 64)
        for (name, url, _n), apps in singles:
            r = apps[0]
            print(f"\n  #{pad(str(r['id']), 4)}{pad(trunc(r['company'], 14), 16)}"
                  f"{pad(trunc(r['position'], 20), 22)}{pad(r['status'], 11)}{stall(r)}d")
            if url:
                print(f"        {url}")

    print(f"\n查完用： qz up <id> --to <新状态>      补入口： qz open <id> --set-url <网址>\n")
    conn.close()


# ---------------------------------------------------------------- 统计辅助

# 「有回音」= 状态离开过「已投递」这个初始点。
# 四档体系下状态本身只有一个前进档，区分不出回音，所以整条逻辑改为看**历史事件**：
# events 里出现过投递之外的任何阶段，就算有过回音（含被拒——拒信也是回应）。
# 这张表同时覆盖旧细状态（历史数据）与新的四档（迁移后的新事件），两头都不漏。
#
# 注意：「流程中」绝不能进这张表。它是四档体系的**基线**，每条记录都有，
# 一旦混进来，所有记录都会被判成「有回音」（曾因此把 113 条误报成 213 条）。
# 「泡池子」同理——它的定义就是「21 天没动静」，本身就是「没有回音」。
# 「已放弃」是本人撤回，不是企业回应，也不算。
RESPONSE_STATUSES = [
    "简历筛选中", "测评待做", "测评完成", "待结果",
    "笔试待做", "笔试完成", "一面", "二面", "三面", "HR面", "OC", "Offer",
    "已挂",
]

# 「拿到测评/笔试」与「到 OC/Offer」都可能是组里任一阶段，必须整组判，不能只查单点。
EXAM_STAGES = ["测评待做", "测评完成", "笔试待做", "笔试完成"]
OFFER_STAGES = ["OC", "Offer"]

# 「进过面试」的标志阶段。同样只认 events 历史——某条后来挂了，
# 也仍然算「到过面试」，这正是漏斗该有的口径。
INTERVIEW_STAGES = ["一面", "二面", "三面", "HR面", "OC", "Offer"]


def _ever_reached(conn, app_id, stages) -> bool:
    """这条记录在历史事件里是否到过其中任一阶段。"""
    marks = ",".join("?" * len(stages))
    return bool(conn.execute(
        f"SELECT 1 FROM events WHERE app_id = ? AND to_status IN ({marks}) LIMIT 1",
        (app_id, *stages)).fetchone())


def _response_at(conn, r):
    """返回 (首次回音日期, 是否可以拿来算响应时长)。

    坑在这里：投递入库时本身就会写一条事件，那条的日期=投递日。
    所以事件流里有回音 ≠ 算得出时长——补录时投递日与状态日常填成同一天，
    差值 0 天是**假值**。因此只有「日期严格晚于投递日」的回音才算可算时长。
    """
    marks = ",".join("?" * len(RESPONSE_STATUSES))
    sql = ("SELECT at FROM events WHERE app_id = ? AND to_status IN (%s)"
           " ORDER BY at") % marks
    evs = [e["at"] for e in conn.execute(sql, (r["id"], *RESPONSE_STATUSES))]
    applied = r["applied_at"] or ""
    if evs:
        first = evs[0]
        d = days_between(applied, first[:10])
        return first, (d is not None and d > 0)
    # 四档体系里状态位置不再是「有没有进展」的判据（只有一个前进档），
    # 改为：只要状态变更日晚于投递日，就说明中间确实有过动静。
    st = r["status_at"] or applied
    d = days_between(applied, st[:10])
    if d is not None and d > 0:
        return st, True
    return "", False


def cmd_stats(args):
    conn = connect()
    rows = conn.execute("SELECT * FROM applications").fetchall()
    n = len(rows)
    if n == 0:
        print("还没有记录。")
        conn.close()
        return

    by_status = {}
    for r in rows:
        by_status[r["status"]] = by_status.get(r["status"], 0) + 1

    def n_ever(stages):
        """有多少条**曾到达过**这组里任一阶段。整组判，不能只查单点。"""
        return sum(1 for r in rows if _ever_reached(conn, r["id"], stages))

    n_applied = n
    resp_rows = []
    for r in rows:
        a, src = _response_at(conn, r)
        resp_rows.append((r, a, src))
    n_resp = sum(1 for _, a, _ in resp_rows if a)
    n_exam = n_ever(EXAM_STAGES)
    n_interview = n_ever(INTERVIEW_STAGES)
    n_offer = n_ever(OFFER_STAGES)

    print(f"\n秋招漏斗（共 {n} 条投递）\n")
    print(f"  {pad('投递', 14)}{pad(str(n_applied), 7)}100.0%")
    for label, v in [("有回音", n_resp), ("拿到测评/笔试", n_exam),
                     ("进入面试", n_interview), ("到 OC/Offer", n_offer)]:
        pct = v / n * 100 if n else 0
        bar = "█" * int(round(pct / 3))
        print(f"  {pad(label, 14)}{pad(str(v), 7)}{pct:5.1f}%  {bar}")

    # 响应时长只认「日期严格晚于投递日」的回音，
    # 补录数据（投递日=状态日）会算出 0 天，那种假值不参与统计。
    deltas = []
    for r, a, ok in resp_rows:
        if not a or not ok:
            continue
        d = days_between(r["applied_at"] or "", a[:10])
        if d is not None and d > 0:
            deltas.append(d)
    n_no_time = n_resp - len(deltas)
    print("\n响应速度（从投递日算起）")
    if len(deltas) >= 3:
        deltas.sort()
        mid = len(deltas) // 2
        med = deltas[mid] if len(deltas) % 2 else (deltas[mid - 1] + deltas[mid]) / 2
        print(f"  能算出时长的 {len(deltas)} 条：平均 {sum(deltas) / len(deltas):.1f} 天，"
              f"中位 {med:.1f} 天（最快 {deltas[0]} 天，最慢 {deltas[-1]} 天）")
    else:
        print(f"  样本不足（能算出时长的只有 {len(deltas)} 条），先不给天数结论。")
    if n_no_time:
        print(f"  另有 {n_no_time} 条有回音但算不出时长——补录时"
              f"投递日与回音日填成同一天，差值 0 天是假值，**已排除**。")
    print("  （往后推进状态都走 qz up，事件流会自然积累，这个数才会准）")

    silent = []
    for r, a, _ in resp_rows:
        if a:
            continue
        d = days_between(r["applied_at"] or "")
        if d is not None:
            silent.append(d)
    if silent:
        silent.sort(reverse=True)
        print(f"  仍在静默：{len(silent)} 条，最久 {silent[0]} 天，"
              f"中位 {silent[len(silent) // 2]} 天")

    print("\n状态分布")
    for st in ALL_STATUS:
        if by_status.get(st):
            print(f"  {pad(st, 10)} {by_status[st]}")

    # 各渠道转化（含回音率）
    ch = {}
    for r, a, _ in resp_rows:
        c = r["channel"] or "未填"
        d = ch.setdefault(c, {"n": 0, "resp": 0, "iv": 0})
        d["n"] += 1
        if a:
            d["resp"] += 1
        if _ever_reached(conn, r["id"], INTERVIEW_STAGES):
            d["iv"] += 1
    if len(ch) > 1:
        print("\n渠道对比（样本很小，只看趋势别下结论）")
        for c, d in sorted(ch.items(), key=lambda x: -x[1]["n"]):
            rr = d["resp"] / d["n"] * 100 if d["n"] else 0
            iv = d["iv"] / d["n"] * 100 if d["n"] else 0
            print(f"  {pad(trunc(c, 10), 12)} {pad(str(d['n']), 5)} 条"
                  f"   回音率 {rr:5.1f}%   面试率 {iv:5.1f}%")

    print()
    conn.close()


def cmd_stall(args):
    """把「卡住 ≥ STALL_DAYS 天」的流程中记录真正改成泡池子（回写库）。

    为什么还要有这个命令：ls 与网页看板会在**显示时**就把这类记录归到
    泡池子（effective_status 动态判定），所以平时你不需要动库。但动态判定
    有个副作用——记录本身仍是「流程中」，按状态筛选、导出 CSV、或者跟别人
    对账时口径会对不上。这个命令负责把判定落库，让两边一致。

    注意：**不修改 status_at**。标泡池子不代表对方有了新动静，最后动静的
    时间必须保持原值，否则卡住天数会被自己抹掉。
    """
    conn = connect()
    rows = fetch_apps(conn, None, None, None, True)
    hits = [r for r in rows if needs_stall_flip(r)]
    if not hits:
        print(f"没有需要改的：没有「流程中」且卡住 ≥{STALL_DAYS} 天的记录。")
        conn.close()
        return
    hits.sort(key=lambda r: -(stall_days(r) or 0))
    print(f"\n以下 {len(hits)} 条卡住 ≥{STALL_DAYS} 天，标为「泡池子」：\n")
    for r in hits:
        print(f"  #{pad(str(r['id']), 4)}{pad(trunc(r['company'], 20), 22)}"
              f"{pad(trunc(r['position'], 20), 22)}"
              f"卡住 {stall_days(r)} 天（自 {r['status_at'] or r['applied_at']}）")
    if not args.apply:
        print("\n【试运行】未写入台账。确认后执行：  qz stall --apply")
        conn.close()
        return
    ts = now_str()
    for r in hits:
        d = stall_days(r)
        conn.execute("UPDATE applications SET status='泡池子', updated_at=? WHERE id=?",
                     (ts, r["id"]))
        log_event(conn, r["id"], "状态变更", r["status"], "泡池子",
                  f"卡住 {d} 天（≥{STALL_DAYS}），自动判为泡池子", source="stall")
    conn.commit()
    print(f"\n已把 {len(hits)} 条标为泡池子（status_at 保持原值，卡住天数不变）。")
    conn.close()


# ---------------------------------------------------------------- 时间线

def cmd_show(args):
    conn = connect()
    r = conn.execute("SELECT * FROM applications WHERE id = ?", (args.id,)).fetchone()
    if not r:
        die(f"找不到 # {args.id}")
    print(f"\n#{r['id']}  {r['company']}"
          f"{' · ' + r['department'] if r['department'] else ''}  ·  {r['position']}")
    print("-" * 60)
    for k, label in [("batch", "批次"), ("channel", "渠道"), ("city", "城市"),
                     ("referrer", "内推人"), ("applied_at", "投递日期"),
                     ("status", "当前状态"), ("status_at", "状态更新于"),
                     ("next_action", "下一步"), ("next_action_at", "下一步时间"),
                     ("portal_url", "系统入口"), ("jd_url", "JD 链接")]:
        if r[k]:
            print(f"  {pad(label, 10)} {r[k]}")
    if r["notes"]:
        print(f"\n备注：\n{r['notes']}")
    evs = conn.execute("SELECT * FROM events WHERE app_id = ? ORDER BY at, id", (r["id"],)).fetchall()
    if evs:
        print("\n流程时间线")
        for e in evs:
            head = f"  {e['at']}"
            if e["kind"] == "状态推进":
                head += f"  {e['from_status']} → {e['to_status']}"
            else:
                head += f"  [{e['kind']}]"
            if e["note"]:
                head += f"  {e['note']}"
            print(head)
    print()
    conn.close()


# ---------------------------------------------------------------- 官网入口

def sync_sites(conn) -> tuple[int, int]:
    """把 DEFAULT_SITES 与 sites 表对齐：新增缺失的，并更新内容有变化的。

    返回 (新增数, 更新数)。**更新这一半很重要**——预置表里改错的网址
    （比如某公司换了 Moka 项目号）如果只做增量插入，是永远修不掉的。
    """
    existing = {
        (r["company"], r["name"]): r
        for r in conn.execute("SELECT id, company, name, url, login_url, notes FROM sites")
    }
    added = updated = 0
    for row in DEFAULT_SITES:
        key = (row[0], row[1])
        cur = existing.get(key)
        if cur is None:
            conn.execute(
                "INSERT INTO sites (company, name, url, login_url, notes) VALUES (?,?,?,?,?)",
                row,
            )
            added += 1
            continue
        if (cur["url"] != row[2] or cur["login_url"] != row[3]
                or (cur["notes"] or "") != row[4]):
            conn.execute(
                "UPDATE sites SET url = ?, login_url = ?, notes = ? WHERE id = ?",
                (row[2], row[3], row[4], cur["id"]),
            )
            updated += 1
    conn.commit()
    return added, updated


def cmd_sites(args):
    conn = connect()
    if getattr(args, "sync", False):
        a, u = sync_sites(conn)
        if a or u:
            print(f"已同步预置入口：新增 {a} 条，更新 {u} 条。")
        else:
            print("预置入口已是最新，无需改动。")
    kw = args.company
    sql = "SELECT * FROM sites"
    params = []
    if kw:
        sql += " WHERE company LIKE ? OR name LIKE ?"
        params = [f"%{kw}%"] * 2
    rows = conn.execute(sql + " ORDER BY id", params).fetchall()
    if not rows:
        print("没有匹配的招聘系统入口。")
        conn.close()
        return
    print(f"\n招聘系统入口（{len(rows)} 条）\n")
    for s in rows:
        print(f"  {pad(trunc(s['company'] or '-', 12), 14)}{pad(trunc(s['name'], 30), 32)}")
        print(f"    {s['url']}")
        if s["notes"]:
            print(f"    · {s['notes']}")
    print("\n打开某条： qz open <投递ID>    或   qz open --company <公司名>\n")
    conn.close()


def _match_sites(conn, company: str):
    """按公司名找招聘系统入口。三层匹配，从严到宽：

      ① sites.company 含记录公司名 —— 记录名更短时命中（如「腾讯」）
      ② 记录公司名含 sites.company —— **记录名更长时命中**，这条最有用：
         台账里存的是投递时的全称（「中国移动通信集团四川有限公司」），
         而 sites 里预置的是集团短名（「中国移动」），只做单向匹配永远匹配不上
      ③ site.name 里含记录公司名 —— 兜底
    """
    company = (company or "").strip()
    if not company:
        return []
    rows = conn.execute("SELECT * FROM sites WHERE company LIKE ?",
                        (f"%{company}%",)).fetchall()
    if rows:
        return rows
    rows = conn.execute(
        "SELECT * FROM sites WHERE company <> '' AND ? LIKE '%' || company || '%'",
        (company,)).fetchall()
    if rows:
        return rows
    return conn.execute("SELECT * FROM sites WHERE name LIKE ?",
                        (f"%{company}%",)).fetchall()


def cmd_open(args):
    """打开招聘系统页面。三种用法：

      qz open 12                     按投递 id 打开它的系统页面
      qz open --company 中国移动       按公司名查预置的系统入口
      qz open 12 --set-url <网址>     给这条投递指定系统入口

    （原 login / portal 两个命令已并到这里——它们做的事和 open 高度重叠。）
    """
    conn = connect()

    # 模式一：给某条投递设系统入口
    if args.set_url:
        if not args.id:
            die("用 --set-url 时必须给投递 id，例如： qz open 12 --set-url https://...")
        r = conn.execute("SELECT company FROM applications WHERE id = ?",
                         (args.id,)).fetchone()
        if not r:
            die(f"找不到 # {args.id}")
        conn.execute("UPDATE applications SET portal_url = ?, updated_at = ? WHERE id = ?",
                     (args.set_url, now_str(), args.id))
        conn.commit()
        print(f"已为 #{args.id} {r['company']} 设置系统入口：{args.set_url}")
        conn.close()
        return

    # 模式二：按公司名查入口
    if args.company:
        rows = _match_sites(conn, args.company)
        if not rows:
            print(f"没有找到「{args.company}」的系统入口。用  qz sites  看已预置的列表。")
            conn.close()
            return
        site = rows[0]
        url = site["login_url"] or site["url"]
        print(f"{site['name']}\n{url}")
        if not args.print_only:
            webbrowser.open(url)
        conn.close()
        return

    # 模式三：按投递 id 打开
    if not args.id:
        die("要给投递 id，或加 --company <公司名>。例如： qz open 12"
            "   /   qz open --company 中国移动")
    r = conn.execute("SELECT * FROM applications WHERE id = ?", (args.id,)).fetchone()
    if not r:
        die(f"找不到 # {args.id}")

    urls = []
    if r["portal_url"]:
        urls.append(r["portal_url"])
    for site in _match_sites(conn, r["company"]):
        urls.append(site["login_url"] or site["url"])

    if not urls:
        print(f"没有为「{r['company']}」找到系统入口。")
        print(f"可以手动补上： qz open {r['id']} --set-url <系统网址>")
        conn.close()
        return

    url = urls[0]
    print(f"#{r['id']} {r['company']} · {r['position']}")
    print(f"状态：{r['status']}")
    print(f"打开：{url}")
    print("\n注意：招聘系统需要你本人登录（浏览器里保存的登录态会直接用上）。")
    print("本工具不保存、不代填你的账号密码。")
    if not args.print_only:
        try:
            webbrowser.open(url)
        except Exception as e:
            print(f"（自动打开失败：{e}，请手动复制上面的网址）")
    conn.close()


# 改字段用中文更顺手：qz set 23 岗位=数据分析师 城市=北京
# 改字段用中文更顺手：qz set 23 岗位=数据分析师 城市=北京
FIELD_ALIAS = {
    "公司": "company", "岗位": "position", "部门": "department",
    "批次": "batch", "渠道": "channel", "城市": "city", "内推人": "referrer",
    "投递日期": "applied_at", "状态": "status", "状态时间": "status_at",
    "备注": "notes", "薪资": "salary", "意向度": "priority",
    "jd": "jd_url", "JD": "jd_url", "入口": "portal_url",
}
DATE_FIELDS = {"applied_at", "status_at", "next_action_at"}


def cmd_set(args):
    """改某条投递的任意字段。补岗位名、改城市、修日期都靠它。"""
    conn = connect()
    r = conn.execute("SELECT * FROM applications WHERE id = ?", (args.id,)).fetchone()
    if not r:
        die(f"找不到 # {args.id}")

    valid = [c["name"] for c in conn.execute("PRAGMA table_info(applications)")]
    updates, msgs = {}, []
    for item in args.kv:
        if "=" not in item:
            die(f"「{item}」格式不对，应为 字段=值")
        k, v = item.split("=", 1)
        k, v = k.strip(), v.strip()
        col = FIELD_ALIAS.get(k, k)
        if col not in valid:
            die(f"没有「{k}」这个字段。可用：{'、'.join(sorted(FIELD_ALIAS))}，"
                f"或直接写英文列名。")
        if col in DATE_FIELDS:
            v = parse_date(v)
        elif col == "status":
            if v not in ALL_STATUS:
                fixed = STATUS_ALIAS.get(v)
                if not fixed:
                    die(f"不认识的状态「{v}」。可用：{' / '.join(ALL_STATUS)}")
                v = fixed
            if v != r["status"] and "status_at" not in updates:
                updates["status_at"] = today_str()
        elif col == "priority":
            try:
                v = int(v or 0)
            except ValueError:
                die("意向度要填数字（0–3）")
        old = r[col]
        if old == v:
            msgs.append(f"  = {k} 本来就是「{v}」，没动")
            continue
        updates[col] = v
        msgs.append(f"  {k}：「{old}」→「{v}」")

    if updates:
        touched = sorted(updates)
        updates["updated_at"] = now_str()
        conn.execute(
            "UPDATE applications SET %s WHERE id = ?"
            % ", ".join(f"{c} = ?" for c in updates),
            list(updates.values()) + [args.id])
        if "status" in updates:
            log_event(conn, args.id, "改字段", r["status"], updates["status"], note="qz set")
        else:
            log_event(conn, args.id, "改字段", note="、".join(touched))
        conn.commit()

    print(f"#{r['id']} {r['company']} · {r['position']}")
    for m in msgs:
        print(m)
    print(f"  已保存 {len(updates) - 1} 项" if updates else "  没有任何改动")
    conn.close()


# ---------------------------------------------------------------- 数据体检

def cmd_doctor(args):
    """一次列出所有『填得不全 / 时间倒置 / 待办过期 / 终态残留』。

    只读，不改任何东西。定位是"把散落在各处的数据问题摊到一张纸上"。
    """
    conn = connect()
    rows = conn.execute("SELECT * FROM applications ORDER BY id").fetchall()
    p0, p1, p2 = [], [], []

    for r in rows:
        tag = f"#{r['id']} {r['company']}"
        pos = (r["position"] or "").strip()
        if not r["applied_at"]:
            p0.append((tag, "缺投递日期——停滞天数、响应时长都会失真",
                       f"qz set {r['id']} 投递日期=09-28"))
        if not pos or "待补" in pos:
            p0.append((tag, f"岗位名待补（现为「{pos or '空'}」）",
                       f'qz set {r["id"]} 岗位="真实岗位名"'))
        if r["applied_at"] and r["status_at"] and r["status_at"] < r["applied_at"]:
            p0.append((tag, f"时间倒置：状态时间 {r['status_at']} 早于投递日期 {r['applied_at']}",
                       ""))
        if r["next_action_at"] and r["status"] not in TERMINAL:
            dd = days_until(r["next_action_at"])
            if dd is not None and dd < 0:
                p1.append((tag, f"待办「{r['next_action']}」已逾期 {abs(dd)} 天",
                           f"qz next {r['id']}          # 做完了就清掉"))
        if r["status"] in TERMINAL and (r["next_action"] or r["next_action_at"]):
            p1.append((tag, f"已进入终态「{r['status']}」，却还留着待办「{r['next_action']}」",
                       f"qz next {r['id']}"))
        if effective_status(r) == "泡池子":
            st = stall_days(r)
            if st is not None and st >= STALL_DAYS * 3:
                p2.append((tag, f"泡池子已 {st} 天，基本可以断定黄了",
                           f"qz up {r['id']} --to 已放弃"))

    # 同公司多投、却没填城市也没填部门 —— 事后分不清哪条是哪条（只在这种情况下报）
    by_co = {}
    for r in rows:
        by_co.setdefault(r["company"], []).append(r)
    for co, rs in by_co.items():
        if len(rs) < 2:
            continue
        vague = [r for r in rs if not r["city"] and not (r["department"] or "").strip()]
        if vague:
            ids = "、".join(f"#{r['id']}" for r in vague)
            p2.append((f"{co}（共 {len(rs)} 条）",
                       f"{ids} 既无城市也无部门，同公司多投时不好区分",
                       f"qz set {vague[0]['id']} 部门=xxx"))

    seen = {}
    for r in rows:
        seen.setdefault((r["company"], r["position"], (r["department"] or "").strip()),
                        []).append(r["id"])
    for (co, po, dp), ids in seen.items():
        if len(ids) > 1:
            p1.append((f"{co}{' / ' + dp if dp else ''} · {po}",
                       f"同名记录 {len(ids)} 条：" + "、".join(f"#{i}" for i in ids),
                       "用 qz set <id> 部门=xxx 区分开"))

    total_issue = len(p0) + len(p1) + len(p2)
    print(f"\n数据体检 —— {len(rows)} 条记录，发现 {total_issue} 处问题\n")
    if not total_issue:
        print("  没发现问题。\n")
        conn.close()
        return

    for title, items, hint in [
        ("P0 必须补（影响统计口径）", p0, None),
        ("P1 建议处理", p1, None),
        ("P2 可选（不影响判断）", p2, None),
    ]:
        if not items:
            continue
        print(f"{title}　{len(items)} 处")
        for tag, msg, cmd in items:
            print(f"  {pad(trunc(tag, 22), 24)}{msg}")
            if cmd:
                print(f"  {'':24}→ {cmd}")
        print()

    print("说明：本命令只读，不会改任何数据。上面每条都给了修的命令，复制即可。")
    print("（负数天数是逾期；`qz set` 改字段，`qz next` 清待办。）\n")
    conn.close()


# ---------------------------------------------------------------- 导出

def export_rows(conn):
    return conn.execute("SELECT * FROM applications ORDER BY id").fetchall()


def _port_open(host: str, port: int) -> bool:
    """端口是否已被占用（能建立 TCP 连接）。"""
    import socket
    for h in (host, "127.0.0.1"):
        try:
            with socket.create_connection((h, port), timeout=1.5):
                return True
        except OSError:
            continue
    return False


def _serving_ours(host: str, port: int) -> bool:
    """这个端口上跑的是不是我们这个服务（而不是别的程序）。

    用**禁用代理**的 opener：访问 127.0.0.1 不该经过系统代理，
    否则机器上开了代理时会被拦成 502，误判成「服务没在跑」。
    """
    import urllib.request
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    for h in (host, "127.0.0.1"):
        try:
            with opener.open(f"http://{h}:{port}/api/meta", timeout=2) as r:
                return r.status == 200 and b"statuses" in r.read()
        except Exception:
            continue
    return False


def stop_serve(port: int) -> int:
    """关掉在跑的本地服务（无窗口启动时没有 cmd 窗口可关，只能靠它）。"""
    if os.name != "nt":
        die("--stop 目前只支持 Windows。")
    if not _serving_ours("127.0.0.1", port):
        print(f"端口 {port} 上没有正在运行的本服务（可能已经关了）。")
        return 0
    out = subprocess.run(["netstat", "-ano"], capture_output=True,
                         text=True, errors="replace").stdout
    pids = set()
    for line in out.splitlines():
        if f":{port}" in line and "LISTENING" in line.upper():
            pids.add(line.split()[-1])
    if not pids:
        print("没找到监听该端口的进程。")
        return 1
    for pid in pids:
        r = subprocess.run(["taskkill", "/PID", pid, "/F"],
                           capture_output=True, text=True, errors="replace")
        print(f"  已结束进程 {pid}：{(r.stdout or r.stderr).strip()}")
    print(f"服务已停止（端口 {port}）。")
    return 0


def cmd_serve(args):
    """启动本地网页界面（在 qz_web.py 里实现）。"""
    if getattr(args, "stop", False):
        return stop_serve(args.port)

    # 已经在跑 → 不再起第二个，直接把页面打开（避免双击两次开出两个服务）
    if _serving_ours(args.host, args.port):
        url = f"http://127.0.0.1:{args.port}/"
        print(f"服务已经在运行，直接打开：{url}")
        if not args.no_browser:
            try:
                webbrowser.open(url)
            except Exception:
                pass
        return 0
    if _port_open(args.host, args.port):
        # 端口被占但不是我们的服务（或探测被代理挡了）——也不再起第二个
        print(f"端口 {args.port} 已被占用，不再启动第二个实例。")
        print(f"如果页面打不开，先用  qz serve --stop  释放端口，"
              f"或换端口： qz serve --port 8800")
        url = f"http://127.0.0.1:{args.port}/"
        if not args.no_browser:
            try:
                webbrowser.open(url)
            except Exception:
                pass
        return 0

    try:
        import qz_web
    except ImportError as e:
        die(f"加载 qz_web.py 失败：{e}")
    return qz_web.run(host=args.host, port=args.port,
                      open_browser=not args.no_browser)


# ---------------------------------------------------------------- 响应基线检查

def cmd_baseline(args):
    """按「响应时长基线」判断哪些投递已超过合理等待期 —— **只读提醒**。

    本命令 2026-10-16 起不再写库。
    依据：标记「泡池子」的判据已收敛为唯一一条（「流程中」且卡住 ≥ STALL_DAYS 天），
    由 effective_status() 负责显示、qz stall --apply 负责落库。若本命令也写，
    就会出现两个判据不同的写入口，互相打架。故 --apply 停用。
    这里保留的价值是**这家该不该催**：按公司/类别的响应窗口 + 工作日宽限，
    以及「公开节点还没到、现在没消息属正常」这一层判断。
    """
    ensure_dirs()
    conn = connect()

    if getattr(args, "list_rules", False):
        print("\n响应基线表（用来判断「什么时候该主动出击」）\n")
        print(f"  {pad('类别', 26)}{pad('窗口', 12)}{pad('公开节点', 14)}置信度")
        print("  " + "-" * 96)
        for rule in RESPONSE_BASELINE:
            print(f"  {pad(trunc(rule['cat'], 24), 26)}"
                  f"{pad(str(rule['wd']) + ' 工作日', 12)}"
                  f"{pad(rule.get('node') or '—', 14)}{rule['conf']}")
        print(f"  {pad(trunc(DEFAULT_BASELINE['cat'], 24), 26)}"
              f"{pad(str(DEFAULT_BASELINE['wd']) + ' 工作日', 12)}"
              f"{pad('—', 14)}{DEFAULT_BASELINE['conf']}")
        print("\n  数据来源只有两类，别把它当「公司平均响应时间」：")
        print("    ① 有公开统一笔试日期的单位 —— 用公告里的真实日期（可核验）；")
        print("    ② 没有公开日期的 —— 用央国企/互联网的行业经验窗口，是估计值。")
        print("  判超期时额外给 %d 个工作日宽限，避免把「公告刚截止还没筛」误判。" % BASELINE_GRACE_WORKDAYS)
        print("  工作日已排除周末与法定节假日（国办发明电〔2025〕7 号）。")
        print("  2027 年放假安排公布后，把日期补进 HOLIDAYS 即可。\n")
        conn.close()
        return

    # 只看还真正在等的：库内「流程中」、且没被卡住天数自动判成泡池子的。
    rows = [r for r in fetch_apps(conn, None, None, None, True)
            if effective_status(r) == "流程中"]
    if not rows:
        print("没有处于静默期的投递需要判断。")
        conn.close()
        return

    buckets = {"wait": [], "ok": [], "overdue": []}
    for r in rows:
        verdict, why, wd, rule = baseline_verdict(r)
        buckets[verdict].append((wd or 0, r, why, rule))

    print(f"\n响应基线检查（静默中的投递共 {len(rows)} 条）—— 只读提醒，不改台账\n")

    if buckets["overdue"]:
        print(f"■ 超过窗口，该催了（{len(buckets['overdue'])} 条）")
        for wd, r, why, rule in sorted(buckets["overdue"], key=lambda x: -x[0]):
            print(f"  #{pad(str(r['id']), 4)}{pad(trunc(r['company'], 20), 22)}"
                  f"{trunc(r['position'], 16)}")
            print(f"        {why}｜{rule['cat']}｜{r['status']}"
                  f"（自 {r['status_at'] or r['applied_at']}）")
    if buckets["ok"]:
        print(f"\n■ 仍在窗口内（{len(buckets['ok'])} 条）")
        for wd, r, why, rule in sorted(buckets["ok"], key=lambda x: -x[0])[:12]:
            print(f"  #{pad(str(r['id']), 4)}{pad(trunc(r['company'], 20), 22)}{why}")
        if len(buckets["ok"]) > 12:
            print(f"  … 另有 {len(buckets['ok']) - 12} 条在窗口内")
    if buckets["wait"]:
        print(f"\n■ 公开节点还没到，静默属正常（{len(buckets['wait'])} 条）")
        for wd, r, why, rule in sorted(buckets["wait"], key=lambda x: -x[0]):
            print(f"  #{pad(str(r['id']), 4)}{pad(trunc(r['company'], 20), 22)}{why}")

    # 落库入口唯一化：本命令不再写「泡池子」。
    n_flip = sum(1 for r in fetch_apps(conn, None, None, None, True) if needs_stall_flip(r))
    print(f"\n  想标记「泡池子」请用唯一入口：  qz stall --apply")
    print(f"  判据：库内「流程中」且卡住 ≥ {STALL_DAYS} 天。眼下符合的有 {n_flip} 条。")
    if args.apply:
        print("  （--apply 已停用：当年的双写入口会让两个判据打架，故收敛为一条。）")
    print()
    conn.close()


# ---------------------------------------------------------------- 邮箱（IMAP）

MAIL_BRAND_ALIASES = [
    (r"中国联合网络通信", ["中国联通", "联通"]),
    (r"中国移动通信|中移物联网|咪咕", ["中国移动", "中移物联网", "咪咕", "中国移动通信"]),
    (r"中国航空发动机|中国航发|航发", ["中国航发", "航发"]),
    (r"中国航空工业|中航通飞", ["中航通飞", "中国航空工业"]),
    (r"南方电网|海南电网", ["南方电网", "南网", "海南电网"]),
    (r"国家电力投资|国家电投|国核|中电国际|电能成套", ["国家电投", "国家电力投资", "国核", "中电国际"]),
    (r"中远海运", ["中远海运", "中远海运集团"]),
    (r"中国机械工业集团|国机集团", ["国机集团", "中国机械工业集团"]),
    (r"华润", ["华润"]),
    (r"广州港", ["广州港"]),
    (r"海南矿业", ["海南矿业"]),
    (r"海南省出版发行", ["海南省出版发行", "海南出版发行"]),
]


def _mail_cfg_path() -> Path:
    return APP_DIR / "data" / "mail.json"


def load_mail_cfg():
    p = _mail_cfg_path()
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:
        die(f"读取 {p} 失败：{e}")


def save_mail_cfg(cfg):
    p = _mail_cfg_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        os.chmod(p, 0o600)
    except Exception:
        pass
    print(f"已写入 {p}")
    print("⚠️ 该文件含邮箱授权码，不要提交到 git、也不要发给别人。")


# 允许作为别名的两字品牌（这三个歧义低、足够特异）
ALIAS2_OK = {"联通", "航发", "南网", "阿里", "百度", "腾讯", "字节", "美团",
             "京东", "网易", "小米", "滴滴", "华润", "华为", "奇瑞", "蔚来"}


def _aliases_for(company: str) -> set:
    """把公司名拆成可用于邮件匹配的别名。

    只保留 ≥3 字的别名（两字词歧义太大，例如「移动」会命中「移动支付」，
    「联通」会命中「联通性」——但这几个品牌本身特异的除外）。
    """
    out = set()
    core = re.sub(r"[（(].*?[)）]", "", company or "").strip()
    if core:
        out.add(core)
    n = core
    for suf in ("股份有限公司", "有限责任公司", "有限公司", "集团有限公司",
                "集团", "公司", "研究院", "研究所"):
        if n.endswith(suf) and len(n) > len(suf) + 1:
            n = n[:-len(suf)]
            out.add(n)
    for pat, al in MAIL_BRAND_ALIASES:
        if re.search(pat, company or ""):
            out |= set(al)
    return {a for a in out if len(a) >= 3 or a in ALIAS2_OK}


def _safe_decode(b, enc):
    """按声明编码解码，编码名无效时逐级回退（QQ 邮件常见 unknown-8bit）。"""
    for e in (enc, "utf-8", "gb18030", "latin-1"):
        if not e:
            continue
        try:
            return b.decode(e, errors="replace")
        except (LookupError, UnicodeDecodeError):
            continue
    return b.decode("utf-8", errors="replace")


def _decode_hdr(raw) -> str:
    from email.header import decode_header
    if not raw:
        return ""
    parts = []
    for text, enc in decode_header(raw):
        if isinstance(text, bytes):
            parts.append(_safe_decode(text, enc))
        else:
            parts.append(text)
    return "".join(parts).strip()


def _seq_set(ids) -> bytes:
    """把序列号列表压成 IMAP 序列集合字符串（连续段用 a:b）。"""
    nums = sorted(int(i) for i in ids)
    out, start, prev = [], nums[0], nums[0]
    for n in nums[1:]:
        if n == prev + 1:
            prev = n
            continue
        out.append(f"{start}:{prev}" if start != prev else f"{start}")
        start = prev = n
    out.append(f"{start}:{prev}" if start != prev else f"{start}")
    return ",".join(out).encode()


def _classify_mail(text: str):
    for status, pat in MAIL_RULES:
        if re.search(pat, text, re.I):
            return status
    return None


def cmd_mail(args):
    """拉取邮箱里最近的招聘邮件 → 分类 → 匹配台账 → 输出候选状态。

    默认**只报告不写库**；核对无误后加 --apply 才落库。
    授权码只存在 data/mail.json，不写在代码里。
    """
    cfg = load_mail_cfg()

    if args.setup:
        print("=== 配置邮箱（IMAP）===")
        print("QQ 邮箱：先到 设置→账户→POP3/IMAP/SMTP服务 开启 IMAP，生成授权码，")
        print("         这里填**授权码**，不是登录密码。\n")
        host = input("IMAP 服务器 [imap.qq.com]: ").strip() or "imap.qq.com"
        port = input("端口 [993]: ").strip() or "993"
        user = input("邮箱地址: ").strip()
        code = input("授权码: ").strip()
        if not user or not code:
            die("邮箱地址与授权码都不能为空。")
        cfg = {"host": host, "port": int(port), "user": user, "authcode": code,
               "folder": "INBOX", "days": 14}
        save_mail_cfg(cfg)
        print("配置完成。接着跑  qz mail --test  验证连接。")
        return

    if not cfg:
        die("还没有配置邮箱。先跑：  qz mail --setup")

    days = args.days or cfg.get("days", 14)
    try:
        import email
        import imaplib
    except ImportError as e:
        die(f"Python 缺少标准库模块：{e}")

    print(f"连接 {cfg['user']} @ {cfg['host']}:{cfg['port']} …")
    try:
        M = imaplib.IMAP4_SSL(cfg["host"], int(cfg.get("port", 993)))
        M.login(cfg["user"], cfg["authcode"])
    except Exception as e:
        die(f"登录失败：{e}\n"
            f"  排查：① 授权码是否用错（不能用登录密码）；"
            f"② QQ 邮箱是否已开启 IMAP 服务；③ 网络/端口是否被拦。")

    if args.test:
        typ, boxes = M.list()
        print("连接成功。邮箱文件夹：")
        for b in boxes[:12]:
            print("   ", b.decode("utf-8", errors="replace"))
        M.logout()
        return

    M.select(cfg.get("folder", "INBOX"))
    since = (date.today() - timedelta(days=days)).strftime("%d-%b-%Y")
    # ⚠️ QQ 邮箱只认「拆成多参数」的写法；写成单个 "(SINCE xx)" 会被静默忽略，
    #    返回整个邮箱（实测 1407 封全回来）。必须 M.search(None, "SINCE", xxx)。
    typ, data = M.search(None, "SINCE", since)
    ids = (data[0].split() if data and data[0] else [])
    if not ids:
        print(f"最近 {days} 天没有邮件。")
        M.logout()
        return

    conn = connect()
    apps = [dict(r) for r in fetch_apps(conn, None, None, None, True)]
    alias_map = []
    for a in apps:
        for al in _aliases_for(a["company"]):
            alias_map.append((al, a))

    print(f"最近 {days} 天共 {len(ids)} 封邮件。先批量拉表头粗筛…")
    # ---- 第一遍：批量拉表头（只取 Subject/From/Date，速度远快于拉全文）----
    headers = {}
    for i in range(0, len(ids), 300):
        typ, resp = M.fetch(_seq_set(ids[i:i + 300]),
                            "(BODY.PEEK[HEADER.FIELDS (SUBJECT FROM DATE)])")
        for item in resp:
            if isinstance(item, tuple):
                m = re.match(rb"(\d+)", item[0])
                if m:
                    headers[int(m.group(1))] = item[1]

    KEEP_RE = (r"招聘|校招|校园招聘|应聘|笔试|测评|面试|offer|OFFER|Offer|录用|"
               r"简历|投递|申请|岗位|感谢|人才|考试|通知")
    interesting = []
    for seq, raw in headers.items():
        msg = email.message_from_bytes(raw)
        subject = _decode_hdr(msg.get("Subject"))
        sender = _decode_hdr(msg.get("From"))
        head = f"{subject}\n{sender}"
        if re.search(KEEP_RE, head) or any(al in head for al, _ in alias_map):
            interesting.append((seq, subject, sender, msg.get("Date", "")))
    print(f"  表头 {len(headers)} 封 → 可能相关 {len(interesting)} 封，拉正文精确匹配…\n")

    if not interesting:
        print("没有像招聘邮件的内容。可以加  --days 60  扩大范围。")
        M.logout()
        conn.close()
        return

    # ---- 第二遍：只对「可能相关」的批量拉全文 ----
    meta = {s: (sub, snd, dt) for s, sub, snd, dt in interesting}
    seqs = list(meta)
    bodies = {}
    for i in range(0, len(seqs), 80):
        typ, resp = M.fetch(_seq_set(seqs[i:i + 80]), "(RFC822)")
        for item in resp:
            if isinstance(item, tuple):
                m = re.match(rb"(\d+)", item[0])
                if m:
                    bodies[int(m.group(1))] = item[1]

    candidates = []
    other_mail = 0
    for seq, raw in bodies.items():
        subject, sender, mdate = meta.get(seq, ("", "", ""))
        try:
            msg = email.message_from_bytes(raw)
        except Exception:
            continue
        chunks = []
        if msg.is_multipart():
            for part in msg.walk():
                if part.get_content_type() in ("text/plain", "text/html"):
                    try:
                        b = part.get_payload(decode=True)
                        if b:
                            chunks.append(_safe_decode(b, part.get_content_charset()))
                    except Exception:
                        pass
        else:
            try:
                b = msg.get_payload(decode=True)
                if b:
                    chunks.append(_safe_decode(b, msg.get_content_charset()))
            except Exception:
                pass
        body = "\n".join(chunks)
        # 先判是不是招聘邮件：判不出状态就跳过，避免把 QQ 会员通知这类
        # 「正文页脚恰好含公司名」的邮件当成招聘回音（实测噪音极大）。
        status = _classify_mail(f"{subject}\n{body[:4000]}")
        if status is None:
            other_mail += 1
            continue
        # 别名只在「主题＋发件人＋正文前 1500 字」里找，不看页脚
        zone = f"{subject}\n{sender}\n{body[:1500]}"
        hits = {}
        for al, a in alias_map:
            if al and al in zone:
                hits[a["id"]] = a
        if not hits:
            continue
        for a in hits.values():
            candidates.append({"mail_date": mdate, "subject": subject,
                               "sender": sender, "status": status, "app": a})

    M.logout()
    print(f"扫完 {len(bodies)} 封相关邮件：能判断状态的 {len(candidates)} 条，"
          f"另有 {other_mail} 封判不出招聘语义、已跳过。\n")

    if not candidates:
        print("没有匹配到能判断状态的招聘邮件。可以加  --days 60  扩大范围，")
        print("或确认招聘邮件的发件人/主题里是否带公司名。")
        conn.close()
        return

    # 同一条记录可能收到多封邮件 → 归并。四档体系下不存在「谁更靠后」的顺序，
    # 改为两条规则：① 终态（已挂/已放弃）信息量最大，优先；
    #              ② 否则取邮件日期最新的那封（较新的信息更接近现状）。
    best = {}
    for c in candidates:
        k = c["app"]["id"]
        cur = best.get(k)
        if cur is None:
            best[k] = c
            continue
        c_term = norm_status(c["status"]) in TERMINAL
        cur_term = norm_status(cur["status"]) in TERMINAL
        if c_term and not cur_term:
            best[k] = c
        elif cur_term:
            continue
        elif (c.get("mail_date") or "") > (cur.get("mail_date") or ""):
            best[k] = c

    from email.utils import parsedate_to_datetime

    def _mail_date(s):
        try:
            return parsedate_to_datetime(s).date()
        except Exception:
            return None

    actionable, noteless, skipped, same_n = [], [], [], 0
    print("匹配结果（按台账记录归并）：\n")
    for c in sorted(best.values(), key=lambda x: x["app"]["id"]):
        a, raw = c["app"], c["status"]
        nst = norm_status(raw)              # 细状态 → 四档
        md = _mail_date(c["mail_date"])
        # 闸门：邮件日期不能早于当前状态变更日，否则是过期信息
        # （老邮件如「笔试通知」在流程已推进后毫无价值，写进去只会打乱计时）
        try:
            cur_d = date.fromisoformat((a["status_at"] or a["applied_at"])[:10])
        except ValueError:
            cur_d = None
        stale = (md is not None and cur_d is not None and md < cur_d)
        if stale:
            skipped.append((c, "邮件早于当前状态"))
            continue

        if nst in TERMINAL:
            # 终态是全库唯一值得直接改状态的邮件（拒信 / 放弃）。
            if a["status"] == nst:
                same_n += 1
                continue
            print(f"  #{pad(str(a['id']), 4)}{pad(trunc(a['company'], 18), 20)}"
                  f"{pad(trunc(c['subject'], 24), 26)}"
                  f"{pad(str(md or '?'), 12)}{a['status']} → {nst}（终态）")
            actionable.append(c)
            continue

        # 非终态：四档里都还是「流程中」，改状态没有意义。
        # 改为把邮件里的细状态写进备注，并刷新 status_at ——
        # 卡住天数重新起算，这才对应「对方又动了一下」的实情。
        if not raw or raw in ALL_STATUS:
            same_n += 1
            continue
        print(f"  #{pad(str(a['id']), 4)}{pad(trunc(a['company'], 18), 20)}"
              f"{pad(trunc(c['subject'], 24), 26)}"
              f"{pad(str(md or '?'), 12)}备注 ← {raw}")
        noteless.append(c)

    if same_n:
        print(f"\n  另有 {same_n} 条邮件没有带来新信息，无需改动。")
    if skipped:
        print(f"\n  ⚠ 有 {len(skipped)} 条邮件被拦下（不会写库）：")
        for c, why in skipped[:12]:
            print(f"     #{c['app']['id']} {trunc(c['app']['company'], 16)}  "
                  f"邮件说「{c['status']}」，台账状态日 "
                  f"{c['app']['status_at'] or c['app']['applied_at']} → {why}")
        if len(skipped) > 12:
            print(f"     … 另有 {len(skipped) - 12} 条")

    print(f"\n可改状态 {len(actionable)} 条；只写备注并刷新计时 {len(noteless)} 条。")
    if not actionable and not noteless:
        conn.close()
        return

    if not args.apply:
        print("【试运行】未写入台账。核对无误后执行：  qz mail --apply")
        conn.close()
        return

    ts = now_str()
    for c in actionable:
        a, nst = c["app"], norm_status(c["status"])
        conn.execute("UPDATE applications SET status=?, status_at=?, updated_at=?"
                     " WHERE id=?", (nst, today_str(), ts, a["id"]))
        log_event(conn, a["id"], "状态变更", a["status"], nst,
                  f"邮件：{trunc(c['subject'], 60)}", source="mail")
    for c in noteless:
        a, raw = c["app"], c["status"]
        d = _mail_date(c["mail_date"])
        day = d.isoformat() if d else today_str()
        new_notes = (a["notes"] + "\n" if a["notes"] else "") + f"[{day}] 邮件：{raw}"
        conn.execute("UPDATE applications SET notes=?, status_at=?, updated_at=?"
                     " WHERE id=?", (new_notes, today_str(), ts, a["id"]))
        log_event(conn, a["id"], "邮件进展", a["status"], a["status"],
                  f"邮件：{trunc(c['subject'], 60)}（{raw}）", source="mail")
    conn.commit()
    print(f"已处理：改状态 {len(actionable)} 条、写备注 {len(noteless)} 条，"
          f"事件来源标记为 mail，可在 qz show 里看到邮件依据。")
    conn.close()


def export_payload(conn) -> dict:
    """完整备份载荷：投递记录 + 事件流。用于 qz export --json / qz import。"""
    return {
        "format": "qz-backup",
        "version": 1,
        "exported_at": now_str(),
        "applications": [dict(r) for r in
                         conn.execute("SELECT * FROM applications ORDER BY id")],
        "events": [dict(r) for r in conn.execute("SELECT * FROM events ORDER BY id")],
    }


def cmd_export(args):
    ensure_dirs()
    conn = connect()
    rows = export_rows(conn)
    if not rows:
        print("没有可导出的记录。")
        conn.close()
        return

    if args.json:
        path = OUT_DIR / f"秋招投递记录_{date.today().isoformat()}.json"
        path.write_text(json.dumps(export_payload(conn), ensure_ascii=False, indent=2),
                        encoding="utf-8")
        print(f"已导出完整备份（含事件流，可用 qz import 还原）： {path}")

    if args.xlsx:
        path = OUT_DIR / f"秋招投递记录_{date.today().isoformat()}.csv"
        with open(path, "w", encoding="utf-8-sig", newline="") as f:
            w = csv.writer(f)
            w.writerow(["ID", "公司", "部门/事业群", "岗位", "批次", "渠道", "城市", "内推人",
                        "投递日期", "当前状态", "状态更新于", "停滞天数", "下一步", "下一步时间",
                        "意向度", "薪资", "JD链接", "系统入口", "备注"])
            for r in rows:
                stall = days_between(r["status_at"]) if r["status_at"] else ""
                w.writerow([r["id"], r["company"], r["department"], r["position"], r["batch"],
                            r["channel"], r["city"], r["referrer"], r["applied_at"],
                            r["status"], r["status_at"], stall, r["next_action"],
                            r["next_action_at"], r["priority"], r["salary"],
                            r["jd_url"], r["portal_url"], r["notes"]])
        print(f"已导出表格（Excel 双击可开）： {path}")

    if args.html:
        path = OUT_DIR / f"秋招看板_{date.today().isoformat()}.html"
        path.write_text(render_html(conn, rows), encoding="utf-8")
        print(f"已导出看板（双击用浏览器打开）： {path}")

    if args.ics:
        path = OUT_DIR / f"秋招日程_{date.today().isoformat()}.ics"
        path.write_text(render_ics(rows), encoding="utf-8")
        print(f"已导出日历（双击导入，可同步到手机）： {path}")

    if not (args.xlsx or args.html or args.ics or args.json):
        # 默认四样都出
        args.xlsx = args.html = args.ics = args.json = True
        cmd_export(args)
        return
    conn.close()


def cmd_import(args):
    """从 qz export --json 的备份还原。按 uid（公司+岗位+批次+部门）判重。"""
    path = Path(args.file)
    if not path.exists():
        die(f"找不到文件：{path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        die(f"读不出 JSON：{e}")

    if isinstance(data, list):
        apps, evs = data, []
    elif isinstance(data, dict):
        apps = data.get("applications") or []
        evs = data.get("events") or []
    else:
        die("JSON 结构不认识，应为备份对象或记录数组。")
    if not apps:
        die("文件里没有 applications 记录。")

    conn = connect()
    valid = {r["name"] for r in conn.execute("PRAGMA table_info(applications)")}

    added = skipped = updated = bad = 0
    old2new = {}
    lines = []
    for a in apps:
        a = dict(a)
        company = (a.get("company") or "").strip()
        position = (a.get("position") or "").strip()
        if not company or not position:
            bad += 1
            lines.append(("✗ 缺公司或岗位", company or "?", position or "?"))
            continue
        uid = a.get("uid") or make_uid(company, position, a.get("batch") or "",
                                       a.get("department") or "")
        cur = conn.execute("SELECT id FROM applications WHERE uid = ?", (uid,)).fetchone()

        if cur:
            old2new[a.get("id")] = cur["id"]
            if not args.update:
                skipped += 1
                lines.append(("跳过（已存在）", company, position))
                continue
            cols = [c for c in a if c in valid and c not in ("id", "uid")]
            if cols:
                conn.execute(
                    "UPDATE applications SET %s WHERE id = ?"
                    % ", ".join(f"{c} = ?" for c in cols),
                    [a[c] for c in cols] + [cur["id"]])
            updated += 1
            lines.append(("更新", company, position))
            continue

        cols = [c for c in a if c in valid and c != "id"]
        conn.execute(
            "INSERT INTO applications (%s) VALUES (%s)"
            % (", ".join(cols), ", ".join("?" * len(cols))),
            [a[c] for c in cols])
        new_id = conn.execute("SELECT last_insert_rowid() AS i").fetchone()["i"]
        old2new[a.get("id")] = new_id
        added += 1
        lines.append(("新增 #%d" % new_id, company, position))

    # 事件流：映射旧 id → 新 id，并跳过已存在的同一事件
    ev_added = ev_skip = 0
    for e in evs:
        e = dict(e)
        new_aid = old2new.get(e.get("app_id"))
        if not new_aid:
            ev_skip += 1
            continue
        dup = conn.execute(
            "SELECT 1 FROM events WHERE app_id = ? AND kind = ? AND at = ?"
            " AND ifnull(to_status,'') = ifnull(?,'')",
            (new_aid, e.get("kind") or "", e.get("at") or "", e.get("to_status"))).fetchone()
        if dup:
            ev_skip += 1
            continue
        conn.execute(
            "INSERT INTO events (app_id, kind, from_status, to_status, at, note, source)"
            " VALUES (?,?,?,?,?,?,?)",
            (new_aid, e.get("kind") or "", e.get("from_status") or "",
             e.get("to_status") or "", e.get("at") or now_str(),
             e.get("note") or "", e.get("source") or "import"))
        ev_added += 1

    if args.dry_run:
        conn.rollback()
        head = "预览（未写入）"
    else:
        conn.commit()
        head = "已导入"
    conn.close()

    print(f"\n{head}：{path}")
    for tag, c, p in lines[:40]:
        print(f"  {pad(tag, 14)} {c} · {p}")
    if len(lines) > 40:
        print(f"  … 另有 {len(lines) - 40} 条")
    print(f"\n投递：新增 {added}，更新 {updated}，跳过 {skipped}"
          + (f"，无效 {bad}" if bad else "")
          + f"；事件流：补入 {ev_added}，跳过 {ev_skip}")
    if args.dry_run:
        print("（--dry-run 只预览，什么都没改）")
    print()


def render_ics(rows) -> str:
    lines = [
        "BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//qz//qiuzhao tracker//CN",
        "CALSCALE:GREGORIAN", "METHOD:PUBLISH",
    ]
    stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
    for r in rows:
        if not r["next_action_at"] or r["status"] in TERMINAL:
            continue
        try:
            d = datetime.strptime(parse_date(r["next_action_at"]), "%Y-%m-%d")
        except Exception:
            continue
        dt = d.strftime("%Y%m%d")
        summary = f"{r['company']} {r['next_action'] or '投递待办'}"
        desc = (f"岗位：{r['position']}\\n批次：{r['batch']}\\n"
                f"当前状态：{r['status']}\\n备注：{(r['notes'] or '').replace(chr(10), ' ')}")
        lines += [
            "BEGIN:VEVENT",
            f"UID:qz-{r['id']}-{dt}@local",
            f"DTSTAMP:{stamp}",
            f"DTSTART;VALUE=DATE:{dt}",
            f"SUMMARY:{summary}",
            f"DESCRIPTION:{desc}",
            "BEGIN:VALARM", "TRIGGER:-P1D", "ACTION:DISPLAY",
            f"DESCRIPTION:{summary}", "END:VALARM",
            "END:VEVENT",
        ]
    lines.append("END:VCALENDAR")
    return "\r\n".join(lines) + "\r\n"


def render_html(conn, rows) -> str:
    def e(s):
        return _html.escape(str(s or ""))

    today = date.today().isoformat()
    n = len(rows)
    active = [r for r in rows if r["status"] not in TERMINAL]
    # 分组用 effective_status：库内「流程中」但卡够天数的，在看板里直接归入泡池子
    by_status = {}
    for r in rows:
        by_status.setdefault(effective_status(r), []).append(r)

    # 卡片口径就是四档分布 —— 用户要的是「还在推 / 卡住了 / 挂了 / 我放弃了」
    # 这个视角，不再按历史阶段做漏斗（细状态已并入备注）。
    eff = [effective_status(r) for r in rows]
    funnel = [("投递", n)] + [(s, eff.count(s)) for s in ALL_STATUS]

    due = []
    for r in active:
        if r["next_action_at"]:
            d = days_until(r["next_action_at"])
            if d is not None and d <= 7:
                due.append((d, r))
    due.sort(key=lambda x: x[0])

    STATUS_COLOR = {
        "流程中": ("#E6F1FB", "#185FA5"), "泡池子": ("#FAEEDA", "#854F0B"),
        "已挂": ("#FCEBEB", "#A32D2D"), "已放弃": ("#F1EFE8", "#888780"),
    }

    parts = [f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>秋招投递看板 {today}</title>
<style>
:root{{--bd:#E3E1DA;--tx:#2C2C2A;--mu:#5F5E5A;--hi:#F7F6F2}}
*{{box-sizing:border-box}}
body{{margin:0;padding:24px;font:14px/1.6 -apple-system,"Segoe UI","Microsoft YaHei",sans-serif;color:var(--tx);background:#FBFAF7}}
.wrap{{max-width:1180px;margin:0 auto}}
h1{{font-size:20px;font-weight:500;margin:0 0 4px}}
.sub{{color:var(--mu);font-size:13px;margin-bottom:20px}}
.cards{{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:10px;margin-bottom:22px}}
.card{{background:#fff;border:1px solid var(--bd);border-radius:12px;padding:14px}}
.card .k{{color:var(--mu);font-size:12px}}
.card .v{{font-size:22px;font-weight:500;margin-top:2px}}
.card .p{{color:var(--mu);font-size:12px}}
h2{{font-size:15px;font-weight:500;margin:26px 0 10px}}
table{{width:100%;border-collapse:collapse;background:#fff;border:1px solid var(--bd);border-radius:12px;overflow:hidden;font-size:13px}}
th{{text-align:left;padding:9px 12px;background:var(--hi);color:var(--mu);font-weight:400;font-size:12px;border-bottom:1px solid var(--bd);white-space:nowrap}}
td{{padding:9px 12px;border-bottom:1px solid #F0EEE8;vertical-align:top}}
tr:last-child td{{border-bottom:none}}
tr:hover td{{background:#FDFCFA}}
.badge{{display:inline-block;padding:1px 8px;border-radius:20px;font-size:12px;white-space:nowrap}}
.warn{{color:#A32D2D;font-weight:500}}
.hot{{color:#854F0B;font-weight:500}}
.mu{{color:var(--mu)}}
.barwrap{{display:flex;align-items:center;gap:8px;margin:5px 0}}
.barlab{{width:80px;color:var(--mu);font-size:12px}}
.bar{{height:16px;background:#B5D4F4;border-radius:3px;min-width:2px}}
.barnum{{font-size:12px;color:var(--mu)}}
@media(max-width:700px){{body{{padding:12px}}th:nth-child(4),td:nth-child(4),th:nth-child(7),td:nth-child(7){{display:none}}}}
</style></head><body><div class="wrap">
<h1>秋招投递看板</h1>
<div class="sub">生成于 {today} · 共 {n} 条投递，进行中 {len(active)} 条</div>
<div class="cards">"""]

    for label, v in funnel:
        pct = f"{v / n * 100:.0f}%" if n else "-"
        parts.append(f'<div class="card"><div class="k">{e(label)}</div>'
                     f'<div class="v">{v}</div><div class="p">{pct}</div></div>')

    if due:
        parts.append('</div><h2>7 天内到期的动作</h2><table><tr><th>剩余</th><th>公司</th>'
                     '<th>岗位</th><th>动作</th><th>日期</th></tr>')
        for d, r in due:
            if d < 0:
                tag, cls = f"逾期 {-d} 天", "warn"
            elif d == 0:
                tag, cls = "今天", "warn"
            elif d == 1:
                tag, cls = "明天", "hot"
            else:
                tag, cls = f"{d} 天后", ("hot" if d <= 3 else "mu")
            parts.append(f'<tr><td class="{cls}">{tag}</td><td>{e(r["company"])}</td>'
                         f'<td>{e(r["position"])}</td><td>{e(r["next_action"])}</td>'
                         f'<td class="mu">{e(r["next_action_at"])}</td></tr>')
        parts.append('</table>')

    parts.append('<h2>全部投递</h2><table>'
                 '<tr><th>ID</th><th>公司</th><th>岗位</th><th>批次/渠道</th>'
                 '<th>状态</th><th>停滞</th><th>下一步</th><th>投递日期</th></tr>')
    order = [s for s in STATUS_FLOW if s in by_status] + [s for s in STATUS_SIDE if s in by_status]
    for st in order:
        # 组内按卡住天数降序，最久没动静的排最前
        for r in sorted(by_status[st], key=lambda x: -(stall_days(x) or 0)):
            bg, fg = STATUS_COLOR.get(st, ("#F1EFE8", "#5F5E5A"))
            stall = stall_days(r)
            if st in TERMINAL or stall is None:
                stall_s = ""
            elif stall >= STALL_DAYS:
                stall_s = f'<span class="warn">{stall} 天</span>'
            elif stall >= STALL_DAYS - 7:
                stall_s = f'<span class="hot">{stall} 天</span>'
            else:
                stall_s = f'<span class="mu">{stall} 天</span>'
            meta = " · ".join(x for x in [r["batch"], r["channel"], r["city"]] if x)
            nx = e(r["next_action"])
            if r["next_action_at"]:
                nx += f'<div class="mu">{e(r["next_action_at"])}</div>'
            parts.append(
                f'<tr><td class="mu">{r["id"]}</td><td>{e(r["company"])}</td>'
                f'<td>{e(r["position"])}</td><td class="mu">{e(meta)}</td>'
                f'<td><span class="badge" style="background:{bg};color:{fg}">{e(st)}</span></td>'
                f'<td>{stall_s}</td><td>{nx}</td><td class="mu">{e(r["applied_at"])}</td></tr>')
    parts.append("</table>\n</div></body></html>")
    return "".join(parts)


# ---------------------------------------------------------------- 主入口

def seed_profile(conn, force: bool = False) -> int:
    """用 PROFILE_FIELDS 灌入资料表。force=True 时先清空（用于按种子文件重建）。"""
    if not PROFILE_FIELDS:
        return 0
    if force:
        conn.execute("DELETE FROM profile")
    now = now_str()
    conn.executemany(
        "INSERT OR REPLACE INTO profile"
        " (grp, grp_order, label, value, note, todo, ord, updated_at)"
        " VALUES (?,?,?,?,?,?,?,?)",
        [(g, go, lb, v, nt, td, od, now) for (g, go, lb, v, nt, td, od) in PROFILE_FIELDS],
    )
    conn.commit()
    return len(PROFILE_FIELDS)


def ensure_profile(conn, seed: bool = True) -> int:
    """确保 profile 表存在；表为空且有种子的场合自动灌入。返回本次写入条数。

    这样老库（本次升级前建的 qiuzhao.db）不用 --force 重建也能用上资料模块。
    """
    conn.execute(PROFILE_SCHEMA)
    n = conn.execute("SELECT COUNT(*) FROM profile").fetchone()[0]
    added = 0
    if seed and n == 0:
        added = seed_profile(conn)
    conn.commit()
    return added


def fetch_profile(conn, kw: str = "", only_todo: bool = False):
    sql = "SELECT * FROM profile"
    where, params = [], []
    if kw:
        where.append("(label LIKE ? OR value LIKE ? OR note LIKE ? OR grp LIKE ?)")
        params += [f"%{kw}%"] * 4
    if only_todo:
        where.append("todo = 1")
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY grp_order, ord, id"
    return list(conn.execute(sql, params))


def profile_grouped(rows):
    """按分组聚拢，保持顺序。"""
    groups, index = [], {}
    for r in rows:
        key = r["grp"]
        if key not in index:
            index[key] = {"grp": key, "items": []}
            groups.append(index[key])
        index[key]["items"].append(r)
    return groups


def profile_to_json(rows) -> str:
    data = []
    for g in profile_grouped(rows):
        data.append({
            "grp": g["grp"],
            "items": [
                {"id": r["id"], "label": r["label"], "value": r["value"],
                 "note": r["note"], "todo": bool(r["todo"])}
                for r in g["items"]
            ],
        })
    return json.dumps(data, ensure_ascii=False, indent=2)


def _cp_to_clipboard(text: str) -> bool:
    """尽量用系统剪贴板。失败返回 False，由调用方退化为打印。"""
    try:
        if os.name == "nt":
            p = subprocess.run("clip", input=text.encode("utf-16le"),
                               shell=True, timeout=6)
            return p.returncode == 0
        import shutil as _sh
        if _sh.which("pbcopy"):
            return subprocess.run(["pbcopy"], input=text.encode("utf-8"),
                                  timeout=6).returncode == 0
        if _sh.which("xclip"):
            return subprocess.run(["xclip", "-selection", "clipboard"],
                                  input=text.encode("utf-8"), timeout=6).returncode == 0
    except Exception:
        pass
    return False


def render_profile_html(rows) -> str:
    """生成自包含的「我的资料」卡片页（离线可开，点即复制）。"""
    groups = []
    for g in profile_grouped(rows):
        items = []
        for r in g["items"]:
            esc_l = _html.escape(r["label"])
            esc_v = _html.escape(r["value"] or "")
            esc_n = _html.escape(r["note"] or "")
            cls = "todo" if (r["todo"] or not r["value"]) else ""
            val = esc_v if r["value"] else "【待补】"
            note = f'<span class="note">↳ {esc_n}</span>' if esc_n else ""
            items.append(
                f'<div class="item {cls}" data-s="{(esc_l + " " + esc_v + " " + esc_n).lower()}">'
                f'<div class="lb">{esc_l}</div>'
                f'<div class="vl">{val}{note}</div></div>'
            )
        groups.append(
            f'<section class="grp"><h2>{_html.escape(g["grp"])}'
            f'<span class="cnt">{len(g["items"])} 项</span></h2>'
            f'<div class="grpbody">{"".join(items)}</div></section>'
        )
    todo_n = sum(1 for r in rows if r["todo"] or not r["value"])
    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>我的资料 · 网申速填</title>
<style>
*{{box-sizing:border-box}}
body{{margin:0;background:#fff;color:#1f2328;font:14px/1.6 "Microsoft YaHei","PingFang SC",Arial,sans-serif}}
header{{position:sticky;top:0;background:#fff;border-bottom:1px solid #e3e6ea;padding:14px 18px}}
h1{{margin:0 0 4px;font-size:17px;font-weight:600}}
.sub{{color:#8a929c;font-size:12px;margin-bottom:10px}}
input{{width:100%;max-width:460px;padding:8px 11px;border:1px solid #e3e6ea;border-radius:8px;font:inherit;outline:none}}
input:focus{{border-color:#c41e3a}}
main{{padding:14px 18px 60px;max-width:1100px}}
.grp{{border:1px solid #e3e6ea;border-radius:12px;margin-bottom:16px;overflow:hidden}}
h2{{margin:0;padding:10px 14px;background:#f6f7f9;font-size:14px;font-weight:600;display:flex;justify-content:space-between}}
.cnt{{color:#8a929c;font-weight:400;font-size:12px}}
.grpbody{{padding:6px 10px 10px}}
.item{{display:flex;gap:10px;padding:7px 4px;border-bottom:1px dashed #e3e6ea}}
.item:last-child{{border-bottom:none}}
.lb{{flex:0 0 128px;color:#5b6470;font-size:12.5px;padding-top:3px;word-break:break-all}}
.vl{{flex:1;min-width:0;white-space:pre-wrap;word-break:break-word;background:#f6f7f9;border:1px solid transparent;border-radius:7px;padding:6px 9px;cursor:pointer;font-size:13.5px}}
.vl:hover{{border-color:#c41e3a;background:#fff}}
.vl.flash{{background:#e8f6ee;border-color:#1a7f4b}}
.note{{display:block;color:#a8620a;font-size:11.5px;margin-top:4px}}
.todo .vl{{background:#fdeceb;color:#b3261e}}
#toast{{position:fixed;left:50%;bottom:26px;transform:translateX(-50%) translateY(14px);background:#1f2328;color:#fff;padding:9px 16px;border-radius:9px;font-size:13px;opacity:0;pointer-events:none;transition:.18s}}
#toast.on{{opacity:1;transform:translateX(-50%) translateY(0)}}
</style></head><body>
<header>
<h1>我的资料 · 网申速填</h1>
<div class="sub">共 {len(rows)} 项，其中 <b>{todo_n}</b> 项待补 · 点内容即复制 · 生成于 {today_str()}</div>
<input id="q" type="search" placeholder="搜索字段或内容，例如：单位性质 / SQL / 荣誉 …">
</header>
<main>{''.join(groups)}</main>
<div id="toast">已复制</div>
<script>
function cp(t,cb){{if(navigator.clipboard&&window.isSecureContext){{navigator.clipboard.writeText(t).then(function(){{cb(true)}},function(){{cb(fb(t))}})}}else{{cb(fb(t))}}}}
function fb(t){{try{{var a=document.createElement("textarea");a.value=t;a.style.position="fixed";a.style.left="-9999px";document.body.appendChild(a);a.select();var ok=document.execCommand("copy");document.body.removeChild(a);return ok}}catch(e){{return false}}}}
var to=document.getElementById("toast"),tm=null;
function toast(m){{to.textContent=m;to.classList.add("on");clearTimeout(tm);tm=setTimeout(function(){{to.classList.remove("on")}},1300)}}
document.querySelectorAll(".vl").forEach(function(el){{
  el.addEventListener("click",function(){{
    var t=el.textContent.split("↳")[0].trim();
    if(t==="【待补】"){{toast("此项待补");return}}
    cp(t,function(ok){{toast(ok?"已复制":"复制失败，请手动选择");if(ok){{el.classList.add("flash");setTimeout(function(){{el.classList.remove("flash")}},480)}}}});
  }});
}});
document.getElementById("q").addEventListener("input",function(e){{
  var k=e.target.value.trim().toLowerCase();
  document.querySelectorAll(".grp").forEach(function(box){{
    var n=0;
    box.querySelectorAll(".item").forEach(function(it){{
      var hit=!k||it.getAttribute("data-s").indexOf(k)!==-1;
      it.style.display=hit?"":"none";if(hit)n++;
    }});
    box.style.display=n?"":"none";
  }});
}});
</script></body></html>"""


def cmd_info(args):
    conn = connect()
    added = ensure_profile(conn)
    if added:
        print(f"（首次使用：已把资料字段写入数据库，共 {added} 条）\n")

    if getattr(args, "reseed", False):
        n = seed_profile(conn, force=True)
        print(f"已按 profile_data.py 重建资料字段：{n} 条")
        print("注意：你手动改过的内容会被种子文件覆盖。")
        return

    if getattr(args, "set", None):
        if "=" not in args.set:
            die('格式： qz info --set "字段名=内容"')
        label, value = args.set.split("=", 1)
        label, value = label.strip(), value.strip()
        hits = list(conn.execute("SELECT id, grp FROM profile WHERE label = ?", (label,)))
        if not hits:
            die(f"没找到字段「{label}」。用 qz info --todo 看待补项，或 qz info <关键词> 搜索。")
        if len(hits) > 1:
            grps = "、".join(h["grp"] for h in hits)
            die(f"「{label}」在同名分组里出现 {len(hits)} 次（{grps}），请换用带序号的完整字段名。")
        conn.execute("UPDATE profile SET value = ?, todo = ?, updated_at = ? WHERE id = ?",
                     (value, 0 if value else 1, now_str(), hits[0]["id"]))
        conn.commit()
        print(f"已更新：{label} = {value or '（清空，标记为待补）'}")
        return

    rows = fetch_profile(conn, getattr(args, "kw", "") or "", getattr(args, "todo", False))

    if getattr(args, "json", False):
        print(profile_to_json(rows))
        return

    if getattr(args, "export_html", False):
        ensure_dirs()
        p = OUT_DIR / f"我的资料_{today_str()}.html"
        p.write_text(render_profile_html(rows), encoding="utf-8")
        print(f"已导出：{p}")
        print("双击用浏览器打开，点任意内容即复制。断网可用。")
        return

    if getattr(args, "copy", None):
        key = args.copy.strip()
        rows = fetch_profile(conn, key)
        exact = [r for r in rows if r["label"] == key]
        pick = exact or rows
        if not pick:
            die(f"没有匹配「{key}」的字段。")
        if len(pick) > 1:
            print(f"「{key}」匹配到 {len(pick)} 项，没法确定你要哪条：")
            for r in pick[:20]:
                print(f"  [{r['grp']}] {r['label']}")
            die("请用更完整的字段名，或直接 qz info <关键词> 看全部。")
        r = pick[0]
        if not r["value"]:
            die(f"「{r['label']}」目前是待补状态，没有内容可复制。")
        if _cp_to_clipboard(r["value"]):
            print(f"已复制到剪贴板：{r['label']}")
            print("→ 直接到网页表单里 Ctrl+V")
        else:
            print(f"（剪贴板不可用，内容如下，请手动选择）\n\n{r['value']}")
        return

    if not rows:
        die("没有匹配的字段。")

    todo_n = sum(1 for r in rows if r["todo"] or not r["value"])
    head = "我的资料"
    if getattr(args, "kw", ""):
        head += f" —— 含「{args.kw}」"
    if getattr(args, "todo", False):
        head = "我的资料 —— 待补项"
    print(f"{head}：{len(rows)} 项" + (f"，其中 {todo_n} 项待补" if todo_n else ""))
    print(f"（改内容： qz info --set \"字段名=内容\"   复制： qz info --copy 字段名）\n")

    last = None
    for r in rows:
        if r["grp"] != last:
            print(f"【{r['grp']}】")
            last = r["grp"]
        mark = " ⚠" if (r["todo"] or not r["value"]) else ""
        val = r["value"] or "【待补】"
        val = trunc(val.replace("\n", " ／ "), 78)
        print(f"  {pad(r['label'], 22)}{val}{mark}")
        if r["note"]:
            print(f"  {'':22}↳ {trunc(r['note'], 74)}")
    if todo_n:
        print(f"\n还有 {todo_n} 项待补，补完用： qz info --set \"字段名=内容\"")


# 哪些命令在跑之前先做一次自动泡池子检查


def main():
    p = argparse.ArgumentParser(
        prog="qz", description="秋招投递记录本地工具",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""常用：
  qz init                       初始化
  qz add --bulk                 批量粘贴录入（一行一条）
  qz ls                         看板
  qz ls --due 3                 3 天内到期
  qz up 12 --to 已挂            推进状态（四档：流程中 / 泡池子 / 已挂 / 已放弃）
  qz next 12 "准备自我介绍" --at 09-29   设置/清除下一步
  qz set 12 岗位=数据分析师 城市=北京     改任意字段
  qz doctor                     数据体检（列出待补/时间倒置/待办过期）
  qz info --todo                我的资料：待补字段
  qz stall                      谁卡住 ≥21 天了（--apply 落库为泡池子）
  qz baseline                   响应基线：哪几家该催了（只读）
  qz stats                      四档分布与统计
  qz export --html --xlsx --ics --json  导出（json 可用 qz import 还原）
  qz import 备份.json            从备份恢复
  qz open 12                    打开该公司招聘系统
  qz show 12                    看单条详情与时间线
""")
    sub = p.add_subparsers(dest="cmd")

    s = sub.add_parser("init", help="初始化数据库")
    s.add_argument("--force", action="store_true", help="重建（会清空现有数据）")
    s.set_defaults(func=cmd_init)

    s = sub.add_parser("add", help="新增投递")
    s.add_argument("--bulk", action="store_true", help="批量粘贴录入")
    s.add_argument("--file", help="从文件读取批量内容")
    s.add_argument("--quick", action="store_true",
                   help="极简模式：一行只写公司（或 公司|岗位），其余用下面预设补齐")
    s.add_argument("--position", help="quick 模式默认岗位")
    s.add_argument("--batch", help="quick 模式默认批次，如 2027秋招")
    s.add_argument("--channel", help="quick 模式默认渠道，如 内推")
    s.add_argument("--city", help="quick 模式默认城市")
    s.add_argument("--status", help="quick 模式默认状态，默认 已投递")
    s.add_argument("--at", help="quick 模式默认投递日期，默认今天")
    s.set_defaults(func=cmd_add)

    s = sub.add_parser("ls", help="投递看板")
    s.add_argument("--status", help="只看某状态")
    s.add_argument("--batch", help="按批次过滤")
    s.add_argument("--kw", help="关键词搜索")
    s.add_argument("--days", type=int, help="只看最近 N 天投递的")
    s.add_argument("--since", help="只看某日及以后投递的，如 09-25")
    s.add_argument("--due", type=int, nargs="?", const=3, help="N 天内到期（默认 3）")
    s.add_argument("--all", action="store_true", help="含已放弃")
    s.set_defaults(func=cmd_ls)

    s = sub.add_parser("up", help="推进状态")
    s.add_argument("id", type=int)
    s.add_argument("--to", help="目标状态")
    s.add_argument("--at", help="日期（默认今天）")
    s.add_argument("--time", help="时间，如 14:00")
    s.add_argument("--note", help="备注")
    s.add_argument("--next", help="顺带设置下一步动作")
    s.add_argument("--at2", help="下一步的时间")
    s.add_argument("--keep-next", action="store_true",
                   help="保留下一步动作（默认推进状态时会自动清掉过期的下一步）")
    s.set_defaults(func=cmd_up)

    s = sub.add_parser("next", help="设置 / 清除某条投递的下一步动作")
    s.add_argument("id", type=int)
    s.add_argument("text", nargs="?", default=None,
                   help="下一步内容；留空则清除")
    s.add_argument("--at", help="下一步的时间，如 10-28")
    s.set_defaults(func=cmd_next)

    s = sub.add_parser("note", help="追加备注")
    s.add_argument("id", type=int)
    s.add_argument("text", nargs="+")
    s.set_defaults(func=cmd_note)

    s = sub.add_parser("set", help="改某条投递的任意字段（补岗位名、改城市、修日期）")
    s.add_argument("id", type=int)
    s.add_argument("kv", nargs="+",
                   help='字段=值，中文即可。如 岗位=数据分析师 城市=北京 投递日期=09-28')
    s.set_defaults(func=cmd_set)

    s = sub.add_parser("show", help="单条详情 + 时间线")
    s.add_argument("id", type=int)
    s.set_defaults(func=cmd_show)

    s = sub.add_parser("check", help="按招聘系统分组，列出该去哪查进度")
    s.add_argument("--stale", type=int, help="只显示静默 N 天以上的")
    s.add_argument("--all", action="store_true", help="含已放弃")
    s.set_defaults(func=cmd_check)

    s = sub.add_parser("stats", help="四档分布与统计")
    s.set_defaults(func=cmd_stats)

    s = sub.add_parser("stall",
                       help=f"把卡住 ≥{STALL_DAYS} 天的「流程中」标为「泡池子」（默认只预览）")
    s.add_argument("--apply", action="store_true", help="真的写入（默认只预览）")
    s.set_defaults(func=cmd_stall)

    s = sub.add_parser("doctor", help="数据体检：列出填得不全 / 时间倒置 / 待办过期等问题")
    s.set_defaults(func=cmd_doctor)

    s = sub.add_parser("export", help="导出 html / csv / ics / json")
    s.add_argument("--html", action="store_true")
    s.add_argument("--xlsx", action="store_true", help="导出 CSV（Excel 可直接打开）")
    s.add_argument("--ics", action="store_true")
    s.add_argument("--json", action="store_true", help="导出完整备份（含事件流，可被 import 还原）")
    s.set_defaults(func=cmd_export)

    s = sub.add_parser("import", help="从 JSON 备份还原投递记录")
    s.add_argument("file", help="JSON 文件路径")
    s.add_argument("--update", action="store_true",
                   help="已存在的记录也覆盖更新（默认只跳过、不覆盖）")
    s.add_argument("--dry-run", action="store_true", help="只预览，不写入")
    s.set_defaults(func=cmd_import)

    s = sub.add_parser("serve", help="启动本地网页界面（推荐用它录入）")
    s.add_argument("--port", type=int, default=8765)
    s.add_argument("--host", default="127.0.0.1",
                   help="默认只本机可访问；填 0.0.0.0 可让手机访问（有风险）")
    s.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
    s.add_argument("--stop", action="store_true",
                   help="停止在跑的服务（无窗口启动时靠它关）")
    s.set_defaults(func=cmd_serve)

    s = sub.add_parser("sites", help="列出预置的招聘系统入口")
    s.add_argument("company", nargs="?", help="按公司过滤")
    s.add_argument("--sync", action="store_true", help="把内置预置表里新增的入口补进数据库")
    s.set_defaults(func=cmd_sites)

    s = sub.add_parser("open", help="打开招聘系统页面（按 id / 按公司名 / 设入口）")
    s.add_argument("id", type=int, nargs="?", help="投递 id")
    s.add_argument("--company", help="按公司名查预置的系统入口")
    s.add_argument("--set-url", dest="set_url", metavar="网址",
                   help="给这条投递指定系统入口")
    s.add_argument("--print-only", action="store_true", help="只打印网址不打开")
    s.set_defaults(func=cmd_open)

    s = sub.add_parser("info", help="我的资料（详细简历字段：查 / 复制 / 改）")
    s.add_argument("kw", nargs="?", default="", help="按字段名或内容搜索")
    s.add_argument("--todo", action="store_true", help="只列待补项")
    s.add_argument("--copy", metavar="字段名", help="把某个字段的内容复制到剪贴板")
    s.add_argument("--set", metavar="字段=内容", help="修改某个字段的内容")
    s.add_argument("--json", action="store_true", help="输出 JSON")
    s.add_argument("--export-html", action="store_true", help="导出自包含的资料卡片页到 out\\")
    s.add_argument("--reseed", action="store_true", help="按 profile_data.py 重建（会覆盖手改内容）")
    s.set_defaults(func=cmd_info)

    s = sub.add_parser("baseline", help="响应时长基线：哪几家该催了（只读，不再写库）")
    s.add_argument("--list", dest="list_rules", action="store_true",
                   help="只打印基线表（含数据来源与置信度）")
    s.add_argument("--apply", action="store_true",
                   help="已停用；标泡池子请用 qz stall --apply")
    s.set_defaults(func=cmd_baseline)

    s = sub.add_parser("mail", help="从邮箱拉招聘邮件并匹配台账（默认只报告）")
    s.add_argument("--setup", action="store_true", help="配置邮箱（写入 data/mail.json）")
    s.add_argument("--test", action="store_true", help="只测连接、列文件夹")
    s.add_argument("--days", type=int, default=0, help="拉取最近多少天的邮件（默认 14）")
    s.add_argument("--apply", action="store_true", help="把识别出的状态变化写进台账")
    s.set_defaults(func=cmd_mail)

    args = p.parse_args()
    if not args.cmd:
        p.print_help()
        return
    # 说明：以前这里有个「跑 ls/stats 时顺手把静默超 14 天的标成泡池子」的自动动作，
    # 已移除。理由有两条：① 只读命令不该偷偷写库；② 那条 14 天的一刀切规则
    # 已被 qz baseline（节假日感知 + 分单位公开节点）取代，留着会互相打架。
    # 现在标记泡池子只有一个入口：qz baseline --apply
    args.func(args)


def _setup_console():
    """让 Windows 控制台按 UTF-8 收发中文。

    必须在任何输出之前调用：cmd 默认用 GBK，直接打印 UTF-8 会乱码。
    同时给 stdout/stderr 换 UTF-8 编码，避免个别字符编不出来。

    ⚠️ 还要处理「无窗口启动」：用 pythonw.exe 双击启动时，sys.stdout/stderr 是
    None，任何 print() 都会抛 AttributeError 把程序打死——而「双击图标、不弹
    cmd 窗口」正是靠 pythonw 实现的。所以这里先把它们接到日志文件上。
    """
    if sys.stdout is None or sys.stderr is None:
        try:
            ensure_dirs()
            log = open(APP_DIR / "data" / "serve.log", "a", encoding="utf-8",
                       errors="replace", buffering=1)
        except Exception:
            log = open(os.devnull, "w", encoding="utf-8")
        if sys.stdout is None:
            sys.stdout = log
        if sys.stderr is None:
            sys.stderr = log
        print("\n===== 启动 %s =====" % now_str())
    if os.name == "nt":
        try:
            import ctypes
            k = ctypes.windll.kernel32
            k.SetConsoleOutputCP(65001)
            k.SetConsoleCP(65001)
        except Exception:
            pass
    for stream in ("stdout", "stderr"):
        try:
            getattr(sys, stream).reconfigure(encoding="utf-8", errors="replace",
                                             line_buffering=True)
        except Exception:
            pass


if __name__ == "__main__":
    _setup_console()
    main()
