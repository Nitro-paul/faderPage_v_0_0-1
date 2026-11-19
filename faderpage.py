#!/usr/bin/env python3
"""faderPage - a web mixing desk for the local PipeWire/PulseAudio session.

Serves a page on the local network with one fader per audio stream (plus
master, microphone and recording streams) and a simple 3-band EQ per
application. Uses only the Python standard library and shells out to
``pactl`` (control), ``parec`` (level meters) and, for the EQ, ``pipewire`` /
``pw-cli`` (a filter-chain per equalized stream).
"""

from __future__ import annotations

import argparse
import array
import ctypes
import json
import math
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

__version__ = "0.0.1"

PACTL = "pactl"
PAREC = "parec"
PIPEWIRE = "pipewire"
PW_CLI = "pw-cli"

STATIC_DIR = Path(__file__).resolve().parent / "static"
STATIC_ROUTES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/style.css": ("style.css", "text/css; charset=utf-8"),
}

VOLUME_NORM = 65536  # PulseAudio raw value for 100 %

METER_CLIENT_NAME = "faderPage-meter"
METER_RATE = 8000  # Hz, mono s16le: cheap, and plenty for a level meter
METER_WINDOW_S = 0.05  # one peak value per 50 ms
METER_CHUNK_BYTES = int(METER_RATE * METER_WINDOW_S) * 2
METER_STALE_S = 0.4  # a meter that stops delivering data reads as silence
METER_RESTART_AFTER_S = 3.0  # don't respawn a dying parec in a tight loop

EQ_BANDS = ("low", "mid", "high")
EQ_FILTERS = {  # band -> (PipeWire biquad, corner/centre frequency in Hz, Q)
    "low": ("bq_lowshelf", 200.0, 0.7),
    "mid": ("bq_peaking", 1000.0, 0.5),
    "high": ("bq_highshelf", 3000.0, 0.7),
}
EQ_RANGE_DB = 12
EQ_STEP_DB = 0.5
EQ_NODE_PREFIX = "faderpage_eq_"  # the chain's virtual sink is EQ_NODE_PREFIX + stream index
EQ_OUTPUT_PREFIX = "output." + EQ_NODE_PREFIX  # ... and its stream into the real device
EQ_FLAT_GRACE_S = 5.0  # an all-flat chain lingers this long, so dragging a band through 0 dB is free
EQ_ATTACH_TIMEOUT_S = 8.0  # give up on a chain whose sink never appears / never takes the stream
EQ_RESTORE_TRIES = 4  # moving a stream back is retried (and checked) this often ...
EQ_RESTORE_POLLS = 6  # ... looking this many times, 50 ms apart, after each attempt

TARGET_RE = re.compile(r"(?:master|mic|(?:playback|recording):\d+)")
EQ_TARGET_RE = re.compile(r"playback:(\d+)")
EQ_OUTPUT_RE = re.compile(re.escape(EQ_OUTPUT_PREFIX) + r"(\d+)")
EVENT_RE = re.compile(r" on (?:sink|source|server)")
GENERIC_MEDIA_NAMES = {
    "", "playstream", "recstream", "playback", "record", "recording",
    "audio stream", "audio", "stream", "output", "input",
}

MAX_BODY_BYTES = 4096
SSE_KEEPALIVE_S = 15


def log(msg: str) -> None:
    print(f"faderpage: {msg}", file=sys.stderr, flush=True)


# --------------------------------------------------------------------------
# pactl helpers and pure state building (unit-tested, no side effects)
# --------------------------------------------------------------------------

def pactl_json(*args: str):
    out = subprocess.run(
        [PACTL, "--format=json", *args],
        capture_output=True, text=True, timeout=5, check=True,
    ).stdout
    return json.loads(out, strict=False)


def fetch_raw() -> dict:
    return {
        "sink_inputs": pactl_json("list", "sink-inputs"),
        "source_outputs": pactl_json("list", "source-outputs"),
        "sinks": pactl_json("list", "sinks"),
        "sources": pactl_json("list", "sources"),
        "info": pactl_json("info"),
    }


def volume_percent(volume: dict | None) -> int:
    """Loudest channel as a percentage (PulseAudio's raw 65536 == 100 %)."""
    values = [ch.get("value", 0) for ch in (volume or {}).values()]
    return round(max(values) * 100 / VOLUME_NORM) if values else 0


def stream_labels(props: dict) -> tuple[str, str]:
    """(title, subtitle) for a stream, derived from its PulseAudio properties."""
    binary = props.get("application.process.binary") or ""
    media = props.get("media.name") or ""
    name = (props.get("application.name") or binary or media or "Unknown")
    sub = media
    if media.strip().lower() in GENERIC_MEDIA_NAMES:
        sub = binary
    if sub.lower() == name.lower():
        sub = ""
    return name, sub


