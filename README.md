# Games over IP

Tiny LAN party games for two to four laptops — **Tic-Tac-Toe, Snakes & Ladders, Ludo and Bingo** —
written in plain Python with tkinter. No installs, no accounts, no internet: one laptop hosts, the
others type its IPv4 address, and you play in a small window that's easy to hide.

## Get it and run it

You only need Python 3.8+ (from [python.org](https://www.python.org/downloads/); tkinter is included
on Windows and macOS).

```bash
git clone https://github.com/<your-user>/games-over-ip.git
cd games-over-ip
python main.py
```

No git? Use **Code → Download ZIP** on GitHub, unzip, and double-click `play.bat` (Windows, no console
window) or run `python main.py`.

Linux: if tkinter is missing, install it with `sudo apt install python3-tk`.

## How a game starts

1. Everyone opens the app on their own laptop. All laptops must be on the **same Wi-Fi or hotspot**.
2. One person picks **Host a game**. Their IPv4 address is filled in automatically (it's the
   "IPv4 Address" that `ipconfig` shows); pick another from the list if they have several.
3. The host picks a game and how many people and bots play, then creates the room.
4. Everyone else picks **Join a game** and either clicks the host in **Nearby games** or types the
   host's IPv4 address.
5. When everyone is in the lobby, the host presses **Start**. Empty seats are played by bots.

| Game | Players | Notes |
|---|---|---|
| Tic-Tac-Toe | 2 (or you vs a bot) | Best of 1, 3 or 5; easy, normal or unbeatable bot |
| Snakes & Ladders | 2–4 | A new random board every game (classic board optional); a 6 rolls again; exact roll to finish |
| Ludo | 2–4 | Ludo King rules: 6 to leave base, safe stars, captures give another roll; quick mode needs 2 tokens home |
| Bingo | 2–4 | Each player gets a secret random 1–25 board; take turns calling numbers; the first to complete 5 lines spells B-I-N-G-O |

If someone's Wi-Fi drops or they close the app, a bot keeps their seat warm. Join the same room
again (Nearby games or the same IP) and you get your seat back with your pieces where they are
now: your app remembers a secret seat key, so this works even with a different name, after
restarting the app, before the host has noticed you dropped, or after the host started a rematch
without you. A friend who arrives mid-game takes a seat a bot is playing for a person (seats the
host gave to bots stay bots).

## Staying low-key

- **Esc** hides the window instantly (or shows a plain "Notes" page instead — choose in Settings).
- **Settings** has window sizes S / M / L, see-through opacity, and three themes: **Neon**,
  **Dim** (dark and low-brightness) and **Paper** (looks like a plain notes app; the window title
  becomes "Notes").
- There is no sound at all.

## Can't connect?

- **Same network?** Every laptop must be on the same Wi-Fi or the same phone hotspot.
- **Windows Firewall:** the first time you host, Windows asks whether Python may use the network.
  Click **Allow**. If you clicked Cancel, open "Allow an app through Windows Firewall" and tick
  Python for Private (and Public, on campus Wi-Fi).
- **Campus or café Wi-Fi often blocks laptop-to-laptop traffic** ("client isolation"). Turn on a
  phone hotspot, connect every laptop to it, and host again — the hotspot gives the host a new IP.
- **Right IP?** Use the IPv4 address shown on the host's screen, not `127.0.0.1`.
- The game uses TCP port 50505 (changeable) and UDP port 50506 for "Nearby games".

## For developers

```bash
python -m unittest discover -s tests -v        # all tests (GUI tests skip without a display)
python tools/devplay.py ludo --bots 3          # play one game without the menus
python tools/devplay.py bingo --autoplay       # watch bots play
python tools/gallery.py --theme neon           # every widget and painter
python tools/e2e_app.py --theme paper --size S # drive the whole app, screenshots in shots/
python tools/click_play.py                     # play every game with synthetic mouse clicks
```

Keyboard: **Space** rolls the dice (Ludo, Snakes & Ladders), **1–4** picks a Ludo token, **1–9** places
a Tic-Tac-Toe mark, **S** / **R** shuffle or ready your Bingo board, **Esc** is the boss key.

Everything is standard library: `socket` + `selectors` for networking, `tkinter.Canvas` for the UI,
and a small pure-Python rasteriser (`goip/gfx.py`) that draws the anti-aliased, glossy sprites
straight into PNG images. The host runs the referee (an authoritative server inside the same app);
other laptops only send moves. See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the full design.
