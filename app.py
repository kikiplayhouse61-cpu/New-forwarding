"""
Flask dashboard for Telegram Forwarder.
"""
import asyncio
import csv
import io
import logging
import os
import secrets
import time
from datetime import datetime, timezone
from functools import wraps

import bcrypt
from flask import (Flask, render_template, request, redirect, url_for,
                   flash, session, jsonify, Response, g)

import database as db
import bot as tg_bot
import scheduler as sched

# ── App setup ─────────────────────────────────────────────────────────────────

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", secrets.token_hex(32))
app.config["SESSION_COOKIE_SECURE"] = os.environ.get("RAILWAY_ENVIRONMENT") == "production"
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger(__name__)

START_TIME = time.time()

# ── Init ──────────────────────────────────────────────────────────────────────

with app.app_context():
    db.init_db()
    sched.get_scheduler()
    sched.schedule_daily_reset()

    # Create default admin if none exists
    admin_user = os.environ.get("ADMIN_USERNAME", "admin")
    admin_pass = os.environ.get("ADMIN_PASSWORD", "changeme123")
    if not db.admin_exists():
        hashed = bcrypt.hashpw(admin_pass.encode(), bcrypt.gensalt()).decode()
        db.create_admin(admin_user, hashed)
        logger.info("Default admin created: %s", admin_user)


# ── Auth helpers ──────────────────────────────────────────────────────────────

def login_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not session.get("admin_logged_in"):
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return wrapper


