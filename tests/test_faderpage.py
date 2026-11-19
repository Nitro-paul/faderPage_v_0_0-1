import array
import os
import sys
import threading
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import faderpage as fp  # noqa: E402


def vol(*percents):
    return {f"ch{i}": {"value": round(p * 65536 / 100)} for i, p in enumerate(percents)}


def sink(index, name, volume=85, mute=False):
    return {"index": index, "name": name, "description": f"Desc {name}",
            "volume": vol(volume, volume), "mute": mute, "monitor_source": f"{name}.monitor"}


def source(index, name, volume=100):
    return {"index": index, "name": name, "description": f"Desc {name}",
            "volume": vol(volume), "mute": False}


def stream(index, target_key, target, app="App", media="", binary="", corked=False, volume=100, **props):
    p = {"application.name": app, "media.name": media, "application.process.binary": binary}
    p.update(props)
    return {"index": index, target_key: target, "corked": corked, "mute": False,
            "volume": vol(volume), "properties": p}


def raw(**overrides):
    base = {
        "sinks": [sink(10, "spk"), sink(11, "hdmi")],
        "sources": [source(20, "mic"), source(21, "spk.monitor"), source(22, "hdmi.monitor")],
        "sink_inputs": [], "source_outputs": [],
        "info": {"default_sink_name": "spk", "default_source_name": "mic"},
    }
    base.update(overrides)
    return base


class VolumeTests(unittest.TestCase):
    def test_percent_is_loudest_channel(self):
        self.assertEqual(fp.volume_percent(vol(30, 60)), 60)

    def test_percent_rounds_and_handles_boost(self):
        self.assertEqual(fp.volume_percent(vol(150)), 150)
        self.assertEqual(fp.volume_percent({"a": {"value": 32768}}), 50)

    def test_percent_empty(self):
        self.assertEqual(fp.volume_percent({}), 0)
        self.assertEqual(fp.volume_percent(None), 0)


class LabelTests(unittest.TestCase):
    def test_media_title_preferred(self):
        self.assertEqual(fp.stream_labels({"application.name": "Firefox",
                                           "application.process.binary": "firefox",
                                           "media.name": "Some Song"}), ("Firefox", "Some Song"))

    def test_generic_media_falls_back_to_binary(self):
        self.assertEqual(fp.stream_labels({"application.name": "WEBRTC VoiceEngine",
                                           "application.process.binary": "Discord",
                                           "media.name": "playStream"}),
                         ("WEBRTC VoiceEngine", "Discord"))

    def test_no_duplicate_subtitle(self):
        self.assertEqual(fp.stream_labels({"application.name": "mpv", "media.name": "mpv"}),
                         ("mpv", ""))

    def test_unknown(self):
        self.assertEqual(fp.stream_labels({}), ("Unknown", ""))