def is_internal_stream(props: dict, own_pids: set[int]) -> bool:
    """Our own meter streams (and other peak-detect taps) are not user audio,
    and neither is the stream an EQ chain uses to play into the real device."""
    if props.get("application.name") == METER_CLIENT_NAME:
        return True
    if props.get("media.name") == "Peak detect":
        return True
    if (props.get("node.name") or "").startswith(EQ_OUTPUT_PREFIX):
        return True
    pid = props.get("application.process.id", "")
    return pid.isdigit() and int(pid) in own_pids


def flat_eq() -> dict[str, float]:
    return {band: 0.0 for band in EQ_BANDS}


def eq_is_flat(gains: dict) -> bool:
    return not any(gains.values())


def parse_eq_changes(obj) -> dict[str, float]:
    """Validate ``{"low": dB, ...}`` from a client; returns snapped gains. Raises ValueError."""
    if not isinstance(obj, dict) or not obj:
        raise ValueError('gains must be an object like {"low": 3}')
    changes = {}
    for band, value in obj.items():
        if band not in EQ_BANDS:
            raise ValueError(f"unknown band (expected one of {', '.join(EQ_BANDS)})")
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ValueError(f"{band} must be a number")
        if abs(value) > EQ_RANGE_DB:
            raise ValueError(f"{band} must be within +/-{EQ_RANGE_DB} dB")
        changes[band] = round(value / EQ_STEP_DB) * EQ_STEP_DB + 0.0  # + 0.0: no "-0.0"
    return changes


def eq_chain_config(name: str, description: str, gains: dict, target: str | None) -> str:
    """Config for ``pipewire -c``: a virtual sink ``name`` that runs its input
    through low-shelf -> peaking -> high-shelf biquads and plays it on to
    ``target`` (or the default device when ``target`` is None).

    The head of the file is what the stock filter-chain.conf provides: this
    runs as a separate client process, not inside the sound server.
    """
    def q(text: str) -> str:  # SPA-JSON string; keep UTF-8 as is (it may not know \uXXXX)
        return json.dumps(text, ensure_ascii=False)

    nodes, links = [], []
    for band in EQ_BANDS:
        label, freq, qual = EQ_FILTERS[band]
        nodes.append(f'{{ type = builtin name = {band} label = {label} '
                     f'control = {{ "Freq" = {freq} "Q" = {qual} "Gain" = {float(gains[band])} }} }}')
    for a, b in zip(EQ_BANDS, EQ_BANDS[1:]):
        links.append(f'{{ output = "{a}:Out" input = "{b}:In" }}')
    # WirePlumber would otherwise remember volumes for these helper nodes.
    no_restore = "state.restore-props = false state.restore-target = false"
    playback = f"node.name = {q('output.' + name)} node.passive = true {no_restore}"
    if target:
        playback += f" target.object = {q(target)}"
    return f"""\
context.properties = {{ log.level = 0 }}
context.spa-libs = {{
    audio.convert.* = audioconvert/libspa-audioconvert
    support.*       = support/libspa-support
}}
context.modules = [
    {{ name = libpipewire-module-rt args = {{ }} flags = [ ifexists nofail ] }}
    {{ name = libpipewire-module-protocol-native }}
    {{ name = libpipewire-module-client-node }}
    {{ name = libpipewire-module-adapter }}
    {{ name = libpipewire-module-filter-chain
        args = {{
            node.description = {q(description)}
            audio.channels = 2
            audio.position = [ FL FR ]
            filter.graph = {{
                nodes = [ {' '.join(nodes)} ]
                links = [ {' '.join(links)} ]
            }}
            capture.props = {{
                node.name = {q(name)}
                node.description = {q(description)}
                media.class = Audio/Sink
                priority.session = 0
                {no_restore}
            }}
            playback.props = {{ {playback} }}
        }}
    }}
]
"""


def eq_params(gains: dict) -> str:
    """``pw-cli set-param ... Props`` argument that sets all three gains."""
    return "{ params = [ " + " ".join(f'"{band}:Gain" {float(gains[band])}' for band in EQ_BANDS) + " ] }"


