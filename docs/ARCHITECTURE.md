# Games over IP — architecture and build spec

This file is the contract every module is built against. If you change an
interface, update this file in the same change.

## 1. Goals and hard constraints

- **Standard library only.** Python 3.8+, `tkinter` for UI, `socket`/`selectors`/`threading`
  for networking. No pip installs, no Pillow, no numpy. Must run with `python main.py`
  on Windows, macOS and Linux right after `git clone`.
- **Small, resizable window.** Presets S 360×520, M 420×600 (default), L 520×740, minimum
  340×480 (logical px at 96 DPI). Every layout is computed from the current canvas size.
- **"Mobile game" polish on a plain `tk.Canvas`**: gradient backgrounds, rounded cards with
  soft shadows, glossy anti-aliased sprites rendered in pure Python into PNG → `PhotoImage`,
  eased animations. **No sound. Nothing flashes or strobes** (this is played in lectures).
- **LAN multiplayer.** One PC hosts (the authoritative server runs inside the same app),
  others join by typing the host's IPv4 (what `ipconfig` shows) or picking it from the
  auto-discovered list.
- **Games:** Tic-Tac-Toe, Snakes & Ladders, Ludo (Ludo King rules), Bingo (5 lines = B-I-N-G-O).
  Seats not taken by people are bots, so "1 vs bot(s)", 2, 3 or 4 players all work.
- **Extras:** boss key (Esc) with stealth themes; a bot takes over a disconnected player's
  seat and the player can reclaim it by rejoining with the same name; auto-find hosts on the LAN.

## 2. Repository layout

```
main.py                    entry: checks Python/tkinter, calls goip.app.main()
play.bat                   Windows: start without a console window (pythonw)
goip/
  __init__.py              version, ports, window sizes              [contract, done]
  __main__.py              python -m goip                             [done]
  protocol.py              JSON-lines framing, message names, clean_name   [contract, done]
  theme.py                 Theme + NEON / DIM / PAPER, font families  [contract, done]
  netutil.py               local IPv4 list, IP validation, friendly error text
  server.py                RoomServer: TCP lobby + game host, own thread
  client.py                Client: connection to a RoomServer, reader thread -> queue
  discovery.py             UDP Beacon (host) + Scanner (join screen)
  gfx.py                   pure-Python RGBA rasteriser -> PNG -> tk.PhotoImage, SpriteCache
  widgets.py               UI context, Animator, canvas widgets, shared painters (dice, tokens)
  settings.py              load/save user prefs (~/.games_over_ip.json)
  app.py                   App: window, DPI, screen router, boss key, network pump, main()
  screens/                 menu.py host.py join.py lobby.py play.py settings.py notes.py
  games/
    __init__.py            registry: GAME_IDS, logic_class(), view_module()   [contract, done]
    base.py                GameLogic contract (server side, no tkinter)      [contract, done]
    viewbase.py            GameView + ViewContext contract (client side)     [contract, done]
    testing.py             FakeHost + make_game for headless tests           [contract, done]
    tictactoe/ snakes/ ludo/ bingo/
        __init__.py        (empty)
        logic.py           class Logic(GameLogic)
        view.py            class View(GameView); def paint_icon(img, theme)
tests/                     unittest; run:  python -m unittest discover -s tests -v
tools/                     snapshot.py  gallery.py  devplay.py  (dev helpers)
docs/ARCHITECTURE.md       this file
```

## 3. Runtime model

- **Host PC:** `RoomServer` runs in one daemon thread: a `selectors` loop plus a timer heap.
  **All game logic runs on that thread**, so games never need locks. The host's own UI is an
  ordinary `Client` that connects to its own server over TCP and authenticates with
  `server.host_token`. Host and joiner UIs therefore share one code path.
- **Every PC:** the Tk main thread owns all widgets. `Client` has a reader thread that puts
  decoded dicts on a `queue.Queue`; `App` drains it every ~15 ms via `root.after`.
  **Never touch Tk from another thread.**
- **The host is the referee.** Clients only send intents (`act`). The server validates,
  mutates state, then sends `event`s (animation hints) followed by per-seat `state` (truth).

