"""Caption support is required even when a working ffmpeg is already installed."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
data = tempfile.TemporaryDirectory(prefix="pc-caption-tools-")
os.environ.update(PEAKCUT_DATA=data.name, DATABASE_URL="")
from studio import render, tools


class CaptionToolTests(unittest.TestCase):
    def test_configured_ffmpeg_directory_overrides_existing_path(self):
        with tempfile.TemporaryDirectory() as folder:
            for name in tools.NEEDED:
                program = Path(folder) / name
                program.write_text("#!/bin/sh\nexit 0\n")
                program.chmod(0o755)
            with patch.dict(os.environ, {"PEAKCUT_FFMPEG_DIR": folder}):
                tools.setup()
                self.assertEqual(Path(tools.shutil.which("ffmpeg")).parent, Path(folder))

    def test_missing_ass_filter_has_installation_instructions(self):
        self.assertTrue(callable(getattr(tools, "require_captions", None)), "Caption support check is missing")
        reply = subprocess.CompletedProcess([], 0, "", "Unknown filter 'ass'.")
        with patch.object(tools.subprocess, "run", return_value=reply), \
                patch.object(tools, "setup"), patch.object(tools.shutil, "which", return_value=sys.executable):
            with self.assertRaisesRegex(RuntimeError, "caption") as failure:
                tools.require_captions()
        self.assertIn("ffmpeg", str(failure.exception))
        if sys.platform == "darwin":
            self.assertIn("brew install ffmpeg-full", str(failure.exception))

    def test_ass_filter_is_accepted(self):
        self.assertTrue(callable(getattr(tools, "require_captions", None)), "Caption support check is missing")
        reply = subprocess.CompletedProcess([], 0, "Filter ass\n  Render ASS subtitles\n", "")
        with patch.object(tools.subprocess, "run", return_value=reply), \
                patch.object(tools, "setup"), patch.object(tools.shutil, "which", return_value=sys.executable):
            tools.require_captions()

    def test_renderer_checks_caption_support_before_encoding(self):
        self.assertTrue(callable(getattr(tools, "require_captions", None)), "Caption support check is missing")
        with patch.object(tools, "require_captions", side_effect=RuntimeError("Missing caption support")), \
                patch.object(render, "_render_in") as encode:
            with self.assertRaisesRegex(RuntimeError, "caption"):
                render.render_clip(Path("source.mp4"), Path(data.name) / "out", "clip01",
                                   {"start": 0, "end": 3}, [], 1920, 1080)
            encode.assert_not_called()


if __name__ == "__main__":
    unittest.main()