def build_snapshot(raw: dict, own_pids: set[int], max_volume: int,
                   eq: dict | None = None, eq_range: int = 0) -> tuple[dict, dict]:
    """Turn raw ``pactl`` JSON into (UI state, meter targets).

    Meter targets map a strip id to ``(device, monitor_stream_index_or_None)``.
    Only streams that are actually running get a target, so idle devices are
    never woken up (or kept awake) just to draw a meter.

    ``eq`` maps strip ids to their wanted gains (see ``EqManager.gains``);
    ``eq_range`` is the +/- dB range of the EQ sliders, 0 if there is no EQ.
    """
    eq = eq or {}
    sinks = {s["index"]: s for s in raw["sinks"]}
    sources = {s["index"]: s for s in raw["sources"]}
    info = raw["info"]
    default_sink = next(
        (s for s in raw["sinks"] if s["name"] == info.get("default_sink_name")), None)
    default_source = next(
        (s for s in raw["sources"] if s["name"] == info.get("default_source_name")), None)
    if default_source and default_source["name"].endswith(".monitor"):
        default_source = None  # no real microphone; don't present a monitor as one

    # An equalized stream plays into its chain's virtual sink, which in turn
    # plays into the real device: remember which, so the real device (and the
    # master meter) still counts as busy.
    chain_device: dict[str, int] = {}
    for si in raw["sink_inputs"]:
        m = EQ_OUTPUT_RE.fullmatch((si.get("properties") or {}).get("node.name") or "")
        if m:
            chain_device[EQ_NODE_PREFIX + m.group(1)] = si.get("sink")

    state = {"max_volume": max_volume, "eq_range_db": eq_range, "master": None, "mic": None,
             "playback": [], "recording": []}
    targets: dict[str, tuple] = {}
    busy_sinks: set[int] = set()
    busy_sources: set[int] = set()

    def item(sid, name, sub, obj, **extra):
        return {"id": sid, "name": name, "sub": sub,
                "volume": volume_percent(obj.get("volume")),
                "mute": bool(obj.get("mute")), **extra}

    for si in sorted(raw["sink_inputs"], key=lambda s: s["index"]):
        props = si.get("properties") or {}
        if is_internal_stream(props, own_pids):
            continue
        sid = f"playback:{si['index']}"
        name, sub = stream_labels(props)
        corked = bool(si.get("corked"))
        state["playback"].append(item(sid, name, sub, si, corked=corked,
                                      eq={**flat_eq(), **eq.get(sid, {})}))
        if not corked:
            sink = sinks.get(si.get("sink"))
            busy_sinks.add(chain_device.get(sink["name"], si.get("sink")) if sink else si.get("sink"))
            if sink:
                targets[sid] = (sink["name"], si["index"])

    for so in sorted(raw["source_outputs"], key=lambda s: s["index"]):
        props = so.get("properties") or {}
        if is_internal_stream(props, own_pids):
            continue
        sid = f"recording:{so['index']}"
        name, sub = stream_labels(props)
        corked = bool(so.get("corked"))
        state["recording"].append(item(sid, name, sub, so, corked=corked))
        if not corked:
            busy_sources.add(so.get("source"))
            source = sources.get(so.get("source"))
            if source:
                targets[sid] = (source["name"], None)

    if default_sink:
        state["master"] = item("master", "Master", default_sink.get("description") or "",
                               default_sink)
        if default_sink["index"] in busy_sinks:
            targets["master"] = (default_sink["monitor_source"], None)
    if default_source:
        state["mic"] = item("mic", "Microphone", default_source.get("description") or "",
                            default_source)
        if default_source["index"] in busy_sources:
            targets["mic"] = (default_source["name"], None)

    return state, targets


def volume_command(target_id: str, what: str, value) -> list[str]:
    """Build the ``pactl`` argv for a volume/mute change; raises ValueError."""
    if not isinstance(target_id, str) or not TARGET_RE.fullmatch(target_id):
        raise ValueError("invalid id")
    kind, _, index = target_id.partition(":")
    obj, ref = {
        "master": ("sink", "@DEFAULT_SINK@"),
        "mic": ("source", "@DEFAULT_SOURCE@"),
        "playback": ("sink-input", index),
        "recording": ("source-output", index),
    }[kind]
    if what == "volume":
        return [PACTL, f"set-{obj}-volume", ref, f"{value}%"]
    return [PACTL, f"set-{obj}-mute", ref, "1" if value else "0"]