class SnapshotTests(unittest.TestCase):
    def test_master_and_streams(self):
        r = raw(sink_inputs=[stream(2, "sink", 10, app="B"), stream(1, "sink", 10, app="A", volume=40)])
        state, targets = fp.build_snapshot(r, set(), 100)
        self.assertEqual([s["id"] for s in state["playback"]], ["playback:1", "playback:2"])
        self.assertEqual(state["playback"][0]["volume"], 40)
        self.assertEqual(state["master"]["volume"], 85)
        self.assertEqual(state["master"]["sub"], "Desc spk")
        self.assertEqual(targets["playback:1"], ("spk", 1))
        self.assertEqual(targets["master"], ("spk.monitor", None))

    def test_corked_streams_are_listed_but_not_metered(self):
        r = raw(sink_inputs=[stream(1, "sink", 10, corked=True)])
        state, targets = fp.build_snapshot(r, set(), 100)
        self.assertEqual(len(state["playback"]), 1)
        self.assertEqual(targets, {})  # not even the master: nothing is running

    def test_master_not_metered_when_streams_go_elsewhere(self):
        r = raw(sink_inputs=[stream(1, "sink", 11)])
        _, targets = fp.build_snapshot(r, set(), 100)
        self.assertEqual(targets, {"playback:1": ("hdmi", 1)})

    def test_own_meter_streams_are_hidden(self):
        r = raw(source_outputs=[
            stream(5, "source", 20, app=fp.METER_CLIENT_NAME),
            stream(6, "source", 20, app="other", **{"application.process.id": "4242"}),
            stream(7, "source", 20, app="pavucontrol", media="Peak detect"),
            stream(8, "source", 20, app="Discord"),
        ])
        state, targets = fp.build_snapshot(r, {4242}, 100)
        self.assertEqual([s["id"] for s in state["recording"]], ["recording:8"])
        self.assertEqual(targets["mic"], ("mic", None))
        self.assertEqual(targets["recording:8"], ("mic", None))

    def test_mic_unmetered_without_recorders(self):
        state, targets = fp.build_snapshot(raw(), set(), 100)
        self.assertEqual(state["mic"]["name"], "Microphone")
        self.assertNotIn("mic", targets)

    def test_monitor_default_source_is_not_a_microphone(self):
        r = raw(info={"default_sink_name": "spk", "default_source_name": "spk.monitor"})
        state, _ = fp.build_snapshot(r, set(), 100)
        self.assertIsNone(state["mic"])

    def test_no_default_sink(self):
        r = raw(info={"default_sink_name": "gone", "default_source_name": "mic"})
        state, _ = fp.build_snapshot(r, set(), 100)
        self.assertIsNone(state["master"])


class CommandTests(unittest.TestCase):
    def test_commands(self):
        self.assertEqual(fp.volume_command("master", "volume", 40),
                         ["pactl", "set-sink-volume", "@DEFAULT_SINK@", "40%"])
        self.assertEqual(fp.volume_command("mic", "mute", True),
                         ["pactl", "set-source-mute", "@DEFAULT_SOURCE@", "1"])
        self.assertEqual(fp.volume_command("playback:12", "volume", 0),
                         ["pactl", "set-sink-input-volume", "12", "0%"])
        self.assertEqual(fp.volume_command("recording:3", "mute", False),
                         ["pactl", "set-source-output-mute", "3", "0"])

    def test_rejects_bad_ids(self):
        for bad in ("", "playback", "playback:", "playback:1;rm", "../x", "-1", None, 5, "playback:1\n"):
            with self.assertRaises(ValueError, msg=repr(bad)):
                fp.volume_command(bad, "volume", 10)


class MixerValidationTests(unittest.TestCase):
    def setUp(self):
        self.mixer = fp.Mixer(fp.Hub(), fp.MeterManager(), 100, False)
        self.calls = []
        original = fp.Mixer._run
        fp.Mixer._run = staticmethod(self.calls.append)
        self.addCleanup(setattr, fp.Mixer, "_run", original)

    def test_volume_bounds_and_types(self):
        self.mixer.set_volume("master", 100)
        self.mixer.set_volume("master", 33.6)
        self.assertEqual(self.calls[-1][-1], "34%")
        for bad in (-1, 101, True, "50", None):
            with self.assertRaises(ValueError, msg=repr(bad)):
                self.mixer.set_volume("master", bad)

    def test_mute_must_be_bool(self):
        self.mixer.set_mute("master", True)
        for bad in (1, "true", None):
            with self.assertRaises(ValueError, msg=repr(bad)):
                self.mixer.set_mute("master", bad)


class PeakTests(unittest.TestCase):
    def test_peak(self):
        buf = array.array("h", [0, 100, -16384, 8000]).tobytes()
        self.assertAlmostEqual(fp.peak_of(buf), 0.5)

    def test_full_scale_negative(self):
        self.assertEqual(fp.peak_of(array.array("h", [-32768]).tobytes()), 1.0)

    def test_empty_and_odd_length(self):
        self.assertEqual(fp.peak_of(b""), 0.0)
        self.assertEqual(fp.peak_of(b"\x00"), 0.0)


