# -*- coding: utf-8 -*-
"""
개인 생활 플래너 백엔드 (Flask + SQLite)
- 일정 CRUD / 반복 전개(lazy occurrence) / 완료·미완료 사유 / 장소별 체크인 / 주간 통계
- 메시지 텍스트 -> Claude 파싱 -> 일정 후보 (데까르트 자동 제안은 검토 대기 큐)
학원 서버 docker 배포: --network host, 내부 포트 5558, Tailscale Funnel 10000.
"""
import json
import os
import sqlite3
import shutil
import subprocess
import secrets
import base64
import time
import threading
import datetime as dt
from functools import wraps

try:
    from zoneinfo import ZoneInfo
except ImportError:  # py<3.9 대비
    ZoneInfo = None

import requests
from flask import (Flask, request, jsonify, session, send_from_directory,
                   render_template, g)

BASE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE, "planner.db")
CONFIG_PATH = os.path.join(BASE, "config.json")

# ---- 상수 ---------------------------------------------------------------
PLACES = ["집", "데까르트", "수담자", "화천", "여주", "직접입력"]
# 데까르트 = 본인 운영 학원(안드로이드 자동읽기 대상). 나머지는 아이폰 직접입력.
AUTO_PLACE = "데까르트"

MISS_REASONS = [
    "시간부족", "급한일이생김", "컨디션·건강", "미루다가못함",
    "다른일정과겹침", "잊어버림", "외부요인", "직접입력",
]

RECUR_FREQS = ["none", "daily", "weekly", "monthly", "yearly"]
SCOPES = ["week", "month", "year"]


# ---- 설정 ---------------------------------------------------------------
def load_config():
    cfg = {
        "ui_password": "",
        "anthropic_api_key": "",
        "claude_model": "claude-haiku-4-5-20251001",
        "timezone": "Asia/Seoul",
        "port": 5558,
    }
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, "r", encoding="utf-8-sig") as f:
            cfg.update(json.load(f))
    return cfg


CONFIG = load_config()


def tz():
    if ZoneInfo:
        try:
            return ZoneInfo(CONFIG.get("timezone", "Asia/Seoul"))
        except Exception:
            return None
    return None


def today():
    return dt.datetime.now(tz()).date()


def now_iso():
    return dt.datetime.now(tz()).replace(microsecond=0).isoformat()


# ---- DB -----------------------------------------------------------------
def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


