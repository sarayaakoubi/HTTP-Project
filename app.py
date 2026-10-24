"""Travel agency web server.

Public site, a client area (book travels, see your bookings) and an admin area
(manage travels, manage users, watch activity). Run with:
    venv\\Scripts\\python.exe app.py
then open http://<this-machine-ip>:5000 from any device on the same network.
"""

import secrets
from datetime import date
from functools import wraps
from pathlib import Path

from flask import (
    Flask, abort, flash, g, redirect, render_template, request, session, url_for,
)

import db
import server

app = Flask(__name__)
app.teardown_appcontext(db.close_db)

# Sessions are signed with this key. It is generated once and kept in a file so
# that restarting the server does not log everybody out; if it changed on every
# start, every existing cookie would fail its signature check.
KEY_FILE = Path(__file__).parent / "secret_key.txt"
if not KEY_FILE.exists():
    KEY_FILE.write_text(secrets.token_hex(32), encoding="utf-8")
app.secret_key = KEY_FILE.read_text(encoding="utf-8").strip()

# Cookie hardening. secure=True is deliberately NOT set: it would stop the
# cookie working over plain http, which is all this server speaks.
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,   # JavaScript cannot read the cookie
    SESSION_COOKIE_SAMESITE="Lax",  # not sent on cross-site POSTs
)

# At import time, not under __main__, so the schema also exists when the app is
# launched by a WSGI server such as waitress.
db.init_db()

AGENCY = {
    "name": "Horizon Travel",
    "tagline": "Journeys designed around you",
    "phone": "+216 00 000 000",
    "email": "contact@horizontravel.tn",
}

SERVICES = [
    ("Flight tickets", "Scheduled and low-cost airlines at the best fare we can find."),
    ("Package holidays", "Hotel, transfers and excursions booked for you."),
    ("Visa assistance", "We build the file with you and follow it through to approval."),
    ("Tailor-made trips", "An itinerary built with you, around your budget."),
]

MAX_TRAVELLERS = 20

# Tunisian formats. Change these if the agency serves another country.
CIN_DIGITS = 8
POSTAL_DIGITS = 4


# --- session plumbing ----------------------------------------------------

@app.before_request
def load_user():
    """Turn the user id in the session cookie into a row, once per request.

    Looking the user up every time (rather than trusting the cookie's contents)
    means a deleted or deactivated account stops working immediately, without
    waiting for the cookie to expire.
    """
    g.user = None
    user_id = session.get("user_id")
    if user_id is None:
        return
    user = db.get_user(user_id)
    if user is None or not user["is_active"]:
        session.clear()
        return
    # The cookie must also carry the token currently stored on the account.
    # Without this, a copied cookie would keep working after sign-out, because
    # a Flask session lives in the cookie rather than on the server.
    if not db.session_is_valid(user, session.get("token")):
        session.clear()
        return
    g.user = user
    if request.endpoint != "static":
        db.touch(user["id"])   # keeps the green dot alive


@app.context_processor
def inject_globals():
    """Values every template can use without each route passing them."""
    return {
        "agency": AGENCY,
        "user": g.get("user"),
        # Used as the `min` on the date picker, so past days cannot be chosen.
        "today": date.today().isoformat(),
        "default_admin_warning": (
            g.get("user") is not None
            and g.user["role"] == "admin"
            and db.using_default_admin_password()
        ),
    }


@app.template_filter("card")
def card(number):
    """Show a stored card as **** **** **** 1234 rather than in full."""
    return db.mask_card(number)


@app.template_filter("money")
def money(value):
    """1450.0 -> '1,450', 1450.5 -> '1,450.50'."""
    amount = float(value)
    return f"{amount:,.0f}" if amount == int(amount) else f"{amount:,.2f}"


def actor():
    """Who made a change: the signed-in username plus the device address."""
    name = g.user["username"] if g.get("user") else "anonymous"
    return (name, request.remote_addr or "unknown")


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if g.get("user") is None:
            flash("Please sign in first.", "info")
            return redirect(url_for("login", next=request.path))
        return view(*args, **kwargs)
    return wrapped