## 4. Protocol

One JSON object per line, UTF-8, `\n` terminated, at most 64 KiB (`protocol.MAX_LINE`); a peer
that exceeds it is disconnected. Every message has a string field `t`. Use
`protocol.encode(msg) -> bytes` and `protocol.LineBuffer().feed(data) -> [msg, ...]`.

### Client → server

| t | fields | who | notes |
|---|---|---|---|
| `hello` | `v`, `app`="goip", `name`, `token`? | anyone | must be the first message, within 5 s |
| `ping` | `n` | anyone | every 3 s; server answers `pong` |
| `act` | `a` (dict) | seated player | forwarded to `logic.on_action(seat, a)` |
| `configure` | `game`, `humans`, `bots`, `options` | host, lobby only | change game / seats / options |
| `start` | – | host, lobby only | start or rematch; open human seats become bots |
| `kick` | `client` | host | remove a joined player (they get `bye` "You were removed by the host") |
| `leave` | – | anyone | polite disconnect |

### Server → client

| t | fields | notes |
|---|---|---|
| `welcome` | `you` (client id), `host` (bool), `room` | reply to `hello` |
| `reject` | `reason` | then the socket is closed |
| `lobby` | `room` | to everyone whenever room info changes |
| `begin` | `game`, `seat` (your seat or null), `seats`, `options` | client opens the play screen |
| `state` | `s` | your seat's view (`logic.view_for(seat)`) |
| `event` | `e` (dict with `kind`) | animation hint, sent before the matching `state` |
| `seats` | `seats` | public seat list changed during play |
| `over` | `result` = `{winners: [seat], text, ...}` | game finished; room is back in lobby state |
| `error` | `msg` | your action was rejected (shown as a toast) |
| `notice` | `msg` | info toast for everyone ("Ravi left — a bot took over") |
| `pong` | `n` | |
| `bye` | `reason` | server closing, or you were kicked |

`room` dict:

```json
{"game": "ludo", "game_name": "Ludo", "humans": 2, "bots": 2, "options": {"turn_timer": 30},
 "state": "lobby", "host_name": "Asha", "address": "192.168.1.5:50505",
 "seats": [{"seat": 0, "kind": "human", "name": "Asha", "client": 1, "color": "red", "host": true},
           {"seat": 1, "kind": "open",  "name": null,   "client": null, "color": "green", "host": false},
           {"seat": 2, "kind": "bot",   "name": "Bot Mia", "client": null, "color": "yellow", "host": false}]}
```

`state` is `"lobby"` or `"playing"`. Public seat dicts (in `begin`, `seats`, `ViewContext.seats`):
`{"seat": i, "name": str, "bot": bool, "color": str}`.

**Client-side delivery notes.** `Client.poll()` never returns `bye` or `pong`: a `bye` becomes the
single `{"t": "_disconnected", "reason": ..., "bye": true}` for that connection (pong only updates
`client.rtt`). `connect()` returns the welcome dict directly; `connect_async()` queues `_connected`
(with `welcome`) or `_connect_failed` (with a friendly `error`). The App therefore only handles
`_connected`, `_connect_failed` and `_disconnected` plus the server messages above.

## 5. Room rules (server.py)

- Seats = `humans + bots`, which must be in `Logic.SEAT_COUNTS`; `humans >= 1`. Seats
  `0..humans-1` are human slots (the host is always seat 0); the rest are bots named
  "Bot <Name>" from a fixed friendly list. Colours come from `Logic.seat_colors(n)`.
- `hello`: wrong `app` or `v` → reject "Version mismatch: host has vX, you have vY". A valid host
  token marks the (single) host client. In lobby: take the first open human seat, else reject
  "Room is full". While playing: if a bot seat has `was_name == name` and its owner is gone,
  the player reclaims it (seat back to human, broadcast `seats` + notice "Asha is back", send them
  `begin` + `state`); otherwise reject "A game is running. Try again when it ends."
- Names are cleaned with `protocol.clean_name`; duplicates get " 2", " 3" suffixes.
- Limits: max 8 sockets, `hello` within 5 s, idle timeout 15 s (no bytes at all), send buffer
  cap 1 MiB per client. Breaking a limit disconnects that client only.