def peak_of(buf: bytes) -> float:
    """Peak amplitude (0..1) of little-endian signed 16-bit samples."""
    samples = array.array("h")
    samples.frombytes(buf[: len(buf) // 2 * 2])
    if sys.byteorder == "big":
        samples.byteswap()
    if not samples:
        return 0.0
    return max(max(samples), -min(samples)) / 32768.0


# --------------------------------------------------------------------------
# Level meters: one ``parec`` per distinct (device, stream), only while a
# browser is connected.
# --------------------------------------------------------------------------

class _Meter:
    def __init__(self, device: str, stream: int | None):
        cmd = [PAREC, "-d", device, f"--rate={METER_RATE}", "--channels=1",
               "--format=s16le", "--latency-msec=40",
               f"--client-name={METER_CLIENT_NAME}", "--stream-name=meter"]
        if stream is not None:
            cmd.append(f"--monitor-stream={stream}")
        self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        self.started = time.monotonic()
        self.peak = 0.0
        self.stamp = 0.0
        self.alive = True
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self) -> None:
        try:
            while True:
                chunk = self.proc.stdout.read(METER_CHUNK_BYTES)
                if not chunk:
                    break
                self.peak = peak_of(chunk)
                self.stamp = time.monotonic()
        finally:
            self.alive = False
            self.proc.stdout.close()
            self.proc.wait()

    def level(self, now: float) -> float:
        return self.peak if now - self.stamp < METER_STALE_S else 0.0

    def stop(self) -> None:
        if self.proc.poll() is None:
            self.proc.terminate()


class MeterManager:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._meters: dict[tuple, _Meter] = {}
        self._keys: dict[str, tuple] = {}
        self._spawn_failed = False

    def sync(self, targets: dict[str, tuple]) -> None:
        now = time.monotonic()
        wanted = set(targets.values())
        with self._lock:
            self._keys = dict(targets)
            for spec in list(self._meters):
                meter = self._meters[spec]
                if spec not in wanted:
                    meter.stop()
                    del self._meters[spec]
                elif not meter.alive and now - meter.started > METER_RESTART_AFTER_S:
                    del self._meters[spec]  # respawned below
            for spec in wanted - self._meters.keys():
                try:
                    self._meters[spec] = _Meter(*spec)
                except OSError as exc:
                    if not self._spawn_failed:
                        log(f"cannot start {PAREC}, level meters disabled: {exc}")
                    self._spawn_failed = True

    def levels(self) -> dict[str, float]:
        now = time.monotonic()
        with self._lock:
            return {key: self._meters[spec].level(now)
                    for key, spec in self._keys.items() if spec in self._meters}

    def pids(self) -> set[int]:
        with self._lock:
            return {m.proc.pid for m in self._meters.values()}

    def stop_all(self) -> None:
        self.sync({})


# --------------------------------------------------------------------------
# Per-stream equalizer.
#
# PulseAudio/PipeWire have no per-stream EQ, so an equalized stream is moved
# onto a small virtual sink ("faderpage_eq_<index>") that lives in a helper
# ``pipewire -c`` process: three biquads (low shelf, mid peak, high shelf)
# that play on into the stream's original device. A chain only exists while
# a band is away from 0 dB, so a mixer whose EQ is untouched does not change
# the audio routing at all. Gains are changed live with ``pw-cli set-param``.
# --------------------------------------------------------------------------

try:
    _PRCTL = ctypes.CDLL(None).prctl
except (OSError, AttributeError):  # not Linux/glibc: helpers just won't die with us
    _PRCTL = None


def _die_with_parent() -> None:
    """Runs in the helper right after fork: SIGTERM it if the server dies."""
    if _PRCTL is not None:
        _PRCTL(1, signal.SIGTERM)  # PR_SET_PDEATHSIG


class EqChain:
    """One helper process hosting the EQ sink of one stream."""

    def __init__(self, stream: int, name: str, proc, conf: str, orig_sink: str,
                 orig_index: int, follows_default: bool, gains: dict, now: float) -> None:
        self.stream = stream
        self.name = name
        self.proc = proc
        self.conf = conf
        self.orig_sink = orig_sink              # where the stream was before
        self.orig_index = orig_index
        self.follows_default = follows_default  # ... and it was the default device
        self.applied = dict(gains)              # gains the live node has right now
        self.born = now
        self.node_id: int | None = None         # PipeWire id of the sink, once it exists
        self.attached = False                   # the stream is playing into the sink
        self.moved = False                      # ... or we asked for it (arrival not seen yet)
        self.flat_since: float | None = None

    def stop(self) -> None:
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait()
        try:
            os.unlink(self.conf)
        except OSError:
            pass


class EqManager:
    """Keeps the wanted EQ gains per application stream and makes the sound
    server match them. ``reconcile`` (called with every fresh snapshot, always
    from the same long-lived thread) is the only place that starts helpers.
    """

    def __init__(self, enabled: bool) -> None:
        self.enabled = enabled
        self._lock = threading.RLock()
        self._gains: dict[int, dict[str, float]] = {}  # sink-input index -> wanted gains
        self._chains: dict[int, EqChain] = {}
        self._streams: set[int] = set()                 # live application streams
        self._dir: str | None = None

    @property
    def range_db(self) -> int:
        return EQ_RANGE_DB if self.enabled else 0

    def gains(self) -> dict[str, dict[str, float]]:
        """Wanted gains by strip id (only streams that ever left 0 dB)."""
        with self._lock:
            return {f"playback:{i}": dict(g) for i, g in self._gains.items()}

    # -- control (any thread) -----------------------------------------------
    def set_gains(self, stream: int, changes: dict[str, float]) -> None:
        with self._lock:
            if stream not in self._streams:
                raise LookupError("no such stream")
            gains = self._gains.setdefault(stream, flat_eq())
            gains.update(changes)
            chain = self._chains.get(stream)
            if chain:
                self._push(chain)
            elif eq_is_flat(gains):
                del self._gains[stream]

    def stop(self) -> None:
        """Hand every stream back to its device and end all helpers."""
        with self._lock:
            for stream in list(self._chains):
                self._drop(stream, restore=True)
            if self._dir:
                shutil.rmtree(self._dir, ignore_errors=True)
                self._dir = None

    # -- reconciling ------------------------------------------------------
    def reconcile(self, raw: dict) -> None:
        if not self.enabled:
            return
        try:
            self._reconcile(raw)
        except Exception as exc:  # noqa: BLE001 - never take the refresh loop down
            log(f"equalizer: {exc!r}")

    def _reconcile(self, raw: dict) -> None:
        now = time.monotonic()
        sinks = {s["index"]: s for s in raw["sinks"]}
        by_name = {s["name"]: s for s in raw["sinks"]}
        default = raw["info"].get("default_sink_name")
        streams = {si["index"]: si for si in raw["sink_inputs"]
                   if not is_internal_stream(si.get("properties") or {}, set())}
        with self._lock:
            self._streams = set(streams)
            for stream in (self._gains.keys() | self._chains.keys()) - streams.keys():
                self._drop(stream, restore=False)  # the stream is gone
            for stream, si in streams.items():
                gains = self._gains.get(stream)
                chain = self._chains.get(stream)
                if chain:
                    self._service(chain, si, by_name, now)
                elif gains and not eq_is_flat(gains):
                    self._start(stream, si, sinks, default, gains, now)

    def _start(self, stream: int, si: dict, sinks: dict, default: str | None,
               gains: dict, now: float) -> None:
        orig = sinks.get(si.get("sink"))
        if orig is None or orig["name"].startswith(EQ_NODE_PREFIX):
            return
        name = EQ_NODE_PREFIX + str(stream)
        app = stream_labels(si.get("properties") or {})[0]
        description = "faderPage EQ - " + (re.sub(r"[^A-Za-z0-9 ._-]", "", app)[:40].strip() or "stream")
        follows_default = orig["name"] == default
        try:
            if self._dir is None:
                self._dir = tempfile.mkdtemp(prefix="faderpage-eq-")
            conf = os.path.join(self._dir, name + ".conf")
            with open(conf, "w", encoding="utf-8") as fh:
                # Following the default device (no explicit target) keeps the
                # stream with the default if the user switches output devices.
                fh.write(eq_chain_config(name, description, gains,
                                         None if follows_default else orig["name"]))
            proc = self._spawn([PIPEWIRE, "-c", conf])
        except OSError as exc:
            log(f"cannot start {PIPEWIRE}, equalizer disabled: {exc}")
            self.enabled = False
            self._gains.clear()
            return
        self._chains[stream] = EqChain(stream, name, proc, conf, orig["name"], orig["index"],
                                       follows_default, gains, now)

    def _service(self, chain: EqChain, si: dict, by_name: dict, now: float) -> None:
        stream = chain.stream
        gains = self._gains.setdefault(stream, flat_eq())
        if chain.proc.poll() is not None:
            log(f"equalizer helper for stream {stream} exited ({chain.proc.returncode}); EQ reset")
            return self._drop(stream, restore=False)
        if not chain.attached and now - chain.born > EQ_ATTACH_TIMEOUT_S:
            log(f"equalizer for stream {stream} did not come up in time; EQ reset")
            return self._drop(stream, restore=False)

        if eq_is_flat(gains):
            if chain.flat_since is None:
                chain.flat_since = now
            elif now - chain.flat_since >= EQ_FLAT_GRACE_S:
                return self._drop(stream, restore=True)
        else:
            chain.flat_since = None

        sink = by_name.get(chain.name)
        if sink is None:
            return  # helper is still starting up
        try:
            chain.node_id = int(sink["properties"]["object.id"])
        except (KeyError, ValueError, TypeError):
            return
        if si.get("sink") == sink["index"]:
            chain.attached = True
        elif chain.attached:
            return self._drop(stream, restore=False)  # moved away on purpose: let go
        else:
            self._move(stream, chain.name)  # retried on every pass until it sticks
            chain.moved = True
        self._push(chain)

    def _drop(self, stream: int, restore: bool) -> None:
        chain = self._chains.pop(stream, None)
        self._gains.pop(stream, None)
        if chain is None:
            return
        # A stream that followed the default device is simply left to the
        # session manager once its sink is gone; pinning it back to a device
        # by name would make it stop following the default later on.
        if restore and (chain.attached or chain.moved) and not chain.follows_default:
            if not self._restore(stream, chain.orig_sink, chain.orig_index):
                log(f"could not put stream {stream} back on {chain.orig_sink}")
        chain.stop()

    def _restore(self, stream: int, sink: str, sink_index: int) -> bool:
        """Put the stream back on its device and make sure it got there.

        Asked soon after the move onto the chain, the sound server sometimes
        drops the request without a word. Ending the helper then would make
        the session manager send the stream to the *default* device instead.
        """
        for _ in range(EQ_RESTORE_TRIES):
            self._move(stream, sink)
            for _ in range(EQ_RESTORE_POLLS):
                if self._sink_of(stream) == sink_index:
                    return True
                time.sleep(0.05)
        return False

    def _push(self, chain: EqChain) -> None:
        gains = self._gains.get(chain.stream)
        if gains is None or chain.node_id is None or chain.applied == gains:
            return
        if self._pw_set(chain.node_id, gains):
            chain.applied = dict(gains)

    # -- side effects (separate methods so tests can replace them) -----------
    @staticmethod
    def _spawn(cmd: list[str]):
        return subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, preexec_fn=_die_with_parent,
                                start_new_session=True)

    @staticmethod
    def _move(stream: int, sink: str) -> bool:
        try:
            return subprocess.run([PACTL, "move-sink-input", str(stream), sink],
                                  capture_output=True, timeout=3).returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            return False

    @staticmethod
    def _sink_of(stream: int) -> int | None:
        """Index of the sink a sink-input is playing into right now."""
        try:
            out = subprocess.run([PACTL, "list", "short", "sink-inputs"],
                                 capture_output=True, text=True, timeout=3).stdout
        except (OSError, subprocess.TimeoutExpired):
            return None
        for line in out.splitlines():
            cols = line.split("\t")
            if len(cols) > 1 and cols[0] == str(stream) and cols[1].isdigit():
                return int(cols[1])
        return None

    @staticmethod
    def _pw_set(node_id: int, gains: dict) -> bool:
        try:
            out = subprocess.run([PW_CLI, "set-param", str(node_id), "Props", eq_params(gains)],
                                 capture_output=True, text=True, timeout=3).stdout
        except (OSError, subprocess.TimeoutExpired):
            return False
        return "Error:" not in out  # pw-cli exits 0 even when it failed


