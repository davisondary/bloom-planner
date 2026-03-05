import os
import io
import json
import sqlite3
import re
from datetime import datetime, time, timedelta

from flask import (
    Flask, render_template, request,
    redirect, url_for, send_file, g, jsonify, session
)

from flask_cors import CORS
from werkzeug.security import generate_password_hash, check_password_hash
import jwt
from functools import wraps

# ===== PDF через Platypus =====
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.lib import colors
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont

# Используем системный шрифт Arial (Windows, поддерживает кириллицу)
font_path = r"C:\Windows\Fonts\arial.ttf"
if os.path.exists(font_path):
    pdfmetrics.registerFont(TTFont("MyCyrillic", font_path))

# ===== Графики =====
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

# ===== Excel =====
import pandas as pd


# ----------------------------------------------------------------
# НАСТРОЙКА FLASK
# ----------------------------------------------------------------

app = Flask(__name__)
app.config["DATABASE"] = os.path.join(os.path.dirname(__file__), "database.db")

# для cookie-сессий (веб)
app.secret_key = "CHANGE_ME_SESSION_SECRET"

# CORS для телефона (API)
CORS(app)

# JWT для телефона (API)
JWT_SECRET = "CHANGE_ME_SECRET_123"
JWT_ALG = "HS256"
JWT_EXPIRE_DAYS = 30


# ----------------------------------------------------------------
# БАЗА ДАННЫХ
# ----------------------------------------------------------------

def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(app.config["DATABASE"])
        g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def close_db(error):
    db = g.pop("db", None)
    if db:
        db.close()


def _cols(db, table):
    try:
        return [r["name"] for r in db.execute(f"PRAGMA table_info({table})").fetchall()]
    except Exception:
        return []


def init_db():
    db = get_db()

    # ---- users ----
    db.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
    """)

    # ---- tasks ----
    # создаём сразу с user_id, чтобы новые базы были правильные
    db.execute("""
        CREATE TABLE IF NOT EXISTS tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            duration INTEGER NOT NULL,
            priority INTEGER NOT NULL,
            deadline TEXT,
            done INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            completed_at TEXT,
            user_id INTEGER
        );
    """)

    # миграции старых баз
    tcols = _cols(db, "tasks")
    if "completed_at" not in tcols:
        try:
            db.execute("ALTER TABLE tasks ADD COLUMN completed_at TEXT;")
        except sqlite3.OperationalError:
            pass
    if "user_id" not in tcols:
        try:
            db.execute("ALTER TABLE tasks ADD COLUMN user_id INTEGER;")
        except sqlite3.OperationalError:
            pass

    # ---- settings (пока общие, не на user_id) ----
    db.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        );
    """)

    # ---- notes (ДОЛЖНЫ БЫТЬ ЗА ПОЛЬЗОВАТЕЛЕМ) ----
    # если notes вообще не было — создастся правильная
    db.execute("""
        CREATE TABLE IF NOT EXISTS notes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            note_date TEXT NOT NULL,
            text TEXT NOT NULL DEFAULT '',
            UNIQUE(user_id, note_date)
        );
    """)

    # если раньше была старая notes(note_date PRIMARY KEY, text) — мигрируем в новую
    ncols = _cols(db, "notes")
    if "user_id" not in ncols:
        # значит это старая таблица — делаем перенос
        db.execute("""
            CREATE TABLE IF NOT EXISTS notes_v2 (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                note_date TEXT NOT NULL,
                text TEXT NOT NULL DEFAULT '',
                UNIQUE(user_id, note_date)
            );
        """)
        try:
            old = db.execute("SELECT note_date, text FROM notes").fetchall()
            # старые заметки положим пользователю id=1 (иначе никак не сопоставить)
            for r in old:
                db.execute("""
                    INSERT OR REPLACE INTO notes_v2 (user_id, note_date, text)
                    VALUES (?, ?, ?)
                """, (1, r["note_date"], r["text"]))
            db.execute("DROP TABLE notes;")
            db.execute("ALTER TABLE notes_v2 RENAME TO notes;")
        except Exception:
            # если вдруг что-то пошло не так — просто оставим как есть
            pass

    db.commit()


# ----------------------------------------------------------------
# ДЕКОРАТОР: обязательный логин для веба
# ----------------------------------------------------------------
def web_login_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if "user_id" not in session:
            return redirect(url_for("web_login"))
        return f(*args, **kwargs)
    return wrapper


# ----------------------------------------------------------------
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ДЛЯ ВРЕМЕНИ
# ----------------------------------------------------------------

def parse_time(v, default):
    if not v:
        v = default
    return datetime.strptime(v, "%H:%M").time()


def parse_rest_periods(rest_json: str):
    try:
        raw = json.loads(rest_json)
    except Exception:
        return []

    good = []
    for r in raw:
        s, e = r.get("start"), r.get("end")
        if s and e and s != "--:--" and e != "--:--":
            good.append({"start": parse_time(s, "13:00"), "end": parse_time(e, "13:30")})
    return good


def combine(date: datetime, t: time):
    return datetime.combine(date.date(), t)


# ----------------------------------------------------------------
# AI — СЛОЖНОСТЬ ЗАДАЧ
# ----------------------------------------------------------------

