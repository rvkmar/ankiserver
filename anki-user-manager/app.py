import os
import json
import sqlite3
import shutil
import subprocess
from datetime import datetime, timedelta
from functools import wraps
from fsrs import Scheduler, Card, Rating, ReviewLog
from flask import Flask, render_template, request, redirect, url_for, flash, session

app = Flask(__name__)
app.secret_key = "supersecret"  # TODO: change in production

# Paths
BASE_DIR = "/home/ubuntu/ankiserver/anki-user-manager"
USERS_FILE = "/home/ubuntu/ankiserver/anki-sync-users.txt"
SYNC_BASE = "/home/ubuntu/ankiserver/anki-sync-data"
UPLOAD_FOLDER = os.path.join(BASE_DIR, "uploads")
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

# --- Simple Admin Auth ---
ADMIN_USER = "admin"
ADMIN_PASS = "Ravi1981"  # TODO: replace with env var or hashed


def login_required(f):
    @wraps(f)
    def wrapped(*args, **kwargs):
        if "logged_in" not in session:
            return redirect(url_for("login"))
        return f(*args, **kwargs)

    return wrapped


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        user = request.form["username"]
        pw = request.form["password"]
        if user == ADMIN_USER and pw == ADMIN_PASS:
            session["logged_in"] = True
            return redirect(url_for("dashboard"))
        else:
            flash("Invalid credentials", "danger")
    return render_template("login.html")


@app.route("/logout")
def logout():
    session.pop("logged_in", None)
    return redirect(url_for("login"))


# --- User management helpers ---
def load_users():
    users = []
    if os.path.exists(USERS_FILE):
        with open(USERS_FILE) as f:
            for line in f:
                if ":" in line:
                    user, pwd = line.strip().split(":", 1)
                    users.append((user, pwd))
    return users


def save_users(users):
    with open(USERS_FILE, "w") as f:
        for user, pwd in users:
            f.write(f"{user}:{pwd}\n")
    subprocess.run(["sudo", "systemctl", "restart", "anki-sync"])


# --- Dashboard (default page) ---
@app.route("/")
@login_required
def dashboard():
    students = [u for u, _ in load_users()]
    stats_list = []
    history = {}

    for student in students:
        try:
            stats = get_student_stats(student)
            if stats:
                stats_list.append(
                    {
                        "username": student,
                        "total_cards": stats.get("total", 0),
                        "due_cards": stats.get("due", 0),
                        "reviews_today": stats.get("reviews_today", 0),
                    }
                )
            # include review history
            history[student] = get_review_history(student, days=14)
        except Exception as e:
            print(f"⚠️ Error fetching stats for {student}: {e}")

    return render_template("dashboard.html", stats_list=stats_list, history=history)


# --- Manage Users ---
@app.route("/users")
@login_required
def manage_users():
    return render_template("manage_users.html", users=load_users())


@app.route("/add", methods=["POST"])
@login_required
def add_user():
    users = load_users()
    users.append((request.form["username"], request.form["password"]))
    save_users(users)
    return redirect(url_for("manage_users"))


@app.route("/delete", methods=["POST"])
@login_required
def delete_user():
    username = request.form["username"]
    users = [(u, p) for u, p in load_users() if u != username]
    save_users(users)
    return redirect(url_for("manage_users"))


# --- Push Deck ---
@app.route("/push_deck", methods=["GET", "POST"])
@login_required
def push_deck():
    students = [u for u, _ in load_users()]

    if request.method == "POST":
        file = request.files["deckfile"]
        if not file or not file.filename.endswith(".apkg"):
            flash("Upload an .apkg file", "error")
            return redirect(url_for("push_deck"))

        filepath = os.path.join(UPLOAD_FOLDER, file.filename)
        file.save(filepath)

        selected = request.form.getlist("students")
        for student in selected:
            target_dir = os.path.join(SYNC_BASE, student, "imports")
            os.makedirs(target_dir, exist_ok=True)
            shutil.copy(filepath, os.path.join(target_dir, file.filename))

        flash(f"Deck copied to: {', '.join(selected)}", "success")
        return redirect(url_for("push_deck"))

    return render_template("push_deck.html", students=students)


# --- Logs ---
@app.route("/logs")
@login_required
def logs():
    log_file = "/var/log/syslog"  # adjust as needed
    logs = ""
    try:
        with open(log_file, "r") as f:
            logs = "".join(f.readlines()[-200:])
    except Exception as e:
        logs = f"⚠️ Error reading logs: {e}"
    return render_template("logs.html", logs=logs)