def close_db(e=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


SCHEMA = """
CREATE TABLE IF NOT EXISTS plans (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    title         TEXT NOT NULL,
    place         TEXT NOT NULL DEFAULT '직접입력',
    place_custom  TEXT,
    note          TEXT,
    scope         TEXT NOT NULL DEFAULT 'week',     -- week/month/year (계획 단위)
    start_date    TEXT NOT NULL,                     -- YYYY-MM-DD
    end_date      TEXT,                              -- 다일 일정(선택)
    start_time    TEXT,                              -- HH:MM (선택)
    end_time      TEXT,
    is_important  INTEGER NOT NULL DEFAULT 0,
    remind_min    INTEGER,                            -- 시작 전 알림(분). NULL=전역기본, -1=끔
    recur_freq    TEXT NOT NULL DEFAULT 'none',      -- none/daily/weekly/monthly/yearly
    recur_interval INTEGER NOT NULL DEFAULT 1,
    recur_byweekday TEXT,                            -- "0,2,4" (0=월 ... 6=일)
    recur_until   TEXT,                              -- YYYY-MM-DD (선택)
    source        TEXT NOT NULL DEFAULT 'manual',    -- manual/auto
    review_status TEXT NOT NULL DEFAULT 'confirmed', -- confirmed/pending/rejected
    source_text   TEXT,                              -- 자동생성 원본 메시지
    created_at    TEXT,
    updated_at    TEXT
);

CREATE TABLE IF NOT EXISTS occurrences (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    plan_id       INTEGER NOT NULL REFERENCES plans(id) ON DELETE CASCADE,
    occ_date      TEXT NOT NULL,                     -- YYYY-MM-DD
    status        TEXT NOT NULL DEFAULT 'pending',   -- pending/done/missed
    completed_at  TEXT,
    miss_category TEXT,
    miss_text     TEXT,
    UNIQUE(plan_id, occ_date)
);

CREATE TABLE IF NOT EXISTS daily_checkin (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    date          TEXT NOT NULL,
    place         TEXT NOT NULL,
    place_custom  TEXT,
    content       TEXT,
    is_none       INTEGER NOT NULL DEFAULT 0,
    updated_at    TEXT,
    UNIQUE(date, place)
);

CREATE TABLE IF NOT EXISTS annual_cycle (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    month         INTEGER NOT NULL,
    day           INTEGER NOT NULL,
    title         TEXT NOT NULL,
    note          TEXT,
    lead_days     INTEGER NOT NULL DEFAULT 0,
    active        INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS devices (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    name          TEXT,
    platform      TEXT,                              -- web/android/ios
    token         TEXT,
    created_at    TEXT
);

CREATE TABLE IF NOT EXISTS settings (
    key           TEXT PRIMARY KEY,
    value         TEXT
);

CREATE TABLE IF NOT EXISTS push_subscriptions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    endpoint      TEXT UNIQUE NOT NULL,
    p256dh        TEXT NOT NULL,
    auth          TEXT NOT NULL,
    platform      TEXT DEFAULT 'web',
    created_at    TEXT
);

CREATE INDEX IF NOT EXISTS idx_occ_date ON occurrences(occ_date);
CREATE INDEX IF NOT EXISTS idx_plan_start ON plans(start_date);
"""


def init_db():
    db = sqlite3.connect(DB_PATH)
    db.executescript(SCHEMA)
    # 기존 DB 마이그레이션 — 없는 컬럼 추가
    cols = {r[1] for r in db.execute("PRAGMA table_info(plans)").fetchall()}
    if "remind_min" not in cols:
        db.execute("ALTER TABLE plans ADD COLUMN remind_min INTEGER")
    db.commit()
    db.close()


def db_conn():
    """요청 컨텍스트 밖(스케줄러 스레드 등)에서 쓰는 독립 커넥션."""
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    return c


def get_setting(key, default=None):
    c = db_conn()
    r = c.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    c.close()
    return r["value"] if r else default


def set_setting(key, value):
    c = db_conn()
    c.execute("INSERT INTO settings(key,value) VALUES(?,?) "
              "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))
    c.commit()
    c.close()


# ---- 인증 ---------------------------------------------------------------
def password_required():
    return bool(CONFIG.get("ui_password"))


def login_ok():
    return (not password_required()) or session.get("auth") is True


def require_login(f):
    @wraps(f)
    def wrapper(*a, **k):
        if not login_ok():
            return jsonify({"error": "unauthorized"}), 401
        return f(*a, **k)
    return wrapper


# ---- 반복 전개 ----------------------------------------------------------
def _parse_weekdays(s):
    if not s:
        return None
    try:
        return {int(x) for x in str(s).split(",") if x != ""}
    except ValueError:
        return None


def expand_plan(plan, frm, to):
    """plan 의 발생 날짜(date 객체) 목록을 [frm, to] 범위에서 계산."""
    start = dt.date.fromisoformat(plan["start_date"])
    freq = plan["recur_freq"] or "none"
    until = dt.date.fromisoformat(plan["recur_until"]) if plan["recur_until"] else None
    interval = max(1, plan["recur_interval"] or 1)

    out = []
    if freq == "none":
        if frm <= start <= to:
            out.append(start)
        return out

    weekdays = _parse_weekdays(plan["recur_byweekday"]) if freq == "weekly" else None
    # 순회 범위: max(start, frm) ~ min(until|to, to)
    cur = max(start, frm)
    end = to if (until is None or to <= until) else until
    one = dt.timedelta(days=1)
    while cur <= end:
        ok = False
        if freq == "daily":
            ok = (cur - start).days % interval == 0
        elif freq == "weekly":
            wk_start = start - dt.timedelta(days=start.weekday())
            wk_cur = cur - dt.timedelta(days=cur.weekday())
            weeks = (wk_cur - wk_start).days // 7
            if weeks % interval == 0:
                if weekdays:
                    ok = cur.weekday() in weekdays
                else:
                    ok = cur.weekday() == start.weekday()
        elif freq == "monthly":
            months = (cur.year - start.year) * 12 + (cur.month - start.month)
            ok = cur.day == start.day and months >= 0 and months % interval == 0
        elif freq == "yearly":
            years = cur.year - start.year
            ok = (cur.month == start.month and cur.day == start.day
                  and years >= 0 and years % interval == 0)
        if ok:
            out.append(cur)
        cur += one
    return out


def plan_to_dict(row):
    d = dict(row)
    d["is_important"] = bool(d["is_important"])
    return d


# ---- Flask 앱 -----------------------------------------------------------
app = Flask(__name__, template_folder="templates", static_folder="static")
def ensure_secret_key():
    """secret_key 고정 — 재시작해도 세션 유지(매번 랜덤이면 재시작 시 로그인 풀림)."""
    sk = CONFIG.get("secret_key")
    if not sk:
        sk = secrets.token_hex(32)
        try:
            cfg = {}
            if os.path.exists(CONFIG_PATH):
                with open(CONFIG_PATH, "r", encoding="utf-8-sig") as f:
                    cfg = json.load(f)
            cfg["secret_key"] = sk
            with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                json.dump(cfg, f, ensure_ascii=False, indent=2)
            CONFIG["secret_key"] = sk
        except Exception as e:
            print("[secret_key] config 저장 실패:", e)
    return sk


app.secret_key = ensure_secret_key()
app.permanent_session_lifetime = dt.timedelta(days=30)
app.teardown_appcontext(close_db)


@app.after_request
def allow_iframe(resp):
    # 다른 대시보드(youtube 허브 등)에 iframe 임베드 허용
    resp.headers.pop("X-Frame-Options", None)
    return resp


# ---- 페이지/메타/인증 ---------------------------------------------------
@app.route("/")
def index():
    return render_template("index.html")


@app.route("/manifest.webmanifest")
def manifest():
    return send_from_directory("static", "manifest.webmanifest",
                               mimetype="application/manifest+json")


@app.route("/sw.js")
def service_worker():
    return send_from_directory("static", "sw.js", mimetype="application/javascript")


@app.route("/api/meta")
def api_meta():
    return jsonify({
        "places": PLACES,
        "auto_place": AUTO_PLACE,
        "miss_reasons": MISS_REASONS,
        "recur_freqs": RECUR_FREQS,
        "scopes": SCOPES,
        "password_required": password_required(),
        "authed": login_ok(),
        "claude_enabled": bool(CONFIG.get("anthropic_api_key") or _claude_cli()),
        "today": today().isoformat(),
    })


@app.route("/api/login", methods=["POST"])
def api_login():
    data = request.get_json(force=True, silent=True) or {}
    if not password_required():
        session.permanent = True
        session["auth"] = True
        return jsonify({"ok": True})
    if data.get("password") == CONFIG.get("ui_password"):
        session.permanent = True   # 30일 유지
        session["auth"] = True
        return jsonify({"ok": True})
    return jsonify({"ok": False, "error": "wrong password"}), 403


@app.route("/api/logout", methods=["POST"])
def api_logout():
    session.pop("auth", None)
    return jsonify({"ok": True})


# ---- 일정 CRUD ----------------------------------------------------------
PLAN_FIELDS = ["title", "place", "place_custom", "note", "scope", "start_date",
               "end_date", "start_time", "end_time", "is_important", "remind_min",
               "recur_freq", "recur_interval", "recur_byweekday", "recur_until",
               "source", "review_status", "source_text"]


def _clean_plan_payload(data):
    p = {k: data.get(k) for k in PLAN_FIELDS if k in data}
    if "is_important" in p:
        p["is_important"] = 1 if p["is_important"] else 0
    if "remind_min" in p:
        v = p["remind_min"]
        p["remind_min"] = None if v in (None, "") else int(v)
    if p.get("place") and p["place"] not in PLACES:
        p["place"] = "직접입력"
    if p.get("recur_freq") and p["recur_freq"] not in RECUR_FREQS:
        p["recur_freq"] = "none"
    if p.get("scope") and p["scope"] not in SCOPES:
        p["scope"] = "week"
    return p


@app.route("/api/plans", methods=["GET"])
@require_login
def api_plans_list():
    """범위 내 발생(occurrence)을 전개해 반환.
       ?from=YYYY-MM-DD&to=YYYY-MM-DD [&place=][&include_pending=1]"""
    frm = request.args.get("from")
    to = request.args.get("to")
    if not frm or not to:
        # 기본: 이번 주(월~일)
        t = today()
        frm = (t - dt.timedelta(days=t.weekday())).isoformat()
        to = (dt.date.fromisoformat(frm) + dt.timedelta(days=6)).isoformat()
    frm_d, to_d = dt.date.fromisoformat(frm), dt.date.fromisoformat(to)
    place = request.args.get("place")
    # pending(검토대기) 자동제안 포함 여부
    include_pending = request.args.get("include_pending", "1") == "1"

    db = get_db()
    q = "SELECT * FROM plans WHERE 1=1"
    args = []
    if place:
        q += " AND place=?"
        args.append(place)
    if not include_pending:
        q += " AND review_status!='pending'"
    q += " AND review_status!='rejected'"
    plans = db.execute(q, args).fetchall()

    # occurrence 상태 미리 로드
    occ_rows = db.execute(
        "SELECT * FROM occurrences WHERE occ_date BETWEEN ? AND ?",
        (frm, to)).fetchall()
    occ_map = {(o["plan_id"], o["occ_date"]): o for o in occ_rows}

    items = []
    for pr in plans:
        for d in expand_plan(pr, frm_d, to_d):
            ds = d.isoformat()
            o = occ_map.get((pr["id"], ds))
            items.append({
                "plan_id": pr["id"],
                "date": ds,
                "title": pr["title"],
                "place": pr["place"],
                "place_custom": pr["place_custom"],
                "note": pr["note"],
                "start_time": pr["start_time"],
                "end_time": pr["end_time"],
                "is_important": bool(pr["is_important"]),
                "remind_min": pr["remind_min"],
                "recur_freq": pr["recur_freq"],
                "scope": pr["scope"],
                "source": pr["source"],
                "review_status": pr["review_status"],
                "status": o["status"] if o else "pending",
                "miss_category": o["miss_category"] if o else None,
                "miss_text": o["miss_text"] if o else None,
            })
    items.sort(key=lambda x: (x["date"], x["start_time"] or "99:99"))
    return jsonify({"items": items, "from": frm, "to": to})


@app.route("/api/plans", methods=["POST"])
@require_login
def api_plans_create():
    data = request.get_json(force=True, silent=True) or {}
    p = _clean_plan_payload(data)
    if not p.get("title") or not p.get("start_date"):
        return jsonify({"error": "title, start_date 필수"}), 400
    p.setdefault("place", "직접입력")
    p.setdefault("scope", "week")
    p.setdefault("recur_freq", "none")
    p["created_at"] = p["updated_at"] = now_iso()
    cols = ",".join(p.keys())
    ph = ",".join("?" * len(p))
    db = get_db()
    cur = db.execute(f"INSERT INTO plans ({cols}) VALUES ({ph})",
                     list(p.values()))
    db.commit()
    return jsonify({"id": cur.lastrowid}), 201


@app.route("/api/plans/<int:pid>", methods=["PUT"])
@require_login
def api_plans_update(pid):
    data = request.get_json(force=True, silent=True) or {}
    p = _clean_plan_payload(data)
    if not p:
        return jsonify({"error": "no fields"}), 400
    p["updated_at"] = now_iso()
    sets = ",".join(f"{k}=?" for k in p)
    db = get_db()
    db.execute(f"UPDATE plans SET {sets} WHERE id=?", list(p.values()) + [pid])
    db.commit()
    return jsonify({"ok": True})


@app.route("/api/plans/<int:pid>", methods=["DELETE"])
@require_login
def api_plans_delete(pid):
    db = get_db()
    db.execute("DELETE FROM plans WHERE id=?", (pid,))
    db.commit()
    return jsonify({"ok": True})


@app.route("/api/plans/<int:pid>/review", methods=["POST"])
@require_login
def api_plans_review(pid):
    """데까르트 자동 제안 승인/거절. {action: confirm|reject}"""
    data = request.get_json(force=True, silent=True) or {}
    action = data.get("action")
    status = {"confirm": "confirmed", "reject": "rejected"}.get(action)
    if not status:
        return jsonify({"error": "action must be confirm/reject"}), 400
    db = get_db()
    db.execute("UPDATE plans SET review_status=?, updated_at=? WHERE id=?",
               (status, now_iso(), pid))
    db.commit()
    return jsonify({"ok": True, "review_status": status})


@app.route("/api/review-queue")
@require_login
def api_review_queue():
    """검토 대기 중인 자동 제안 목록 (안드/데까르트)."""
    db = get_db()
    rows = db.execute(
        "SELECT * FROM plans WHERE review_status='pending' ORDER BY created_at DESC"
    ).fetchall()
    return jsonify({"items": [plan_to_dict(r) for r in rows]})


# ---- 완료/미완료 사유 ---------------------------------------------------
@app.route("/api/occurrences/status", methods=["POST"])
@require_login
def api_occ_status():
    """{plan_id, date, status: done|missed|pending, miss_category, miss_text}"""
    data = request.get_json(force=True, silent=True) or {}
    pid, date_s, status = data.get("plan_id"), data.get("date"), data.get("status")
    if not pid or not date_s or status not in ("done", "missed", "pending"):
        return jsonify({"error": "plan_id, date, status 필요"}), 400
    miss_cat = data.get("miss_category") if status == "missed" else None
    miss_txt = data.get("miss_text") if status == "missed" else None
    if status == "missed" and not miss_cat:
        return jsonify({"error": "미완료 사유(miss_category) 필요"}), 400
    completed = now_iso() if status == "done" else None
    db = get_db()
    db.execute("""
        INSERT INTO occurrences (plan_id, occ_date, status, completed_at, miss_category, miss_text)
        VALUES (?,?,?,?,?,?)
        ON CONFLICT(plan_id, occ_date) DO UPDATE SET
            status=excluded.status, completed_at=excluded.completed_at,
            miss_category=excluded.miss_category, miss_text=excluded.miss_text
    """, (pid, date_s, status, completed, miss_cat, miss_txt))
    db.commit()
    return jsonify({"ok": True})


# ---- 장소별 강제 체크인 -------------------------------------------------
@app.route("/api/checkin", methods=["GET"])
@require_login
def api_checkin_get():
    """장소별 일일 점검 — 그날 장소별 실제 일정 목록 + '없음' 플래그.
       장소마다 일정이 1건 이상 있거나 '없음' 체크면 점검 완료."""
    date_s = request.args.get("date") or today().isoformat()
    d = dt.date.fromisoformat(date_s)
    db = get_db()
    rows = db.execute("SELECT * FROM daily_checkin WHERE date=?", (date_s,)).fetchall()
    existing = {r["place"]: dict(r) for r in rows}

    # 그날 확정 일정을 장소별로 묶기
    plans = db.execute("SELECT * FROM plans WHERE review_status='confirmed'").fetchall()
    occ_rows = db.execute("SELECT * FROM occurrences WHERE occ_date=?", (date_s,)).fetchall()
    occ_map = {(o["plan_id"], o["occ_date"]): o for o in occ_rows}
    plans_by_place = {p: [] for p in PLACES}
    for pr in plans:
        if expand_plan(pr, d, d):
            o = occ_map.get((pr["id"], date_s))
            plans_by_place.setdefault(pr["place"], []).append({
                "plan_id": pr["id"], "title": pr["title"],
                "start_time": pr["start_time"], "place_custom": pr["place_custom"],
                "is_important": bool(pr["is_important"]),
                "status": o["status"] if o else "pending",
            })
    for lst in plans_by_place.values():
        lst.sort(key=lambda x: x["start_time"] or "99:99")

    result = []
    for place in PLACES:
        r = existing.get(place)
        pls = plans_by_place.get(place, [])
        result.append({
            "place": place,
            "plans": pls,
            "is_none": bool(r["is_none"]) if r else False,
            "place_custom": r["place_custom"] if r else "",
        })
    complete = all((c["is_none"] or c["plans"]) for c in result)
    return jsonify({"date": date_s, "items": result, "complete": complete})


@app.route("/api/checkin", methods=["POST"])
@require_login
def api_checkin_post():
    """{date, place, content, is_none, place_custom}"""
    data = request.get_json(force=True, silent=True) or {}
    date_s, place = data.get("date"), data.get("place")
    if not date_s or place not in PLACES:
        return jsonify({"error": "date, place 필요"}), 400
    db = get_db()
    db.execute("""
        INSERT INTO daily_checkin (date, place, content, is_none, place_custom, updated_at)
        VALUES (?,?,?,?,?,?)
        ON CONFLICT(date, place) DO UPDATE SET
            content=excluded.content, is_none=excluded.is_none,
            place_custom=excluded.place_custom, updated_at=excluded.updated_at
    """, (date_s, place, data.get("content", ""),
          1 if data.get("is_none") else 0, data.get("place_custom", ""), now_iso()))
    db.commit()
    return jsonify({"ok": True})


# ---- 통계 ---------------------------------------------------------------
@app.route("/api/stats")
@require_login
def api_stats():
    """?from=&to= 기간 완료율 + 장소별/사유별 집계 (기본 이번 주)."""
    frm = request.args.get("from")
    to = request.args.get("to")
    if not frm or not to:
        t = today()
        frm = (t - dt.timedelta(days=t.weekday())).isoformat()
        to = (dt.date.fromisoformat(frm) + dt.timedelta(days=6)).isoformat()
    frm_d, to_d = dt.date.fromisoformat(frm), dt.date.fromisoformat(to)
    db = get_db()
    plans = db.execute(
        "SELECT * FROM plans WHERE review_status='confirmed'").fetchall()
    occ_rows = db.execute(
        "SELECT * FROM occurrences WHERE occ_date BETWEEN ? AND ?",
        (frm, to)).fetchall()
    occ_map = {(o["plan_id"], o["occ_date"]): o for o in occ_rows}

    total = done = missed = pending = 0
    by_place = {p: {"total": 0, "done": 0, "missed": 0} for p in PLACES}  # 모든 장소 표시
    by_reason = {}            # {사유: 개수}
    reason_items = {}         # {사유: [{title,date,place,miss_text}, ...]}
    tdy = today()
    for pr in plans:
        for d in expand_plan(pr, frm_d, to_d):
            total += 1
            o = occ_map.get((pr["id"], d.isoformat()))
            st = o["status"] if o else "pending"
            # 지난 날짜인데 완료 안 했으면 미완료로 집계 (기한 지남)
            if st == "pending" and d < tdy:
                st = "missed"
            bp = by_place.setdefault(pr["place"], {"total": 0, "done": 0, "missed": 0})
            bp["total"] += 1
            if st == "done":
                done += 1
                bp["done"] += 1
            elif st == "missed":
                missed += 1
                bp["missed"] += 1
                cat = (o["miss_category"] if o and o["miss_category"] else "미입력")
                by_reason[cat] = by_reason.get(cat, 0) + 1
                reason_items.setdefault(cat, []).append({
                    "title": pr["title"], "date": d.isoformat(), "place": pr["place"],
                    "miss_text": (o["miss_text"] if o else None),
                })
            else:
                pending += 1
    rate = round(done / total * 100, 1) if total else 0.0
    return jsonify({
        "from": frm, "to": to,
        "total": total, "done": done, "missed": missed, "pending": pending,
        "completion_rate": rate,
        "by_place": by_place, "by_reason": by_reason, "reason_items": reason_items,
    })


# ---- 메시지 -> Claude 파싱 ----------------------------------------------
PARSE_SYSTEM = """너는 한국어 일정 텍스트를 구조화하는 도우미야.
입력된 메시지(카톡/문자/메모 등)에서 일정을 뽑아 JSON 배열로만 답해.
각 원소 형식:
{"title": "간단한 제목", "start_date": "YYYY-MM-DD", "start_time": "HH:MM" 또는 null,
 "end_time": "HH:MM" 또는 null, "place": "{places}",
 "is_important": true/false, "note": "부가설명 또는 빈 문자열",
 "recur_freq": "none|daily|weekly|monthly|yearly"}
규칙:
- 오늘 날짜는 {today} (요일 포함). "다음주 화요일", "이번 금요일" 같은 표현을 실제 날짜로 환산.
- 장소가 분명치 않으면 place는 "직접입력".
- 일정이 아니면(잡담 등) 빈 배열 [] 반환.
- 설명/코드블록 없이 순수 JSON 배열만 출력."""


def _claude_cli():
    """claude CLI 실행 경로 (config 우선, 없으면 PATH 탐색)."""
    return CONFIG.get("claude_cli_path") or shutil.which("claude")


def _build_sys():
    t = today()
    weekday_kr = ["월", "화", "수", "목", "금", "토", "일"][t.weekday()]
    return (PARSE_SYSTEM
            .replace("{today}", f"{t.isoformat()}({weekday_kr})")
            .replace("{places}", "|".join(PLACES)))


def _extract_json_array(out):
    out = (out or "").strip()
    if out.startswith("```"):
        out = out.strip("`")
        if out.startswith("json"):
            out = out[4:]
    parsed = json.loads(out)
    if isinstance(parsed, dict):
        parsed = [parsed]
    return parsed


def _parse_via_api(text, sys):
    """anthropic_api_key 가 설정된 경우 직접 API 호출."""
    key = CONFIG.get("anthropic_api_key")
    r = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={"x-api-key": key, "anthropic-version": "2023-06-01",
                 "content-type": "application/json"},
        json={"model": CONFIG.get("claude_model", "claude-haiku-4-5-20251001"),
              "max_tokens": 2000, "system": sys,
              "messages": [{"role": "user", "content": text}]},
        timeout=60,
    )
    r.raise_for_status()
    body = r.json()
    out = "".join(b.get("text", "") for b in body.get("content", []))
    return _extract_json_array(out)


def _parse_file_via_cli(path):
    """이미지/PDF 파일을 claude CLI(Read 도구)로 읽어 일정 추출. Max 구독 사용."""
    cli = _claude_cli()
    if not cli:
        raise RuntimeError("claude CLI 를 찾을 수 없음")
    sys = _build_sys()
    prompt = (sys + "\n\n아래 파일을 읽어줘(이미지·스캔·사진이면 글자를 인식/OCR 해서). "
              "보이는 일정을 모두 추출해. 파일 경로: " + path +
              "\n설명 없이 순수 JSON 배열만 출력.")
    model = CONFIG.get("vision_model") or CONFIG.get("claude_model", "claude-haiku-4-5-20251001")
    proc = subprocess.run(
        [cli, "-p", "--output-format", "json", "--model", model, "--allowedTools", "Read"],
        input=prompt, capture_output=True, text=True, encoding="utf-8",
        timeout=int(CONFIG.get("cli_timeout_file", 240)),
    )
    if proc.returncode != 0:
        raise RuntimeError("claude CLI 오류: " + (proc.stderr or proc.stdout or "")[:300])
    outer = json.loads(proc.stdout)
    if outer.get("is_error"):
        raise RuntimeError("claude CLI: " + str(outer.get("result", ""))[:300])
    return _extract_json_array(outer.get("result", ""))


def claude_parse_file(path):
    """파일 -> 일정 후보. (candidates, error)"""
    try:
        return _parse_file_via_cli(path), None
    except Exception as e:
        return None, str(e)


def _parse_via_cli(text, sys):
    """API 키가 없으면 Claude Code CLI(`claude -p`)로 파싱 — Max 구독 사용, 추가 비용 없음.
       (youtube-uploader 필기TEST 와 동일한 패턴)"""
    cli = _claude_cli()
    if not cli:
        raise RuntimeError("claude CLI 를 찾을 수 없음 (PATH 또는 config.claude_cli_path)")
    prompt = sys + "\n\n메시지:\n" + text
    model = CONFIG.get("claude_model", "claude-haiku-4-5-20251001")
    proc = subprocess.run(
        [cli, "-p", "--output-format", "json", "--model", model],
        input=prompt, capture_output=True, text=True, encoding="utf-8",
        timeout=int(CONFIG.get("cli_timeout", 120)),
    )
    if proc.returncode != 0:
        raise RuntimeError("claude CLI 오류: " + (proc.stderr or proc.stdout or "")[:300])
    outer = json.loads(proc.stdout)
    if outer.get("is_error"):
        raise RuntimeError("claude CLI: " + str(outer.get("result", ""))[:300])
    return _extract_json_array(outer.get("result", ""))


def claude_parse(text):
    """메시지 텍스트 -> 일정 후보 리스트. (candidates, error)
       anthropic_api_key 있으면 API, 없으면 claude CLI(Max 구독) 사용."""
    sys = _build_sys()
    try:
        if CONFIG.get("anthropic_api_key"):
            return _parse_via_api(text, sys), None
        return _parse_via_cli(text, sys), None
    except Exception as e:
        return None, str(e)


@app.route("/api/parse", methods=["POST"])
@require_login
def api_parse():
    """메시지 텍스트 -> 일정 후보. {text, save: bool, source: manual|auto}
       save=true 면 바로 DB 저장(auto 면 review_status=pending)."""
    data = request.get_json(force=True, silent=True) or {}
    text = (data.get("text") or "").strip()
    if not text:
        return jsonify({"error": "text 필요"}), 400
    candidates, err = claude_parse(text)
    if err:
        return jsonify({"error": err}), 502

    if not data.get("save"):
        return jsonify({"candidates": candidates})

    source = data.get("source", "manual")
    saved, review = _save_candidates(candidates, source, text)
    return jsonify({"candidates": candidates, "saved": saved,
                    "saved_ids": [s["id"] for s in saved], "review_status": review})


import tempfile

ALLOWED_UPLOAD = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".pdf", ".heic"}