def classify_difficulty(priority, duration):
    if priority == 1 and duration > 60:
        return "hard"
    if priority in (1, 2) and 30 <= duration <= 60:
        return "medium"
    return "easy"


def ai_base_score(priority, duration):
    score = priority * 2
    if duration <= 30:
        score += 1.5
    elif duration <= 60:
        score += 0.5
    else:
        score -= 0.5
    return score


# ----------------------------------------------------------------
# ФОРМИРОВАНИЕ РАБОЧИХ ИНТЕРВАЛОВ
# ----------------------------------------------------------------

def build_intervals(day: datetime, settings):
    sleep_start = parse_time(settings["sleep_start"], "23:00")
    sleep_end = parse_time(settings["sleep_end"], "07:00")

    rest = parse_rest_periods(settings["rest_periods"])

    start = combine(day, sleep_end)
    end = combine(day, sleep_start)

    if end <= start:
        start = combine(day, time(8, 0))
        end = combine(day, time(22, 0))

    intervals = [{"start": start, "end": end}]

    for rp in rest:
        s = combine(day, rp["start"])
        e = combine(day, rp["end"])

        new = []
        for iv in intervals:
            ivs, ive = iv["start"], iv["end"]

            if e <= ivs or s >= ive:
                new.append(iv)
                continue

            if s <= ivs < e < ive:
                new.append({"start": e, "end": ive})
            elif ivs < s < ive <= e:
                new.append({"start": ivs, "end": s})
            elif ivs < s and e < ive:
                new.append({"start": ivs, "end": s})
                new.append({"start": e, "end": ive})

        intervals = new

    return sorted(intervals, key=lambda x: x["start"])


# ----------------------------------------------------------------
# AI ПЛАНИРОВЩИК
# ----------------------------------------------------------------

def pick_task(candidates, last_diff):
    if len(last_diff) >= 2 and last_diff[-1] == "hard" and last_diff[-2] == "hard":
        light = [t for t in candidates if t["difficulty"] != "hard"]
        if light:
            light.sort(key=lambda x: x["score"], reverse=True)
            return light[0]

    candidates.sort(key=lambda x: x["score"], reverse=True)
    return candidates[0]


