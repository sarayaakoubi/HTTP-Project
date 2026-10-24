
# Travel Agency — web server

Flask server for a travel agency, served to devices on the local network. It has
a public site, a client area (book travels, see your bookings) and an admin area
(manage travels, manage users, watch activity).

## Run it

```powershell
cd C:\Users\USER\Desktop\TravelAgency
.\venv\Scripts\python.exe app.py
```

The console prints the addresses to use:

```
  Local    : http://127.0.0.1:5000
  Network  : http://10.58.105.200:5000  
  Admin    : http://10.58.105.200:5000/admin/travels
```

Stop the server with `Ctrl+C`.

## Signing in

The first run creates one administrator:

| Username | Password   |
| -------- | ---------- |
| `admin`  | `admin123` |

**Change that password straight away** — sign in, open `/me`, use "Change my
password". A warning banner shows on every admin page until you do.

Clients create their own accounts at `/signup`. Signup always produces a client;
the role field is ignored there, so nobody can make themselves an admin. An admin
can also create accounts by hand at `/admin/users/new`, including other admins.

## The two roles

| | Client | Admin |
| --- | --- | --- |
| Browse travels | yes | yes |
| Book a travel | yes | no |
| See own bookings (`/me`) | yes | yes |
| Cancel a booking | own only | anyone's |
| Add / edit / delete travels | no | yes |
| Add / edit / delete users | no | yes |
| See all bookings | no | yes |
| See the activity log | no | yes |

The navigation bar shows only what the signed-in role may use. The rules are
enforced on the routes themselves, not just hidden in the menu — a client who
types an admin URL gets 403.

Two guards protect you from locking yourself out: the last active admin cannot be
demoted or deactivated, and you cannot delete the account you are signed in with.

## Sessions and the green/red dot

`/admin/users` marks each account with a dot. **Green** means that account sent a
request in the last 2 minutes; **red** means it did not.

It cannot mean more than that. HTTP has no permanent connection to a web server —
a browser asks for a page, gets it, and the connection ends. Nobody is ever
"connected" in the way a chat app means it. So presence is inferred: every
request from a signed-in user updates their `last_seen`, and signing out clears
it so the dot turns red at once instead of after the window. Someone who closes
their browser without signing out shows green for up to 2 minutes. Change
`ONLINE_WINDOW` in [db.py](db.py) to widen or narrow that.

Sessions are a signed cookie holding the user id plus a random token. The same
token is stored on the user row, and every request checks that the two match,
which means:

- Signing out really ends the session. A copied cookie stops working.
- Deactivating or deleting an account kills its live session immediately.
- Signing in on a second device ends the session on the first.

The signing key lives in `secret_key.txt`, generated on first run and git-ignored.
Deleting it signs everybody out.

## Routes

| Method | Path | Who | Response |
| ------ | ---- | --- | -------- |
| GET | `/` | anyone | Home page with the travels |
| GET/POST | `/login` | anyone | Sign in (401 on bad credentials) |
| GET/POST | `/signup` | anyone | Create a client account |
| POST | `/logout` | signed in | Ends the session |
| POST | `/travels/<id>/book` | client | Creates a booking |
| GET | `/me` | signed in | Profile and own bookings |
| POST | `/bookings/<id>/cancel` | owner or admin | Cancels a booking |
| POST | `/me/password` | signed in | Change own password |
| GET | `/admin/travels` | admin | Manage travels |
| GET/POST | `/admin/travels/new` | admin | Add a travel |
| GET/POST | `/admin/travels/<id>/edit` | admin | Edit a travel |
| POST | `/admin/travels/<id>/delete` | admin | Delete a travel |
| GET | `/admin/users` | admin | Users, with the presence dots |
| GET/POST | `/admin/users/new` | admin | Create an account |
| GET/POST | `/admin/users/<id>/edit` | admin | Edit an account |
| POST | `/admin/users/<id>/delete` | admin | Delete an account |
| GET | `/admin/bookings` | admin | Every booking |
| GET | `/admin/activity` | admin | The change log |
| GET | `/health` | anyone | `{"status": "ok", ...}` |

Writes are POST-only and redirect afterwards, so a refresh never repeats them.
Deleting is never a GET: a plain link would let a browser prefetch wipe a row.
Signed-out visitors are redirected to `/login`; signed-in clients hitting an admin
URL get 403.

## Data

One SQLite file, `travels.db`, created and migrated automatically on start.