@app.route("/share-target", methods=["POST"])
def share_target():
    """PWA 공유 대상 — 갤러리/카톡에서 '공유 -> 플래너'로 보낸 사진/PDF/텍스트 처리.
       파싱해서 바로 등록(manual) 후 결과 페이지로."""
    if not login_ok():
        return _share_result_page("로그인이 필요해. 앱을 먼저 열어 로그인해줘.", [])
    results = []
    shared_text = (request.form.get("text") or request.form.get("title") or "").strip()
    if shared_text:
        cands, err = claude_parse(shared_text)
        if not err and cands:
            _save_candidates(cands, "manual", shared_text)
            results += cands
    for f in request.files.getlist("files"):
        if not f or not f.filename:
            continue
        ext = os.path.splitext(f.filename)[1].lower()
        if ext not in ALLOWED_UPLOAD:
            continue
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=ext, dir="/tmp"
                                          if os.path.isdir("/tmp") else None)
        try:
            f.save(tmp.name)
            tmp.close()
            cands, err = claude_parse_file(tmp.name)
            if not err and cands:
                _save_candidates(cands, "manual", "[공유] " + f.filename)
                results += cands
        finally:
            try:
                os.unlink(tmp.name)
            except OSError:
                pass
    msg = ("%d건의 일정을 등록했어!" % len(results)) if results else \
          "일정을 못 찾았어. 다른 사진이나 텍스트로 다시 시도해줘."
    return _share_result_page(msg, results)


