"""Speed changes: blocked services, crash recovery and real timestamp boundaries."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
import unittest
from unittest.mock import patch, Mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
data = tempfile.TemporaryDirectory(prefix="pc-speed-")
os.environ.update(PEAKCUT_DATA=data.name, DATABASE_URL="")
from studio import ctx, jobs, live, load, media, render


class SpeedTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory(dir=data.name)
        self.addCleanup(self.folder.cleanup)
        self.d = Path(self.folder.name)
        self.s = live.Session({"id": "speed", "dir": str(self.d), "clips": [],
                               "creator": None, "title": "Test", "ai": {},
                               "source": {"name": "Test"}}, {})

    def test_slow_judge_does_not_stop_transcription_or_queue_more_checks(self):
        entered, release = threading.Event(), threading.Event()
        users = []

        def judge(*args):
            users.append(ctx.uid())
            entered.set()
            release.wait(3)

        with patch.object(self.s, "_ai_check", side_effect=judge), \
                patch.object(self.s, "_transcribe_step", return_value=True) as transcribe:
            try:
                with ctx.as_user("owner"):
                    self.assertTrue(self.s._schedule_check((0, 60), "reaction"))
                self.assertTrue(entered.wait(1))
                self.assertFalse(self.s._schedule_check((60, 120), "reaction"))
                self.assertTrue(self.s._transcribe_step())
                transcribe.assert_called_once()
            finally:
                release.set()
                self.s._finish_checks()
        self.assertEqual(users, ["owner"])

    def test_stop_prevents_late_ai_clip(self):
        self.s.user_stopped = True
        with patch.object(jobs, "save"), patch.object(jobs, "submit_live") as submit:
            self.s._add_clip({}, automatic=True)
        self.assertEqual(self.s.job["clips"], [])
        submit.assert_not_called()

    def test_analysis_loop_keeps_hearing_and_finishes_before_cleanup(self):
        entered, release, progressed = (threading.Event() for _ in range(3))
        calls = 0

        def hear():
            nonlocal calls
            calls += 1
            if calls == 1:
                self.s.t_done = 180
                return True
            if entered.wait(1):
                progressed.set()
            self.s.t_done = 186
            self.s.ended = True
            return False

        def judge(*args):
            entered.set()
            release.wait(3)

        with patch.object(self.s, "_transcribe_step", side_effect=hear), \
                patch.object(self.s, "_ai_check", side_effect=judge) as check, \
                patch.object(self.s, "_scan_segments"), patch.object(self.s, "_watch_spikes"), \
                patch.object(self.s, "_publish"), patch.object(jobs, "save"), \
                patch.object(live.creators, "log"), patch.object(live.alerts, "notify"), \
                patch.object(jobs, "submit_live") as submit:
            worker = threading.Thread(target=self.s._analyze)
            worker.start()
            try:
                self.assertTrue(progressed.wait(1.5), "a waiting judge must not pause the analysis loop")
                submit.assert_not_called()
            finally:
                release.set()
                worker.join(3)
            self.assertFalse(worker.is_alive())
            self.assertEqual(check.call_count, 2, "the final new words should also be checked")
            submit.assert_called_once()

    def test_transcript_journal_recovers_without_rewriting_history(self):
        from studio import transcript
        words = [{"w": "one", "s": 0, "e": 1}]
        transcript.append(self.d, 0, words)
        self.assertFalse((self.d / "words.json").exists())
        self.assertEqual(transcript.read(self.d), words)
        transcript.checkpoint(self.d, words)
        newer = [{"w": "two", "s": 1, "e": 2}]
        # A crash after replacing the checkpoint but before clearing the journal.
        transcript.append(self.d, 0, words)
        transcript.append(self.d, 1, newer)
        with (self.d / "words.jsonl").open("a") as f:
            f.write('{"from":2,"words":')
        self.assertEqual(transcript.read(self.d), words + newer)

    def test_read_during_checkpoint_cannot_miss_journal_words(self):
        from studio import transcript
        old = [{"w": "old", "s": 0, "e": 1}]
        newer = old + [{"w": "new", "s": 1, "e": 2}]
        transcript.checkpoint(self.d, old)
        transcript.append(self.d, 1, newer[1:])
        reading, release, writing, done = (threading.Event() for _ in range(4))
        results = []
        original = Path.read_text

        def paused_read(path, *args, **kwargs):
            text = original(path, *args, **kwargs)
            if path.name == "words.json" and threading.current_thread().name == "reader":
                reading.set()
                release.wait(3)
            return text

        def write():
            writing.set()
            transcript.checkpoint(self.d, newer)
            done.set()

        with patch.object(Path, "read_text", paused_read):
            reader = threading.Thread(name="reader", target=lambda: results.append(transcript.read(self.d)))
            writer = threading.Thread(target=write)
            reader.start()
            try:
                self.assertTrue(reading.wait(1))
                writer.start()
                self.assertTrue(writing.wait(1))
                self.assertFalse(done.wait(.1), "checkpoint must wait for an in-flight transcript reader")
            finally:
                release.set()
                reader.join(3)
                if writer.ident:
                    writer.join(3)
        self.assertEqual(results, [newer])

    def test_live_lanes_overlap_but_cleanup_waits_for_own_renders(self):
        entered, other, release, cleaned = (threading.Event() for _ in range(4))
        users = []

        def slow():
            users.append(ctx.uid())
            entered.set()
            release.wait(3)

        try:
            with ctx.as_user("owner"):
                jobs.submit_live(slow, lane="a")
                jobs.submit_live(cleaned.set, lane="a")
                jobs.submit_live(other.set, lane="b")
            self.assertTrue(entered.wait(1))
            self.assertTrue(other.wait(1), "another recording should not wait behind this render")
            self.assertFalse(cleaned.is_set())
        finally:
            release.set()
        self.assertTrue(cleaned.wait(2))
        self.assertEqual(users, ["owner"])

    def test_hardware_failure_falls_back_and_is_not_retried(self):
        def encode(cmd, **kw):
            codec = cmd[cmd.index("-c:v") + 1]
            calls.append(codec)
            ok = codec == "libx264"
            if ok:
                (Path(kw["cwd"]) / "clip.mp4").touch()
            return Mock(returncode=0 if ok else 1, stderr="no device")

        def cover(cmd, cwd):
            (Path(cwd) / "clip.jpg").touch()

        calls = []
        with patch.object(sys, "platform", "linux"), \
                patch.object(render, "_available_encoders", return_value={"h264_nvenc", "libx264"}), \
                patch.object(render, "_failed_encoders", set()), \
                patch.object(render.subprocess, "run", side_effect=encode), patch.object(render, "run", side_effect=cover):
            for i in range(2):
                work = self.d / str(i)
                work.mkdir()
                render._render_in(work, self.d / "src.mp4", self.d, "clip", 0, 1, "fit", 1080, "")
        self.assertEqual(calls, ["h264_nvenc", "libx264", "libx264"])

    def test_apple_encoder_is_preferred_when_available(self):
        with patch.object(sys, "platform", "darwin"), \
                patch.object(render, "_available_encoders", return_value={"h264_videotoolbox", "libx264"}), \
                patch.object(render, "_failed_encoders", set()):
            encoders = render._encoders()
        self.assertEqual(encoders[0][0], "h264_videotoolbox")
        self.assertEqual(encoders[-1][0], "libx264")

    def test_encoders_match_operating_system_even_with_all_encoders_installed(self):
        available = {"h264_videotoolbox", "h264_nvenc", "h264_qsv", "libx264"}
        expected = {"darwin": ["h264_videotoolbox", "libx264"],
                    "linux": ["h264_nvenc", "h264_qsv", "libx264"],
                    "win32": ["h264_nvenc", "h264_qsv", "libx264"],
                    "freebsd": ["libx264"]}
        for platform, names in expected.items():
            with self.subTest(platform=platform), patch.object(sys, "platform", platform), \
                    patch.object(render, "_available_encoders", return_value=available), \
                    patch.object(render, "_failed_encoders", set()):
                self.assertEqual([name for name, _ in render._encoders()], names)

    def test_mac_without_working_videotoolbox_uses_software(self):
        for available, failed in [({"h264_nvenc", "h264_qsv"}, set()),
                                  ({"h264_videotoolbox", "h264_nvenc"}, {"h264_videotoolbox"})]:
            with self.subTest(available=available), patch.object(sys, "platform", "darwin"), \
                    patch.object(render, "_available_encoders", return_value=available), \
                    patch.object(render, "_failed_encoders", failed):
                self.assertEqual([name for name, _ in render._encoders()], ["libx264"])

    def test_audio_download_requests_only_selected_range(self):
        (self.d / "audio_src.m4a").touch()
        with patch.object(media, "_ydl") as ydl:
            media.download_audio("https://example.com/video", self.d, start=120, end=150)
        opts = ydl.call_args.args[0]
        ranges = list(opts["download_ranges"]({}, None))
        self.assertEqual(ranges, [{"start_time": 120, "end_time": 150}])
        self.assertTrue(opts["force_keyframes_at_cuts"])

    def test_local_audio_seek_keeps_only_requested_seconds(self):
        src, wav = self.d / "source.wav", self.d / "part.wav"
        subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i",
                        "sine=frequency=440:duration=4", "-y", str(src)], check=True)
        media.extract_audio(src, wav, start=1, end=2.5)
        self.assertEqual(len(media.load_audio(wav)), 24000)

    def test_partial_audio_alignment_preserves_original_video_clock(self):
        import numpy as np
        clip = {"id": "c", "start": 125, "end": 130}
        job = {"dir": str(self.d), "options": {"quality": "720"}, "source": {"url": "test"}, "duration": 300}
        part = self.d / "part.mp4"
        with patch.object(media, "download_section", return_value=part), \
                patch.object(media, "extract_audio"), patch.object(media, "load_audio", return_value=np.zeros(10)), \
                patch.object(media, "probe", return_value={"duration": 20, "width": 1280, "height": 720}), \
                patch.object(media, "find_offset", return_value=7) as align:
            jobs._fetch_piece(job, clip, np.zeros(20), audio_start=110)
        self.assertEqual(align.call_args.args[2], 7)
        self.assertEqual(clip["src_offset"], 117)

    def test_mac_cpu_uses_host_counters(self):
        import ctypes
        lib = Mock()
        lib.mach_host_self.return_value = 1

        def stats(host, kind, counts, count):
            values = ctypes.cast(counts, ctypes.POINTER(ctypes.c_uint))
            for i, value in enumerate([100, 50, 800, 50]):
                values[i] = value
            return 0

        lib.host_statistics.side_effect = stats
        with patch.object(load.sys, "platform", "darwin"), patch("ctypes.CDLL", return_value=lib):
            self.assertEqual(load._cpu_times(), (200, 1000))

    def test_mac_memory_reads_page_size_and_available_pages(self):
        reply = 'Mach Virtual Memory Statistics: (page size of 16384 bytes)\nPages free: 10000.\nPages inactive: 20000.\nPages speculative: 5000.\n'
        with patch.object(load.sys, "platform", "darwin"), \
                patch.object(load.subprocess, "check_output", return_value=reply):
            self.assertAlmostEqual(load._ram_free_gb(), 35000 * 16384 / 1e9)

    def test_all_jobs_share_render_limit(self):
        entered, release = threading.Event(), threading.Event()
        running, peak = 0, 0
        lock = threading.Lock()

        def encode(*args):
            nonlocal running, peak
            with lock:
                running += 1
                peak = max(peak, running)
                if running == 2:
                    entered.set()
            release.wait(3)
            with lock:
                running -= 1
            return {}

        with patch.object(render, "_render_slots", threading.BoundedSemaphore(2)), \
                patch.object(render.tools, "require_captions"), patch.object(render, "build_ass", return_value=""), \
                patch.object(render, "_render_in", side_effect=encode), ThreadPoolExecutor(4) as pool:
            pending = [pool.submit(render.render_clip, self.d / "in.mp4", self.d, str(i),
                                   {"start": 0, "end": 1}, [], 1280, 720) for i in range(4)]
            try:
                self.assertTrue(entered.wait(1))
            finally:
                release.set()
            for future in pending:
                future.result()
        self.assertEqual(peak, 2)

    def test_stage_timings_include_failed_calls_and_accumulate(self):
        from studio import timing
        job = {}
        with patch.object(timing.time, "monotonic", side_effect=[10, 12, 20, 23]):
            self.assertEqual(timing.call(job, "ai", lambda: 42), 42)
            with self.assertRaises(ValueError):
                timing.call(job, "ai", lambda: (_ for _ in ()).throw(ValueError("offline")))
        self.assertEqual(job["timings"]["ai"], 5)

    def test_selected_upload_keeps_transcript_and_clip_on_original_clock(self):
        import numpy as np
        from contextlib import ExitStack
        job = dict(self.s.job, options={**jobs.DEFAULTS, "range_start": 120, "range_end": 130},
                   source={"type": "upload", "path": str(self.d / "in.mp4")}, progress=0, duration=0, uploader="")
        words = [{"w": "word", "s": i, "e": i + .5} for i in range(8)]
        candidate = {"start": 1, "end": 8, "score": 90}
        with ExitStack() as stack:
            def mock(obj, name, **kw):
                return stack.enter_context(patch.object(obj, name, **kw))
            mock(jobs, "save")
            mock(jobs.usage, "set_context")
            mock(jobs.creators, "log")
            mock(jobs.transcribe, "model_ready", return_value=True)
            mock(media, "probe", return_value={"duration": 300, "width": 1280, "height": 720})
            mock(media, "save_thumb", return_value=False)
            extract = mock(media, "extract_audio")
            mock(media, "load_audio", return_value=np.ones(160000, dtype=np.int16))
            mock(jobs.transcribe, "transcribe", return_value=words)
            mock(jobs.brain, "find_clips", return_value=([candidate], {}))
            rendered = mock(jobs, "render_one")
            jobs._run_job(job)
        self.assertEqual(extract.call_args.kwargs, {"start": 120, "end": 130})
        self.assertEqual(job["clips"][0]["start"], 121)
        self.assertEqual(rendered.call_args.args[2][0]["s"], 120)
        self.assertEqual(json.loads((self.d / "words.json").read_text())[0]["s"], 120)

    def test_selected_url_preserves_clock_with_own_or_youtube_transcript(self):
        import numpy as np
        from contextlib import ExitStack
        for youtube in (False, True):
            with self.subTest(youtube=youtube), ExitStack() as stack:
                job = dict(self.s.job, options={**jobs.DEFAULTS, "range_start": 120, "range_end": 130},
                           source={"type": "url", "url": "https://youtube.com/watch?v=abcdefghijk"},
                           progress=0, duration=0, uploader="")
                words = [{"w": "word", "s": i * .48, "e": i * .48 + .4} for i in range(20)]

                def mock(obj, name, **kw):
                    return stack.enter_context(patch.object(obj, name, **kw))
                mock(jobs, "save")
                mock(jobs.usage, "set_context")
                mock(jobs.creators, "log")
                mock(jobs.transcribe, "model_ready", return_value=True)
                mock(media, "info", return_value={"kind": "video", "duration": 300,
                                                 "title": "Test", "uploader": "Test", "is_live": False})
                mock(media, "save_thumb", return_value=False)
                audio = self.d / "audio.m4a"
                audio.touch()
                download = mock(media, "download_audio", return_value=audio)
                mock(media, "extract_audio")
                mock(media, "load_audio", return_value=np.ones(86 * 16000, dtype=np.int16))
                asr = mock(jobs.transcribe, "transcribe", return_value=words)
                mock(media, "youtube_words", return_value=(
                    ([dict(w, s=w["s"] + 120, e=w["e"] + 120) for w in words], "es") if youtube else None))
                mock(jobs.brain, "find_clips", return_value=([{"start": 1, "end": 8, "score": 90}], {}))
                piece = mock(jobs, "_fetch_piece")
                rendered = mock(jobs, "render_one")
                jobs._run_job(job)
                self.assertEqual(download.call_args.kwargs, {"start": 82, "end": 168})
                self.assertEqual(piece.call_args.kwargs["audio_start"], 82)
                self.assertEqual(job["clips"][0]["start"], 121)
                self.assertEqual(rendered.call_args.args[2][0]["s"], 120)
                self.assertEqual(asr.call_count, 0 if youtube else 1)


if __name__ == "__main__":
    unittest.main()
