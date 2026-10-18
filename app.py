
import os
import secrets
import sqlite3
from datetime import date, datetime
import qrcode
from io import BytesIO
from flask import send_file

from dotenv import load_dotenv
from flask import (
    Flask, flash, redirect, render_template,
    request, url_for
)

# Find the folder containing this Python file
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# Load configuration from .env
load_dotenv(os.path.join(BASE_DIR, ".env"))

# Absolute path of the database
DB_PATH = os.path.join(BASE_DIR, "messtoken.db")

# Create Flask application
app = Flask(__name__, template_folder="template")
app.secret_key = os.environ["flask_secret_key"]

@app.route("/qr/<token_code>")
def show_qr(token_code):
    img = qrcode.make(token_code)
    buffer = BytesIO()
    img.save(buffer, format="PNG")
    buffer.seek(0)

    return send_file(buffer, mimetype="image/png")


def get_db():
    """Create and return a database connection."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    """Create the required tables and indexes."""
    conn = get_db()

    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS students (
                roll_no INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                assigned_mess TEXT NOT NULL
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS tokens (
                token_id INTEGER PRIMARY KEY AUTOINCREMENT,
                student_id INTEGER NOT NULL,
                selected_mess TEXT NOT NULL,
                meal_date TEXT NOT NULL,
                meal_type TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'issued',
                issue_time TEXT NOT NULL,
                token_code TEXT NOT NULL,
                served_time TEXT,
                FOREIGN KEY (student_id)
                    REFERENCES students(roll_no),
                UNIQUE (student_id, meal_date, meal_type)
            )
        """)

        # Support databases created by an earlier version
        columns = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(tokens)")
        }

        if "served_time" not in columns:
            conn.execute(
                "ALTER TABLE tokens ADD COLUMN served_time TEXT"
            )

        # Every token code must be unique
        conn.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS unique_token_code
            ON tokens(token_code)
        """)

        conn.commit()

    finally:
        conn.close()

@app.route("/enroll", methods=["GET", "POST"])
def enroll():
    if request.method == "POST":
        roll_no = request.form.get("roll_no", "").strip()
        name = request.form.get("name", "").strip()
        assigned_mess = request.form.get("assigned_mess", "").strip()

        if not roll_no or not name or assigned_mess not in ["A", "B"]:
            flash("Please enter valid student details.", "error")
            return redirect("/enroll")

        try:
            conn = sqlite3.connect(DB_PATH)
            conn.execute(
                """INSERT INTO students (roll_no, name, assigned_mess)
                   VALUES (?, ?, ?)""",
                (roll_no, name, assigned_mess)
            )
            conn.commit()
            conn.close()
            flash("Student enrolled successfully!", "success")

        except sqlite3.IntegrityError:
            flash("This roll number is already enrolled.", "error")

        return redirect("/enroll")

    return render_template("enroll.html")

def issue_token(roll_no, selected_mess, meal_type, meal_date):
    """Validate a request and issue a meal token."""

    selected_mess = selected_mess.strip()
    meal_type = meal_type.strip().lower()
    meal_date = meal_date.strip()

    if not selected_mess:
        return False, "Please select a mess."

    if meal_type not in {"breakfast", "lunch", "dinner"}:
        return False, "Please select a valid meal."

    try:
        parsed_date = date.fromisoformat(meal_date)
    except ValueError:
        return False, "Invalid date. Use YYYY-MM-DD."

    if parsed_date < date.today():
        return False, "Tokens cannot be issued for a past date."

    conn = get_db()

    try:
        # Check whether the student exists
        student = conn.execute(
            """
            SELECT roll_no, name, assigned_mess
            FROM students
            WHERE roll_no = ?
            """,
            (roll_no,)
        ).fetchone()

        if student is None:
            return False, "Student roll number was not found."

        # Check mess eligibility
        if (
            student["assigned_mess"].strip().casefold()
            != selected_mess.casefold()
        ):
            return False, "You are not eligible for this mess."

        # Prevent duplicate tokens for the same meal
        existing = conn.execute(
            """
            SELECT token_code, status
            FROM tokens
            WHERE student_id = ?
              AND meal_date = ?
              AND meal_type = ?
            """,
            (roll_no, meal_date, meal_type)
        ).fetchone()

        if existing:
            return False, (
                f"A token already exists. Code: "
                f"{existing['token_code']}"
            )

        # Generate a unique token code
        token_code = secrets.token_urlsafe(9).upper()
        issue_time = datetime.now().isoformat(timespec="seconds")

        # Save the new token
        conn.execute(
            """
            INSERT INTO tokens (
                student_id, selected_mess, meal_date,
                meal_type, issue_time, token_code, status
            )
            VALUES (?, ?, ?, ?, ?, ?, 'issued')
            """,
            (
                roll_no, selected_mess, meal_date,
                meal_type, issue_time, token_code
            )
        )

        conn.commit()

        return True, (
            f"Token issued for {student['name']}: {token_code}"
        )

    except sqlite3.IntegrityError:
        conn.rollback()
        return False, "A token already exists for this meal."

    finally:
        conn.close()


def redeem_token(token_code):
    """Verify and mark a token as served."""

    token_code = token_code.strip()

    if not token_code:
        return False, "Please enter a token code."

    conn = get_db()

    try:
        # Find the token and its student
        token = conn.execute(
            """
            SELECT
                t.token_id, t.status, t.meal_date,
                t.meal_type, t.selected_mess,
                s.name, s.roll_no
            FROM tokens t
            JOIN students s ON s.roll_no = t.student_id
            WHERE t.token_code = ?
            """,
            (token_code,)
        ).fetchone()

        if token is None:
            return False, "Token not found."

        if token["status"] == "served":
            return False, "This token has already been redeemed."

        if token["meal_date"] != date.today().isoformat():
            return False, "This token is not valid for today's meal."

        served_time = datetime.now().isoformat(timespec="seconds")

        # Only update a token that has not already been served
        cursor = conn.execute(
            """
            UPDATE tokens
            SET status = 'served', served_time = ?
            WHERE token_id = ? AND status = 'issued'
            """,
            (served_time, token["token_id"])
        )

        conn.commit()

        if cursor.rowcount != 1:
            return False, "This token was already redeemed."

        return True, (
            f"Served: {token['name']} (roll {token['roll_no']}), "
            f"{token['selected_mess']} mess, {token['meal_type']}."
        )

    finally:
        conn.close()


@app.route("/")
def home():
    return render_template(
        "index.html",
        today=date.today().isoformat()
    )


@app.route("/get-token", methods=["POST"])
def get_token():
    roll_text = request.form.get("roll_no", "").strip()
    selected_mess = request.form.get("selected_mess", "").strip()
    meal_type = request.form.get("meal_type", "").strip()
    meal_date = request.form.get("meal_date", "").strip()

    if not roll_text.isdigit():
        flash("Enter a valid numeric roll number.", "error")
        return redirect(url_for("home"))

    ok, message = issue_token(
        int(roll_text),
        selected_mess,
        meal_type,
        meal_date
    )

    flash(message, "success" if ok else "error")
    return redirect(url_for("home"))


@app.route("/staff", methods=["GET", "POST"])
def staff():
    if request.method == "POST":
        ok, message = redeem_token(
            request.form.get("token_code", "")
        )

        flash(message, "success" if ok else "error")
        return redirect(url_for("staff"))

    return render_template("staff.html")


@app.route("/stats")
def stats():
    conn = get_db()

    try:
        totals = conn.execute("""
            SELECT
                COUNT(*) AS issued,
                SUM(
                    CASE WHEN status = 'served'
                    THEN 1 ELSE 0 END
                ) AS served
            FROM tokens
        """).fetchone()

        return render_template(
            "stats.html",
            issued=totals["issued"] or 0,
            served=totals["served"] or 0
        )

    finally:
        conn.close()


if __name__ == "__main__":
    init_db()
    app.run(debug=False)
