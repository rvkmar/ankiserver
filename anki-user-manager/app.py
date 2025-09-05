import os
import json
import sqlite3
import shutil
import subprocess
import bcrypt
from datetime import datetime, timedelta
from functools import wraps
from flask import Flask, render_template, request, redirect, url_for, flash, session #, jsonify
import sqlite3, tempfile, pandas as pd

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
ADMIN_PASS_HASH = "$2b$12$wE8i6eepCsjfgVI9Dpjev.3d5IEAu0hPUF/j1Wj5TEq.ochOH1b4K"  # TODO: replace with env var or hashed


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

        if user == ADMIN_USER and bcrypt.checkpw(
            pw.encode("utf-8"), ADMIN_PASS_HASH.encode("utf-8")
        ):
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
    # history = {}

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
            # history[student] = get_review_history(student, days=14)
        except Exception as e:
            print(f"⚠️ Error fetching stats for {student}: {e}")

    return render_template("dashboard.html", stats_list=stats_list) #, history=history)


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
    try:
        stats = get_student_stats(username) or []
        history = get_review_history(username, days=30) or []
        deck_stats = get_deck_stats(username) or []
        full_stats = get_full_stats(username) or {}
        review_time = get_review_time(username, days=30) or {"daily": [], "avg_time": 0}
        fsrs_stats = get_fsrs_stats(username)

    except Exception as e:
        import traceback

        traceback.print_exc()
        flash(f"Error loading stats for {username}: {e}", "danger")
        return redirect(url_for("dashboard"))

    return render_template(
        "student_dashboard.html",
        stats=stats,
        history=history,
        deck_stats=deck_stats,
        student=username,
        full_stats=full_stats,
        review_time=review_time.get("daily", []),
        avg_time=review_time.get("avg_time", 0),
        fsrs_stats=fsrs_stats,  # ✅ always passed
    )

# --- Helpers ---
# Updated def_student_stats to use a safe temporary copy of the DB to avoid locking issues
def get_student_stats(username):
    tmp_path = safe_copy_db(username)
    if not tmp_path:
        return None

    total, due, reviews_today = 0, 0, 0
    try:
        conn = sqlite3.connect(tmp_path)
        c = conn.cursor()

        try:
            c.execute("SELECT COUNT(*) FROM cards")
            total = c.fetchone()[0] or 0
        except Exception as e:
            print(f"⚠️ total cards error for {username}: {e}")

        try:
            # NOTE: In some Anki versions, `due` is days since epoch, not timestamp
            c.execute("SELECT COUNT(*) FROM cards WHERE due <= strftime('%s','now')")
            due = c.fetchone()[0] or 0
        except Exception as e:
            print(f"⚠️ due cards error for {username}: {e}")

        try:
            today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
            tomorrow = today + timedelta(days=1)
            today_start = int(today.timestamp() * 1000)
            tomorrow_start = int(tomorrow.timestamp() * 1000)

            c.execute(
                "SELECT COUNT(*) FROM revlog WHERE id BETWEEN ? AND ?",
                (today_start, tomorrow_start),
            )
            reviews_today = c.fetchone()[0] or 0
        except Exception as e:
            print(f"⚠️ reviews_today error for {username}: {e}")

    finally:
        conn.close()
        os.remove(tmp_path)

    return {"total": total, "due": due, "reviews_today": reviews_today}

# Updated helper functions to use a safe temporary copy of the DB to avoid locking issues
# def safe_copy_db(username):
#     """Return a safe temporary copy of the user DB or None if missing."""
#     db_path = os.path.join(SYNC_BASE, username, "collection.anki2")
#     if not os.path.exists(db_path):
#         return None
#     with tempfile.NamedTemporaryFile(delete=False) as tmp:
#         tmp_path = tmp.name
#     shutil.copy(db_path, tmp_path)
#     return tmp_path

def safe_copy_db(username):
    """
    Make a safe temporary copy of the user's collection.anki2.
    Returns the path to the copy, or None if it fails.
    """
    try:
        src_path = f"/home/ubuntu/ankiserver/anki-sync-data/{username}/collection.anki2"
        if not os.path.exists(src_path):
            return None

        # Create a unique temporary file
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".anki2")
        tmp.close()  # close the handle so sqlite can open it
        shutil.copy2(src_path, tmp.name)
        return tmp.name
    except Exception as e:
        app.logger.error(f"safe_copy_db failed for {username}: {e}")
        return None