- `configure`: validate the game id, seat counts and options (`Logic.clean_options`); reply with
  `error` if more humans are connected than requested.
- `start`: only in lobby. Open human slots become bots for this game. Builds `[Seat]`, creates
  `Logic(host=room, seats, options, rng)`, sends each client `begin` (with their seat), then calls
  `logic.start()`.
- Disconnect while playing: the seat becomes a bot (`seat.is_bot = True`, name kept,
  `was_name` remembered), broadcast `seats` + notice, call `logic.on_seat_changed(i)`.
  Disconnect while in lobby: the seat becomes open again.
- Host client disconnects, or `server.stop()`: send `bye` "Host closed the room" to everyone and
  close everything.
- `game_over(result)`: broadcast `over`, state → lobby, seats whose human left become open again,
  broadcast `lobby`.
- Any exception from game code is logged with a traceback; the acting client gets `error`
  "Something went wrong on the host"; the server keeps running.
- While the room exists, `discovery.Beacon` advertises it about once per second.

## 6. Server-side game contract

See `goip/games/base.py` (authoritative, with docstrings). Summary:

- Class attributes: `ID`, `NAME`, `TAGLINE`, `SEAT_COUNTS`, `OPTIONS` (tuple of `Option`),
  `BOT_DELAY`. Class methods `seat_colors(n)` and `clean_options(dict)`.
- Implement `start()`, `view_for(seat)`, `on_action(seat, action)`, `bot_act(seat)`.
- Use `emit(kind, to_seat=None, **data)` for events, then `push()` for state,
  `schedule_actor(seat, delay)` whenever a seat must decide next, `clear_actor()` when a human
  acts, `later(delay, fn)` for other timers, `finish(winners, text, **extra)` to end.
- `view_for` must include `"turn"` (seat or null), `"timer"` (`self.time_left()`), `"phase"`
  (string) and `"finished"` (bool). Use lists indexed by seat, never int-keyed dicts.
- Shared options: `TURN_TIMER` (auto-move when a human is idle, default 30 s) and `BOT_LEVEL`.
- Tests use `goip.games.testing.make_game(Logic, n_seats, humans, options, seed)` and
  `FakeHost.run_until_over()`; everything is JSON round-tripped like the real wire.

## 7. Client-side view contract

See `goip/games/viewbase.py`. The play screen gives each game one `tk.Canvas` below a slim
header. `View(canvas, ctx)` must implement `on_state`, `on_event`, `on_resize(w, h)`, and may
implement `on_key(keysym)` and `on_seats(seats)`. `ctx.act(a="...", ...)` sends an action.
Each `view.py` also defines `paint_icon(img, theme)`, which draws the game-card icon into a square
`gfx.Image` (64–96 px).

**State is truth, events are hints.** A view never blocks on an event and never desyncs because one
was missed. Clicks while it isn't your turn give a gentle toast or nothing; never an exception.

## 8. UI kit (gfx.py + widgets.py)

Consumers (screens and games) rely on exactly this API. Additions are fine; renames are not.

### gfx.py

```
parse(color) -> (r, g, b, a)          "#rgb" "#rrggbb" "#rrggbbaa" (r,g,b) (r,g,b,a)
mix(c1, c2, t) -> "#rrggbb"           lighten(c, t) / darken(c, t) -> "#rrggbb"
with_alpha(c, a: 0..255) -> (r, g, b, a)

class Image(w, h, fill=None)          straight-alpha RGBA, "over" compositing, float coords, AA edges
    fill_rrect(x, y, w, h, r, color, grad_to=None)       grad_to = bottom colour of a vertical gradient
    stroke_rrect(x, y, w, h, r, color, width)
    fill_circle(cx, cy, r, color, grad_to=None)
    stroke_circle(cx, cy, r, color, width)
    radial_circle(cx, cy, r, inner, outer)               radial-gradient disc
    line(x1, y1, x2, y2, width, color)                   round caps
    polyline(points, width, color)                       points = [(x, y), ...]
    fill_polygon(points, color)
    soft_rrect(x, y, w, h, r, color, blur)               blurred shape for shadows and glows
    soft_circle(cx, cy, r, color, blur)
    to_photo(master=None) -> tk.PhotoImage               PNG bytes via zlib/struct, alpha kept
    save_png(path)

class SpriteCache(master, max_items=600)
    get(key, w, h, paint) -> tk.PhotoImage   paint(img) runs on a miss; key must include
                                             everything that changes the look (theme.key, colours, state)
    clear()
```

