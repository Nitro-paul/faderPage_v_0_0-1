# faderPage

A web mixing desk for your Linux desktop. Run one small server on the machine whose sound you want
to control; open its page from any phone, tablet or laptop on the local network and you get:

- one **fader per application** that is playing audio (Firefox, Discord, mpv, …),
- a **master fader** for the default output,
- a **microphone fader** and one fader per **recording application**,
- a **mute button** and a **live level meter** on every strip,
- an **EQ tab** with a simple 3-band equalizer (low / mid / high) for every application.

Faders follow changes made elsewhere (KDE's volume applet, `pavucontrol`, an app changing its own
volume), and strips appear/disappear as applications start and stop playing.

## Requirements

- Linux with PipeWire (with `pipewire-pulse`) or PulseAudio — i.e. `pactl` and `parec` on the PATH
  (package `libpulse` / `pulseaudio-utils`).
- Python 3.9+. No third-party packages.
- For the EQ tab: PipeWire's `pipewire` and `pw-cli` on the PATH (its `filter-chain` module does the
  work; developed against PipeWire 1.6). Without them everything else works and the EQ tab says so.

## Run

```sh
./faderpage.py                 # listens on 0.0.0.0:8765
```

It prints the address to open, e.g. `http://192.168.1.20:8765/`.

| Option | Default | |
| --- | --- | --- |
| `--host ADDR` | `0.0.0.0` | `127.0.0.1` = this machine only |
| `--port N` | `8765` | |
| `--max-volume PERCENT` | `100` | top of every fader (100–200; above 100 is software boost) |
| `--no-meters` | off | don't start any `parec` processes |
| `--no-eq` | off | disable the equalizer: no `pipewire` helpers, and no stream is ever re-routed |
| `-v`, `--verbose` | off | log every HTTP request |

If another device can't connect, open the port in your firewall (e.g. `sudo firewall-cmd --add-port=8765/tcp`
or `sudo ufw allow 8765/tcp`).

### Start on login (systemd user service)

`~/.config/systemd/user/faderpage.service` — adjust the path:

```ini
[Unit]
Description=faderPage volume faders
After=pipewire-pulse.service

[Service]
ExecStart=/usr/bin/python3 %h/path/to/faderpage.py
Restart=on-failure

[Install]
WantedBy=default.target
```

Then `systemctl --user enable --now faderpage`.

## Using the page

- Drag a fader, or use the arrow keys / PageUp / PageDown / Home / End when it has focus.
  Double-click (or double-tap) a fader to jump to 100 %.
- The master strip stays docked on the right; the other strips scroll sideways on small screens.
- The **EQ** tab has one strip per application with three sliders, **Low** (shelf below ~200 Hz),
  **Mid** (broad bell around 1 kHz) and **High** (shelf above ~3 kHz), each ±12 dB. Double-click a
  slider (or press `0`/`Delete` on it) to zero it, **Flat** zeroes the whole strip. A dot on the tab
  shows that some EQ is active. The tab is remembered in the URL (`#eq`).
- Faders set all channels of a stream to the same level (any left/right balance is flattened).
- Percentages are PulseAudio's own, so they match what KDE and `pavucontrol` show.

## Things worth knowing

- **There is no authentication.** Anyone who can reach the port can change your volume. That is fine
  on a trusted home network; otherwise bind to `127.0.0.1` or firewall the port. The server does refuse
  cross-origin POSTs, so a random website you visit can't drive the faders through your browser.
- **Meters only run while a page is open**, and only for streams that are actually playing/recording
  (not paused ones). This is on purpose: metering a suspended device would wake it up, and for the
  microphone it would keep it open. A paused stream therefore shows a fader but a dead meter.
  Meters show the signal after the stream's own volume, so pulling a fader down lowers its meter.
- The page itself keeps no state; everything is read from, and written to, the sound server.

### The equalizer

PulseAudio/PipeWire have no per-application EQ, so faderPage builds one: the first time you move a
band away from 0 dB it starts a small `pipewire -c` helper (a filter-chain: three biquads) that appears
as a virtual output named *faderPage EQ - \<app\>*, and moves that application's stream onto it. The
helper plays on into the device the stream was using. The fader, mute and meter of the strip keep
working as before (the meter then shows the signal after the EQ). Things to know:

- **Nothing changes until you touch an EQ slider.** The helper is removed again (and the stream put
  back) a few seconds after the strip is flat, when the application stops, or when faderPage exits.
  If faderPage is killed outright, the helpers die with it and the session manager sends the streams
  to the default output.
- **Application streams only.** Master and microphone are shared devices; an EQ for them would mean
  replacing the default device, which faderPage deliberately doesn't do. Recording applications have
  no EQ either.
- **The EQ belongs to the stream, not the application**: when an application restarts, its EQ starts
  flat again.
- While an EQ is active the stream is mixed down to stereo and gains a little latency. Each active
  EQ is one extra process of about 10 MB.
- The virtual output is visible to other volume controls (KDE's applet, `pavucontrol`) while it
  exists. WirePlumber also remembers the move as a "target" for that application; this is ignored once
  the virtual output is gone (it only restores targets that exist) but may leave a stale line in
  `~/.local/state/wireplumber/stream-properties`.
- Moving a stream to another device yourself while its EQ is active ends its EQ.

## Development

```sh
python3 -m unittest discover -s tests -v
```

Layout: `faderpage.py` (server: state building, `pactl` control, meters, EQ chains, HTTP +
Server-Sent Events) and `static/` (the page: `index.html`, `style.css`, `app.js`).

How it works: `pactl subscribe` reports changes → the server re-reads the stream lists with
`pactl --format=json` → the new state is pushed to every browser over `/api/events`. Faders send
`POST /api/volume` / `POST /api/mute`, which run `pactl set-*-volume` / `set-*-mute`. Meters are
`parec --monitor-stream` processes read at 20 Hz and pushed over the same event stream.

The EQ is `POST /api/eq` with `{"id": "playback:12", "gains": {"low": 3, "mid": -1.5}}` (any subset of
the bands, in dB, snapped to 0.5). `EqManager` keeps the wanted gains and, on every fresh snapshot,
makes the sound server match: start a helper, `pactl move-sink-input` the stream onto its sink, push
gain changes with `pw-cli set-param <node> Props`, and tear everything down again. Its side effects
are separate methods, so the tests drive it with a fake clock and fake helpers.