def build_schedule(tasks, settings, day=None):
    if day is None:
        day_date = datetime.now().date()
    else:
        if isinstance(day, datetime):
            day_date = day.date()
        else:
            day_date = day

    intervals = build_intervals(datetime.combine(day_date, time(0, 0)), settings)

    items = []
    for t in tasks:
        pr = int(t["priority"])
        dur = int(t["duration"])
        deadline = t.get("deadline")
        bonus = 0

        if deadline:
            try:
                d = datetime.strptime(deadline, "%Y-%m-%d").date()
                days = (d - day_date).days
                if days <= 0:
                    bonus = 5
                elif days <= 2:
                    bonus = 3
                elif days <= 5:
                    bonus = 1
            except Exception:
                pass

        items.append({
            "id": t["id"],
            "title": t["title"],
            "duration": dur,
            "priority": pr,
            "difficulty": classify_difficulty(pr, dur),
            "score": ai_base_score(pr, dur) + bonus,
            "deadline": deadline,
        })

    schedule = []
    last_diff = []

    for iv in intervals:
        now = iv["start"]
        end = iv["end"]

        while now < end and items:
            chosen = pick_task(items, last_diff)

            left_minutes = int((end - now).total_seconds() // 60)
            if left_minutes <= 0:
                break

            block = min(chosen["duration"], left_minutes)
            finish = now + timedelta(minutes=block)

            schedule.append({
                "start": now,
                "end": finish,
                "title": chosen["title"],
                "priority": chosen["priority"],
                "difficulty": chosen["difficulty"],
            })

            now = finish
            chosen["duration"] -= block

            last_diff.append(chosen["difficulty"])
            if len(last_diff) > 5:
                last_diff.pop(0)

            if chosen["duration"] <= 0:
                items = [x for x in items if x["id"] != chosen["id"]]

    return schedule


# ----------------------------------------------------------------
# ИИ-АССИСТЕНТ (инсайты)
# ----------------------------------------------------------------

def ai_assistant_insights(tasks, schedule, settings):
    insights = []

    if not tasks:
        return [{"type": "info", "text": "Задач нет — ИИ не может выполнить анализ."}]

    total = sum(int(t["duration"]) for t in tasks)
    intervals = build_intervals(datetime.now(), settings)
    available = sum((iv["end"] - iv["start"]).seconds // 60 for iv in intervals)

    if total > available:
        insights.append({
            "type": "warning",
            "text": f"Суммарное время задач ({total} мин) превышает доступное время дня ({available} мин)."
        })
    else:
        insights.append({
            "type": "ok",
            "text": f"Все задачи помещаются в доступное время дня ({available} мин)."
        })

    hard = [t for t in tasks if classify_difficulty(t["priority"], t["duration"]) == "hard"]
    if len(hard) >= 3:
        insights.append({
            "type": "warning",
            "text": f"Много сложных задач ({len(hard)}). Добавьте перерывы или распределите их равномерно."
        })

    today = datetime.now().date()
    close = []
    for t in tasks:
        if not t["deadline"]:
            continue
        try:
            ddate = datetime.strptime(t["deadline"], "%Y-%m-%d").date()
        except Exception:
            continue

        days = (ddate - today).days
        if days <= 0:
            close.append(f"«{t['title']}» (дедлайн сегодня!)")
        elif days <= 2:
            close.append(f"«{t['title']}» (≤2 дней)")
        elif days <= 5:
            close.append(f"«{t['title']}» (≤5 дней)")

    if close:
        insights.append({
            "type": "warning",
            "text": "Близкие дедлайны: " + ", ".join(close)
        })
    else:
        insights.append({
            "type": "ok",
            "text": "Все дедлайны находятся в комфортных пределах."
        })

    if schedule:
        consecutive = 0
        for s in schedule:
            if s["difficulty"] == "hard":
                consecutive += 1
                if consecutive >= 3:
                    insights.append({
                        "type": "warning",
                        "text": "В расписании есть три сложные задачи подряд. Добавьте отдых."
                    })
                    break
            else:
                consecutive = 0

    high = [t for t in tasks if t["priority"] == 1 and int(t["duration"]) <= 90]
    if high:
        high.sort(key=lambda x: int(x["duration"]))
        best = high[0]
        insights.append({
            "type": "tip",
            "text": f"Оптимально начать день с задачи «{best['title']}»."
        })

    return insights


# ----------------------------------------------------------------
# ИИ-оценка длительности, если пользователь не указал время
# ----------------------------------------------------------------

def suggest_duration(priority: int, deadline: str | None) -> int:
    base_map = {1: 90, 2: 60, 3: 30}
    base = base_map.get(priority, 45)

    if not deadline:
        return base

    try:
        ddate = datetime.strptime(deadline, "%Y-%m-%d").date()
        days_left = (ddate - datetime.now().date()).days

        if days_left <= 0:
            return max(base, 120)
        elif days_left <= 2:
            return max(base, 90)
        elif days_left <= 5:
            return max(base, 60)
        else:
            return base
    except Exception:
        return base


# ----------------------------------------------------------------
# НАСТРОЙКИ (пока общие, не на user_id)
# ----------------------------------------------------------------

def save_setting(key, value):
    db = get_db()
    db.execute("""
        INSERT INTO settings (key, value)
        VALUES (?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value
    """, (key, value))
    db.commit()


def load_settings():
    db = get_db()
    data = db.execute("SELECT key, value FROM settings").fetchall()
    out = {r["key"]: r["value"] for r in data}

    out.setdefault("sleep_start", "23:00")
    out.setdefault("sleep_end", "07:00")
    out.setdefault("rest_periods", "[]")
    return out


@app.route("/settings", methods=["GET", "POST"])
@web_login_required
def settings_view():
    if request.method == "POST":
        sleep_start = request.form["sleep_start"]
        sleep_end = request.form["sleep_end"]

        starts = request.form.getlist("rest_start")
        ends = request.form.getlist("rest_end")

        rest = []
        for s, e in zip(starts, ends):
            if s and e and s != "--:--" and e != "--:--":
                rest.append({"start": s, "end": e})

        save_setting("sleep_start", sleep_start)
        save_setting("sleep_end", sleep_end)
        save_setting("rest_periods", json.dumps(rest))

        return redirect(url_for("settings_view"))

    st = load_settings()
    rest = json.loads(st["rest_periods"])
    return render_template(
        "settings.html",
        sleep_start=st["sleep_start"],
        sleep_end=st["sleep_end"],
        rest_periods=rest
    )


# ============================
# WEB AUTH (браузер)
# ============================

@app.route("/register", methods=["GET", "POST"])
def web_register():
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "").strip()

        if not email or not password:
            return render_template("register.html", error="Введите email и пароль")
        if len(password) < 6:
            return render_template("register.html", error="Пароль минимум 6 символов")

        db = get_db()
        try:
            db.execute(
                "INSERT INTO users (email, password_hash) VALUES (?, ?)",
                (email, generate_password_hash(password))
            )
            db.commit()
        except sqlite3.IntegrityError:
            return render_template("register.html", error="Такой email уже зарегистрирован")

        user = db.execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone()
        session["user_id"] = int(user["id"])
        return redirect(url_for("index"))

    return render_template("register.html")


@app.route("/login", methods=["GET", "POST"])
def web_login():
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "").strip()

        db = get_db()
        user = db.execute(
            "SELECT id, password_hash FROM users WHERE email = ?",
            (email,)
        ).fetchone()

        if not user or not check_password_hash(user["password_hash"], password):
            return render_template("login.html", error="Неверный email или пароль")

        session["user_id"] = int(user["id"])
        return redirect(url_for("index"))

    return render_template("login.html")


@app.route("/logout")
def web_logout():
    session.pop("user_id", None)
    return redirect(url_for("web_login"))


# ==========================================================
# JWT AUTH (телефон)
# ==========================================================

def make_token(user_id):
    payload = {
        "user_id": int(user_id),
        "exp": datetime.utcnow() + timedelta(days=JWT_EXPIRE_DAYS)
    }
    token = jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALG)
    if isinstance(token, bytes):
        token = token.decode("utf-8")
    return token


