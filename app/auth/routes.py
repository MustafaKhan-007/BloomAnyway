"""Passwordless magic-link authentication."""
import hashlib
import logging
import re
import secrets
from datetime import datetime, timedelta

from flask import (current_app, flash, redirect, render_template, request,
                   session, url_for)
from flask_login import current_user, login_required, login_user, logout_user

from ..extensions import db, limiter
from ..models import LoginToken, User, utcnow
from ..services.mailer import send_login_link
from . import bp

log = logging.getLogger(__name__)

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
UNIFORM_MESSAGE = ("If that address exists (or is new \u2014 welcome), a login link is on "
                   "its way. It works for 15 minutes. Check your spam folder too.")


def _normalize(email: str) -> str:
    return (email or "").strip().lower()


def _email_key():
    """Rate-limit key: the submitted email (3 requests/email/hour)."""
    return _normalize(request.form.get("email", "")) or request.remote_addr


def _safe_next(target: str | None) -> str:
    """Only relative paths — prevents open redirects."""
    if target and target.startswith("/") and not target.startswith("//") and "\\" not in target:
        return target
    return url_for("main.account")


@bp.route("/login", methods=["GET", "POST"])
@limiter.limit("10 per hour", methods=["POST"])
@limiter.limit("3 per hour", key_func=_email_key, methods=["POST"])
def login():
    if request.method == "GET":
        if current_user.is_authenticated:
            return redirect(url_for("main.account"))
        return render_template("auth/login.html", next=request.args.get("next", ""))

    email = _normalize(request.form.get("email"))
    next_path = request.form.get("next", "")
    if not EMAIL_RE.match(email) or len(email) > 255:
        flash("That doesn't look like an email address \u2014 mind checking it?", "error")
        return render_template("auth/login.html", next=next_path), 400

    user = User.query.filter_by(email=email).first()
    if user is None:
        user = User(email=email)
        db.session.add(user)
        db.session.flush()

    if user.deleted_at is not None:
        # Uniform response — never reveal account state.
        log.info("auth: login link requested for soft-deleted account")
        db.session.commit()
        flash(UNIFORM_MESSAGE, "success")
        return render_template("auth/login_sent.html")

    raw_token = secrets.token_urlsafe(32)
    db.session.add(LoginToken(
        user_id=user.id,
        token_hash=hashlib.sha256(raw_token.encode()).hexdigest(),
        expires_at=utcnow() + timedelta(minutes=current_app.config["MAGIC_LINK_MAX_AGE_MINUTES"]),
        request_ip=request.remote_addr,
    ))
    db.session.commit()

    link = current_app.config["SITE_URL"] + url_for("auth.verify", token=raw_token)
    if next_path.startswith("/"):
        link += "&next=" + next_path
    send_login_link(email, link)
    log.info("auth: login link issued (ip=%s)", request.remote_addr)

    flash(UNIFORM_MESSAGE, "success")
    return render_template("auth/login_sent.html")


@bp.route("/auth/verify")
def verify():
    raw_token = request.args.get("token", "")
    token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
    row = LoginToken.query.filter_by(token_hash=token_hash).first()

    valid = (
        row is not None
        and row.used_at is None
        and row.expires_at > utcnow()
        and row.user is not None
        and row.user.deleted_at is None
    )
    if not valid:
        log.info("auth: verify failed (ip=%s)", request.remote_addr)
        return render_template("auth/verify_failed.html"), 400

    row.used_at = utcnow()
    row.user.last_login_at = utcnow()
    db.session.commit()

    login_user(row.user, remember=True)
    session["logged_in_at"] = datetime.utcnow().isoformat()
    log.info("auth: user %s logged in (ip=%s)", row.user.id, request.remote_addr)
    return redirect(_safe_next(request.args.get("next")))


@bp.route("/logout", methods=["POST"])
@login_required
def logout():
    logout_user()
    session.pop("logged_in_at", None)
    flash("You're signed out. Come back any time.", "success")
    return redirect(url_for("main.index"))