Performance budget: a 64×64 sprite under 15 ms and a 360×360 board under 400 ms on a laptop.
Use row spans: only edge pixels get per-pixel coverage maths; interior runs are written with
`bytearray` slice assignment.

### widgets.py

```
class UI                     one per App; games receive it as ctx.ui
    root, theme, sprites (SpriteCache), anim (Animator), scale (1.0 at size M)
    font(px, bold=False, display=False) -> tk font tuple   (px = size at scale 1.0)
    px(n) -> int                                           round(n * scale), at least 1
    set_theme(theme)                                       clears the sprite cache

class Animator(root, fps=60)
    tween(duration, step, ease="out_cubic", done=None, owner=None) -> handle
                             step(t) with eased t from 0 to 1; step(1.0) is always called last
    cancel(handle)   cancel_owner(owner)   busy(owner) -> bool   pause()   resume()
    EASES: linear in_quad out_quad out_cubic in_out_cubic out_back out_bounce out_elastic

draw_background(canvas, ui, w, h)      items tagged "bg", kept lowest; vertical gradient, plus soft
                                       blobs when theme.glow; cheap enough to call on every resize

Button(ui, canvas, text, command, kind="primary"|"secondary"|"ghost"|"danger", icon=None, font_px=15)
    .place(x, y, w, h)  .set_text(s)  .set_enabled(b)  .set_visible(b)  .destroy()
    hover and press states drawn with sprites
Panel(ui, canvas, radius=16, kind="card"|"inset"|"glass")      .place(x, y, w, h)  .destroy()
TextField(ui, canvas, value="", placeholder="", on_enter=None, max_len=40)
    themed tk.Entry on a rounded panel:  .place(x, y, w, h)  .value (get/set)  .focus()
    .set_error(msg or None)  .destroy()
Segmented(ui, canvas, choices=[(value, label)], value, on_change)
    .place(x, y, w, h)  .value  .set_choices(choices, value)  .destroy()
Dropdown(ui, canvas, choices=[(value, label)], value, on_change)
    button that opens a themed tk.Menu:  .place(...)  .value  .set_choices(...)  .destroy()
Toaster(ui, canvas)    .show(text, kind="info"|"good"|"bad", seconds=2.5)   stacked, slide + fade
confetti(ui, canvas, x, y, count=60, colors=None)    particle burst; no-op when not theme.glow
Dice(ui, canvas, size)
    .place(cx, cy)  .show(value)  .roll(value, done=None)  (~0.6 s tumble, then lands on value)
    .set_color(seat_color)  .set_active(bool)  (soft pulse ring = "click me")
    .bind_click(fn)  .destroy()
PlayerChip(ui, canvas)
    .place(x, y, w, h)  .update(name, color, bot=False, active=False, subtitle="", timer=None, me=False)
    glossy avatar with initial, name, "BOT" tag, turn glow, countdown ring when timer is set
paint_die(img, value, face, pip)                     sprite painter
paint_token(img, main, light, dark, style="pawn"|"disc")   glossy token with soft shadow
```

## 9. App and screens

- `app.main()`: on Windows call `ctypes.windll.shcore.SetProcessDpiAwareness(1)` (in try/except),
  create the root, measure `dpi = root.winfo_fpixels("1i")`, multiply window presets by `dpi/96`,
  apply settings (theme, size, opacity via `root.attributes("-alpha", x)`), set the title from the
  theme, `minsize`, and show the menu.