def run_async(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# ── Auth routes ───────────────────────────────────────────────────────────────

@app.route("/login", methods=["GET", "POST"])
def login():
    if session.get("admin_logged_in"):
        return redirect(url_for("dashboard"))
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        admin = db.get_admin(username)
        if admin and bcrypt.checkpw(password.encode(), admin["password_hash"].encode()):
            session["admin_logged_in"] = True
            session["admin_username"] = username
            flash("Logged in successfully.", "success")
            return redirect(url_for("dashboard"))
        flash("Invalid credentials.", "danger")
    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    flash("Logged out.", "info")
    return redirect(url_for("login"))


# ── Dashboard ─────────────────────────────────────────────────────────────────

@app.route("/")
@login_required
def dashboard():
    stats = db.get_dashboard_stats()
    campaigns = db.get_campaigns()
    notifications = sched.get_notifications(clear=False)
    uptime_seconds = int(time.time() - START_TIME)
    return render_template("dashboard.html",
                           stats=stats,
                           campaigns=campaigns,
                           notifications=notifications[:10],
                           uptime=uptime_seconds)


# ── Accounts ──────────────────────────────────────────────────────────────────

@app.route("/accounts")
@login_required
def accounts():
    all_accounts = db.get_accounts()
    return render_template("accounts.html", accounts=all_accounts)


@app.route("/accounts/add", methods=["POST"])
@login_required
def add_account():
    token = request.form.get("bot_token", "").strip()
    if not token:
        flash("Bot token is required.", "danger")
        return redirect(url_for("accounts"))

    info = run_async(tg_bot.validate_bot_token(token))
    if not info:
        flash("Invalid bot token or Telegram API unreachable.", "danger")
        return redirect(url_for("accounts"))

    db.add_account(
        bot_token=token,
        bot_username=info.get("username", ""),
        bot_name=info.get("first_name", "")
    )
    flash(f"Bot @{info.get('username')} added successfully.", "success")
    return redirect(url_for("accounts"))


@app.route("/accounts/<int:account_id>/delete", methods=["POST"])
@login_required
def delete_account(account_id):
    db.delete_account(account_id)
    flash("Account removed.", "info")
    return redirect(url_for("accounts"))


# ── Groups ────────────────────────────────────────────────────────────────────

@app.route("/groups")
@login_required
def groups():
    all_groups = db.get_groups(active_only=False)
    all_accounts = db.get_accounts()
    return render_template("groups.html", groups=all_groups, accounts=all_accounts)


@app.route("/groups/add", methods=["POST"])
@login_required
def add_group():
    account_id = request.form.get("account_id")
    chat_id = request.form.get("chat_id", "").strip()
    manual_name = request.form.get("group_name", "").strip()

    if not account_id or not chat_id:
        flash("Account and Chat ID are required.", "danger")
        return redirect(url_for("groups"))

    account = db.get_account(int(account_id))
    if not account:
        flash("Account not found.", "danger")
        return redirect(url_for("groups"))

    info = run_async(tg_bot.fetch_group_info(account["bot_token"], chat_id))
    if info:
        db.add_group(
            account_id=int(account_id),
            chat_id=info["chat_id"],
            group_name=info["group_name"],
            group_type=info["group_type"],
            member_count=info["member_count"],
        )
        flash(f"Group \"{info['group_name']}\" added.", "success")
    elif manual_name:
        db.add_group(int(account_id), chat_id, manual_name)
        flash(f"Group added manually as \"{manual_name}\".", "warning")
    else:
        flash("Could not fetch group info. Add manually or check the bot has access.", "danger")

    return redirect(url_for("groups"))


@app.route("/groups/<int:group_id>/toggle", methods=["POST"])
@login_required
def toggle_group(group_id):
    is_active = int(request.form.get("is_active", 1))
    db.toggle_group(group_id, is_active)
    return jsonify({"ok": True})


@app.route("/groups/<int:group_id>/delete", methods=["POST"])
@login_required
def delete_group(group_id):
    db.delete_group(group_id)
    flash("Group removed.", "info")
    return redirect(url_for("groups"))


# ── Campaigns ─────────────────────────────────────────────────────────────────

@app.route("/campaigns")
@login_required
def campaigns():
    all_campaigns = db.get_campaigns()
    all_accounts = db.get_accounts()
    all_groups = db.get_groups()
    templates = db.get_templates()
    return render_template("campaigns.html",
                           campaigns=all_campaigns,
                           accounts=all_accounts,
                           groups=all_groups,
                           templates=templates)


@app.route("/campaigns/create", methods=["POST"])
@login_required
def create_campaign():
    name = request.form.get("name", "").strip()
    account_id = request.form.get("account_id")
    message_text = request.form.get("message_text", "").strip()
    interval_seconds = int(request.form.get("interval_seconds", 3600))
    group_ids = [int(x) for x in request.form.getlist("group_ids")]
    start_time = request.form.get("start_time") or None
    end_time = request.form.get("end_time") or None

    if not all([name, account_id, message_text, group_ids]):
        flash("Name, account, message and at least one group are required.", "danger")
        return redirect(url_for("campaigns"))

    cid = db.create_campaign(
        name=name,
        account_id=int(account_id),
        message_text=message_text,
        interval_seconds=max(interval_seconds, 10),
        group_ids=group_ids,
        start_time=start_time,
        end_time=end_time,
    )
    flash(f"Campaign \"{name}\" created (ID {cid}).", "success")
    return redirect(url_for("campaigns"))


@app.route("/campaigns/<int:campaign_id>/start", methods=["POST"])
@login_required
def start_campaign(campaign_id):
    sched.start_campaign(campaign_id)
    flash("Campaign started.", "success")
    return redirect(url_for("campaigns"))


@app.route("/campaigns/<int:campaign_id>/pause", methods=["POST"])
@login_required
def pause_campaign(campaign_id):
    sched.pause_campaign(campaign_id)
    flash("Campaign paused.", "warning")
    return redirect(url_for("campaigns"))


@app.route("/campaigns/<int:campaign_id>/resume", methods=["POST"])
@login_required
def resume_campaign(campaign_id):
    sched.resume_campaign(campaign_id)
    flash("Campaign resumed.", "success")
    return redirect(url_for("campaigns"))


@app.route("/campaigns/<int:campaign_id>/stop", methods=["POST"])
@login_required
def stop_campaign(campaign_id):
    sched.stop_campaign(campaign_id)
    flash("Campaign stopped.", "danger")
    return redirect(url_for("campaigns"))


@app.route("/campaigns/<int:campaign_id>/run_now", methods=["POST"])
@login_required
def run_now(campaign_id):
    results = sched.run_now(campaign_id)
    flash(f"Sent now — Success: {results.get('success', 0)}, Failed: {results.get('failed', 0)}", "info")
    return redirect(url_for("campaigns"))


@app.route("/campaigns/<int:campaign_id>/duplicate", methods=["POST"])
@login_required
def duplicate_campaign(campaign_id):
    new_id = db.duplicate_campaign(campaign_id)
    flash(f"Campaign duplicated (ID {new_id}).", "success")
    return redirect(url_for("campaigns"))


@app.route("/campaigns/<int:campaign_id>/delete", methods=["POST"])
@login_required
def delete_campaign(campaign_id):
    sched.stop_campaign(campaign_id)
    db.delete_campaign(campaign_id)
    flash("Campaign deleted.", "info")
    return redirect(url_for("campaigns"))


@app.route("/campaigns/<int:campaign_id>/edit", methods=["POST"])
@login_required
def edit_campaign(campaign_id):
    kwargs = {}
    for field in ("name", "message_text", "interval_seconds", "start_time", "end_time"):
        val = request.form.get(field)
        if val is not None:
            kwargs[field] = val if val else None
    if "interval_seconds" in kwargs:
        kwargs["interval_seconds"] = max(int(kwargs["interval_seconds"]), 10)
    db.update_campaign(campaign_id, **kwargs)
    # Reschedule if running
    c = db.get_campaign(campaign_id)
    if c and c["status"] == "running":
        sched.start_campaign(campaign_id)
    flash("Campaign updated.", "success")
    return redirect(url_for("campaigns"))


# ── Templates ─────────────────────────────────────────────────────────────────

@app.route("/templates/add", methods=["POST"])
@login_required
def add_template():
    name = request.form.get("name", "").strip()
    content = request.form.get("content", "").strip()
    if not name or not content:
        return jsonify({"ok": False, "error": "Name and content required"})
    db.add_template(name, content)
    return jsonify({"ok": True})


@app.route("/templates/<int:tid>/delete", methods=["POST"])
@login_required
def delete_template(tid):
    db.delete_template(tid)
    return jsonify({"ok": True})


# ── Logs ──────────────────────────────────────────────────────────────────────

@app.route("/logs")
@login_required
def logs():
    page = int(request.args.get("page", 1))
    limit = 50
    offset = (page - 1) * limit
    filters = {
        "group_id": request.args.get("group_id") or None,
        "campaign_id": request.args.get("campaign_id") or None,
        "status": request.args.get("status") or None,
        "date_from": request.args.get("date_from") or None,
        "date_to": request.args.get("date_to") or None,
    }
    log_rows = db.get_logs(limit=limit, offset=offset, **filters)
    all_groups = db.get_groups(active_only=False)
    all_campaigns = db.get_campaigns()
    return render_template("logs.html",
                           logs=log_rows,
                           page=page,
                           groups=all_groups,
                           campaigns=all_campaigns,
                           filters=filters)


@app.route("/logs/export")
@login_required
def export_logs():
    rows = db.get_logs(limit=10000)
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=[
        "id", "sent_at", "campaign_name", "group_name",
        "status", "error_reason", "message_preview"
    ])
    writer.writeheader()
    for row in rows:
        writer.writerow({k: row.get(k, "") for k in writer.fieldnames})
    output.seek(0)
    return Response(
        output.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment;filename=message_logs.csv"}
    )


