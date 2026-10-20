# Network Device Dashboard

A self-hosted dashboard that watches your printers, receipt printers, computers,
and phones — across one or more branches — and tells you the moment one drops
off the network.

## What's new in this version
- **Device types are now yours to manage.** Open **⚙ Settings → Manage types**
  to add a new kind of device (security camera, router, tablet, kiosk...),
  **rename** one, **change its icon** (13 to pick from), **change the ports**
  it's checked on, or **delete** it. Renaming a type renames its group
  everywhere and the devices on it stay put. Deleting a type moves its
  devices to **Other** (the catch-all type, which can be renamed but not
  deleted) - nothing stops being monitored. The Add/Edit Device form, the type
  filter, the group headings and the logs all use your list.
  The **ports** setting matters: when a device doesn't answer ping, the
  dashboard tries that type's ports before calling it offline, so a camera
  type with ports `80, 554` is checked sensibly. Types are saved under
  `"types"` in `devices.json`.
- **Mistakes in `devices.json` no longer break things.** If you edit the file
  by hand and make a typo (a missing or extra comma, curly quotes pasted
  from Word/chat, a leftover `//` comment, a missing bracket...), the
  dashboard keeps running on your last working list and shows a red banner
  at the top saying **which line** is wrong and what to do about it. Fix the
  file and save - it's picked up automatically within a few seconds, no
  restart. While the file is broken, changes made in the dashboard are
  refused rather than overwriting your edit. A device with a bad IP, a
  duplicate name or an unknown type is skipped with a yellow banner telling
  you which one; everything else keeps being monitored. Every time the
  dashboard saves, the previous version is kept as `devices.json.bak`.
- **⚙ Settings popup** (top right). Change the **port** (applies instantly -
  the page switches to the new address by itself), choose whether the
  dashboard opens in **its own window**, your **browser**, or **nothing**,
  set how often devices are checked (2 s – 1 min), choose whether other
  computers on your network can view it, or **Quit dashboard** completely.
  These are saved in `devices.json` under `"settings"`.
- **Easier to get around**, same layout: the **total / online / offline**
  counts at the top are now clickable filters; offline devices sort to the
  top of each group; your last branch/type/status filter is remembered;
  **Esc** closes any popup and **Enter** saves a form; delete/quit
  confirmations are proper popups instead of the plain browser box; the
  footer shows a live "updated" indicator; and the Logs popup has an
  **Export CSV** button for sharing a problem history.
- **Only one copy runs.** Launching it a second time just reopens the
  window. If your chosen port is taken by another program, it automatically
  uses the next free one and remembers it.
- **Fixed false "online" on unused IPs.** On Windows, pinging an address
  nobody is using can print "Destination host unreachable" yet still report
  success, which made empty IPs flash online with a ~1500 ms response. A
  device now only counts as online if it sends a real echo reply (or answers
  on one of its TCP ports), and an address the network says is empty is
  marked offline straight away.
- **Refreshes every 5 seconds** (the checks and the page). Set
  `CHECK_INTERVAL_SECONDS` near the top of `app.py` to change it. Because
  checks are now far more frequent, logs are kept for 7 days by default
  (`LOG_RETENTION_DAYS`).
- **Fixed "Invalid Date".** The server was generating a malformed timestamp
  that the browser couldn't parse, so the last-checked time on each card
  showed "Invalid Date". That's fixed — and that spot on each card is now a
  **📜 Logs** button (see below) instead of a timestamp.
- **Connection logs.** Every single check (online or offline) is now
  recorded with its timestamp, response time, which method found it
  (ping vs. a specific TCP port), and — when it fails — *why* (timed out,
  connection refused, destination unreachable, etc.). Click **📜 Logs** in
  the branch row to browse the full history with filters for device,
  branch, online/offline, time range, and a text search. Click the **📜 Logs**
  button on an individual card to jump straight into that device's history.
  Logs are stored locally in `logs.db` and auto-pruned after 14 days so the
  file doesn't grow forever — adjust `LOG_RETENTION_DAYS` near the top of
  `app.py` if you want to keep more or less history.
- **More reliable online detection.** Previously a device only got checked
  on ping, then one fallback TCP port. Now it tries several common ports
  for its device type before giving up (e.g. a computer tries 445, 3389,
  139, 135, 22, 80 in turn), which catches a lot of real devices that the
  single-port check was missing. See the troubleshooting note below if
  things still show offline when you know they're on.