def auth_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        auth_header = request.headers.get("Authorization")
        if not auth_header or not auth_header.startswith("Bearer "):
            return jsonify({"error": "Unauthorized"}), 401

        token = auth_header.split(" ", 1)[1].strip()
        if not token:
            return jsonify({"error": "Unauthorized"}), 401

        try:
            data = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALG])
            g.user_id = int(data["user_id"])
        except jwt.ExpiredSignatureError:
            return jsonify({"error": "Token expired"}), 401
        except Exception:
            return jsonify({"error": "Invalid token"}), 401

        return f(*args, **kwargs)

    return decorated


# ============================
# AUTH API (телефон)
# ============================

@app.route("/api/auth/register", methods=["POST"])
def api_register():
    data = request.get_json() or {}
    email = (data.get("email") or "").strip().lower()
    password = (data.get("password") or "").strip()

    if not email or not password:
        return jsonify({"error": "Email and password required"}), 400
    if len(password) < 6:
        return jsonify({"error": "Password must be at least 6 chars"}), 400

    db = get_db()
    existing = db.execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone()
    if existing:
        return jsonify({"error": "User already exists"}), 400

    cursor = db.execute(
        "INSERT INTO users (email, password_hash) VALUES (?, ?)",
        (email, generate_password_hash(password))
    )
    db.commit()

    token = make_token(cursor.lastrowid)
    return jsonify({"token": token})


@app.route("/api/auth/login", methods=["POST"])
def api_login():
    data = request.get_json() or {}
    email = (data.get("email") or "").strip().lower()
    password = (data.get("password") or "").strip()

    db = get_db()
    user = db.execute("SELECT id, password_hash FROM users WHERE email = ?", (email,)).fetchone()

    if not user:
        return jsonify({"error": "User not found"}), 404
    if not check_password_hash(user["password_hash"], password):
        return jsonify({"error": "Wrong password"}), 401

    token = make_token(user["id"])
    return jsonify({"token": token})


# ============================
# TASKS API (для Flutter)
# ============================

@app.route("/api/tasks", methods=["GET"])
@auth_required
def api_get_tasks():
    db = get_db()
    rows = db.execute("""
        SELECT id, title, duration, priority, deadline, done, created_at, completed_at
        FROM tasks
        WHERE user_id = ?
        ORDER BY done ASC, id DESC
    """, (g.user_id,)).fetchall()

    return jsonify([dict(r) for r in rows])


@app.route("/api/tasks", methods=["POST"])
@auth_required
def api_add_task():
    data = request.get_json() or {}

    title = (data.get("title") or "").strip()
    priority = int(data.get("priority") or 2)
    deadline = data.get("deadline")
    duration_raw = data.get("duration")

    if not title:
        return jsonify({"error": "title required"}), 400

    if duration_raw is None or str(duration_raw).strip() == "":
        duration = suggest_duration(priority, deadline)
    else:
        duration = int(duration_raw)

    db = get_db()
    db.execute("""
        INSERT INTO tasks (title, duration, priority, deadline, done, user_id)
        VALUES (?, ?, ?, ?, 0, ?)
    """, (title, duration, priority, deadline, g.user_id))
    db.commit()

    return jsonify({"ok": True}), 201


@app.route("/api/tasks/<int:task_id>/done", methods=["POST"])
@auth_required
def api_done_task(task_id):
    db = get_db()
    today_str = datetime.now().date().isoformat()

    db.execute("""
        UPDATE tasks
        SET done = 1,
            completed_at = ?
        WHERE id = ? AND user_id = ?
    """, (today_str, task_id, g.user_id))
    db.commit()

    return jsonify({"ok": True})


@app.route("/api/tasks/<int:task_id>", methods=["DELETE"])
@auth_required
def api_delete_task(task_id):
    db = get_db()
    db.execute("""
        DELETE FROM tasks
        WHERE id = ? AND user_id = ?
    """, (task_id, g.user_id))
    db.commit()

    return jsonify({"ok": True})


# ----------------------------------------------------------------
# NOTES SAVE (WEB) — заметки за пользователем + за датой
# ----------------------------------------------------------------

@app.route("/notes/save", methods=["POST"])
@web_login_required
def save_notes():
    data = request.get_json() or {}
    text = data.get("text", "")
    note_date = data.get("date") or datetime.now().date().isoformat()

    db = get_db()
    uid = int(session["user_id"])

    db.execute("""
        INSERT INTO notes (user_id, note_date, text)
        VALUES (?, ?, ?)
        ON CONFLICT(user_id, note_date) DO UPDATE SET text = excluded.text
    """, (uid, note_date, text))
    db.commit()

    return {"status": "ok"}


# ----------------------------------------------------------------
# ВЕБ-СТРАНИЦЫ: ЗАДАЧИ
# ----------------------------------------------------------------