class ClientTests(unittest.TestCase):
    def test_latest_value_wins(self):
        c = fp.Client()
        c.push("meters", "1")
        c.push("meters", "2")
        c.push("state", "s")
        self.assertEqual(c.wait(0), {"meters": "2", "state": "s"})
        self.assertEqual(c.wait(0), {})

    def test_wait_wakes_on_push(self):
        c = fp.Client()
        threading.Timer(0.05, c.push, ("state", "x")).start()
        self.assertEqual(c.wait(2), {"state": "x"})


# --------------------------------------------------------------------------
# equalizer
# --------------------------------------------------------------------------

def eq_sink(stream, node_id=141):
    return {**sink(30 + stream, f"faderpage_eq_{stream}"), "properties": {"object.id": str(node_id)}}


def eq_output(index, stream, on_sink):
    """The sink-input an EQ chain uses to play into the real device."""
    return stream_(index, on_sink, **{"node.name": f"output.faderpage_eq_{stream}"})


def stream_(index, target, **props):
    return {"index": index, "sink": target, "corked": False, "mute": False,
            "volume": vol(100), "properties": props}


class EqParseTests(unittest.TestCase):
    def test_snaps_to_half_db_and_accepts_subsets(self):
        self.assertEqual(fp.parse_eq_changes({"low": 3.2, "high": -12, "mid": 0}),
                         {"low": 3.0, "high": -12.0, "mid": 0.0})
        self.assertEqual(fp.parse_eq_changes({"mid": 0.3}), {"mid": 0.5})

    def test_no_negative_zero(self):
        out = fp.parse_eq_changes({"low": -0.1})
        self.assertEqual(repr(out["low"]), "0.0")

    def test_rejects_bad_input(self):
        bad = [None, [], {}, "low", {"bass": 1}, {"low": "3"}, {"low": None}, {"low": True},
               {"low": 12.5}, {"low": -13}, {"low": float("nan")}, {"low": float("inf")}]
        for obj in bad:
            with self.assertRaises(ValueError, msg=repr(obj)):
                fp.parse_eq_changes(obj)

    def test_limits_are_inclusive(self):
        self.assertEqual(fp.parse_eq_changes({"low": 12, "high": -12}), {"low": 12.0, "high": -12.0})


class EqConfigTests(unittest.TestCase):
    GAINS = {"low": 3.0, "mid": 0.0, "high": -4.5}

    def test_chain_config(self):
        conf = fp.eq_chain_config("faderpage_eq_7", "faderPage EQ - Firefox", self.GAINS, "alsa_output.x")
        self.assertIn('node.name = "faderpage_eq_7"', conf)
        self.assertIn("media.class = Audio/Sink", conf)
        self.assertIn('node.name = "output.faderpage_eq_7"', conf)
        self.assertIn('target.object = "alsa_output.x"', conf)
        self.assertIn('label = bq_lowshelf control = { "Freq" = 200.0 "Q" = 0.7 "Gain" = 3.0 }', conf)
        self.assertIn('label = bq_peaking control = { "Freq" = 1000.0 "Q" = 0.5 "Gain" = 0.0 }', conf)
        self.assertIn('label = bq_highshelf control = { "Freq" = 3000.0 "Q" = 0.7 "Gain" = -4.5 }', conf)
        self.assertIn("libpipewire-module-protocol-native", conf)  # runs as its own client

    def test_following_the_default_device_has_no_target(self):
        conf = fp.eq_chain_config("faderpage_eq_7", "d", self.GAINS, None)
        self.assertNotIn("target.object", conf)

    def test_odd_device_names_stay_quoted(self):
        conf = fp.eq_chain_config("faderpage_eq_7", "d", self.GAINS, 'we"ird\\name')
        self.assertIn('target.object = "we\\"ird\\\\name"', conf)

    def test_props_param(self):
        self.assertEqual(fp.eq_params(self.GAINS),
                         '{ params = [ "low:Gain" 3.0 "mid:Gain" 0.0 "high:Gain" -4.5 ] }')