def admin_required(view):
    """Admin-only. 403 rather than a redirect: the user is known, just not allowed."""
    @wraps(view)
    def wrapped(*args, **kwargs):
        if g.get("user") is None:
            flash("Please sign in first.", "info")
            return redirect(url_for("login", next=request.path))
        if g.user["role"] != "admin":
            abort(403)
        return view(*args, **kwargs)
    return wrapped


def safe_next(target):
    """Only follow a `next` that is a path on this site, never another host."""
    if target and target.startswith("/") and not target.startswith("//"):
        return target
    return None


# --- authentication ------------------------------------------------------

@app.route("/login", methods=["GET", "POST"])
def login():
    if g.get("user"):
        return redirect(url_for("home"))

    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        user = db.verify_login(username, password)
        if user is None:
            # One message for both cases, so it cannot be used to discover
            # which usernames exist.
            return render_template(
                "auth/login.html",
                username=username,
                error="Wrong username or password, or the account is disabled.",
            ), 401

        session.clear()
        session["user_id"] = user["id"]
        session["token"] = db.mark_login(user["id"])
        session.permanent = False   # cookie ends when the browser closes
        db.log("login", "session", user["id"], user["username"], "",
               (user["username"], request.remote_addr or "unknown"))
        flash(f"Welcome back, {user['full_name'] or user['username']}.", "success")
        return redirect(safe_next(request.form.get("next")) or url_for("home"))

    return render_template("auth/login.html", username="", error=None)


@app.route("/signup", methods=["GET", "POST"])
def signup():
    """Self-service registration. Always creates a client, never an admin."""
    if g.get("user"):
        return redirect(url_for("home"))

    if request.method == "POST":
        data = {
            "username": request.form.get("username", "").strip(),
            "full_name": request.form.get("full_name", "").strip(),
            "email": request.form.get("email", "").strip(),
            "role": "client",
            "is_active": 1,
        }
        password = request.form.get("password", "")
        errors = validate_user(data, password, require_password=True)

        if errors:
            return render_template("auth/signup.html", form=data, errors=errors), 400

        user_id = db.create_user(data, password, (data["username"], request.remote_addr or "unknown"))
        session.clear()
        session["user_id"] = user_id
        session["token"] = db.mark_login(user_id)
        flash("Your account is ready. Browse the travels and book one.", "success")
        return redirect(url_for("home"))

    blank = {"username": "", "full_name": "", "email": "", "role": "client", "is_active": 1}
    return render_template("auth/signup.html", form=blank, errors={})


@app.route("/logout", methods=["POST"])
@login_required
def logout():
    db.log("logout", "session", g.user["id"], g.user["username"], "", actor())
    db.mark_logout(g.user["id"])
    session.clear()
    flash("You are signed out.", "info")
    return redirect(url_for("home"))


# --- validation ----------------------------------------------------------

def validate_user(data, password, require_password, existing_id=None):
    errors = {}

    name = data["username"]
    if not name:
        errors["username"] = "A username is required."
    elif len(name) < 3:
        errors["username"] = "Use at least 3 characters."
    elif len(name) > 40:
        errors["username"] = "Keep it under 40 characters."
    elif not all(c.isalnum() or c in "._-" for c in name):
        errors["username"] = "Letters, digits, dot, dash and underscore only."
    else:
        clash = db.get_user_by_username(name)
        if clash is not None and clash["id"] != existing_id:
            errors["username"] = "That username is already taken."

    if data["email"] and "@" not in data["email"]:
        errors["email"] = "That does not look like an email address."

    if data["role"] not in db.ROLES:
        errors["role"] = "Unknown role."

    if require_password or password:
        if len(password) < 6:
            errors["password"] = "Use at least 6 characters."

    return errors