def _share_result_page(msg, items):
    rows = "".join(
        "<li>%s · %s %s</li>" % ((c.get("title") or "?"),
                                 (c.get("start_date") or "?"), (c.get("start_time") or ""))
        for c in items)
    return ("""<!doctype html><html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>body{font-family:-apple-system,sans-serif;background:#f4f6fb;color:#1f2433;padding:20px}
.box{background:#fff;border-radius:16px;padding:24px;max-width:420px;margin:30px auto;
box-shadow:0 1px 4px rgba(0,0,0,.1)}a{display:inline-block;margin-top:16px;background:#6366f1;
color:#fff;padding:12px 24px;border-radius:10px;text-decoration:none}ul{text-align:left}</style>
</head><body><div class="box"><h2>📅 공유 완료</h2><p>%s</p><ul>%s</ul>
<a href="/">플래너 열기</a></div></body></html>""" % (msg, rows))


@app.route("/api/parse-file", methods=["POST"])
@require_login
def api_parse_file():
    """이미지/PDF 업로드 -> claude OCR -> 일정 후보.
       multipart form: file, save(0/1), source(manual|auto)."""
    f = request.files.get("file")
    if not f or not f.filename:
        return jsonify({"error": "file 필요"}), 400
    ext = os.path.splitext(f.filename)[1].lower()
    if ext not in ALLOWED_UPLOAD:
        return jsonify({"error": "지원 형식: 이미지(png/jpg/gif/webp/heic) 또는 pdf"}), 400
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=ext, dir="/tmp"
                                      if os.path.isdir("/tmp") else None)
    try:
        f.save(tmp.name)
        tmp.close()
        candidates, err = claude_parse_file(tmp.name)
        if err:
            return jsonify({"error": err}), 502
        save = request.form.get("save") in ("1", "true", "True")
        if not save:
            return jsonify({"candidates": candidates})
        source = request.form.get("source", "manual")
        saved, review = _save_candidates(candidates, source, "[파일] " + f.filename)
        return jsonify({"candidates": candidates, "saved": saved,
                        "saved_ids": [s["id"] for s in saved], "review_status": review})
    finally:
        try:
            os.unlink(tmp.name)
        except OSError:
            pass