- `App` holds `root`, `ui`, `settings`, `client`, `server` (host only), `room` (latest dict),
  `my_id`, `is_host`, and the current screen. `_pump()` runs every 15 ms: drains `client.poll()`
  and routes messages. `_connected` (joiners) and the host's synchronous welcome open the Lobby;
  `lobby` updates `app.room` and notifies the screen; `begin` opens Play;
  `state`/`event`/`seats`/`over` go to Play; `error`/`notice` become toasts; `_disconnected` goes
  back to Menu with a toast. Window presets are clamped to the screen's work area.
- Settings live in `~/.games_over_ip.json`; set `GOIP_SETTINGS` to use another file (tests do).
- Screens are classes `Screen(app)` that build onto their own full-window canvas and implement
  `layout(w, h)`, `on_room(room)` and `destroy()`. Switching screens destroys the old one.
- **Menu:** title + logo, Host a game, Join a game, Settings; footer "Esc hides the window".
- **Host (step 1):** your name; your IPv4 (Dropdown of `netutil.local_ipv4s()` with labels, plus an
  editable TextField pre-filled with the best guess); port (default 50505); Next.
- **Host (step 2):** 2×2 game cards (icon from `paint_icon`, name, tagline); People 1–4 and
  Bots 0–3 chips limited to valid totals (`SEAT_COUNTS`); each `Option` as a Segmented control;
  Create room → `RoomServer(...).start()` (friendly error inline on failure), then the host
  `Client` connects with the token, then Lobby. Also used in "change game" mode from the lobby
  (sends `configure` instead of creating a room).
- **Join:** your name; host IPv4 (validated) and port; a "Nearby games" list fed by
  `discovery.Scanner` (click fills the IP and connects); Connect; on failure, show the friendly
  error plus a short "Can't connect?" help (same Wi-Fi, host clicked Allow in the Windows Firewall
  prompt, campus Wi-Fi may block laptop-to-laptop traffic, so try a phone hotspot).
- **Lobby:** big "Friends join with 192.168.1.5" share box; PlayerChips for every seat (open seats
  shown as "Waiting…"); host: kick buttons, Start (label says how many seats become bots),
  Change game; others: "Waiting for the host to start…"; Leave.
- **Play:** slim header (game name, room address, Leave). The game View fills the rest. On `over`:
  overlay with result text, confetti if you won, and buttons: host gets Rematch (sends `start`)
  and Lobby; others get Lobby. Leaving as host asks "Close the room for everyone?".
- **Settings:** name, theme (Neon / Dim / Paper), window size (S / M / L), opacity
  (100 / 90 / 80 / 70 %), boss key action (Minimise / Fake notes).
- **Boss key:** Esc anywhere. "minimise" → `root.iconify()` and pause the Animator; "notes" →
  overlay a plain Paper-styled notes editor (a real `tk.Text`, title "Notes") on top of everything
  without destroying the current screen; Esc again hides it. Games keep running underneath.
- Closing the window: close the client, stop the server, save settings.

## 10. Game rules

### Tic-Tac-Toe (`tictactoe`)
- `SEAT_COUNTS = (2,)`. Options: `BOT_LEVEL`, `match` best of 1 / 3 (default) / 5, `TURN_TIMER` (default 15).
- Seat 0 is X, seat 1 is O. Round 1 starts with seat 0; the starter alternates each round.
- Action `{"a": "place", "cell": 0..8}`. Three in a row wins the round; a full board is a draw.
- After a round: event `round_over {winner, line}`, short pause (~1.8 s), next round. The match ends
  when someone reaches the majority (best of 3 → 2 wins). Draws don't count; after
  `2 × best_of` rounds the higher score wins, a tie is a draw (`winners: []`).
- Bot: easy = random, but takes a winning move half the time; normal = win, else block, else
  minimax 60% of the time; hard = perfect minimax.
- State: `board[9]` (null/0/1), `turn`, `scores[2]`, `round`, `best_of`, `line` (winning cells or
  null), `last` (last cell), `phase` ("play" | "round_over"), `timer`, `finished`.

### Snakes and Ladders (`snakes`)
- `SEAT_COUNTS = (2, 3, 4)`. Options: `layout` classic / random, `six_again` on (default) / off,
  `exact_finish` on (default) / off, `TURN_TIMER` (default 30), `BOT_LEVEL` is not needed.