- **Branch field is now a dropdown** in the Add/Edit Device form, matching
  how Type already worked — pick from your existing branches instead of
  typing one. Create new branches ahead of time from **⚙ Manage**.
- **Fixed the rename bug.** Renaming a device could get "funny" — the card
  would keep showing the old name, and renaming it again right after would
  fail. That's fixed: the server now updates everything it's tracking about
  a device the instant you rename it, so you can rename the same device
  multiple times in a row with no issue.
- **Uptime % removed**, per your request — cards now just show live status,
  response time, and when they were last checked.
- **Branches.** Every device has a `branch` (e.g. "Main Office", "Tel1").
  Use the branch row at the top of the dashboard to view one branch at a
  time, or "All branches" to see everything at once. Your current device
  list was auto-assigned branches based on naming (`Chris`/`Nick` →
  "Main Office", `Tel1_*` → "Tel1", etc.).
- **Manage Branches.** Click **⚙ Manage** next to the branch row to add a
  brand-new branch (even before any device uses it), rename a branch — which
  updates every device on it automatically — or delete one. Deleting a
  branch that still has devices on it moves them to "Unassigned" rather than
  deleting the devices, so nothing gets lost.
- **Packaged as a standalone .exe** — see below.

## How it works
- A background thread checks every device every 5 seconds.
- It tries an ICMP ping first. If that fails (many printers/phones block ping),
  it falls back to a TCP connection check on a relevant port (e.g. 9100 for
  printers, 445 for PCs, 80 for phones) so you don't get false "offline" alerts.
- A web dashboard shows live status, grouped by device type, filterable by
  branch and type, with search.

## Editing devices from the dashboard
You don't need to touch `devices.json` by hand.
- **+ Add device** — add a new device with its name, IP, type, branch, and
  an optional port override.
- **Hover any card → pencil icon** — edit a device's name, IP (e.g. if it
  got a new one from DHCP), type, branch, or port, or delete it.

Changes save straight to `devices.json` and take effect on the next check cycle.

---

## Option A: Run the standalone .exe (recommended - no Python needed to run it)

This folder includes **`build_exe.bat`**, which builds a single
`Network Dashboard.exe` that runs on any Windows PC with no Python install
required *to run it*. Building it the one time does need Python + an internet
connection (to fetch PyInstaller), since I can't produce a Windows .exe
directly from here — I can only hand you a script that builds it on your
own Windows machine.