# --- Student Dashboard ---
@app.route("/dashboard/<username>")
@login_required
def student_dashboard(username):
    stats = get_student_stats(username)
    history = get_review_history(username, days=30)
    deck_stats = get_deck_stats(username)
    full_stats = get_full_stats(username)
    review_time = get_review_time(username, days=30)
    fsrs_stats = get_fsrs_stats(username)

    if not stats:
        flash(f"No stats available for {username}", "error")
        return redirect(url_for("dashboard"))

    return render_template(
        "student_dashboard.html",
        stats=stats,
        history=history,
        deck_stats=deck_stats,
        student=username,
        full_stats=full_stats,
        review_time=review_time["daily"],
        avg_time=review_time["avg_time"],
        fsrs_stats=fsrs_stats
    )


# --- Helpers ---
def get_student_stats(username):
    db_path = os.path.join(SYNC_BASE, username, "collection.anki2")
    if not os.path.exists(db_path):
        return None

    conn = sqlite3.connect(db_path)
    c = conn.cursor()

    c.execute("SELECT COUNT(*) FROM cards")
    total = c.fetchone()[0] or 0

    c.execute("SELECT COUNT(*) FROM cards WHERE due <= strftime('%s','now')")
    due = c.fetchone()[0] or 0

    today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    tomorrow = today + timedelta(days=1)
    today_start = int(today.timestamp() * 1000)
    tomorrow_start = int(tomorrow.timestamp() * 1000)

    c.execute(
        "SELECT COUNT(*) FROM revlog WHERE id BETWEEN ? AND ?",
        (today_start, tomorrow_start),
    )
    reviews_today = c.fetchone()[0] or 0

    conn.close()
    return {"total": total, "due": due, "reviews_today": reviews_today}


def get_review_history(username, days=30):
    db_path = os.path.join(SYNC_BASE, username, "collection.anki2")
    if not os.path.exists(db_path):
        return []

    conn = sqlite3.connect(db_path)
    c = conn.cursor()

    start = datetime.now() - timedelta(days=days)
    start_ts = int(start.timestamp() * 1000)

    c.execute(
        """
        SELECT (id/1000), COUNT(*)
        FROM revlog
        WHERE id >= ?
        GROUP BY strftime('%Y-%m-%d', id/1000, 'unixepoch')
    """,
        (start_ts,),
    )

    history = [
        {"day": datetime.fromtimestamp(ts).strftime("%Y-%m-%d"), "count": count}
        for ts, count in c.fetchall()
    ]
    conn.close()
    return history


def get_deck_stats(username):
    db_path = os.path.join(SYNC_BASE, username, "collection.anki2")
    if not os.path.exists(db_path):
        return []

    conn = sqlite3.connect(db_path)
    c = conn.cursor()

    deck_map = {}
    try:
        c.execute("SELECT decks FROM col")
        row = c.fetchone()
        if row and row[0].strip():
            decks_json = json.loads(row[0])
            for did, info in decks_json.items():
                deck_map[int(did)] = info.get("name", f"Deck {did}")
    except Exception as e:
        print(f"⚠️ Deck map error: {e}")

    today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    tomorrow = today + timedelta(days=1)
    today_start = int(today.timestamp() * 1000)
    tomorrow_start = int(tomorrow.timestamp() * 1000)

    deck_stats = []
    for did, total, due in c.execute("""
        SELECT did, COUNT(*), SUM(due <= strftime('%s','now'))
        FROM cards
        GROUP BY did
    """):
        c.execute(
            """
            SELECT COUNT(*)
            FROM revlog r
            JOIN cards c ON r.cid = c.id
            WHERE c.did = ? AND r.id BETWEEN ? AND ?
        """,
            (did, today_start, tomorrow_start),
        )
        reviews_today = c.fetchone()[0]

        deck_stats.append(
            {
                "deck": deck_map.get(int(did), f"Deck {did}"),
                "total": total,
                "due": due or 0,
                "reviews_today": reviews_today,
            }
        )

    conn.close()
    return deck_stats