- Board 1..100, row-by-row zig-zag starting bottom-left (1 bottom-left, 10 bottom-right,
  11 above 10, … 100 top-left). Tokens start at 0 (off the board, beside square 1).
- Classic layout. Ladders: 1→38, 4→14, 9→31, 21→42, 28→84, 36→44, 51→67, 71→91, 80→100.
  Snakes: 16→6, 47→26, 49→11, 56→53, 62→19, 64→60, 87→24, 93→73, 95→75, 98→78.
- Random layout: 8 ladders and 8 snakes, each at least 10 squares long, all endpoints distinct,
  no snake head on 100, no ladder starting on 1 or ending on 100, no chain (an end is never another start).
- Turn: `{"a": "roll"}` from the seat whose turn it is. Move forward the rolled number; with
  exact_finish, an overshoot of 100 means you don't move. Ladder bottom → climb; snake head → slide.
  Rolling a 6 gives another roll (if six_again), at most 3 rolls in a row. Reaching 100 wins
  immediately (`winners: [seat]`, the others ranked by position).
- Events: `roll {seat, value}` then `move {seat, from, path: [squares stepped], to, via}` where
  `via` is `{"type": "ladder" | "snake", "from": a, "to": b}` or null. Then push. The next actor
  is scheduled after the animation time (~0.6 s dice + 0.16 s per step + 0.7 s for a ladder or snake).
- State: `board {ladders: [[a, b]], snakes: [[head, tail]]}`, `pos[n]`, `turn`, `last_roll`,
  `streak`, `phase` ("roll"), `timer`, `finished`, `winner`.

### Ludo (`ludo`) — Ludo King rules
- `SEAT_COUNTS = (2, 3, 4)`. `seat_colors`: 2 → red, yellow (opposite corners); 3 → red, green,
  yellow; 4 → red, green, yellow, blue. Corners: red bottom-left, green top-left, yellow
  top-right, blue bottom-right. Turn order is seat order, which goes clockwise.
- Options: `BOT_LEVEL`, `TURN_TIMER` (default 30), `goal`: tokens to bring home 4 (Classic, default)
  or 2 (Quick).
- Track: 52 shared squares; absolute square 0 is red's start square, then clockwise; colour
  start offsets red 0, green 13, yellow 26, blue 39. Each token has a relative progress: -1 = base,
  0..50 = main track (absolute = (progress + offset) % 52), 51..55 = its own home column,
  56 = home (centre).
- Leaving base needs a 6 and puts the token on progress 0. Moves must be exact to reach 56.
- Capture: landing on a non-safe square holding opponent tokens sends all of them back to base,
  and the capturer gets an extra roll. Safe squares: the four start squares (0, 13, 26, 39) and
  the four stars (8, 21, 34, 47). Same-colour stacking is allowed; there are no blockades.
- A 6 gives another roll; a third 6 in a row ends the turn with no move. Bringing a token home
  gives another roll unless that finishes your goal. No legal move → the turn passes.
- Turn steps: phase `roll` → `{"a": "roll"}`; if there are no legal moves the turn passes; if
  exactly one distinct move exists (or all movable tokens are in base) the server auto-plays it
  after a short pause; else phase `move` with `movable: [token indexes]` → `{"a": "move", "token": k}`.
- The first player to bring `goal` tokens home wins (`winners: [seat]`); others are ranked by progress.
- Bot: priority capture > leave base > reach home > reach a safe square > escape danger >
  advance the token furthest along (easy = random legal move 50% of the time; hard also avoids
  landing within 1–6 squares in front of an opponent).
- Events: `roll {seat, value}`, `move {seat, token, path: [progress values], captured: [{seat, token}]}`,
  `home {seat, token}`, `skip {seat, reason}`.
- State: `tokens[n][4]` (progress), `colors[n]`, `turn`, `dice`, `phase` ("roll" | "move"),
  `movable`, `sixes`, `home_count[n]`, `goal`, `timer`, `finished`, `ranking`.