def read_travel_form():
    data = {
        "name": request.form.get("name", "").strip(),
        "summary": request.form.get("summary", "").strip(),
        "duration": request.form.get("duration", "").strip(),
        "price": request.form.get("price", "").strip(),
    }
    errors = {}

    if not data["name"]:
        errors["name"] = "A name is required."
    elif len(data["name"]) > 120:
        errors["name"] = "Keep the name under 120 characters."

    try:
        price = float(data["price"])
        if price < 0:
            errors["price"] = "The price cannot be negative."
        else:
            data["price"] = price
    except ValueError:
        errors["price"] = "The price must be a number, for example 650 or 1450.50."

    return data, errors


# --- public site ---------------------------------------------------------

@app.route("/")
def home():
    return render_template("index.html", destinations=db.list_travels(), services=SERVICES)


@app.route("/health")
def health():
    """Small endpoint to check the server is responding."""
    return {"status": "ok", "service": AGENCY["name"]}


# --- client area ---------------------------------------------------------

def luhn_ok(digits):
    """The checksum every real card number satisfies. Catches typos.

    Not called while the lab setting in read_checkout_form accepts any digits.
    """
    total, double = 0, False
    for ch in reversed(digits):
        d = int(ch)
        if double:
            d *= 2
            if d > 9:
                d -= 9
        total += d
        double = not double
    return total % 10 == 0


def read_checkout_form(travel):
    """Pull the checkout form apart into (data, errors)."""
    form = {
        "travellers": request.form.get("travellers", "1").strip(),
        "depart_date": request.form.get("depart_date", "").strip(),
        "notes": request.form.get("notes", "").strip()[:500],
        "bill_name": request.form.get("bill_name", "").strip(),
        "bill_cin": request.form.get("bill_cin", "").strip(),
        "bill_phone": request.form.get("bill_phone", "").strip(),
        "bill_address": request.form.get("bill_address", "").strip(),
        "bill_city": request.form.get("bill_city", "").strip(),
        "bill_postal_code": request.form.get("bill_postal_code", "").strip(),
        "payment_method": request.form.get("payment_method", "agency"),
        "card_holder": request.form.get("card_holder", "").strip(),
        "card_number": request.form.get("card_number", "").strip(),
        "card_expiry": request.form.get("card_expiry", "").strip(),
        "card_cvv": request.form.get("card_cvv", "").strip(),
    }
    errors = {}

    try:
        count = int(form["travellers"])
        if not 1 <= count <= MAX_TRAVELLERS:
            raise ValueError
        form["travellers"] = count
    except ValueError:
        errors["travellers"] = f"Choose between 1 and {MAX_TRAVELLERS} travellers."
        form["travellers"] = 1

    # <input type="date"> gives us yyyy-mm-dd, and is keyboard-operable and
    # screen-reader friendly for free — no date-picker library needed.
    if not form["depart_date"]:
        errors["depart_date"] = "Choose a departure date."
    else:
        try:
            departure = date.fromisoformat(form["depart_date"])
            if departure < date.today():
                errors["depart_date"] = "That date has already passed."
        except ValueError:
            errors["depart_date"] = "Use the date picker, or type yyyy-mm-dd."

    if not form["bill_name"]:
        errors["bill_name"] = "A full name is required."

    cin = form["bill_cin"].replace(" ", "")
    if not cin:
        errors["bill_cin"] = "A CIN number is required."
    elif not (cin.isdigit() and len(cin) == CIN_DIGITS):
        errors["bill_cin"] = f"A CIN is {CIN_DIGITS} digits."
    else:
        form["bill_cin"] = cin

    phone = form["bill_phone"].replace(" ", "")
    if phone and not all(c.isdigit() or c == "+" for c in phone):
        errors["bill_phone"] = "Digits and a leading + only."

    if not form["bill_address"]:
        errors["bill_address"] = "An address is required."
    if not form["bill_city"]:
        errors["bill_city"] = "A city is required."

    postal = form["bill_postal_code"].replace(" ", "")
    if not postal:
        errors["bill_postal_code"] = "A postal code is required."
    elif not (postal.isdigit() and len(postal) == POSTAL_DIGITS):
        errors["bill_postal_code"] = f"A postal code is {POSTAL_DIGITS} digits."
    else:
        form["bill_postal_code"] = postal

    if form["payment_method"] not in db.PAYMENT_METHODS:
        errors["payment_method"] = "Choose a payment method."
    elif form["payment_method"] == "card":
        if not form["card_holder"]:
            errors["card_holder"] = "The name on the card is required."

        # Lab setting: any digits are accepted, so invented numbers go through.
        # A real checkout would also require 13-19 digits and a passing Luhn
        # checksum; to put that back, restore the two checks below.
        #     elif not 13 <= len(number) <= 19:
        #         errors["card_number"] = "A card number is 13 to 19 digits."
        #     elif not luhn_ok(number):
        #         errors["card_number"] = "That card number fails its checksum."
        number = "".join(c for c in form["card_number"] if c.isdigit())
        if not number:
            errors["card_number"] = "A card number is required."
        else:
            form["card_number"] = number

        expiry = form["card_expiry"]
        month_year = expiry.split("/") if "/" in expiry else []
        if len(month_year) != 2:
            errors["card_expiry"] = "Use MM/YY, for example 04/29."
        else:
            try:
                month, year = int(month_year[0]), int(month_year[1])
                if not 1 <= month <= 12:
                    raise ValueError
                today = date.today()
                # A card is valid through the last day of its expiry month.
                if (2000 + year, month) < (today.year, today.month):
                    errors["card_expiry"] = "That card has expired."
            except ValueError:
                errors["card_expiry"] = "Use MM/YY, for example 04/29."

        if not form["card_cvv"].isdigit() or not 3 <= len(form["card_cvv"]) <= 4:
            errors["card_cvv"] = "A CVV is 3 or 4 digits."
    else:
        # Keep stray card input out of the record when another method is picked.
        form["card_holder"] = form["card_number"] = ""
        form["card_expiry"] = form["card_cvv"] = ""

    return form, errors