def get_review_history(username, days=30):
    tmp_path = safe_copy_db(username)
    if not tmp_path:
        return []
    history = []
    try:
        conn = sqlite3.connect(tmp_path)
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
    except Exception as e:
        print(f"⚠️ get_review_history error for {username}: {e}")
    finally:
        conn.close()
        os.remove(tmp_path)
    return history

# Make deck stats more robust with list
# def get_deck_stats(username):
#     tmp_path = safe_copy_db(username)
#     if not tmp_path:
#         return []

#     stats = []
#     deck_map = {}
#     try:
#         conn = sqlite3.connect(tmp_path)
#         c = conn.cursor()

#         # --- Try col.decks JSON ---
#         try:
#             c.execute("SELECT decks FROM col")
#             row = c.fetchone()
#             if row and row[0] and row[0].strip():
#                 decks_json = json.loads(row[0])
#                 for key, info in decks_json.items():
#                     deck_map[int(key)] = info.get("name", f"Deck {key}")
#         except Exception:
#             pass

#         # --- Fallback: legacy decks table ---
#         if not deck_map:
#             try:
#                 c.execute("SELECT id, name FROM decks")
#                 for did, name in c.fetchall():
#                     deck_map[int(did)] = name
#             except Exception:
#                 pass

#         # --- Initialize stats for all decks ---
#         for did, name in deck_map.items():
#             stats.append(
#                 {
#                     "deck": name,
#                     "total": 0,
#                     "due": 0,
#                     "reviews_today": 0,
#                     "id": did,
#                     "is_total": False,  # 👈 marker
#                 }
#             )

#         # --- Update counts from cards ---
#         c.execute(
#             "SELECT did, COUNT(*), SUM(due <= strftime('%s','now')) FROM cards GROUP BY did"
#         )
#         counts = {did: (total, due or 0) for did, total, due in c.fetchall()}

#         for s in stats:
#             did = s["id"]
#             if did in counts:
#                 total, due = counts[did]
#                 s["total"] = total
#                 s["due"] = due

#         # --- Update reviews today ---
#         today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
#         tomorrow = today + timedelta(days=1)
#         today_start = int(today.timestamp() * 1000)
#         tomorrow_start = int(tomorrow.timestamp() * 1000)

#         c.execute(
#             """
#             SELECT c.did, COUNT(*)
#             FROM revlog r
#             JOIN cards c ON r.cid = c.id
#             WHERE r.id BETWEEN ? AND ?
#             GROUP BY c.did
#         """,
#             (today_start, tomorrow_start),
#         )
#         review_counts = {did: cnt for did, cnt in c.fetchall()}

#         for s in stats:
#             did = s["id"]
#             if did in review_counts:
#                 s["reviews_today"] = review_counts[did]

#         # --- Add total row ---
#         total_row = {
#             "deck": "Total (All Decks)",
#             "total": sum(s["total"] for s in stats),
#             "due": sum(s["due"] for s in stats),
#             "reviews_today": sum(s["reviews_today"] for s in stats),
#             "id": -1,
#             "is_total": True,  # 👈 marker
#         }
#         stats.append(total_row)

#     finally:
#         conn.close()
#         os.remove(tmp_path)

#     return stats

