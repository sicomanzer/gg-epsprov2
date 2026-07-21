import secrets

from flask import request, session


def get_csrf_token():
    token = session.get("csrf_token")
    if not token:
        token = secrets.token_hex(16)
        session["csrf_token"] = token
    return token


def is_valid_csrf_token():
    form_token = request.form.get("csrf_token", "")
    session_token = session.get("csrf_token", "")
    return bool(form_token and session_token and secrets.compare_digest(form_token, session_token))