@app.route("/")
@web_login_required
def index():
    db = get_db()
    uid = int(session["user_id"])

    tasks = db.execute("""
        SELECT id, title, duration, priority, deadline, done
        FROM tasks
        WHERE user_id = ?
        ORDER BY id
    """, (uid,)).fetchall()

    today_date = datetime.now().date().isoformat()

    # заметки (сегодня) — ТОЛЬКО ПОЛЬЗОВАТЕЛЯ
    n = db.execute("""
        SELECT text FROM notes
        WHERE user_id = ? AND note_date = ?
    """, (uid, today_date)).fetchone()
    note_text = n["text"] if n else ""

    # прогресс дня (только пользователь, только задачи созданные сегодня)
    row_total = db.execute("""
        SELECT COUNT(*) AS c
        FROM tasks
        WHERE user_id = ?
          AND substr(created_at, 1, 10) = ?
    """, (uid, today_date)).fetchone()
    total_today = row_total["c"] if row_total else 0

    row_done = db.execute("""
        SELECT COUNT(*) AS c
        FROM tasks
        WHERE user_id = ?
          AND done = 1
          AND substr(created_at, 1, 10) = ?
    """, (uid, today_date)).fetchone()
    done_today = row_done["c"] if row_done else 0

    progress_percent = int(done_today / total_today * 100) if total_today > 0 else 0

    return render_template(
        "index.html",
        tasks=tasks,
        note_text=note_text,
        progress_done=done_today,
        progress_percent=progress_percent,
        total_today=total_today,
        today_date=today_date,
    )


@app.route("/add", methods=["POST"])
@web_login_required
def add_task():
    title = request.form["title"].strip()
    duration_raw = request.form.get("duration", "").strip()
    priority = int(request.form["priority"])
    deadline = request.form.get("deadline") or None

    if duration_raw:
        duration = int(duration_raw)
    else:
        duration = suggest_duration(priority, deadline)

    db = get_db()
    db.execute("""
        INSERT INTO tasks (title, duration, priority, deadline, done, user_id)
        VALUES (?, ?, ?, ?, 0, ?)
    """, (title, duration, priority, deadline, int(session["user_id"])))
    db.commit()

    return redirect(url_for("index"))


@app.route("/add_big", methods=["POST"])
@web_login_required
def add_big_task():
    title = request.form["big_title"].strip()
    daily_minutes_raw = request.form.get("daily_minutes", "0").strip()
    days_count_raw = request.form.get("days_count", "1").strip()
    priority = int(request.form.get("big_priority", "2"))
    start_date_str = request.form.get("start_date")

    if not title:
        return redirect(url_for("index"))

    try:
        daily_minutes = max(1, int(daily_minutes_raw))
    except ValueError:
        daily_minutes = 60

    try:
        days_count = max(1, int(days_count_raw))
    except ValueError:
        days_count = 1

    if start_date_str:
        try:
            start_date = datetime.strptime(start_date_str, "%Y-%m-%d").date()
        except Exception:
            start_date = datetime.now().date()
    else:
        start_date = datetime.now().date()

    db = get_db()
    uid = int(session["user_id"])

    for i in range(days_count):
        day_date = start_date + timedelta(days=i)
        day_title = f"{title} (день {i+1})"
        deadline = day_date.isoformat()

        db.execute("""
            INSERT INTO tasks (title, duration, priority, deadline, done, user_id)
            VALUES (?, ?, ?, ?, 0, ?)
        """, (day_title, daily_minutes, priority, deadline, uid))

    db.commit()
    return redirect(url_for("index"))


# ----------------------------------------------------------------
# Парсинг "человеческих" дат для ИИ-помощника
# ----------------------------------------------------------------
def parse_natural_date(text, today):
    low = text.lower()

    m = re.search(r"(\d{1,2})[.\-/](\d{1,2})(?:[.\-/](\d{4}))?", low)
    if m:
        day = int(m.group(1))
        month = int(m.group(2))
        year = int(m.group(3)) if m.group(3) else today.year
        try:
            return datetime(year, month, day).date()
        except ValueError:
            pass

    m_iso = re.search(r"(\d{4})-(\d{2})-(\d{2})", low)
    if m_iso:
        y, m2, d2 = map(int, m_iso.groups())
        try:
            return datetime(y, m2, d2).date()
        except ValueError:
            pass

    if "послезавтра" in low:
        return today + timedelta(days=2)
    if "завтра" in low:
        return today + timedelta(days=1)
    if "сегодня" in low:
        return today

    m_days = re.search(r"через\s+(\d+)\s*(дн|день|дня|дней)", low)
    if m_days:
        return today + timedelta(days=int(m_days.group(1)))

    if "через неделю" in low:
        return today + timedelta(days=7)
    m_weeks = re.search(r"через\s+(\d+)\s*недел", low)
    if m_weeks:
        return today + timedelta(days=7 * int(m_weeks.group(1)))

    weekdays = {
        "понедельник": 0, "пн": 0,
        "вторник": 1, "вт": 1,
        "среда": 2, "ср": 2,
        "четверг": 3, "чт": 3,
        "пятница": 4, "пт": 4,
        "суббота": 5, "сб": 5,
        "воскресенье": 6, "вс": 6,
    }
    for word, idx in weekdays.items():
        if re.search(r"\b" + word + r"\b", low):
            delta = (idx - today.weekday()) % 7
            if delta == 0:
                delta = 7
            return today + timedelta(days=delta)

    return None


# ----------------------------------------------------------------
# МАРШРУТ — ИИ-ПОМОЩНИК (добавление задач текстом) — ВЕБ
# ----------------------------------------------------------------