def get_deck_stats(username):
    tmp_path = safe_copy_db(username)
    if not tmp_path:
        return []

    stats = []
    deck_map = {}
    conn = None
    try:
        conn = sqlite3.connect(tmp_path)
        c = conn.cursor()

        # --- Try col.decks JSON ---
        try:
            c.execute("SELECT decks FROM col")
            row = c.fetchone()
            if row and row[0] and row[0].strip():
                decks_json = json.loads(row[0])
                for key, info in decks_json.items():
                    deck_map[int(key)] = info.get("name", f"Deck {key}")
        except Exception:
            pass

        # --- Fallback: legacy decks table ---
        if not deck_map:
            try:
                c.execute("SELECT id, name FROM decks")
                for did, name in c.fetchall():
                    deck_map[int(did)] = name
            except Exception:
                pass

        # --- Initialize stats for all decks ---
        for did, name in deck_map.items():
            stats.append(
                {
                    "deck": name,
                    "total": 0,
                    "due": 0,
                    "reviews_today": 0,
                    "id": did,
                    "is_total": False,  # 👈 marker
                }
            )

        # --- Update counts from cards ---
        c.execute(
            "SELECT did, COUNT(*), SUM(due <= strftime('%s','now')) FROM cards GROUP BY did"
        )
        counts = {did: (total, due or 0) for did, total, due in c.fetchall()}

        for s in stats:
            did = s["id"]
            if did in counts:
                total, due = counts[did]
                s["total"] = total
                s["due"] = due

        # --- Update reviews today ---
        today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        tomorrow = today + timedelta(days=1)
        today_start = int(today.timestamp() * 1000)
        tomorrow_start = int(tomorrow.timestamp() * 1000)

        c.execute(
            """
            SELECT c.did, COUNT(*)
            FROM revlog r
            JOIN cards c ON r.cid = c.id
            WHERE r.id BETWEEN ? AND ?
            GROUP BY c.did
            """,
            (today_start, tomorrow_start),
        )
        review_counts = {did: cnt for did, cnt in c.fetchall()}

        for s in stats:
            did = s["id"]
            if did in review_counts:
                s["reviews_today"] = review_counts[did]

        # --- Add total row ---
        total_row = {
            "deck": "Total (All Decks)",
            "total": sum(s["total"] for s in stats),
            "due": sum(s["due"] for s in stats),
            "reviews_today": sum(s["reviews_today"] for s in stats),
            "id": -1,
            "is_total": True,  # 👈 marker
        }
        stats.append(total_row)

    finally:
        if conn:
            conn.close()
        if os.path.exists(tmp_path):
            os.remove(tmp_path)  # ✅ cleanup always

    return stats


def get_full_stats(username):
    tmp_path = safe_copy_db(username)
    if not tmp_path:
        return {}
    stats = {}
    try:
        conn = sqlite3.connect(tmp_path)
        c = conn.cursor()

        try:
            c.execute("SELECT type, COUNT(*) FROM cards GROUP BY type")
            stats["card_types"] = {t: n for t, n in c.fetchall()}
        except Exception:
            stats["card_types"] = {}

        try:
            c.execute("SELECT ease, COUNT(*) FROM revlog GROUP BY ease")
            stats["ease_counts"] = {e: n for e, n in c.fetchall()}
        except Exception:
            stats["ease_counts"] = {}

        try:
            start = datetime.now() - timedelta(days=30)
            start_ts = int(start.timestamp() * 1000)
            c.execute(
                """
                SELECT strftime('%Y-%m-%d', id/1000, 'unixepoch'), COUNT(*)
                FROM revlog WHERE id >= ?
                GROUP BY strftime('%Y-%m-%d', id/1000, 'unixepoch')
            """,
                (start_ts,),
            )
            stats["reviews_per_day"] = c.fetchall()
        except Exception:
            stats["reviews_per_day"] = []

        try:
            today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
            today_ts = int(today.timestamp())
            end_ts = int((today + timedelta(days=30)).timestamp())
            c.execute(
                """
                SELECT strftime('%Y-%m-%d', due, 'unixepoch'), COUNT(*)
                FROM cards WHERE due BETWEEN ? AND ?
                GROUP BY strftime('%Y-%m-%d', due, 'unixepoch')
            """,
                (today_ts, end_ts),
            )
            stats["future_due"] = c.fetchall()
        except Exception:
            stats["future_due"] = []

        try:
            c.execute("SELECT ivl FROM cards WHERE ivl > 0")
            intervals = [row[0] for row in c.fetchall()]
            bins = [1, 3, 7, 15, 30, 90, 180, 365, 9999]
            labels = ["1d", "3d", "1w", "2w", "1m", "3m", "6m", "1y+"]
            counts = [0] * (len(bins) - 1)
            for ivl in intervals:
                for i in range(len(bins) - 1):
                    if bins[i] <= ivl < bins[i + 1]:
                        counts[i] += 1
                        break
            stats["intervals"] = list(zip(labels, counts))
        except Exception:
            stats["intervals"] = []
    finally:
        conn.close()
        os.remove(tmp_path)
    return stats