class EqSnapshotTests(unittest.TestCase):
    def test_strips_carry_eq_and_state_carries_range(self):
        r = raw(sink_inputs=[stream(1, "sink", 10), stream(2, "sink", 10)])
        state, _ = fp.build_snapshot(r, set(), 100, {"playback:2": {"low": 6.0}}, 12)
        self.assertEqual(state["eq_range_db"], 12)
        self.assertEqual(state["playback"][0]["eq"], {"low": 0.0, "mid": 0.0, "high": 0.0})
        self.assertEqual(state["playback"][1]["eq"], {"low": 6.0, "mid": 0.0, "high": 0.0})
        self.assertEqual(fp.build_snapshot(r, set(), 100)[0]["eq_range_db"], 0)

    def test_chain_output_stream_is_not_a_strip(self):
        r = raw(sinks=[sink(10, "spk"), sink(11, "hdmi"), eq_sink(1)],
                sink_inputs=[stream(1, "sink", 31), eq_output(2, 1, 10)])
        state, _ = fp.build_snapshot(r, set(), 100)
        self.assertEqual([s["id"] for s in state["playback"]], ["playback:1"])

    def test_master_still_metered_through_a_chain(self):
        # the only playing stream goes through its EQ sink (31) into the default device (10)
        r = raw(sinks=[sink(10, "spk"), sink(11, "hdmi"), eq_sink(1)],
                sink_inputs=[stream(1, "sink", 31), eq_output(2, 1, 10)])
        _, targets = fp.build_snapshot(r, set(), 100)
        self.assertEqual(targets["playback:1"], ("faderpage_eq_1", 1))  # metered after the EQ
        self.assertEqual(targets["master"], ("spk.monitor", None))

    def test_other_device_is_not_master_busy_through_a_chain(self):
        r = raw(sinks=[sink(10, "spk"), sink(11, "hdmi"), eq_sink(1)],
                sink_inputs=[stream(1, "sink", 31), eq_output(2, 1, 11)])
        _, targets = fp.build_snapshot(r, set(), 100)
        self.assertNotIn("master", targets)


class FakeProc:
    def __init__(self):
        self.returncode = None

    def poll(self):
        return self.returncode

    def terminate(self):
        self.returncode = -15

    def kill(self):
        self.returncode = -9

    def wait(self, timeout=None):
        return self.returncode


class FakeEq(fp.EqManager):
    """EqManager whose side effects are recorded instead of performed."""

    def __init__(self, enabled=True):
        super().__init__(enabled)
        self.spawned, self.moves, self.sets = [], [], []
        self.spawn_error = None
        self.set_ok = True
        self.lost = 0    # the next N move requests are silently dropped, like the real thing sometimes does
        self.where = {}  # stream -> sink name it was last successfully moved to

    def _spawn(self, cmd):
        if self.spawn_error:
            raise self.spawn_error
        with open(cmd[-1], encoding="utf-8") as fh:
            self.spawned.append((cmd, fh.read(), FakeProc()))
        return self.spawned[-1][2]

    SINKS = {"spk": 10, "hdmi": 11}  # sink name -> index, for the fake _sink_of

    def _move(self, stream, sink):
        self.moves.append((stream, sink))
        if self.lost:
            self.lost -= 1
        else:
            self.where[stream] = sink
        return True

    def _sink_of(self, stream):
        return self.SINKS.get(self.where.get(stream))

    def _pw_set(self, node_id, gains):
        self.sets.append((node_id, dict(gains)))
        return self.set_ok

    def chain_proc(self, i=-1):
        return self.spawned[i][2]