@app.route("/travels/<int:travel_id>/book", methods=["GET", "POST"])
@login_required
def book(travel_id):
    travel = db.get_travel(travel_id)
    if travel is None:
        abort(404)

    if request.method == "POST":
        form, errors = read_checkout_form(travel)
        if errors:
            # Printed so a 400 in the console says which field was refused,
            # instead of leaving you guessing from the access log.
            app.logger.warning(
                "checkout refused for %s: %s",
                g.user["username"],
                "; ".join(f"{field}: {message}" for field, message in errors.items()),
            )
            return render_template("checkout.html", travel=travel, form=form,
                                   errors=errors, methods=db.PAYMENT_METHODS,
                                   labels=db.PAYMENT_LABELS), 400

        db.create_booking(g.user, travel, form["travellers"], form, actor())
        db.save_billing_details(g.user["id"], form)
        flash(f"Booked “{travel['name']}” for {form['travellers']}. "
              "See it in your bookings.", "success")
        return redirect(url_for("profile"))

    try:
        travellers = max(1, min(MAX_TRAVELLERS, int(request.args.get("travellers", 1))))
    except ValueError:
        travellers = 1

    # Prefilled from the account, so a returning client retypes nothing.
    prefill = {
        "travellers": travellers, "notes": "", "depart_date": "",
        "bill_name": g.user["full_name"] or "",
        "bill_cin": g.user["cin"] or "",
        "bill_phone": g.user["phone"] or "",
        "bill_address": g.user["address"] or "",
        "bill_city": g.user["city"] or "",
        "bill_postal_code": g.user["postal_code"] or "",
        "payment_method": "agency",
        "card_holder": "", "card_number": "", "card_expiry": "", "card_cvv": "",
    }
    return render_template("checkout.html", travel=travel, form=prefill,
                           errors={}, methods=db.PAYMENT_METHODS,
                           labels=db.PAYMENT_LABELS)


