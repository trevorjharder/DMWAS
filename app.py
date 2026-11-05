"""District of Mount Waddington Access Society website."""

from __future__ import annotations

import logging
import os
import re
import secrets
import smtplib
from datetime import datetime, timezone
from email.message import EmailMessage
from functools import wraps
from pathlib import Path
from typing import Callable, TypeVar, cast

import click
from flask import (
    Flask,
    abort,
    flash,
    g,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy.exc import IntegrityError
from werkzeug.security import check_password_hash, generate_password_hash


db = SQLAlchemy()
F = TypeVar("F", bound=Callable[..., object])
EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
MIN_PASSWORD_LENGTH = 12


class User(db.Model):
    """A member account or a pending membership access request."""

    id = db.Column(db.Integer, primary_key=True)
    full_name = db.Column(db.String(120), nullable=False)
    email = db.Column(db.String(254), unique=True, nullable=False, index=True)
    phone = db.Column(db.String(40), nullable=True)
    username = db.Column(db.String(40), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(255), nullable=False)
    status = db.Column(db.String(20), nullable=False, default="pending", index=True)
    is_admin = db.Column(db.Boolean, nullable=False, default=False)
    created_at = db.Column(
        db.DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc)
    )

    def set_password(self, password: str) -> None:
        self.password_hash = generate_password_hash(password)

    def check_password(self, password: str) -> bool:
        return check_password_hash(self.password_hash, password)


def _database_uri() -> str:
    database_url = os.environ.get("DATABASE_URL", "sqlite:///dmwas.db")
    if database_url.startswith("postgres://"):
        return database_url.replace("postgres://", "postgresql://", 1)
    return database_url


def _send_request_notification(user: User) -> None:
    """Send an optional SMTP notification when mail settings are configured."""
    smtp_host = os.environ.get("SMTP_HOST")
    smtp_from = os.environ.get("SMTP_FROM")
    if not smtp_host or not smtp_from:
        logging.getLogger(__name__).info(
            "SMTP is not configured; skipped membership request notification."
        )
        return

    message = EmailMessage()
    message["Subject"] = "New DMWAS membership access request"
    message["From"] = smtp_from
    message["To"] = os.environ.get("ADMIN_NOTIFICATION_EMAIL", "members@accessni.ca")
    message.set_content(
        "A membership access request is waiting for review.\n\n"
        f"Name: {user.full_name}\n"
        f"Email: {user.email}\n"
        f"Username: {user.username}\n\n"
        "Sign in to the Society website's admin area to review it."
    )

    smtp_port = int(os.environ.get("SMTP_PORT", "587"))
    with smtplib.SMTP(smtp_host, smtp_port, timeout=10) as smtp:
        if os.environ.get("SMTP_USE_TLS", "true").lower() == "true":
            smtp.starttls()
        smtp_username = os.environ.get("SMTP_USERNAME")
        smtp_password = os.environ.get("SMTP_PASSWORD")
        if smtp_username and smtp_password:
            smtp.login(smtp_username, smtp_password)
        smtp.send_message(message)


