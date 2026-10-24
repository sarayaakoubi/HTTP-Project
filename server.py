"""Serving the application over the network.

Everything about *how* the site is served lives here, separate from the routes
and the data layer: which address and port to bind, how to find this machine's
address on the local network, and what to print on startup. app.py imports this
and calls run().

Network configuration outside this file
---------------------------------------
Binding to 0.0.0.0 is only half of what makes the site reachable from another
device. Windows Firewall decides independently whether packets ever arrive, and
that is not configurable from Python. Both have to allow it:

    teammate's browser
       -> Windows Firewall   (a rule for inbound TCP on PORT)
       -> the socket bound here
       -> Flask routes

The firewall commands are in README.md. Note that a *block* rule for python.exe
overrides an allow rule for the port, which is the usual reason a correctly
configured server is still unreachable.
"""

import socket

# Bind address. 0.0.0.0 means every network interface on this machine, so other
# devices can connect; 127.0.0.1 would accept connections from this machine
# only, whatever the firewall says.
HOST = "0.0.0.0"
PORT = 5000

# Flask's reloader and debugger. Convenient while building, unsafe to expose:
# the debugger can run code on this machine. Set False before any real use.
DEBUG = True


def local_ip():
    """This machine's address on the local network.

    Opens a UDP socket toward a public address and asks the OS which of our
    addresses it would use to get there. No data is sent and nothing is
    contacted — connect() on UDP only sets the socket's default peer.
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"          # offline, or no route out
    finally:
        s.close()


def urls(host=HOST, port=PORT):
    """The addresses to open, as (label, url) pairs."""
    pairs = [("Local", f"http://127.0.0.1:{port}")]
    if host == "0.0.0.0":
        ip = local_ip()
        pairs.append(("Network", f"http://{ip}:{port}"))
        pairs.append(("Admin", f"http://{ip}:{port}/admin/travels"))
    else:
        pairs.append(("Network", "off — bound to this machine only"))
        pairs.append(("Admin", f"http://{host}:{port}/admin/travels"))
    return pairs


def banner(host=HOST, port=PORT, notes=()):
    """Print where to reach the site, plus any one-off warnings."""
    for label, url in urls(host, port):
        print(f"  {label:<9}: {url}")
    for note in notes:
        print(f"  {note}")


def run(app, host=HOST, port=PORT, debug=DEBUG, notes=()):
    """Print the banner, then hand the app to Flask's development server."""
    banner(host, port, notes)
    app.run(host=host, port=port, debug=debug)