# ── Reports ───────────────────────────────────────────────────────────────────

@app.route("/reports")
@login_required
def reports():
    period = request.args.get("period", "daily")
    report_rows = db.get_reports(period)
    return render_template("reports.html", rows=report_rows, period=period)


@app.route("/reports/export")
@login_required
def export_report():
    period = request.args.get("period", "daily")
    rows = db.get_reports(period)
    output = io.StringIO()
    if rows:
        writer = csv.DictWriter(output, fieldnames=rows[0].keys())
        writer.writeheader()
        writer.writerows(rows)
    output.seek(0)
    return Response(
        output.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment;filename=report_{period}.csv"}
    )


# ── Settings ──────────────────────────────────────────────────────────────────

@app.route("/settings", methods=["GET", "POST"])
@login_required
def settings():
    if request.method == "POST":
        for key in ("max_retries", "retry_delay", "daily_summary_time",
                    "notification_on_fail", "backup_enabled"):
            val = request.form.get(key)
            if val is not None:
                db.set_setting(key, val)
        # Password change
        new_pass = request.form.get("new_password", "").strip()
        if new_pass:
            hashed = bcrypt.hashpw(new_pass.encode(), bcrypt.gensalt()).decode()
            import sqlite3
            with db.get_db() as conn:
                conn.execute(
                    "UPDATE admin SET password_hash=? WHERE username=?",
                    (hashed, session.get("admin_username"))
                )
        flash("Settings saved.", "success")
        return redirect(url_for("settings"))

    current = db.get_settings()
    return render_template("settings.html", settings=current)


# ── API endpoints (JSON) ──────────────────────────────────────────────────────

@app.route("/api/stats")
@login_required
def api_stats():
    return jsonify(db.get_dashboard_stats())


@app.route("/api/notifications")
@login_required
def api_notifications():
    items = sched.get_notifications(clear=True)
    return jsonify(items)


@app.route("/api/campaigns")
@login_required
def api_campaigns():
    return jsonify(db.get_campaigns())


# ── Health check (Railway) ────────────────────────────────────────────────────

@app.route("/health")
def health():
    uptime = int(time.time() - START_TIME)
    return jsonify({
        "status": "ok",
        "uptime_seconds": uptime,
        "timestamp": datetime.now(timezone.utc).isoformat()
    })


# ── Main ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    debug = os.environ.get("FLASK_DEBUG", "false").lower() == "true"
    app.run(host="0.0.0.0", port=port, debug=debug)