# --------------------------------------------------------------------------
# Fan-out of events to connected browsers (latest value wins per event type,
# so a slow client can never make memory grow).
# --------------------------------------------------------------------------

class Client:
    def __init__(self) -> None:
        self._cv = threading.Condition()
        self._pending: dict[str, str] = {}

    def push(self, event: str, payload: str) -> None:
        with self._cv:
            self._pending[event] = payload
            self._cv.notify()

    def wait(self, timeout: float) -> dict[str, str]:
        with self._cv:
            if not self._pending:
                self._cv.wait(timeout)
            items, self._pending = self._pending, {}
            return items


class Hub:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._clients: set[Client] = set()
        self.on_change = lambda: None

    def add(self) -> Client:
        client = Client()
        with self._lock:
            self._clients.add(client)
        self.on_change()
        return client

    def remove(self, client: Client) -> None:
        with self._lock:
            self._clients.discard(client)
        self.on_change()

    def count(self) -> int:
        with self._lock:
            return len(self._clients)

    def publish(self, event: str, payload: str) -> None:
        with self._lock:
            clients = list(self._clients)
        for client in clients:
            client.push(event, payload)


# --------------------------------------------------------------------------
# Mixer: keeps the UI state in sync with the sound server
# --------------------------------------------------------------------------