@app.route("/ai_add", methods=["POST"])
@web_login_required
def ai_add_task():
    text = request.form.get("prompt", "").strip()
    if not text:
        return redirect(url_for("index"))

    low = text.lower()
    today = datetime.now().date()
    uid = int(session["user_id"])

    if "высок" in low:
        priority = 1
    elif "низк" in low:
        priority = 3
    elif "средн" in low:
        priority = 2
    else:
        priority = 2

    m = re.search(r"(\d+)\s*(минут[аы]?|мин|m|min)?", low)
    minutes = int(m.group(1)) if m else None

    d = re.search(r"(\d+)\s*(дн|день|дня|дней|day|days)", low)
    days = int(d.group(1)) if d else 1

    natural_date = parse_natural_date(low, today)

    title = text
    for word in ["добавь", "добавить", "создай", "создать", "задачу", "задача",
                 "по", "на", "до", "к", "с", "пожалуйста", "плиз"]:
        title = re.sub(r"\b" + word + r"\b", "", title, flags=re.IGNORECASE)

    title = re.sub(r"\d+\s*(минут[аы]?|мин|дней|дня|день|day|days)", "", title, flags=re.IGNORECASE)
    title = re.sub(r"(высокий|средний|низкий)", "", title, flags=re.IGNORECASE)
    title = re.sub(r"(сегодня|завтра|послезавтра)", "", title, flags=re.IGNORECASE)
    title = re.sub(r"через\s+\d+\s*\w+", "", title, flags=re.IGNORECASE)
    title = re.sub(r"(понедельник|вторник|среда|четверг|пятница|суббота|воскресенье|пн|вт|ср|чт|пт|сб|вс)",
                   "", title, flags=re.IGNORECASE)
    title = re.sub(r"\d{1,2}[.\-/]\d{1,2}(?:[.\-/](\d{2,4}))?", "", title)
    title = re.sub(r"\d{4}-\d{2}-\d{2}", "", title)
    title = re.sub(r"\s+", " ", title).strip() or "Задача"

    if minutes is None:
        minutes = suggest_duration(priority, natural_date.isoformat() if natural_date else None)

    db = get_db()

    if days > 1:
        start_date = natural_date or today
        for i in range(days):
            day_date = start_date + timedelta(days=i)
            day_title = f"{title} (день {i+1})"
            deadline = day_date.isoformat()

            db.execute("""
                INSERT INTO tasks (title, duration, priority, deadline, done, user_id)
                VALUES (?, ?, ?, ?, 0, ?)
            """, (day_title, minutes, priority, deadline, uid))
        db.commit()
    else:
        deadline = natural_date.isoformat() if natural_date else None
        db.execute("""
            INSERT INTO tasks (title, duration, priority, deadline, done, user_id)
            VALUES (?, ?, ?, ?, 0, ?)
        """, (title, minutes, priority, deadline, uid))
        db.commit()

    return redirect(url_for("index"))


@app.route("/add_ai", methods=["POST"])
@web_login_required
def add_ai_command():
    text = request.form.get("command", "").strip()
    if not text:
        return redirect(url_for("index"))

    low = text.lower()
    uid = int(session["user_id"])

    priority = 2
    if "высок" in low:
        priority = 1
    elif "низк" in low:
        priority = 3

    minutes = None
    m = re.search(r"(\d+)\s*(минут|мин|m|min)", low)
    if m:
        minutes = int(m.group(1))

    days_count = 1
    m_days = re.search(r"(\d+)\s*(день|дня|дней)", low)
    if m_days:
        days_count = int(m_days.group(1))

    today = datetime.now().date()
    start_date = today
    deadline_for_single = None

    if "завтра" in low:
        start_date = today + timedelta(days=1)
        deadline_for_single = start_date
    else:
        m_rel = re.search(r"через\s+(\d+)\s*дн", low)
        if m_rel:
            offset = int(m_rel.group(1))
            start_date = today + timedelta(days=offset)
            deadline_for_single = start_date

    words = text.split()
    title_words = []
    for w in words:
        lw = w.lower()
        if lw in ("добавь", "добавить", "создай", "сделай", "поставь"):
            continue
        if any(p in lw for p in ("высок", "средн", "низк")):
            break
        if lw.isdigit():
            break
        if "минут" in lw or "мин" in lw:
            break
        if "дн" in lw:
            break
        if lw in ("завтра", "через"):
            break
        title_words.append(w)

    title = " ".join(title_words) if title_words else text

    db = get_db()

    if days_count > 1:
        if minutes is None:
            minutes = suggest_duration(priority, None)

        for i in range(days_count):
            day_date = start_date + timedelta(days=i)
            day_title = f"{title} (день {i + 1})"
            deadline = day_date.isoformat()

            db.execute("""
                INSERT INTO tasks (title, duration, priority, deadline, done, user_id)
                VALUES (?, ?, ?, ?, 0, ?)
            """, (day_title, minutes, priority, deadline, uid))
    else:
        deadline_str = deadline_for_single.isoformat() if deadline_for_single else None
        if minutes is None:
            minutes = suggest_duration(priority, deadline_str)

        db.execute("""
            INSERT INTO tasks (title, duration, priority, deadline, done, user_id)
            VALUES (?, ?, ?, ?, 0, ?)
        """, (title, minutes, priority, deadline_str, uid))

    db.commit()
    return redirect(url_for("index"))