@app.route("/me")
@login_required
def profile():
    return render_template("profile.html", bookings=db.list_bookings_for_user(g.user["id"]))


@app.route("/bookings/<int:booking_id>/cancel", methods=["POST"])
@login_required
def booking_cancel(booking_id):
    booking = db.get_booking(booking_id)
    if booking is None:
        abort(404)
    # A client may only touch their own bookings; an admin may cancel any.
    if booking["user_id"] != g.user["id"] and g.user["role"] != "admin":
        abort(403)

    name = db.cancel_booking(booking_id, actor())
    flash(f"Cancelled “{name}”." if name else "That booking was already cancelled.",
          "success" if name else "info")
    return redirect(request.form.get("back") or url_for("profile"))


@app.route("/me/password", methods=["POST"])
@login_required
def change_own_password():
    current = request.form.get("current_password", "")
    new = request.form.get("new_password", "")
    if db.verify_login(g.user["username"], current) is None:
        flash("Your current password is not correct.", "error")
    elif len(new) < 6:
        flash("The new password needs at least 6 characters.", "error")
    else:
        db.change_password(g.user["id"], new, actor())
        flash("Password changed.", "success")
    return redirect(url_for("profile"))


# --- admin: travels ------------------------------------------------------

@app.route("/admin/travels")
@admin_required
def travel_list():
    return render_template("admin/travels.html", travels=db.list_travels())


@app.route("/admin/travels/new", methods=["GET", "POST"])
@admin_required
def travel_new():
    if request.method == "POST":
        data, errors = read_travel_form()
        if errors:
            return render_template("admin/travel_form.html",
                                   travel=data, errors=errors, mode="new"), 400
        db.create_travel(data, actor())
        flash(f"Added “{data['name']}”.", "success")
        return redirect(url_for("travel_list"))

    blank = {"name": "", "summary": "", "duration": "", "price": ""}
    return render_template("admin/travel_form.html", travel=blank, errors={}, mode="new")


@app.route("/admin/travels/<int:travel_id>/edit", methods=["GET", "POST"])
@admin_required
def travel_edit(travel_id):
    travel = db.get_travel(travel_id)
    if travel is None:
        abort(404)

    if request.method == "POST":
        data, errors = read_travel_form()
        if errors:
            return render_template("admin/travel_form.html",
                                   travel={**data, "id": travel_id},
                                   errors=errors, mode="edit"), 400
        changes = db.update_travel(travel_id, data, actor())
        flash(f"Updated “{data['name']}”: {changes}" if changes else "Nothing changed.",
              "success" if changes else "info")
        return redirect(url_for("travel_list"))

    return render_template("admin/travel_form.html", travel=travel, errors={}, mode="edit")


@app.route("/admin/travels/<int:travel_id>/delete", methods=["POST"])
@admin_required
def travel_delete(travel_id):
    """POST only: a link that deletes on GET would fire on any prefetch."""
    name = db.delete_travel(travel_id, actor())
    if name is None:
        abort(404)
    flash(f"Deleted “{name}”. Existing bookings for it are kept.", "success")
    return redirect(url_for("travel_list"))


# --- admin: users --------------------------------------------------------