- **travels** — what the agency sells.
- **users** — accounts. Passwords are stored as scrypt hashes, never plaintext.
- **bookings** — who booked what. The travel's name and price are *copied* into
  the booking, so deleting a travel leaves client history readable; the booking
  shows "no longer offered". Deleting a user removes their bookings with them.
- **activity** — every sign-in, sign-out, add, edit, delete, booking and
  cancellation, with the username, the device IP, and field-by-field detail of
  what an edit changed. Each entry is written in the same transaction as the
  change it describes, so the two cannot disagree.

Cancelling a booking marks it `cancelled` rather than deleting the row, so the
history survives.

Delete `travels.db` to start over; it rebuilds with the four seed travels and a
fresh `admin` account, and the log goes with it.

## Let other devices reach it

`app.run(host="0.0.0.0")` at the bottom of [app.py](app.py) already makes the
server listen on every network interface. Windows Firewall still blocks incoming
connections on port 5000, so add a rule from PowerShell **as Administrator**.

```powershell
New-NetFirewallRule -DisplayName "TravelAgency Flask 5000" `
  -Direction Inbound -Protocol TCP -LocalPort 5000 `
  -Action Allow -Profile Public
```

Check it:

```powershell
Get-NetFirewallRule -DisplayName "TravelAgency Flask 5000" |
  Select-Object DisplayName, Enabled, Profile, Action
```

### Turn it off again



```powershell
Remove-NetFirewallRule -DisplayName "TravelAgency Flask 5000"
```



### If a teammate still cannot connect

- They must be joined to the same hotspot.
- Some hotspots isolate clients from each other; no firewall rule works around
  that. Test by pinging this machine's IP from their device.
- The IP changes when the hotspot reassigns it. Re-read it from the console
  output each time the server starts.

## Layout

```
app.py                            routes, sessions, roles, validation
db.py                             schema, migrations, queries, activity logging
travels.db                        the database (created on first run, not committed)
secret_key.txt                    session signing key (generated, not committed)
templates/base.html               shared layout: role-aware nav, flash messages
templates/index.html              public home page with the booking buttons
templates/profile.html            client area: bookings, change password
templates/error.html              403 and 404
templates/auth/login.html         sign in
templates/auth/signup.html        register
templates/admin/travels.html      manage travels
templates/admin/travel_form.html  add + edit a travel
templates/admin/users.html        manage users, with presence dots
templates/admin/user_form.html    add + edit a user
templates/admin/bookings.html     every booking
templates/admin/activity.html     the change log
templates/checkout.html           booking checkout: dates, details, payment
static/css/style.css              design system + all styles
static/js/reveal.js               scroll reveals (~1.3 KB, no dependencies)
requirements.txt                  Flask
venv/                             virtualenv (not committed)
```

Travels and users are edited through the site. The agency name, phone, email and
the `SERVICES` list are still constants near the top of `app.py`.

## Design

Palette: deep green and terracotta over cream, with coral kept for calls to
action. All of it is defined as custom properties on `:root` in
[static/css/style.css](static/css/style.css) — change the tokens there and the
whole site follows. Type pairs Fraunces (display serif) with Inter (body).

Every text/background pair in use was checked against WCAG AA. Two results
worth knowing before you edit colours:

- **Coral buttons carry dark ink, never white.** White on coral is 2.8:1, which
  fails; ink on coral is 5.7:1.
- **The hero needs its scrim.** White text over the bright part of the sunrise
  gradient is 1.3:1 on its own. The `.scene-dawn::before` overlay brings the
  worst case to 4.9:1. Any hero photo you add later needs the same treatment.

The home page tells one journey as you scroll: sunrise departure, climbing above
cloud, arrival on the coast, then the days in between. The scenes are CSS
gradients and inline SVG rather than photographs, so there is nothing to
download and nothing to lazy-load. To swap in real photography, put an
`<img loading="lazy" decoding="async" width height alt>` inside `.scene-art`
and delete the `<svg>`.

Motion: `reveal.js` adds `.is-visible` via IntersectionObserver, with a stagger
from the `--i` custom property. Everything decorative is disabled under
`prefers-reduced-motion: reduce`, and a `<noscript>` block reveals all content
if scripting is off.

Accessibility: skip link as the first tab stop, visible `:focus-visible` rings,
`aria-live` on flash messages, decorative SVG marked `aria-hidden`, and a native
`<input type="date">` for the departure date — keyboard-operable and
screen-reader labelled without a picker library.