def _save_candidates(candidates, source, text):
    """파싱된 일정 후보를 DB에 저장. auto 면 검토대기(pending).
       반환: (저장된 plan dict 목록, review_status)."""
    review = "pending" if source == "auto" else "confirmed"
    db = get_db()
    saved = []
    for c in candidates:
        p = _clean_plan_payload(c)
        if not p.get("title") or not p.get("start_date"):
            continue
        p.setdefault("place", "직접입력")
        p.setdefault("scope", "week")
        p.setdefault("recur_freq", "none")
        p["source"] = source
        p["review_status"] = review
        p["source_text"] = text
        p["created_at"] = p["updated_at"] = now_iso()
        cols = ",".join(p.keys())
        ph = ",".join("?" * len(p))
        cur = db.execute(f"INSERT INTO plans ({cols}) VALUES ({ph})", list(p.values()))
        row = db.execute("SELECT * FROM plans WHERE id=?", (cur.lastrowid,)).fetchone()
        saved.append(plan_to_dict(row))
    db.commit()
    return saved, review


def get_ingest_token():
    """안드로이드 자동읽기용 수신 토큰 (settings 에 저장, 없으면 생성)."""
    t = get_setting("ingest_token")
    if not t:
        t = secrets.token_urlsafe(24)
        set_setting("ingest_token", t)
    return t