### Bingo (`bingo`)
- `SEAT_COUNTS = (2, 3, 4)`. Options: `BOT_LEVEL`, `TURN_TIMER` (default 30).
- Phase `arrange` (up to 40 s): each player gets a random 5×5 board of the numbers 1..25 that only
  they can see. Actions: `{"a": "shuffle"}` (new random board), `{"a": "swap", "i": 0..24, "j": 0..24}`,
  `{"a": "ready"}`. Bots are ready immediately. When everyone is ready, or the time runs out, the
  game moves to phase `play`.
- Phase `play`: turns rotate starting from a random seat. The current player sends
  `{"a": "call", "n": 1..25}` with a number not called yet. Every board marks it. Lines are the
  5 rows, 5 columns and 2 diagonals; each completed line lights one letter of B-I-N-G-O. Anyone
  with 5 or more lines after a call wins; several at once share the win ("Asha and Ravi tie!").
- Bot: picks the uncalled number that most advances its own nearly complete lines (weight
  4^marks per line through the cell), with randomness at easy.
- Events: `called {seat, n}`, `line {seat, count}` (only to that seat for their own new lines),
  `ready {seat}`.
- `view_for(seat)`: own `board[25]`, `called` (ordered list), `lines[n]` (counts for everyone),
  `my_lines` (indexes 0..11 of your completed lines), `ready[n]`, `turn`, `phase`, `timer`,
  `finished`; when finished, `boards[n]` reveals every board.

## 11. Visual style

- Neon is the default. Indigo-to-violet gradient background, cards (`theme.panel`, radius 16)
  with a soft shadow, primary buttons as an `accent → accent_hi` gradient with a faint glow.
  Boards sit on light surfaces (`board_a` / `board_b`) so the colourful tokens pop.
- Text: title 26 px display bold; heading 18 bold; body 14; small 12 (all at scale 1.0 via
  `ui.font`). Spacing unit 8 px; outer margin 16 px.
- Motion: UI transitions 150–300 ms ease-out; token moves 0.12–0.18 s per square with a small
  hop; dice tumble ~0.6 s. Respect `theme.glow` (Dim and Paper: no glows, particles or blobs).
- Everything must look right at size S (360×520) and at L. Test both.

## 12. Testing and tools

- `python -m unittest discover -s tests -v` must pass with no display. GUI tests call
  `tk.Tk()` inside try/except and `skipTest` if that fails.
- Logic tests use `testing.make_game` and play complete all-bot games for every seat count and
  option combination (several seeds), asserting rule invariants on every push (`host.on_push`).
- `tools/snapshot.py`: `capture(widget, path)` saves a tk window to PNG (Windows `PrintWindow`
  via ctypes, PNG encoded with gfx). Used to check visuals by looking at the PNG.
- `tools/gallery.py`: shows every widget and painter in all three themes; `--shot out.png`.
- `tools/devplay.py <game> [--humans N] [--bots M] [--autoplay] [--size S|M|L] [--theme neon]
  [--shot out.png --seconds 6]`: runs a local RoomServer on 127.0.0.1, one Client, and the game's
  View in a window without the app screens. Lets you play or watch bots play.
- `tools/e2e_app.py [--theme] [--size] [--game]`: drives the real App through host -> lobby ->
  play -> result and join (bad port, then good) -> lobby -> host closes, saving screenshots.
- `tools/click_play.py [games...]`: plays seat 0 of every game with synthetic mouse clicks against a
  real server (idle timer off, so a missed click fails the run).
- `tests/test_views.py` replays complete recorded games (events + states in wire order) into every
  View in all themes; `tests/test_app.py` smoke-tests every screen, theme, size, the boss key and a
  hosted game. Both fail on any Tk callback exception or logged error.
- Game views share `goip/games/viewkit.py` (named canvas items, hop/glide tweening).

## 13. Code rules

- Standard library only. Python 3.8 syntax: no `match`, no runtime `X | Y` unions,
  no `str.removeprefix`, no `zip(strict=...)`, no parenthesised context managers.
  Start every module with `from __future__ import annotations`.
- No `print()` in library code; use `logging.getLogger(__name__)`.
- Keep modules under ~900 lines; split helpers out if needed. Short docstrings.
- UI copy: sentence case, short, friendly, no exclamation marks except in winner banners.