class EqManagerTests(unittest.TestCase):
    def setUp(self):
        self.now = 1000.0
        patcher = mock.patch.object(fp.time, "monotonic", lambda: self.now)
        patcher.start()
        self.addCleanup(patcher.stop)
        for patcher in (mock.patch.object(fp, "log"), mock.patch.object(fp.time, "sleep")):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.eq = FakeEq()
        self.addCleanup(self.eq.stop)

    # snapshots: stream 7 plays on sink 10 ("spk", the default) unless told otherwise
    def plain(self, sink_index=10):
        return raw(sink_inputs=[stream(7, "sink", sink_index, app="Firefox")])

    def chained(self, attached):
        return raw(sinks=[sink(10, "spk"), sink(11, "hdmi"), eq_sink(7)],
                   sink_inputs=[stream(7, "sink", 37 if attached else 10, app="Firefox"),
                                eq_output(8, 7, 10)])

    def engage(self):
        """Walk a stream on the default device up to 'attached to a live chain'."""
        self.eq.reconcile(self.plain())
        self.eq.set_gains(7, {"low": 3.0})
        self.eq.reconcile(self.plain())          # starts the helper
        self.eq.reconcile(self.chained(False))   # sink is up: moves the stream
        self.eq.reconcile(self.chained(True))    # stream arrived

    def test_unknown_stream_is_refused(self):
        with self.assertRaises(LookupError):
            self.eq.set_gains(7, {"low": 3.0})
        self.eq.reconcile(self.plain())
        self.eq.set_gains(7, {"low": 3.0})  # known now

    def test_flat_gains_never_start_a_helper(self):
        self.eq.reconcile(self.plain())
        self.eq.set_gains(7, {"low": 0.0, "mid": 0.0})
        self.eq.reconcile(self.plain())
        self.assertEqual(self.eq.spawned, [])
        self.assertEqual(self.eq.gains(), {})

    def test_disabled_manager_does_nothing(self):
        eq = FakeEq(enabled=False)
        eq.reconcile(self.plain())
        self.assertEqual(eq.range_db, 0)
        self.assertEqual(eq.spawned, [])

    def test_engaging_starts_one_helper_then_moves_the_stream(self):
        self.eq.reconcile(self.plain())
        self.eq.set_gains(7, {"low": 3.0})
        self.assertEqual(self.eq.spawned, [])  # spawning is left to reconcile
        self.eq.reconcile(self.plain())
        self.assertEqual(len(self.eq.spawned), 1)
        cmd, conf, _ = self.eq.spawned[0]
        self.assertEqual(cmd[:2], ["pipewire", "-c"])
        self.assertNotIn("target.object", conf)      # stream followed the default device
        self.assertIn('"Gain" = 3.0', conf)
        self.assertEqual(self.eq.moves, [])          # its sink does not exist yet
        self.eq.reconcile(self.plain())
        self.assertEqual(len(self.eq.spawned), 1)    # and no second helper
        self.eq.reconcile(self.chained(False))
        self.assertEqual(self.eq.moves, [(7, "faderpage_eq_7")])

    def test_stream_on_another_device_keeps_that_device_as_target(self):
        self.eq.reconcile(self.plain(sink_index=11))  # hdmi, not the default
        self.eq.set_gains(7, {"mid": -3.0})
        self.eq.reconcile(self.plain(sink_index=11))
        self.assertIn('target.object = "hdmi"', self.eq.spawned[0][1])

    def test_live_changes_use_the_running_node(self):
        self.engage()
        self.eq.sets.clear()
        self.eq.set_gains(7, {"mid": -6.0})
        self.assertEqual(self.eq.sets, [(141, {"low": 3.0, "mid": -6.0, "high": 0.0})])
        self.eq.set_gains(7, {"mid": -6.0})  # unchanged: nothing to send
        self.assertEqual(len(self.eq.sets), 1)
        self.assertEqual(len(self.eq.spawned), 1)

    def test_failed_push_is_retried_on_the_next_pass(self):
        self.engage()
        self.eq.set_ok = False
        self.eq.set_gains(7, {"high": 2.0})
        self.eq.set_ok = True
        self.eq.sets.clear()
        self.eq.reconcile(self.chained(True))
        self.assertEqual(self.eq.sets[-1][1]["high"], 2.0)

    def test_going_flat_removes_the_chain_after_the_grace_period(self):
        self.engage()
        self.eq.set_gains(7, {"low": 0.0})
        self.eq.reconcile(self.chained(True))           # flat noticed
        self.now += fp.EQ_FLAT_GRACE_S - 1
        self.eq.reconcile(self.chained(True))
        self.assertIsNone(self.eq.chain_proc().returncode)   # still there
        self.now += 2
        self.eq.reconcile(self.chained(True))
        self.assertEqual(self.eq.chain_proc().returncode, -15)
        self.assertEqual(self.eq.moves, [(7, "faderpage_eq_7")])  # no move back: it followed the default
        self.assertEqual(self.eq.gains(), {})

    def test_a_stream_pinned_to_a_device_is_moved_back_there(self):
        self.eq.reconcile(self.plain(sink_index=11))
        self.eq.set_gains(7, {"low": 3.0})
        self.eq.reconcile(self.plain(sink_index=11))
        chained = raw(sinks=[sink(10, "spk"), sink(11, "hdmi"), eq_sink(7)],
                      sink_inputs=[stream(7, "sink", 37, app="Firefox"), eq_output(8, 7, 11)])
        self.eq.reconcile(chained)
        self.eq.set_gains(7, {"low": 0.0})
        self.eq.reconcile(chained)
        self.now += fp.EQ_FLAT_GRACE_S + 1
        self.eq.reconcile(chained)
        self.assertEqual(self.eq.moves[-1], (7, "hdmi"))

    def test_stopping_right_after_the_move_still_puts_the_stream_back(self):
        # the stream was asked to move but its arrival has not been observed yet
        self.eq.reconcile(self.plain(sink_index=11))
        self.eq.set_gains(7, {"low": 3.0})
        self.eq.reconcile(self.plain(sink_index=11))
        pending = raw(sinks=[sink(10, "spk"), sink(11, "hdmi"), eq_sink(7)],
                      sink_inputs=[stream(7, "sink", 11, app="Firefox"), eq_output(8, 7, 11)])
        self.eq.reconcile(pending)
        self.assertEqual(self.eq.moves, [(7, "faderpage_eq_7")])
        self.eq.stop()
        self.assertEqual(self.eq.moves[-1], (7, "hdmi"))
        self.assertEqual(self.eq.chain_proc().returncode, -15)

    def pinned_and_attached(self):
        self.eq.reconcile(self.plain(sink_index=11))
        self.eq.set_gains(7, {"low": 3.0})
        self.eq.reconcile(self.plain(sink_index=11))
        chained = raw(sinks=[sink(10, "spk"), sink(11, "hdmi"), eq_sink(7)],
                      sink_inputs=[stream(7, "sink", 37, app="Firefox"), eq_output(8, 7, 11)])
        self.eq.reconcile(chained)
        self.eq.reconcile(chained)

    def test_a_dropped_move_back_is_retried_before_the_helper_goes(self):
        self.pinned_and_attached()
        self.eq.lost = 2  # the sound server swallows the first two requests
        self.eq.stop()
        self.assertEqual(self.eq.moves, [(7, "hdmi")] * 3)  # two lost, the third one lands
        self.assertEqual(self.eq.where[7], "hdmi")
        self.assertEqual(self.eq.chain_proc().returncode, -15)

    def test_restoring_gives_up_eventually_but_still_ends_the_helper(self):
        self.pinned_and_attached()
        self.eq.lost = 99
        self.eq.stop()
        self.assertEqual(len(self.eq.moves), fp.EQ_RESTORE_TRIES)
        self.assertEqual(self.eq.chain_proc().returncode, -15)

    def test_dragging_through_zero_does_not_tear_the_chain_down(self):
        self.engage()
        self.eq.set_gains(7, {"low": 0.0})
        self.eq.reconcile(self.chained(True))
        self.now += 2
        self.eq.set_gains(7, {"low": -3.0})
        self.eq.reconcile(self.chained(True))
        self.now += fp.EQ_FLAT_GRACE_S * 2
        self.eq.reconcile(self.chained(True))
        self.assertIsNone(self.eq.chain_proc().returncode)
        self.assertEqual(len(self.eq.spawned), 1)

    def test_finished_stream_takes_its_chain_with_it(self):
        self.engage()
        self.eq.reconcile(raw())  # the stream (and the chain's output) are gone
        self.assertEqual(self.eq.chain_proc().returncode, -15)
        self.assertEqual(self.eq.gains(), {})
        self.assertEqual(self.eq.moves, [(7, "faderpage_eq_7")])  # nothing to restore

    def test_user_moving_the_stream_away_ends_the_eq(self):
        self.engage()
        self.eq.reconcile(self.chained(False))  # the stream is back on "spk" after having been attached
        self.assertEqual(self.eq.chain_proc().returncode, -15)
        self.assertEqual(self.eq.moves, [(7, "faderpage_eq_7")])  # we do not fight over it

    def test_helper_dying_resets_the_eq(self):
        self.engage()
        self.eq.chain_proc().returncode = 1
        self.eq.reconcile(self.chained(True))
        self.assertEqual(self.eq.gains(), {})
        self.assertEqual(len(self.eq.spawned), 1)  # and it is not respawned in a loop

    def test_chain_that_never_comes_up_is_abandoned(self):
        self.eq.reconcile(self.plain())
        self.eq.set_gains(7, {"low": 3.0})
        self.eq.reconcile(self.plain())
        self.now += fp.EQ_ATTACH_TIMEOUT_S + 1
        self.eq.reconcile(self.plain())
        self.assertEqual(self.eq.chain_proc().returncode, -15)
        self.assertEqual(self.eq.gains(), {})

    def test_missing_pipewire_disables_the_equalizer(self):
        self.eq.spawn_error = FileNotFoundError("pipewire")
        self.eq.reconcile(self.plain())
        self.eq.set_gains(7, {"low": 3.0})
        self.eq.reconcile(self.plain())
        self.assertFalse(self.eq.enabled)
        self.assertEqual(self.eq.range_db, 0)
        self.assertEqual(self.eq.gains(), {})

    def test_stop_hands_streams_back_and_removes_the_config(self):
        self.engage()
        conf = self.eq.spawned[0][0][-1]
        self.assertTrue(os.path.exists(conf))
        self.eq.stop()
        self.assertEqual(self.eq.chain_proc().returncode, -15)
        self.assertFalse(os.path.exists(conf))

    def test_chains_own_stream_is_never_treated_as_an_app(self):
        self.engage()
        self.eq.reconcile(self.chained(True))
        self.assertEqual(len(self.eq.spawned), 1)
        self.assertEqual(list(self.eq.gains()), ["playback:7"])