@app.route("/api/ingest-token", methods=["GET"])
@require_login
def api_ingest_token():
    return jsonify({"token": get_ingest_token()})


def get_ingest_excludes():
    """자동읽기 제외 키워드 목록 (방 이름/문구). settings 에 JSON 배열로 저장."""
    raw = get_setting("ingest_excludes", "[]")
    try:
        v = json.loads(raw)
        return [str(x).strip() for x in v if str(x).strip()]
    except Exception:
        return []


@app.route("/api/ingest-excludes", methods=["GET"])
@require_login
def api_ingest_excludes_get():
    return jsonify({"excludes": get_ingest_excludes()})


@app.route("/api/ingest-excludes", methods=["POST"])
@require_login
def api_ingest_excludes_set():
    data = request.get_json(force=True, silent=True) or {}
    items = data.get("excludes", [])
    if not isinstance(items, list):
        return jsonify({"error": "excludes must be a list"}), 400
    cleaned = []
    for x in items:
        s = str(x).strip()
        if s and s not in cleaned:
            cleaned.append(s)
    set_setting("ingest_excludes", json.dumps(cleaned, ensure_ascii=False))
    return jsonify({"ok": True, "excludes": cleaned})


@app.route("/api/ingest", methods=["POST"])
def api_ingest():
    """안드로이드(또는 자동화 앱)가 알림 텍스트를 보내는 수신구. 토큰 인증.
       {token, text, place?} -> claude 파싱 -> 데까르트 자동 제안(검토대기)."""
    data = request.get_json(force=True, silent=True) or {}
    token = (request.headers.get("X-Ingest-Token") or data.get("token")
             or request.args.get("token"))
    if token != get_ingest_token():
        return jsonify({"error": "invalid token"}), 403
    # text 는 JSON 필드 또는 본문 그대로(plain text) 둘 다 허용 (MacroDroid 친화)
    text = (data.get("text") or "").strip()
    if not text:
        text = (request.get_data(as_text=True) or "").strip()
    if not text:
        return jsonify({"error": "text 필요"}), 400
    # 제외 키워드(방 이름/문구) 필터 — 포함되면 무시(claude 호출도 안 함)
    low = text.lower()
    for kw in get_ingest_excludes():
        if kw and kw.lower() in low:
            return jsonify({"ok": True, "skipped": True, "reason": "excluded:" + kw,
                            "saved_ids": []})
    candidates, err = claude_parse(text)
    if err:
        return jsonify({"error": err}), 502
    # 자동읽기 출처는 기본 데까르트, 검토대기 큐로
    forced_place = data.get("place", AUTO_PLACE)
    for c in candidates:
        if not c.get("place") or c.get("place") == "직접입력":
            c["place"] = forced_place
    saved, review = _save_candidates(candidates, "auto", text)
    return jsonify({"ok": True, "candidates": candidates,
                    "saved_ids": [s["id"] for s in saved]})


