"""YouTube chat: real monitoring code with recorded API replies; no keys or network needed."""
import collections
from datetime import datetime, timezone
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
data = tempfile.TemporaryDirectory(prefix="pc-chat-")
os.environ.update(PEAKCUT_DATA=data.name, DATABASE_URL="")
from studio import config, live

NOW = 1800000000.0


def message(mid, age=1, kind="textMessageEvent"):
    return {"id": mid, "snippet": {"type": kind, "publishedAt":
            datetime.fromtimestamp(NOW - age, timezone.utc).isoformat()}}


def page(items=(), token="next", interval=5000, **extra):
    return {"items": list(items), "nextPageToken": token, "pollingIntervalMillis": interval, **extra}


class YouTubeChatTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory(dir=data.name)
        self.s = live.Session({"id": "chat-test", "dir": self.folder.name, "clips": [],
                               "source": {"url": "https://youtube.com/@creator/live"}}, {})
        self.s.samples = 120 * live.SR
        self.s.db = [-30] * 120
        self.addCleanup(self.folder.cleanup)

    def monitor(self, replies):
        self.assertTrue(callable(getattr(self.s, "_youtube_chat", None)), "YouTube chat monitor is missing")
        calls, waits = [], []
        replies = iter(replies)

        def request(req):
            calls.append(req)
            reply = next(replies)
            if isinstance(reply, Exception):
                raise reply
            status, body, headers = (reply if len(reply) == 3 else (*reply, {})) if isinstance(reply, tuple) else (200, reply, {})
            return httpx.Response(status, json=body, headers=headers)

        # Advance only the waiting clock, without sleeping or launching recordings.
        def wait(seconds):
            waits.append(seconds)
            if len(waits) >= self.stop_after:
                self.s.stop_flag.set()
            return self.s.stop_flag.is_set()

        client_type = httpx.Client
        with patch("httpx.Client", side_effect=lambda **kw: client_type(transport=httpx.MockTransport(request), **kw)), \
                patch.object(config, "youtube_chat_key", return_value="test-key"), \
                patch.object(live.time, "time", return_value=NOW), \
                patch.object(self.s.stop_flag, "wait", side_effect=wait):
            self.s._youtube_chat("abcdefghijk")
        return calls, waits

    def test_history_duplicates_timing_and_poll_interval(self):
        self.stop_after = 3
        calls, waits = self.monitor([
            {"items": [{"liveStreamingDetails": {"activeLiveChatId": "chat-id"}}]},
            page([message("old")], "p1", 7000),
            page([message("old"), message("a", 2), message("b", 1),
                  message("delete", kind="messageDeletedEvent"), message("bad", 999)], "p2", 8000),
            page([message("a", 2), message("c", 1)], "p3", 9000),
        ])
        self.assertEqual(self.s.chat, collections.Counter({118: 1, 119: 2}))
        self.assertEqual(self.s.chat_total, 3)
        self.assertEqual(waits, [7, 8, 9])
        self.assertEqual(calls[0].url.params["id"], "abcdefghijk")
        self.assertEqual(calls[1].url.params["liveChatId"], "chat-id")
        self.assertEqual(calls[2].url.params["pageToken"], "p1")
        self.assertNotIn("key", calls[0].url.params)
        self.assertEqual(calls[0].headers["x-goog-api-key"], "test-key")
        self.assertFalse(self.s.chat_on)

    def test_missing_key_does_not_contact_youtube(self):
        self.assertTrue(callable(getattr(self.s, "_youtube_chat", None)), "YouTube chat monitor is missing")
        with patch.object(config, "youtube_chat_key", return_value=""), patch("httpx.Client") as client:
            self.s._youtube_chat("abcdefghijk")
        client.assert_not_called()
        self.assertIn("key", self.s.chat_status.lower())
        self.assertFalse(self.s.stop_flag.is_set())

    def test_disabled_chat_leaves_recording_running(self):
        self.stop_after = 2
        self.monitor([{"items": [{"liveStreamingDetails": {}}]}])
        self.assertIn("unavailable", self.s.chat_status.lower())
        self.assertFalse(self.s.stop_flag.is_set())

    def test_quota_error_is_visible_without_leaking_api_response(self):
        self.stop_after = 2
        self.monitor([(403, {"error": {"message": "secret-key", "errors": [{"reason": "quotaExceeded"}]}})])
        self.assertIn("quota", self.s.chat_status.lower())
        self.assertNotIn("secret-key", self.s.chat_status)
        self.assertFalse(self.s.stop_flag.is_set())

    def test_temporary_failure_retries_same_page_without_recounting(self):
        self.stop_after = 4
        calls, waits = self.monitor([
            {"items": [{"liveStreamingDetails": {"activeLiveChatId": "chat-id"}}]},
            page([], "p1"),
            httpx.ReadTimeout("offline"),
            page([message("a")], "p2"),
            page([message("a"), message("b")], "p3"),
        ])
        self.assertEqual(self.s.chat_total, 2)
        self.assertEqual(calls[2].url.params["pageToken"], calls[3].url.params["pageToken"])
        self.assertGreaterEqual(waits[1], 5)

    def test_stream_end_stops_chat_without_stopping_recording(self):
        self.stop_after = 2
        self.monitor([{"items": [{"liveStreamingDetails": {"activeLiveChatId": "chat-id"}}]},
                      page(offlineAt="2027-01-15T08:00:00Z")])
        self.assertFalse(self.s.stop_flag.is_set())
        self.assertFalse(self.s.chat_on)

    def test_rate_limit_waits_as_long_as_youtube_requests(self):
        self.stop_after = 1
        _, waits = self.monitor([(429, {}, {"Retry-After": "30"})])
        self.assertEqual(waits, [30])

    def test_bad_response_retries_without_stopping_recording(self):
        self.stop_after = 1
        self.monitor([[]])
        self.assertIn("trying again", self.s.chat_status)

    def test_expired_page_token_skips_history_again(self):
        self.stop_after = 4
        calls, _ = self.monitor([
            {"items": [{"liveStreamingDetails": {"activeLiveChatId": "chat-id"}}]},
            page([], "p1"),
            (400, {"error": {"errors": [{"reason": "invalidPageToken"}]}}),
            page([message("history")], "p2"),
            page([message("fresh")], "p3"),
        ])
        self.assertNotIn("pageToken", calls[3].url.params)
        self.assertEqual(self.s.chat_total, 1)

    def test_key_can_come_from_environment_or_secret_file(self):
        secret = Path(self.folder.name) / "key.txt"
        secret.write_text("test-youtube-key-from-file")
        config._kept.clear()
        with patch.dict(os.environ, {"PEAKCUT_YOUTUBE_CHAT_KEY": "", "PEAKCUT_YOUTUBE_CHAT_KEY_FILE": str(secret)}):
            self.assertEqual(config.youtube_chat_key(), "test-youtube-key-from-file")
            config._kept.clear()
            os.environ["PEAKCUT_YOUTUBE_CHAT_KEY"] = "test-youtube-key-from-env"
            self.assertEqual(config.youtube_chat_key(), "test-youtube-key-from-env")
        config._kept.clear()

    def test_stopped_session_does_not_fetch_more_chat(self):
        self.stop_after = 1
        self.s.stop_flag.set()
        calls, _ = self.monitor([])
        self.assertEqual(calls, [])

    def test_youtube_and_twitch_choose_their_own_monitor(self):
        self.assertTrue(callable(getattr(self.s, "_start_chat", None)), "Chat routing is missing")
        with patch.object(live.ctx, "bind", side_effect=lambda f: f), patch.object(live.threading, "Thread") as thread:
            self.s._start_chat({"video_id": "abcdefghijk"})
            self.assertEqual(thread.call_args.kwargs["target"], self.s._youtube_chat)
            self.assertEqual(thread.call_args.kwargs["args"], ("abcdefghijk",))
            self.s.job["source"]["url"] = "https://twitch.tv/Creator"
            self.s._start_chat({"video_id": "not-youtube"})
            self.assertEqual(thread.call_args.kwargs["target"], self.s._chat)
            self.assertEqual(thread.call_args.kwargs["args"], ("creator",))

    def test_resolve_preserves_broadcast_id_for_channel_urls(self):
        with patch("yt_dlp.YoutubeDL") as ydl:
            ydl.return_value.__enter__.return_value.extract_info.return_value = {
                "is_live": True, "id": "abcdefghijk", "url": "https://media.example/stream"}
            info = live.resolve("https://youtube.com/@creator/live")
        self.assertEqual(info.get("video_id"), "abcdefghijk")

    def test_chat_burst_triggers_review_before_the_reaction(self):
        self.stop_after = 2
        items = [message(str(i), 1 + i % 7) for i in range(28)]
        self.monitor([{"items": [{"liveStreamingDetails": {"activeLiveChatId": "chat-id"}}]},
                      page([], "p1"), page(items, "p2")])
        self.s._watch_spikes()
        self.assertIsNotNone(self.s.spike)
        self.assertIn("chat", self.s.spike["why"])
        self.assertEqual(self.s.spike["t"] - self.s.AUTO_BEFORE, 87)
        self.assertEqual(self.s.spike["t"] + self.s.AUTO_AFTER, 137)
        # Exercise the actual analysis loop too: it sends the earlier setup to the AI.
        self.s.job.update(creator=None, title="Test stream", ai={})
        self.s.job["source"]["name"] = "Test stream"
        self.s.ended = True
        self.s.t_done = 137
        with patch.object(self.s, "_scan_segments"), patch.object(self.s, "_transcribe_step", return_value=False), \
                patch.object(self.s, "_manual_step"), patch.object(self.s, "_publish"), \
                patch.object(self.s, "_ai_check") as check, patch.object(live.jobs, "save"), \
                patch.object(live.creators, "log"), patch.object(live.alerts, "notify"), \
                patch.object(live.jobs, "submit_live"):
            self.s._analyze()
        self.assertEqual(check.call_args.args[0], (87, 137))
        self.assertIn("chat", check.call_args.args[1])

    def test_chat_status_is_published_to_the_live_card(self):
        self.s.chat_status = "Watching YouTube chat"
        self.s.chat_on = True
        with patch.object(live.jobs, "save"):
            self.s._publish()
        self.assertEqual(self.s.job["live"]["chat_status"], "Watching YouTube chat")
        self.assertEqual(len(self.s.job["live"]["chat"]), 60)


if __name__ == "__main__":
    unittest.main()