class MixerEqTests(unittest.TestCase):
    def setUp(self):
        self.eq = FakeEq()
        self.addCleanup(self.eq.stop)
        self.mixer = fp.Mixer(fp.Hub(), fp.MeterManager(), 100, False, self.eq)
        self.eq.reconcile(raw(sink_inputs=[stream(7, "sink", 10)]))

    def test_sets_gains_of_an_application_stream(self):
        self.mixer.set_eq("playback:7", {"low": 3})
        self.assertEqual(self.eq.gains(), {"playback:7": {"low": 3.0, "mid": 0.0, "high": 0.0}})
        self.assertTrue(self.mixer._wake.is_set())

    def test_only_application_streams_have_an_eq(self):
        for bad in ("master", "mic", "recording:7", "playback:", "playback:7;x", None, 7):
            with self.assertRaises(ValueError, msg=repr(bad)):
                self.mixer.set_eq(bad, {"low": 3})

    def test_bad_gains_and_unknown_streams(self):
        with self.assertRaises(ValueError):
            self.mixer.set_eq("playback:7", {"low": 99})
        with self.assertRaises(LookupError):
            self.mixer.set_eq("playback:8", {"low": 3})

    def test_refused_when_the_equalizer_is_unavailable(self):
        mixer = fp.Mixer(fp.Hub(), fp.MeterManager(), 100, False)
        with self.assertRaises(ValueError):
            mixer.set_eq("playback:7", {"low": 3})


if __name__ == "__main__":
    unittest.main()