def create_app(test_config: dict[str, object] | None = None) -> Flask:
    app = Flask(__name__)
    secret_key = os.environ.get("SECRET_KEY")
    if not secret_key and (
        os.environ.get("RAILWAY_PROJECT_ID")
        or os.environ.get("ENVIRONMENT", "").lower() == "production"
    ):
        raise RuntimeError("SECRET_KEY must be configured in production.")
    app.config.from_mapping(
        SECRET_KEY=secret_key or secrets.token_hex(32),
        SQLALCHEMY_DATABASE_URI=_database_uri(),
        SQLALCHEMY_TRACK_MODIFICATIONS=False,
        PUBLIC_CONTACT_EMAIL=os.environ.get("PUBLIC_CONTACT_EMAIL", ""),
        PUBLIC_CONTACT_PHONE=os.environ.get("PUBLIC_CONTACT_PHONE", ""),
        PUBLIC_CONTACT_ADDRESS=os.environ.get("PUBLIC_CONTACT_ADDRESS", ""),
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=os.environ.get("SESSION_COOKIE_SECURE", "false").lower()
        == "true",
    )
    if test_config:
        app.config.update(test_config)

    db.init_app(app)

    @app.before_request
    def protect_post_requests() -> None:
        if request.method != "POST":
            return
        expected_token = session.get("csrf_token")
        submitted_token = request.form.get("csrf_token", "")
        if not expected_token or not secrets.compare_digest(expected_token, submitted_token):
            abort(400, description="The form expired or was invalid. Please try again.")

    @app.before_request
    def load_current_user() -> None:
        user_id = session.get("user_id")
        g.current_user = db.session.get(User, user_id) if user_id else None

    @app.context_processor
    def inject_template_values() -> dict[str, object]:
        if not session.get("csrf_token"):
            session["csrf_token"] = secrets.token_urlsafe(32)
        return {"current_user": g.get("current_user")}

    @app.after_request
    def set_security_headers(response):
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        return response

    def login_required(view: F) -> F:
        @wraps(view)
        def wrapped(*args: object, **kwargs: object):
            user = g.get("current_user")
            if not user or user.status != "active":
                session.clear()
                flash("Please sign in with an approved member account.", "info")
                return redirect(url_for("login", next=request.path))
            return view(*args, **kwargs)

        return cast(F, wrapped)

    def admin_required(view: F) -> F:
        @wraps(view)
        @login_required
        def wrapped(*args: object, **kwargs: object):
            if not g.current_user.is_admin:
                abort(403)
            return view(*args, **kwargs)

        return cast(F, wrapped)

    @app.get("/")
    def index():
        return redirect(url_for("about"))

    @app.get("/about")
    def about():
        return render_template("about.html", title="About")

    @app.get("/notices")
    def notices():
        return render_template("notices.html", title="Notices")

    @app.get("/contact")
    def contact():
        return render_template("contact.html", title="Contact")

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if request.method == "POST":
            username = request.form.get("username", "").strip().lower()
            password = request.form.get("password", "")
            user = User.query.filter_by(username=username).first()
            if user and user.status == "active" and user.check_password(password):
                session.clear()
                session["user_id"] = user.id
                flash("You are signed in.", "success")
                destination = request.args.get("next", "")
                if destination.startswith("/") and not destination.startswith("//"):
                    return redirect(destination)
                return redirect(url_for("member_home"))
            flash("The username or password was not accepted, or the account is awaiting approval.", "error")
        return render_template("login.html", title="Member sign in")

    @app.route("/membership/request", methods=["GET", "POST"])
    def membership_request():
        if request.method == "POST":
            full_name = request.form.get("full_name", "").strip()
            email = request.form.get("email", "").strip().lower()
            phone = request.form.get("phone", "").strip()
            username = request.form.get("username", "").strip().lower()
            password = request.form.get("password", "")
            confirm_password = request.form.get("confirm_password", "")

            errors: list[str] = []
            if not full_name or len(full_name) > 120:
                errors.append("Enter your name (up to 120 characters).")
            if not EMAIL_PATTERN.fullmatch(email):
                errors.append("Enter a valid email address.")
            if phone and len(phone) > 40:
                errors.append("The phone number must be 40 characters or fewer.")
            if not re.fullmatch(r"[a-z0-9_.-]{3,40}", username):
                errors.append("Choose a username of 3–40 letters, numbers, dots, underscores, or hyphens.")
            if len(password) < MIN_PASSWORD_LENGTH:
                errors.append(f"Password must be at least {MIN_PASSWORD_LENGTH} characters.")
            if password != confirm_password:
                errors.append("The passwords do not match.")
            if User.query.filter_by(email=email).first() or User.query.filter_by(username=username).first():
                errors.append("That email or username cannot be used. Contact the Society if you already requested access.")

            if errors:
                for error in errors:
                    flash(error, "error")
                return render_template("membership_request.html", title="Request member access"), 400

            user = User(
                full_name=full_name,
                email=email,
                phone=phone or None,
                username=username,
                status="pending",
            )
            user.set_password(password)
            db.session.add(user)
            try:
                db.session.commit()
            except IntegrityError:
                db.session.rollback()
                flash("That email or username cannot be used. Contact the Society if you already requested access.", "error")
                return render_template("membership_request.html", title="Request member access"), 400

            try:
                _send_request_notification(user)
            except (OSError, smtplib.SMTPException, ValueError):
                app.logger.exception("Could not send membership request notification")
            flash("Your request has been received. Sign-in will be available after Society approval.", "success")
            return redirect(url_for("login"))

        return render_template("membership_request.html", title="Request member access")

    @app.post("/logout")
    @login_required
    def logout():
        session.clear()
        flash("You are signed out.", "success")
        return redirect(url_for("about"))

    @app.get("/member")
    @login_required
    def member_home():
        return render_template("member_home.html", title="Member area")

    @app.get("/admin/requests")
    @admin_required
    def admin_requests():
        pending_users = User.query.filter_by(status="pending", is_admin=False).order_by(User.created_at).all()
        return render_template("admin_requests.html", title="Access requests", pending_users=pending_users)

    @app.post("/admin/requests/<int:user_id>/approve")
    @admin_required
    def approve_request(user_id: int):
        user = db.session.get(User, user_id)
        if not user or user.is_admin or user.status != "pending":
            abort(404)
        if request.form.get("checks_confirmed") != "yes":
            flash("Confirm that the Society's required checks are complete before approving.", "error")
            return redirect(url_for("admin_requests"))
        user.status = "active"
        db.session.commit()
        flash(f"Access approved for {user.username}.", "success")
        return redirect(url_for("admin_requests"))

    @app.post("/admin/requests/<int:user_id>/decline")
    @admin_required
    def decline_request(user_id: int):
        user = db.session.get(User, user_id)
        if not user or user.is_admin or user.status != "pending":
            abort(404)
        user.status = "declined"
        db.session.commit()
        flash("The access request was declined.", "success")
        return redirect(url_for("admin_requests"))

    @app.cli.command("init-db")
    def init_db_command() -> None:
        """Create the database tables for a new installation."""
        with app.app_context():
            db.create_all()
        click.echo("Database tables created.")

    @app.cli.command("create-admin")
    @click.option("--username", prompt=True)
    def create_admin_command(username: str) -> None:
        """Create the first administrator using an interactive password prompt."""
        normalized_username = username.strip().lower()
        if not re.fullmatch(r"[a-z0-9_.-]{3,40}", normalized_username):
            raise click.ClickException("Username must be 3–40 letters, numbers, dots, underscores, or hyphens.")
        if User.query.filter_by(username=normalized_username).first():
            raise click.ClickException("That username already exists.")
        email = click.prompt("Administrator email").strip().lower()
        if not EMAIL_PATTERN.fullmatch(email):
            raise click.ClickException("Enter a valid email address.")
        password = click.prompt("Administrator password", hide_input=True, confirmation_prompt=True)
        if len(password) < MIN_PASSWORD_LENGTH:
            raise click.ClickException(f"Password must be at least {MIN_PASSWORD_LENGTH} characters.")
        administrator = User(
            full_name="Society administrator",
            email=email,
            username=normalized_username,
            status="active",
            is_admin=True,
        )
        administrator.set_password(password)
        db.session.add(administrator)
        try:
            db.session.commit()
        except IntegrityError as error:
            db.session.rollback()
            raise click.ClickException("That email address already has an account.") from error
        click.echo("Administrator account created.")

    return app


app = create_app()