@app.route("/edit/<int:id>", methods=["GET", "POST"])
@web_login_required
def edit_task(id):
    db = get_db()
    uid = int(session["user_id"])

    if request.method == "POST":
        title = request.form["title"]
        duration = request.form["duration"]
        priority = request.form["priority"]
        deadline = request.form.get("deadline")

        db.execute("""
            UPDATE tasks
            SET title=?, duration=?, priority=?, deadline=?
            WHERE id=? AND user_id=?
        """, (title, duration, priority, deadline, id, uid))
        db.commit()
        return redirect(url_for("index"))

    task = db.execute("SELECT * FROM tasks WHERE id=? AND user_id=?", (id, uid)).fetchone()
    if task is None:
        return redirect(url_for("index"))

    return render_template("edit.html", task=task)


@app.route("/delete/<int:id>", methods=["POST"])
@web_login_required
def delete_task(id):
    db = get_db()
    db.execute("DELETE FROM tasks WHERE id=? AND user_id=?", (id, int(session["user_id"])))
    db.commit()
    return redirect(url_for("index"))


@app.route("/done/<int:id>", methods=["POST"])
@web_login_required
def mark_done(id):
    db = get_db()
    today_str = datetime.now().date().isoformat()
    db.execute("""
        UPDATE tasks
        SET done = 1,
            completed_at = ?
        WHERE id = ? AND user_id = ?
    """, (today_str, id, int(session["user_id"])))
    db.commit()
    return redirect(url_for("index"))


# ----------------------------------------------------------------
# ИИ-АССИСТЕНТ (страница) — ВЕБ
# ----------------------------------------------------------------

@app.route("/ai_assistant")
@web_login_required
def ai_assistant_view():
    db = get_db()
    uid = int(session["user_id"])

    rows = db.execute("""
        SELECT id, title, duration, priority, deadline, done
        FROM tasks
        WHERE user_id = ?
        ORDER BY id
    """, (uid,)).fetchall()

    tasks = [dict(r) for r in rows if not r["done"]]
    settings = load_settings()
    schedule = build_schedule(tasks, settings)
    insights = ai_assistant_insights(tasks, schedule, settings)

    schedule_view = [{
        "start": s["start"].strftime("%H:%M"),
        "end": s["end"].strftime("%H:%M"),
        "title": s["title"],
        "priority": s["priority"],
        "difficulty": s["difficulty"],
    } for s in schedule]

    return render_template(
        "ai.html",
        insights=insights,
        schedule=schedule_view,
        tasks=rows
    )


# ----------------------------------------------------------------
# ВСПОМОГАТЕЛЬНАЯ ФУНКЦИЯ: какие задачи попадают в расписание дня
# ----------------------------------------------------------------

def get_tasks_for_planning(selected_date, user_id: int):
    db = get_db()
    today_date = datetime.now().date()
    selected_str = selected_date.isoformat()

    if selected_date <= today_date:
        rows = db.execute("""
            SELECT id, title, duration, priority, deadline
            FROM tasks
            WHERE done = 0
              AND user_id = ?
              AND (
                    deadline IS NULL
                 OR deadline <= ?
              )
            ORDER BY
                CASE
                    WHEN deadline < ? THEN 0
                    WHEN deadline = ? THEN 1
                    WHEN deadline IS NULL THEN 2
                    ELSE 3
                END,
                deadline,
                priority DESC
        """, (user_id, selected_str, selected_str, selected_str)).fetchall()
    else:
        rows = db.execute("""
            SELECT id, title, duration, priority, deadline
            FROM tasks
            WHERE done = 0
              AND user_id = ?
              AND (
                    deadline IS NULL
                 OR deadline = ?
              )
            ORDER BY
                CASE
                    WHEN deadline = ? THEN 0
                    WHEN deadline IS NULL THEN 1
                    ELSE 2
                END,
                deadline,
                priority DESC
        """, (user_id, selected_str, selected_str)).fetchall()

    return [dict(r) for r in rows]


# ----------------------------------------------------------------
# РАСПИСАНИЕ — ВЕБ
# ----------------------------------------------------------------

@app.route("/schedule_day")
@web_login_required
def schedule_day():
    today_date = datetime.now().date()

    date_str = request.args.get("date")
    if date_str:
        try:
            selected_date = datetime.strptime(date_str, "%Y-%m-%d").date()
        except Exception:
            selected_date = today_date
    else:
        selected_date = today_date

    selected_str = selected_date.isoformat()

    tasks = get_tasks_for_planning(selected_date, int(session["user_id"]))
    settings = load_settings()
    schedule = build_schedule(tasks, settings, day=selected_date)

    view = [{
        "start": s["start"].strftime("%H:%M"),
        "end": s["end"].strftime("%H:%M"),
        "title": s["title"],
        "priority": s["priority"],
        "difficulty": s["difficulty"],
    } for s in schedule]

    prev_date = (selected_date - timedelta(days=1)).isoformat()
    next_date = (selected_date + timedelta(days=1)).isoformat()

    return render_template(
        "schedule_day.html",
        schedule=view,
        current_date=selected_str,
        prev_date=prev_date,
        next_date=next_date,
        is_today=(selected_date == today_date)
    )