def get_review_time(username, days=30):
    tmp_path = safe_copy_db(username)
    if not tmp_path:
        return {"daily": [], "avg_time": 0}
    avg_time, daily = 0, []
    try:
        conn = sqlite3.connect(tmp_path)
        c = conn.cursor()
        start = datetime.now() - timedelta(days=days)
        start_ts = int(start.timestamp() * 1000)

        try:
            c.execute(
                """
                SELECT SUM(time)/1000.0, COUNT(*)
                FROM revlog WHERE id >= ?
            """,
                (start_ts,),
            )
            total_time, count = c.fetchone()
            avg_time = round(total_time / count, 2) if count else 0
        except Exception:
            avg_time = 0

        try:
            c.execute(
                """
                SELECT (id/1000) as day, SUM(time)/1000.0 as seconds
                FROM revlog WHERE id >= ?
                GROUP BY strftime('%Y-%m-%d', id/1000, 'unixepoch')
            """,
                (start_ts,),
            )
            daily = [
                {
                    "date": datetime.fromtimestamp(ts).strftime("%Y-%m-%d"),
                    "seconds": sec,
                }
                for ts, sec in c.fetchall()
            ]
        except Exception:
            daily = []
    finally:
        conn.close()
        os.remove(tmp_path)
    return {"daily": daily, "avg_time": avg_time}

def get_fsrs_stats(username, days=30):
    tmp_path = safe_copy_db(username)
    if not tmp_path:
        return {
            "avg_difficulty": 0,
            "avg_stability": 0,
            "avg_retrievability": 0,
            "true_retention": 0,
            "is_dummy": True,
        }

    stats = {
        "avg_difficulty": None,
        "avg_stability": None,
        "avg_retrievability": None,
        "true_retention": None,
        "is_dummy": False,
    }

    try:
        conn = sqlite3.connect(tmp_path)
        c = conn.cursor()

        # --- 1. Avg difficulty ---
        try:
            c.execute("SELECT ease, COUNT(*) FROM revlog GROUP BY ease")
            ease_counts = dict(c.fetchall())
            total_reviews = sum(ease_counts.values())
            if total_reviews > 0:
                weighted_sum = sum(e * cnt for e, cnt in ease_counts.items())
                stats["avg_difficulty"] = round(weighted_sum / total_reviews, 2)
        except Exception as e:
            print(f"⚠️ FSRS difficulty calc failed: {e}")

        # --- 2. Avg stability ---
        try:
            c.execute("SELECT AVG(ivl) FROM cards WHERE ivl > 0")
            avg_ivl = c.fetchone()[0]
            if avg_ivl:
                stats["avg_stability"] = round(avg_ivl, 2)
        except Exception as e:
            print(f"⚠️ FSRS stability calc failed: {e}")

        # --- 3. Avg retrievability ---
        try:
            since = datetime.now() - timedelta(days=days)
            since_ts = int(since.timestamp() * 1000)
            c.execute(
                "SELECT ease, COUNT(*) FROM revlog WHERE id >= ? GROUP BY ease",
                (since_ts,),
            )
            rows = c.fetchall()
            correct = sum(cnt for ease, cnt in rows if ease > 1)
            total = sum(cnt for _, cnt in rows)
            if total > 0:
                stats["avg_retrievability"] = round(correct / total, 2)
        except Exception as e:
            print(f"⚠️ FSRS retrievability calc failed: {e}")

        # --- 4. True retention ---
        try:
            c.execute(
                "SELECT COUNT(*), SUM(CASE WHEN ease > 1 THEN 1 ELSE 0 END) FROM revlog"
            )
            total, correct = c.fetchone()
            if total and correct is not None:
                stats["true_retention"] = round((correct / total) * 100, 2)
        except Exception as e:
            print(f"⚠️ FSRS retention calc failed: {e}")

    finally:
        conn.close()
        os.remove(tmp_path)

    # Fallback if any missing
    defaults = {
        "avg_difficulty": 2.5,
        "avg_stability": 15,
        "avg_retrievability": 0.85,
        "true_retention": 92.3,
    }
    for key, val in list(stats.items()):
        if key != "is_dummy" and val is None:
            stats[key] = defaults[key]
            stats["is_dummy"] = True  # mark as estimated

    return stats


# --- Run App ---
if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=False)