class Mixer:
    def __init__(self, hub: Hub, meters: MeterManager, max_volume: int,
                 meters_enabled: bool, eq: EqManager | None = None) -> None:
        self.hub = hub
        self.meters = meters
        self.max_volume = max_volume
        self.meters_enabled = meters_enabled
        self.eq = eq or EqManager(False)
        self.state_json = json.dumps({"max_volume": max_volume, "eq_range_db": 0,
                                      "master": None, "mic": None,
                                      "playback": [], "recording": []})
        self._targets: dict[str, tuple] = {}
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._subscriber: subprocess.Popen | None = None
        hub.on_change = self.apply_meters

    # -- state ------------------------------------------------------------
    def refresh(self) -> None:
        raw = fetch_raw()
        # May move streams around; `raw` is then slightly stale, but those
        # moves raise new events, which trigger the next refresh.
        self.eq.reconcile(raw)
        # Taken after the fetch: any meter stream visible in `raw` was spawned
        # (and its pid registered) before the fetch started.
        state, targets = build_snapshot(raw, self.meters.pids(), self.max_volume,
                                        self.eq.gains(), self.eq.range_db)
        payload = json.dumps(state, separators=(",", ":"), sort_keys=True)
        with self._lock:
            changed = payload != self.state_json
            self.state_json = payload
            self._targets = targets
        self.apply_meters()
        if changed:
            self.hub.publish("state", payload)

    def apply_meters(self) -> None:
        with self._lock:
            targets = self._targets
        live = self.meters_enabled and self.hub.count() > 0
        self.meters.sync(targets if live else {})

    # -- control ----------------------------------------------------------
    def set_volume(self, target_id, volume) -> None:
        if isinstance(volume, bool) or not isinstance(volume, (int, float)):
            raise ValueError("volume must be a number")
        volume = round(volume)
        if not 0 <= volume <= self.max_volume:
            raise ValueError(f"volume must be 0..{self.max_volume}")
        self._run(volume_command(target_id, "volume", volume))

    def set_mute(self, target_id, mute) -> None:
        if not isinstance(mute, bool):
            raise ValueError("mute must be true or false")
        self._run(volume_command(target_id, "mute", mute))

    def set_eq(self, target_id, gains) -> None:
        """Set some of the three EQ gains (dB) of an application stream."""
        if not self.eq.enabled:
            raise ValueError("equalizer unavailable (needs pipewire and pw-cli; see --no-eq)")
        m = EQ_TARGET_RE.fullmatch(target_id) if isinstance(target_id, str) else None
        if not m:
            raise ValueError("invalid id (the equalizer is for application streams)")
        self.eq.set_gains(int(m.group(1)), parse_eq_changes(gains))
        self._wake.set()  # apply and publish right away

    @staticmethod
    def _run(cmd: list[str]) -> None:
        try:
            subprocess.run(cmd, capture_output=True, text=True, timeout=3, check=True)
        except subprocess.CalledProcessError as exc:
            raise LookupError(exc.stderr.strip() or "pactl failed") from exc
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise LookupError(str(exc)) from exc

    # -- background threads -------------------------------------------------
    def start(self) -> None:
        self.refresh()  # fail early (with a clear error) if the sound server is gone
        for target in (self._subscribe_loop, self._refresh_loop, self._meter_loop):
            threading.Thread(target=target, daemon=True).start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._subscriber and self._subscriber.poll() is None:
            self._subscriber.terminate()
        self.meters.stop_all()
        self.eq.stop()

    def _subscribe_loop(self) -> None:
        """Wake the refresh loop whenever the sound server reports a change."""
        while not self._stop.is_set():
            try:
                proc = subprocess.Popen([PACTL, "subscribe"], stdout=subprocess.PIPE,
                                        stderr=subprocess.DEVNULL, text=True, bufsize=1)
            except OSError:
                time.sleep(2)
                continue
            self._subscriber = proc
            for line in proc.stdout:
                if EVENT_RE.search(line):
                    self._wake.set()
            proc.wait()
            if not self._stop.is_set():
                time.sleep(1)  # sound server restarted? resync once it is back
                self._wake.set()

    def _refresh_loop(self) -> None:
        failing = False
        while not self._stop.is_set():
            self._wake.wait(timeout=5)  # the timeout doubles as a safety-net resync
            if self._stop.is_set():
                break
            time.sleep(0.03)  # coalesce bursts of events
            self._wake.clear()
            try:
                self.refresh()
                failing = False
            except Exception as exc:  # noqa: BLE001 - keep the daemon alive
                if not failing:
                    log(f"cannot talk to the sound server: {exc}")
                failing = True
                time.sleep(1)

    def _meter_loop(self) -> None:
        was_silent = False
        while not self._stop.is_set():
            time.sleep(METER_WINDOW_S)
            if self.hub.count() == 0:
                continue
            levels = self.meters.levels()
            silent = not any(levels.values())
            if silent and was_silent:
                continue  # nothing new to say; clients already show silence
            was_silent = silent
            self.hub.publish("meters", json.dumps(
                {key: round(level, 3) for key, level in levels.items()},
                separators=(",", ":")))


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------