**To build it:**
1. Make sure this whole folder (`app.py`, `devices.json`, `templates/`,
   `static/`, `dashboard.ico`, `build_exe.bat`) is together in one place on
   a Windows PC that has Python installed (get it free from
   [python.org](https://www.python.org/downloads/) if needed — check "Add
   python.exe to PATH" during install).
2. Double-click **`build_exe.bat`**. It installs PyInstaller and builds the
   exe — this takes a minute or two.
3. When it finishes, your exe is at `dist\Network Dashboard.exe` — it uses
   `dashboard.ico` as its icon, has no console window, and opens the
   dashboard in its own window when launched (see Settings below).
4. Copy `Network Dashboard.exe` wherever you want it to live (your
   dedicated PC/server) and double-click it to run. A `devices.json` will
   appear right next to the exe after the first launch — that's a live copy
   of your device list, seeded from the one bundled in this folder, and
   it's what gets updated when you add/edit/delete devices from the
   dashboard. Back it up like you would any config file.

**About "localhost":** under the hood the program runs a small web server on
your PC (that's how other computers can view it too), so the address is
always `http://<this PC>:<port>`. By default it now opens in **its own
window** (using Microsoft Edge or Chrome in "app" mode - no address bar, no
tabs), so you won't see the address at all. If neither is installed it
falls back to your normal browser.

**Closing it:** closing the window does *not* stop monitoring - the program
keeps running in the background. Double-clicking the exe again just reopens
the window. To stop it completely, use **Settings → Quit dashboard**.

**Running it at startup:** drop a shortcut to the exe in the Windows
Startup folder (`shell:startup` in the Run dialog) so it launches
automatically when the PC boots.

## Option B: Run it with Python directly (no building step)

Nothing to install to run it this way either — it's pure standard library.

1. Make sure Python 3 is installed.
2. Open a terminal in this folder and run:
   ```
   python app.py
   ```
   (or `python3 app.py` depending on your system)
3. It opens the dashboard automatically (or go to `http://localhost:5000`
   yourself - or whichever port you set). From another device on the
   network: `http://<this-machine's-LAN-IP>:<port>` (Settings shows the exact address).

---

## Device config reference
`devices.json`:
```json
{ "name": "Front Desk Printer", "ip": "192.168.1.50", "type": "printer", "branch": "Main Office" }
```
`type` is the id of one of your device types (by default `computer`, `phone`,
`printer`, `receipt_printer`, `other`) - or just its name, like "Receipt Printer".
Manage them under **Settings → Manage types**.
`branch` can be any text you want — new branch names just show up as a new
filter option.
Optional: add `"port": 631` to a device to override the default TCP fallback port.

## Editing `devices.json` by hand
Easiest is the **+ Add device** / pencil buttons, but hand-editing works too.
The rules (the dashboard will point at the line if one is broken):
- Every device is a `{ ... }` block; blocks are separated by a comma, but
  there must be **no comma after the last one**.
- Inside a block each `"name": value` pair is also separated by commas, with
  **no comma after the last pair**.
- Use plain straight quotes `"` (not curly ones like “ ”), and no comments.
- `name` must be unique; `ip` must look like `192.168.1.50`; `type` is one of your device
  types (see Settings → Manage types).
- A new device needs at least `name` and `ip` - for example:
  ```json
  { "name": "Back Printer", "ip": "10.19.1.90", "type": "printer", "branch": "Tel1" },
  ```
- If you get stuck, copy `devices.json.bak` (the last version the dashboard
  itself saved) over `devices.json`.

## If the program stops unexpectedly
A windowed .exe has no console to show errors, so if it ever does crash it
shows a message and saves the details to **`dashboard-error.log`** next to the
exe. Send me that file and the cause is usually obvious. A typo in
`devices.json` no longer causes this - that just shows the red banner.

## Finding your devices' IPs
Most routers have a "connected devices" or "DHCP clients" page in their
admin panel listing every device's name and IP — usually more reliable than
scanning, since printers/phones often show a manufacturer name there.

## Keeping it running 24/7
- **Windows (exe)**: put a shortcut to `Network Dashboard.exe` in the
  Startup folder (`shell:startup`), or use Task Scheduler to run it at logon.
- **Windows/Linux (python app.py)**: Task Scheduler on Windows, or a
  systemd service on Linux:
  ```
  [Unit]
  Description=Network Device Dashboard
  After=network.target

  [Service]
  WorkingDirectory=/path/to/network-dashboard
  ExecStart=/usr/bin/python3 app.py
  Restart=always

  [Install]
  WantedBy=multi-user.target
  ```
  Then: `sudo systemctl enable --now net-dashboard`

## Notes on scale
Built to comfortably handle dozens to a couple hundred devices — checks run
in parallel threads, so 30-100 devices at a 5s interval is fine. If
you grow past a few hundred, raise `CHECK_INTERVAL_SECONDS` in `app.py`.

## Troubleshooting: a device shows offline but you know it's on
This is almost always a firewall blocking the check, not a bug in the
dashboard — click **📜 Logs** on that device's card to see exactly what each
attempt reported (e.g. "timed out" vs "connection refused" tells you very
different things). A few common causes:

- **Windows PCs block ping by default.** Windows Firewall does *not* allow
  incoming ICMP Echo Requests out of the box. On the PC in question: Control
  Panel → Windows Defender Firewall → Advanced Settings → Inbound Rules →
  enable **"File and Printer Sharing (Echo Request - ICMPv4-In)"** for your
  network profile. Once that's on, ping-based detection will work for it.
- **No open ports either.** If ping is blocked *and* nothing is listening on
  the fallback ports this dashboard tries (445, 3389, 139, 135, 22, 80 for
  computers), it'll show offline even though the PC is on — there's simply
  nothing for it to check. Enabling the ICMP rule above is the easiest fix.
  Alternatively, set a `"port"` in `devices.json` (or the edit panel) for a
  port you know is open and reachable on that machine.
- **Printers/receipt printers**: these almost always answer on port 9100
  (raw printing) even with ping blocked, so they tend to "just work."

## Firewall note
Make sure the dashboard's port (5000 by default) is allowed through the server's firewall so other
computers on the network can reach the dashboard. Ping (ICMP) also needs to
be allowed outbound from this machine, which it is by default on most setups.