# ---- 연간 사이클 (P4 기초) ----------------------------------------------
@app.route("/api/annual", methods=["GET"])
@require_login
def api_annual_get():
    db = get_db()
    rows = db.execute(
        "SELECT * FROM annual_cycle WHERE active=1 ORDER BY month, day").fetchall()
    return jsonify({"items": [dict(r) for r in rows]})


@app.route("/api/annual", methods=["POST"])
@require_login
def api_annual_post():
    data = request.get_json(force=True, silent=True) or {}
    db = get_db()
    cur = db.execute(
        "INSERT INTO annual_cycle (month, day, title, note, lead_days) VALUES (?,?,?,?,?)",
        (data.get("month"), data.get("day"), data.get("title", ""),
         data.get("note", ""), data.get("lead_days", 0)))
    db.commit()
    return jsonify({"id": cur.lastrowid}), 201


# ---- 웹 푸시 (VAPID) ----------------------------------------------------
def _gen_vapid():
    """반환: (raw private key base64url, application server key base64url).
       py_vapid.from_string 은 raw 32바이트 키를 기대하므로 PEM 대신 raw 로 저장."""
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives import serialization
    pk = ec.generate_private_key(ec.SECP256R1())
    raw_priv = pk.private_numbers().private_value.to_bytes(32, "big")
    priv_b64 = base64.urlsafe_b64encode(raw_priv).rstrip(b"=").decode()
    raw_pub = pk.public_key().public_bytes(serialization.Encoding.X962,
                                           serialization.PublicFormat.UncompressedPoint)
    app_key = base64.urlsafe_b64encode(raw_pub).rstrip(b"=").decode()
    return priv_b64, app_key


def ensure_vapid():
    priv = get_setting("vapid_private")
    pub = get_setting("vapid_public")
    if not priv or not pub:
        priv, pub = _gen_vapid()
        set_setting("vapid_private", priv)
        set_setting("vapid_public", pub)
    elif "BEGIN" in priv:
        # 구버전 PEM 개인키 -> raw 로 마이그레이션 (키쌍/기존 구독 유지)
        from cryptography.hazmat.primitives.serialization import load_pem_private_key
        pk = load_pem_private_key(priv.encode(), password=None)
        raw = pk.private_numbers().private_value.to_bytes(32, "big")
        priv = base64.urlsafe_b64encode(raw).rstrip(b"=").decode()
        set_setting("vapid_private", priv)
    return priv, pub


def send_push(sub_row, payload):
    from pywebpush import webpush, WebPushException
    priv, _ = ensure_vapid()
    try:
        webpush(
            subscription_info={"endpoint": sub_row["endpoint"],
                               "keys": {"p256dh": sub_row["p256dh"], "auth": sub_row["auth"]}},
            data=json.dumps(payload, ensure_ascii=False),
            vapid_private_key=priv,
            vapid_claims={"sub": CONFIG.get("vapid_sub", "mailto:modu0629@gmail.com")},
            ttl=86400,   # WNS(윈도우 푸시)는 ttl=0 을 거부(400) → 반드시 설정
            timeout=15,
        )
        return True, None
    except WebPushException as e:
        return False, getattr(e.response, "status_code", None)
    except Exception:
        return False, None


def broadcast(payload):
    c = db_conn()
    subs = c.execute("SELECT * FROM push_subscriptions").fetchall()
    sent = 0
    for s in subs:
        ok, status = send_push(s, payload)
        if ok:
            sent += 1
        elif status in (404, 410):  # 만료된 구독 정리
            c.execute("DELETE FROM push_subscriptions WHERE id=?", (s["id"],))
    c.commit()
    c.close()
    return sent


@app.route("/api/push/key")
def push_key():
    _, pub = ensure_vapid()
    return jsonify({"key": pub})