def get_full_stats(username):
    db_path = os.path.join(SYNC_BASE, username, "collection.anki2")
    if not os.path.exists(db_path):
        return {}

    conn = sqlite3.connect(db_path)
    c = conn.cursor()

    stats = {}

    # --- Card types ---
    c.execute("SELECT type, COUNT(*) FROM cards GROUP BY type")
    stats["card_types"] = {t: n for t, n in c.fetchall()}

    # --- Ease counts ---
    c.execute("SELECT ease, COUNT(*) FROM revlog GROUP BY ease")
    stats["ease_counts"] = {e: n for e, n in c.fetchall()}

    # --- Reviews per day (last 30 days) ---
    start = datetime.now() - timedelta(days=30)
    start_ts = int(start.timestamp() * 1000)
    c.execute(
        """
        SELECT strftime('%Y-%m-%d', id/1000, 'unixepoch'), COUNT(*)
        FROM revlog
        WHERE id >= ?
        GROUP BY strftime('%Y-%m-%d', id/1000, 'unixepoch')
    """,
        (start_ts,),
    )
    stats["reviews_per_day"] = c.fetchall()

    # --- Future due (next 30 days) ---
    today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    today_ts = int(today.timestamp())
    end_ts = int((today + timedelta(days=30)).timestamp())
    c.execute(
        """
        SELECT strftime('%Y-%m-%d', due, 'unixepoch'), COUNT(*)
        FROM cards
        WHERE due BETWEEN ? AND ?
        GROUP BY strftime('%Y-%m-%d', due, 'unixepoch')
    """,
        (today_ts, end_ts),
    )
    stats["future_due"] = c.fetchall()

    # --- Intervals histogram ---
    c.execute("SELECT ivl FROM cards WHERE ivl > 0")
    intervals = [row[0] for row in c.fetchall()]
    bins = [1, 3, 7, 15, 30, 90, 180, 365, 9999]  # similar to Anki buckets
    labels = ["1d", "3d", "1w", "2w", "1m", "3m", "6m", "1y+"]

    counts = [0] * (len(bins) - 1)
    for ivl in intervals:
        for i in range(len(bins) - 1):
            if bins[i] <= ivl < bins[i + 1]:
                counts[i] += 1
                break

    stats["intervals"] = list(zip(labels, counts))

    conn.close()
    return stats

def get_review_time(username, days=30):
    db_path = os.path.join(SYNC_BASE, username, "collection.anki2")
    if not os.path.exists(db_path):
        return {"daily": [], "avg_time": 0}

    conn = sqlite3.connect(db_path)
    c = conn.cursor()

    start = datetime.now() - timedelta(days=days)
    start_ts = int(start.timestamp() * 1000)

    # Total time + count for avg
    c.execute(
        """
        SELECT SUM(time)/1000.0, COUNT(*)
        FROM revlog
        WHERE id >= ?
    """,
        (start_ts,),
    )
    total_time, count = c.fetchone()
    avg_time = round(total_time / count, 2) if count else 0

    # Per day totals
    c.execute(
        """
        SELECT (id/1000) as day, SUM(time)/1000.0 as seconds
        FROM revlog
        WHERE id >= ?
        GROUP BY strftime('%Y-%m-%d', id/1000, 'unixepoch')
    """,
        (start_ts,),
    )
    rows = c.fetchall()
    conn.close()

    return {
        "daily": [
            {"date": datetime.fromtimestamp(ts).strftime("%Y-%m-%d"), "seconds": sec}
            for ts, sec in rows
        ],
        "avg_time": avg_time,
    }

# FSRS statistics
def get_fsrs_stats(username):
    db_path = os.path.join(SYNC_BASE, username, "collection.anki2")
    if not os.path.exists(db_path):
        return None

    conn = sqlite3.connect(db_path)
    rows = conn.execute("SELECT id, cid, ease, time, type FROM revlog").fetchall()
    conn.close()

    scheduler = Scheduler()
    cards = {}

    for id_, cid, ease, time_ms, type_ in rows:
        rating = Rating(ease)  # fsrs Rating expects exact enum
        review_time = datetime.fromtimestamp(id_ / 1000)
        if cid not in cards:
            cards[cid] = Card()
        scheduler.review_card(
            cards[cid], ReviewLog(rating=rating, timestamp=review_time)
        )

    vals = list(cards.values())
    return {
        "avg_difficulty": round(sum(c.difficulty for c in vals) / len(vals), 2),
        "avg_stability": round(sum(c.stability for c in vals) / len(vals), 2),
        "avg_retrievability": round(
            sum(c.retrievability(datetime.now()) for c in vals) / len(vals), 2
        ),
        "true_retention": round(
            sum(1 for _, _, ease_, _, _, _, _ in rows if ease_ > 1) / len(rows) * 100, 2
        ),
    }

if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=False)