# ----------------------------------------------------------------
# СТАТИСТИКА + ГРАФИКИ — ВЕБ
# ----------------------------------------------------------------

@app.route("/stats")
@web_login_required
def stats_view():
    db = get_db()
    uid = int(session["user_id"])

    rows = db.execute("""
        SELECT duration, priority, done
        FROM tasks
        WHERE user_id = ?
    """, (uid,)).fetchall()

    total_tasks = len(rows)
    total_duration = sum(r["duration"] for r in rows)
    done_count = sum(1 for r in rows if r["done"])

    return render_template("stats.html",
                           total_tasks=total_tasks,
                           total_duration=total_duration,
                           done_count=done_count)


@app.route("/chart_priority")
@web_login_required
def chart_priority():
    db = get_db()
    uid = int(session["user_id"])

    rows = db.execute("""
        SELECT priority, duration
        FROM tasks
        WHERE user_id = ?
    """, (uid,)).fetchall()

    p1 = sum(r["duration"] for r in rows if r["priority"] == 1)
    p2 = sum(r["duration"] for r in rows if r["priority"] == 2)
    p3 = sum(r["duration"] for r in rows if r["priority"] == 3)

    fig, ax = plt.subplots()
    ax.pie([p1, p2, p3], labels=["Высокий", "Средний", "Низкий"], autopct="%1.1f%%")

    buf = io.BytesIO()
    plt.savefig(buf, format="png")
    buf.seek(0)
    return send_file(buf, mimetype="image/png")


@app.route("/chart_timeline")
@web_login_required
def chart_timeline():
    db = get_db()
    uid = int(session["user_id"])

    rows = db.execute("""
        SELECT duration
        FROM tasks
        WHERE user_id = ?
        ORDER BY id
    """, (uid,)).fetchall()

    values = [r["duration"] for r in rows]
    acc = []
    total = 0
    for v in values:
        total += v
        acc.append(total)

    fig, ax = plt.subplots()
    ax.plot(acc)
    ax.set_title("Накопленная длительность задач, мин")

    buf = io.BytesIO()
    plt.savefig(buf, format="png")
    buf.seek(0)
    return send_file(buf, mimetype="image/png")


# ----------------------------------------------------------------
# ЭКСПОРТ РАСПИСАНИЯ ДНЯ В EXCEL — ВЕБ
# ----------------------------------------------------------------

@app.route("/export/excel")
@web_login_required
def export_excel():
    selected_date = datetime.now().date()
    date_str = selected_date.isoformat()

    tasks = get_tasks_for_planning(selected_date, int(session["user_id"]))
    settings = load_settings()
    schedule = build_schedule(tasks, settings, day=selected_date)

    rows = []
    for s in schedule:
        rows.append({
            "Начало": s["start"].strftime("%H:%M"),
            "Конец": s["end"].strftime("%H:%M"),
            "Задача": s["title"],
            "Приоритет": {1: "Высокий", 2: "Средний", 3: "Низкий"}[s["priority"]],
            "Сложность": s["difficulty"],
        })

    df = pd.DataFrame(rows)

    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name="Сегодня")

    buf.seek(0)
    return send_file(buf, as_attachment=True, download_name=f"schedule_today_{date_str}.xlsx")


# ----------------------------------------------------------------
# ЭКСПОРТ РАСПИСАНИЯ ДНЯ В PDF — ВЕБ
# ----------------------------------------------------------------

@app.route("/export/pdf")
@web_login_required
def export_pdf():
    selected_date = datetime.now().date()
    date_str = selected_date.isoformat()

    tasks = get_tasks_for_planning(selected_date, int(session["user_id"]))
    settings = load_settings()
    schedule = build_schedule(tasks, settings, day=selected_date)

    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4)
    style = getSampleStyleSheet()["Normal"]
    if os.path.exists(font_path):
        style.fontName = "MyCyrillic"

    data = [[
        Paragraph("Начало", style),
        Paragraph("Конец", style),
        Paragraph("Задача", style),
        Paragraph("Приоритет", style),
        Paragraph("Сложность", style),
    ]]

    for s in schedule:
        data.append([
            Paragraph(s["start"].strftime("%H:%M"), style),
            Paragraph(s["end"].strftime("%H:%M"), style),
            Paragraph(s["title"], style),
            Paragraph({1: "Высокий", 2: "Средний", 3: "Низкий"}[s["priority"]], style),
            Paragraph(s["difficulty"], style),
        ])

    table = Table(data, colWidths=[25*mm, 25*mm, 70*mm, 25*mm, 25*mm])
    ts = [
        ("BACKGROUND", (0, 0), (-1, 0), colors.lightgrey),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.black),
        ("FONTSIZE", (0, 0), (-1, -1), 10),
    ]
    if os.path.exists(font_path):
        ts.append(("FONTNAME", (0, 0), (-1, -1), "MyCyrillic"))
    table.setStyle(TableStyle(ts))

    doc.build([table])
    buf.seek(0)

    return send_file(buf, as_attachment=True, download_name=f"schedule_today_{date_str}.pdf")


# ----------------------------------------------------------------
# MAIN
# ----------------------------------------------------------------

if __name__ == "__main__":
    with app.app_context():
        init_db()
    app.run(host="0.0.0.0", port=10000)