@app.route("/api/push/subscribe", methods=["POST"])
@require_login
def push_subscribe():
    data = request.get_json(force=True, silent=True) or {}
    sub = data.get("subscription") or data
    try:
        endpoint = sub["endpoint"]
        keys = sub["keys"]
        p256dh, auth = keys["p256dh"], keys["auth"]
    except (KeyError, TypeError):
        return jsonify({"error": "invalid subscription"}), 400
    db = get_db()
    db.execute("INSERT INTO push_subscriptions(endpoint,p256dh,auth,platform,created_at) "
               "VALUES(?,?,?,?,?) ON CONFLICT(endpoint) DO UPDATE SET "
               "p256dh=excluded.p256dh, auth=excluded.auth, platform=excluded.platform",
               (endpoint, p256dh, auth, data.get("platform", "web"), now_iso()))
    db.commit()
    return jsonify({"ok": True})


@app.route("/api/push/unsubscribe", methods=["POST"])
@require_login
def push_unsubscribe():
    endpoint = (request.get_json(force=True, silent=True) or {}).get("endpoint")
    db = get_db()
    db.execute("DELETE FROM push_subscriptions WHERE endpoint=?", (endpoint,))
    db.commit()
    return jsonify({"ok": True})


@app.route("/api/push/test", methods=["POST"])
@require_login
def push_test():
    n = broadcast({"title": "🔔 테스트 알림", "body": "푸시가 잘 도착해!", "url": "/"})
    return jsonify({"ok": True, "sent": n})


# ---- 알림 설정 ----------------------------------------------------------
NOTIF_DEFAULTS = {
    "morning_time": "08:00", "weekly_time": "12:00", "weekly_days": "0,4",
    "morning_enabled": "1", "weekly_enabled": "1",
    "remind_enabled": "1", "remind_default": "30",   # 일정 시작 전 알림 기본 30분
}


@app.route("/api/notif-settings", methods=["GET"])
@require_login
def notif_get():
    out = {}
    for k, dv in NOTIF_DEFAULTS.items():
        v = get_setting("notif_" + k, dv)
        out[k] = (v == "1") if k.endswith("_enabled") else v
    return jsonify(out)


@app.route("/api/notif-settings", methods=["POST"])
@require_login
def notif_set():
    d = request.get_json(force=True, silent=True) or {}
    for k in NOTIF_DEFAULTS:
        if k in d:
            val = ("1" if d[k] else "0") if k.endswith("_enabled") else d[k]
            set_setting("notif_" + k, val)
    return jsonify({"ok": True})


# ---- 스케줄러 (월/금 12시 입력알람 · 빈 요일 아침알람) -------------------
def confirmed_count_on(date_obj):
    c = db_conn()
    plans = c.execute("SELECT * FROM plans WHERE review_status='confirmed'").fetchall()
    cnt = sum(1 for p in plans if expand_plan(p, date_obj, date_obj))
    c.close()
    return cnt


def check_notifications():
    now = dt.datetime.now(tz())
    hhmm = now.strftime("%H:%M")
    today_s = now.date().isoformat()

    # 1) 주간 계획 입력 알람 (기본 월·금 12:00)
    if get_setting("notif_weekly_enabled", "1") == "1" and \
            hhmm == get_setting("notif_weekly_time", "12:00"):
        days = [int(x) for x in get_setting("notif_weekly_days", "0,4").split(",") if x != ""]
        if now.weekday() in days:
            key = "sent:weekly:" + today_s
            if not get_setting(key):
                set_setting(key, "1")
                broadcast({"title": "📅 이번 주 계획 세우자",
                           "body": "한 주 일정을 입력할 시간이야!", "url": "/"})

    # 2) 빈 요일 아침 알람 (오늘 일정 0건일 때만)
    if get_setting("notif_morning_enabled", "1") == "1" and \
            hhmm == get_setting("notif_morning_time", "08:00"):
        key = "sent:morning:" + today_s
        if not get_setting(key):
            set_setting(key, "1")  # 일정 있어도 '오늘 체크 완료'로 기록(중복 방지)
            if confirmed_count_on(now.date()) == 0:
                broadcast({"title": "☀️ 오늘 할 일 없어?",
                           "body": "오늘 잡힌 일정이 없네. 추가할 게 없는지 확인해봐.", "url": "/"})

    # 3) 일정 시작 전 알림 (개별 remind_min, 없으면 전역 기본값)
    if get_setting("notif_remind_enabled", "1") == "1":
        try:
            default_min = int(get_setting("notif_remind_default", "30"))
        except ValueError:
            default_min = 30
        c = db_conn()
        plans = c.execute(
            "SELECT * FROM plans WHERE review_status='confirmed' "
            "AND start_time IS NOT NULL AND start_time!=''").fetchall()
        c.close()
        for pr in plans:
            if not expand_plan(pr, now.date(), now.date()):
                continue
            mins = default_min if pr["remind_min"] is None else pr["remind_min"]
            if mins is None or mins < 0:   # -1 = 알림 끔
                continue
            try:
                sh, sm = [int(x) for x in pr["start_time"].split(":")[:2]]
            except (ValueError, AttributeError):
                continue
            start_dt = now.replace(hour=sh, minute=sm, second=0, microsecond=0)
            remind_dt = start_dt - dt.timedelta(minutes=mins)
            if remind_dt.date() == now.date() and remind_dt.strftime("%H:%M") == hhmm:
                key = "sent:remind:%s:%s" % (today_s, pr["id"])
                if not get_setting(key):
                    set_setting(key, "1")
                    body = pr["start_time"] + " 시작" + ("" if mins == 0 else " · %d분 전" % mins)
                    broadcast({"title": "⏰ " + pr["title"], "body": body, "url": "/"})


def scheduler_loop():
    while True:
        try:
            check_notifications()
        except Exception as e:
            print("[scheduler]", e)
        time.sleep(30)


_scheduler_started = False


def start_scheduler():
    global _scheduler_started
    if _scheduler_started:
        return
    _scheduler_started = True
    threading.Thread(target=scheduler_loop, daemon=True).start()


# ---- 헬스체크 -----------------------------------------------------------
@app.route("/api/health")
def health():
    return jsonify({"ok": True, "today": today().isoformat()})


if __name__ == "__main__":
    init_db()
    start_scheduler()
    app.run(host="0.0.0.0", port=CONFIG.get("port", 5558), debug=False)