class App:
    def __init__(self, hub: Hub, mixer: Mixer, verbose: bool) -> None:
        self.hub = hub
        self.mixer = mixer
        self.verbose = verbose


SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": ("default-src 'self'; img-src 'self' data:; "
                                "frame-ancestors 'none'; base-uri 'none'"),
}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "faderPage/" + __version__
    sys_version = ""
    disable_nagle_algorithm = True

    @property
    def app(self) -> App:
        return self.server.app  # type: ignore[attr-defined]

    def log_message(self, fmt, *args) -> None:
        if self.app.verbose:
            super().log_message(fmt, *args)

    # -- responses ----------------------------------------------------------
    def _send(self, status: int, body: bytes, ctype: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for name, value in SECURITY_HEADERS.items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: int, obj) -> None:
        self._send(status, json.dumps(obj).encode(), "application/json")

    # -- GET --------------------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802
        path = urlsplit(self.path).path
        if path == "/api/events":
            return self._events()
        if path == "/api/state":
            return self._send(200, self.app.mixer.state_json.encode(), "application/json")
        route = STATIC_ROUTES.get(path)
        if route:
            filename, ctype = route
            try:
                return self._send(200, (STATIC_DIR / filename).read_bytes(), ctype)
            except OSError:
                return self._json(500, {"error": "static file missing"})
        self._json(404, {"error": "not found"})

    def _events(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.send_header("X-Accel-Buffering", "no")
        for name, value in SECURITY_HEADERS.items():
            self.send_header(name, value)
        self.end_headers()
        self.close_connection = True
        client = self.app.hub.add()
        try:
            client.push("state", self.app.mixer.state_json)
            while True:
                items = client.wait(SSE_KEEPALIVE_S)
                if not items:
                    self.wfile.write(b": keepalive\n\n")
                for event, data in items.items():
                    self.wfile.write(f"event: {event}\ndata: {data}\n\n".encode())
        except OSError:
            pass  # browser went away
        finally:
            self.app.hub.remove(client)

    # -- POST -------------------------------------------------------------
    def do_POST(self) -> None:  # noqa: N802
        path = urlsplit(self.path).path
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = -1
        if not 0 <= length <= MAX_BODY_BYTES:
            self.close_connection = True
            return self._json(413, {"error": "bad body size"})
        body = self.rfile.read(length)

        # Cross-site pages must not be able to drive the faders: require a
        # JSON content type (forces a CORS preflight, which we never grant)
        # and refuse requests whose Origin is not this very host.
        origin = self.headers.get("Origin")
        if origin and urlsplit(origin).netloc != self.headers.get("Host"):
            return self._json(403, {"error": "cross-origin request refused"})
        if self.headers.get_content_type() != "application/json":
            return self._json(415, {"error": "content type must be application/json"})

        try:
            data = json.loads(body)
            if not isinstance(data, dict):
                raise ValueError("expected a JSON object")
            if path == "/api/volume":
                self.app.mixer.set_volume(data.get("id"), data.get("volume"))
            elif path == "/api/mute":
                self.app.mixer.set_mute(data.get("id"), data.get("mute"))
            elif path == "/api/eq":
                self.app.mixer.set_eq(data.get("id"), data.get("gains"))
            else:
                return self._json(404, {"error": "not found"})
        except ValueError as exc:  # includes JSON decode errors
            return self._json(400, {"error": str(exc)})
        except LookupError as exc:
            return self._json(404, {"error": str(exc)})
        self._json(200, {"ok": True})


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    app: App


class Server6(Server):
    address_family = socket.AF_INET6


# --------------------------------------------------------------------------
# entry point
# --------------------------------------------------------------------------

def lan_address() -> str | None:
    """Best guess at this machine's LAN address (no packet is actually sent)."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))
            return s.getsockname()[0]
    except OSError:
        return None


def parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Serve a web page with a volume fader per audio stream.")
    p.add_argument("--host", default="0.0.0.0",
                   help="address to listen on (default: 0.0.0.0 = whole LAN; "
                        "use 127.0.0.1 for this machine only)")
    p.add_argument("--port", type=int, default=8765, help="port (default: 8765)")
    p.add_argument("--max-volume", type=int, default=100, metavar="PERCENT",
                   help="top of every fader, 100-200 (default: 100)")
    p.add_argument("--no-meters", action="store_true",
                   help="disable the live level meters (no parec processes)")
    p.add_argument("--no-eq", action="store_true",
                   help="disable the per-application equalizer (never re-routes a stream)")
    p.add_argument("-v", "--verbose", action="store_true", help="log every HTTP request")
    args = p.parse_args(argv)
    if not 100 <= args.max_volume <= 200:
        p.error("--max-volume must be between 100 and 200")
    return args


def main(argv=None) -> int:
    args = parse_args(argv)
    if not shutil.which(PACTL):
        log(f"'{PACTL}' not found - install libpulse (pipewire-pulse / pulseaudio-utils)")
        return 1
    meters_enabled = not args.no_meters
    if meters_enabled and not shutil.which(PAREC):
        log(f"'{PAREC}' not found - level meters disabled")
        meters_enabled = False

    eq_enabled = not args.no_eq
    if eq_enabled and not (shutil.which(PIPEWIRE) and shutil.which(PW_CLI)):
        log(f"'{PIPEWIRE}' and '{PW_CLI}' not found - equalizer disabled")
        eq_enabled = False

    hub = Hub()
    meters = MeterManager()
    mixer = Mixer(hub, meters, args.max_volume, meters_enabled, EqManager(eq_enabled))
    try:
        mixer.start()
    except (subprocess.SubprocessError, OSError, ValueError) as exc:
        log(f"cannot talk to the sound server (is PipeWire/PulseAudio running?): {exc}")
        return 1

    server_cls = Server6 if ":" in args.host else Server
    try:
        server = server_cls((args.host, args.port), Handler)
    except OSError as exc:
        log(f"cannot listen on {args.host}:{args.port}: {exc}")
        mixer.stop()
        return 1
    server.app = App(hub, mixer, args.verbose)

    def on_sigterm(*_):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, on_sigterm)
    print(f"faderPage {__version__} listening on {args.host}:{args.port}", flush=True)
    if args.host in ("0.0.0.0", "::"):
        print("  anyone on your network can open this page and change the volume", flush=True)
        ip = lan_address()
        if ip:
            print(f"  open  http://{ip}:{args.port}/  from another device", flush=True)
    print(f"  local http://localhost:{args.port}/", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        mixer.stop()
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