@app.route("/admin/users")
@admin_required
def user_list():
    users = db.list_users()
    online = {u["id"]: db.is_online(u["last_seen"]) for u in users}
    return render_template(
        "admin/users.html",
        users=users, online=online,
        window_minutes=int(db.ONLINE_WINDOW.total_seconds() // 60),
    )


@app.route("/admin/users/new", methods=["GET", "POST"])
@admin_required
def user_new():
    if request.method == "POST":
        data = {
            "username": request.form.get("username", "").strip(),
            "full_name": request.form.get("full_name", "").strip(),
            "email": request.form.get("email", "").strip(),
            "role": request.form.get("role", "client"),
            "is_active": 1 if request.form.get("is_active") else 0,
        }
        password = request.form.get("password", "")
        errors = validate_user(data, password, require_password=True)
        if errors:
            return render_template("admin/user_form.html",
                                   form=data, errors=errors, mode="new",
                                   roles=db.ROLES), 400
        db.create_user(data, password, actor())
        flash(f"Created “{data['username']}”.", "success")
        return redirect(url_for("user_list"))

    blank = {"username": "", "full_name": "", "email": "", "role": "client", "is_active": 1}
    return render_template("admin/user_form.html", form=blank, errors={},
                           mode="new", roles=db.ROLES)


@app.route("/admin/users/<int:user_id>/edit", methods=["GET", "POST"])
@admin_required
def user_edit(user_id):
    target = db.get_user(user_id)
    if target is None:
        abort(404)

    if request.method == "POST":
        data = {
            "username": request.form.get("username", "").strip(),
            "full_name": request.form.get("full_name", "").strip(),
            "email": request.form.get("email", "").strip(),
            "role": request.form.get("role", "client"),
            "is_active": 1 if request.form.get("is_active") else 0,
        }
        password = request.form.get("password", "")
        errors = validate_user(data, password, require_password=False, existing_id=user_id)

        # Refuse the edit that would leave the agency with no working admin.
        losing_admin = target["role"] == "admin" and (
            data["role"] != "admin" or not data["is_active"]
        )
        if losing_admin and db.count_admins(exclude_id=user_id) == 0:
            errors["role"] = "This is the last active admin. Promote someone else first."

        if errors:
            return render_template("admin/user_form.html",
                                   form={**data, "id": user_id}, errors=errors,
                                   mode="edit", roles=db.ROLES), 400

        changes = db.update_user(user_id, data, password, actor())
        flash(f"Updated “{data['username']}”: {changes}" if changes else "Nothing changed.",
              "success" if changes else "info")
        return redirect(url_for("user_list"))

    return render_template("admin/user_form.html", form=target, errors={},
                           mode="edit", roles=db.ROLES)


@app.route("/admin/users/<int:user_id>/delete", methods=["POST"])
@admin_required
def user_delete(user_id):
    target = db.get_user(user_id)
    if target is None:
        abort(404)
    if target["id"] == g.user["id"]:
        flash("You cannot delete the account you are signed in with.", "error")
        return redirect(url_for("user_list"))
    if target["role"] == "admin" and db.count_admins(exclude_id=user_id) == 0:
        flash("That is the last active admin. Promote someone else first.", "error")
        return redirect(url_for("user_list"))

    name = db.delete_user(user_id, actor())
    flash(f"Deleted “{name}” and their bookings.", "success")
    return redirect(url_for("user_list"))


@app.route("/admin/bookings")
@admin_required
def booking_list():
    return render_template("admin/bookings.html", bookings=db.list_all_bookings())


@app.route("/admin/activity")
@admin_required
def activity():
    return render_template("admin/activity.html", entries=db.list_activity())


# --- error pages ---------------------------------------------------------

@app.errorhandler(403)
def forbidden(e):
    return render_template("error.html", code=403,
                           message="That area is for administrators."), 403


@app.errorhandler(404)
def not_found(e):
    return render_template("error.html", code=404,
                           message="That page does not exist."), 404


@app.errorhandler(405)
def wrong_method(e):
    """Reached by, for example, opening a delete URL directly in the browser."""
    return render_template("error.html", code=405,
                           message="This address only answers to a form "
                                   "submission, not to being opened directly."), 405


@app.errorhandler(500)
def server_error(e):
    """Only seen with debug off; otherwise the Werkzeug debugger takes over."""
    return render_template("error.html", code=500,
                           message="The server hit an unexpected problem. "
                                   "Nothing was saved — please try again."), 500


if __name__ == "__main__":
    # How the site is served — bind address, port, finding this machine's
    # address on the network — all lives in server.py.
    notes = []
    if db.using_default_admin_password():
        notes.append(
            f"Sign in as '{db.DEFAULT_ADMIN['username']}' / "
            f"'{db.DEFAULT_ADMIN['password']}' and change that password."
        )
    server.run(app, notes=notes)
