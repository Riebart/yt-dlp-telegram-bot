import pytest
import subprocess
import os
import sys
import signal
import time
from itertools import cycle
from pathlib import Path
from unittest.mock import MagicMock, patch, mock_open
from bot.processor import FFmpegProcessor, LargeVideoSplitter
from bot.state import CANCELLATIONS

def test_ffmpeg_binary(mock_config):
    fp = FFmpegProcessor(mock_config)
    assert fp.binary("ffmpeg") == "ffmpeg"
    
    mock_config.ffmpeg_location = "/opt/bin"
    assert fp.binary("ffmpeg") == str(Path("/opt/bin/ffmpeg"))

def test_ffmpeg_get_duration_real(mock_ffmpeg):
    # Use real asset generated in setup
    video_path = Path("tests/assets/test_video.mp4")
    duration = mock_ffmpeg.get_duration(video_path)
    assert duration is not None
    # Generated duration was 1s, allow some float variance
    assert 0.9 <= duration <= 1.1

def test_ffmpeg_get_video_bitrate_real(mock_ffmpeg):
    # Use real asset generated in setup
    video_path = Path("tests/assets/test_video.mp4")
    bitrate = mock_ffmpeg.get_video_bitrate(video_path)
    assert bitrate is not None
    assert bitrate > 0

def test_ffmpeg_get_duration_success(mock_ffmpeg, mocker):
    mock_run = mocker.patch("subprocess.run")
    mock_run.return_value.returncode = 0
    mock_run.return_value.stdout = "123.45"
    
    assert mock_ffmpeg.get_duration(Path("video.mp4")) == 123.45

def test_ffmpeg_get_duration_failure(mock_ffmpeg, mocker):
    mock_run = mocker.patch("subprocess.run")
    mock_run.return_value.returncode = 1
    mock_run.return_value.stderr = "Error"
    
    assert mock_ffmpeg.get_duration(Path("video.mp4")) is None

def test_ffmpeg_get_duration_exception(mock_ffmpeg, mocker):
    mocker.patch("subprocess.run", side_effect=Exception("boom"))
    assert mock_ffmpeg.get_duration(Path("video.mp4")) is None

def test_ffmpeg_get_video_bitrate_success(mock_ffmpeg, mocker):
    mock_run = mocker.patch("subprocess.run")
    mock_run.return_value.returncode = 0
    mock_run.return_value.stdout = "2000000"
    assert mock_ffmpeg.get_video_bitrate(Path("v.mp4")) == 2000000
    
    mock_run.return_value.stdout = "3000000,1234"
    assert mock_ffmpeg.get_video_bitrate(Path("v.mp4")) == 3000000

def test_ffmpeg_get_video_bitrate_failure(mock_ffmpeg, mocker):
    mock_run = mocker.patch("subprocess.run")
    mock_run.return_value.returncode = 1
    mock_run.return_value.stderr = "Err"
    assert mock_ffmpeg.get_video_bitrate(Path("v.mp4")) is None
    
    mock_run.return_value.returncode = 0
    mock_run.return_value.stdout = ""
    assert mock_ffmpeg.get_video_bitrate(Path("v.mp4")) is None
    
    mocker.patch("subprocess.run", side_effect=Exception("err"))
    assert mock_ffmpeg.get_video_bitrate(Path("v.mp4")) is None

def test_ffmpeg_compress_to_size_various_branches(mock_ffmpeg, mocker):
    CANCELLATIONS.clear()
    mocker.patch.object(mock_ffmpeg, "get_duration", return_value=100)
    
    # Test with no progress_callback and no chat_id
    mock_popen = mocker.patch("subprocess.Popen")
    mock_proc = mock_popen.return_value
    mock_proc.returncode = 0
    mock_proc.stdout = MagicMock()
    mock_proc.stdout.readline.side_effect = ["progress=continue\n", ""]
    mock_proc.stderr = None 
    mock_proc.poll.side_effect = cycle([None, 0])
    
    start_time = time.time()
    mocker.patch("time.monotonic", side_effect=lambda: time.time() - start_time + 100)

    ok, out, err = mock_ffmpeg.compress_to_size(Path("i.mp4"), 1000000)
    assert ok

    # Test with proc.stdout = None
    mock_proc.stdout = None
    mock_proc.poll.side_effect = cycle([None, 0])
    ok, out, err = mock_ffmpeg.compress_to_size(Path("i.mp4"), 1000000)
    assert ok

    # Test error tail extraction
    mock_proc.returncode = 2 # Generic error
    mock_proc.stdout = MagicMock()
    mock_proc.stdout.readline.side_effect = ["Detailed error message\n", ""]
    mock_proc.stderr = None
    mock_proc.poll.side_effect = cycle([None, 2])
    # Use audio_bps=64000 to avoid retry logic which checks for "-c:a copy"
    ok, out, err = mock_ffmpeg.compress_to_size(Path("i.mp4"), 1000000, audio_bps=64000)
    assert not ok
    assert "Detailed error message" in err

    # Test SIGTERM (Linux cancellation)
    mocker.patch("sys.platform", "linux")
    mocker.patch("os.setsid", MagicMock(), create=True)
    mocker.patch("os.getpgid", return_value=5678, create=True)
    mock_kill = mocker.patch("os.killpg", create=True)
    
    mock_popen = mocker.patch("subprocess.Popen")
    mock_proc = mock_popen.return_value
    mock_proc.pid = 5678
    mock_proc.returncode = 1
    mock_proc.stdout = MagicMock()
    mock_proc.stdout.readline.return_value = ""
    mock_proc.poll.side_effect = cycle([None, 1])
    
    CANCELLATIONS.add(99)
    
    ok, out, err = mock_ffmpeg.compress_to_size(Path("in.mp4"), 1000000, chat_id=99)
    
    assert not ok
    assert "cancelled" in err.lower()
    mock_kill.assert_called_with(5678, signal.SIGTERM)
    CANCELLATIONS.clear()

def test_ffmpeg_compress_to_size_win32_cancellation(mock_ffmpeg, mocker):
    CANCELLATIONS.clear()
    mocker.patch("sys.platform", "win32")
    # Mock Windows-only constant
    mocker.patch("subprocess.CREATE_NEW_PROCESS_GROUP", 0x00000200, create=True)
    mocker.patch.object(mock_ffmpeg, "get_duration", return_value=100)
    
    mock_popen = mocker.patch("subprocess.Popen")
    mock_proc = mock_popen.return_value
    mock_proc.pid = 5678
    mock_proc.returncode = 1
    mock_proc.stdout = MagicMock()
    mock_proc.stdout.readline.return_value = ""
    mock_proc.poll.side_effect = cycle([None, 1])
    
    mock_run = mocker.patch("subprocess.run")
    
    CANCELLATIONS.add(88)
    
    ok, out, err = mock_ffmpeg.compress_to_size(Path("in.mp4"), 1000000, chat_id=88)
    
    assert not ok
    assert "cancelled" in err.lower()
    assert mock_run.called
    CANCELLATIONS.clear()

def test_ffmpeg_compress_to_size_cancellation_timeout(mock_ffmpeg, mocker):
    CANCELLATIONS.clear()
    mocker.patch("sys.platform", "win32")
    mocker.patch("subprocess.CREATE_NEW_PROCESS_GROUP", 0x00000200, create=True)
    mocker.patch.object(mock_ffmpeg, "get_duration", return_value=100)
    
    mock_popen = mocker.patch("subprocess.Popen")
    mock_proc = mock_popen.return_value
    mock_proc.pid = 5678
    mock_proc.returncode = 1
    mock_proc.stdout = MagicMock()
    mock_proc.stdout.readline.return_value = ""
    mock_proc.poll.side_effect = cycle([None])
    
    mock_run = mocker.patch("subprocess.run")
    
    # Mock time to trigger hard timeout (max_duration = 1200)
    start_time = 1000.0
    mocker.patch("time.monotonic", side_effect=[start_time, start_time, start_time + 1300, start_time + 1300, start_time + 1300, start_time + 1300])
    
    ok, out, err = mock_ffmpeg.compress_to_size(Path("in.mp4"), 1000000, chat_id=99)
    
    assert not ok
    assert "operation timed out" in err.lower()
    assert mock_run.called
    CANCELLATIONS.clear()

def test_ffmpeg_compress_to_size_audio_retry(mock_ffmpeg, mocker):
    CANCELLATIONS.clear()
    mocker.patch.object(mock_ffmpeg, "get_duration", return_value=100)
    
    mock_popen = mocker.patch("subprocess.Popen")
    
    # First call: fail with -c:a copy
    mock_proc1 = MagicMock()
    mock_proc1.returncode = 2 # Generic error
    mock_proc1.stdout = iter([])
    mock_proc1.stderr = MagicMock()
    mock_proc1.stderr.read.return_value = "Invalid audio stream"
    mock_proc1.poll.return_value = 2
    
    # Second call: success with aac
    mock_proc2 = MagicMock()
    mock_proc2.returncode = 0
    mock_proc2.stdout = iter([])
    mock_proc2.poll.return_value = 0
    
    mock_popen.side_effect = [mock_proc1, mock_proc2]
    
    ok, out, err = mock_ffmpeg.compress_to_size(Path("in.mp4"), 1000000)
    assert ok
    assert mock_popen.call_count == 2
    args, kwargs = mock_popen.call_args
    cmd = " ".join(args[0])
    assert "aac" in cmd

def test_ffmpeg_compress_to_size_launch_fail(mock_ffmpeg, mocker):
    CANCELLATIONS.clear()
    mocker.patch.object(mock_ffmpeg, "get_duration", return_value=100)
    mocker.patch("subprocess.Popen", side_effect=Exception("launch failed"))
    
    ok, path, err = mock_ffmpeg.compress_to_size(Path("i.mp4"), 100000)
    assert not ok
    assert "Failed to launch ffmpeg" in err

# --- LargeVideoSplitter Tests ---

def test_splitter_get_keyframes_success(mock_ffmpeg, mocker):
    splitter = LargeVideoSplitter(mock_ffmpeg._cfg, mock_ffmpeg)
    mock_run = mocker.patch("subprocess.run")
    mock_run.return_value.returncode = 0
    mock_run.return_value.stdout = "10.0\n20.5, \n30.0\ninvalid\n"
    
    keyframes = splitter.get_keyframes(Path("v.mp4"))
    assert keyframes == [10.0, 20.5, 30.0]

def test_splitter_get_keyframes_failure(mock_ffmpeg, mocker):
    splitter = LargeVideoSplitter(mock_ffmpeg._cfg, mock_ffmpeg)
    mock_run = mocker.patch("subprocess.run")
    mock_run.return_value.returncode = 1
    assert splitter.get_keyframes(Path("v.mp4")) == []
    
    mock_run.side_effect = Exception("err")
    assert splitter.get_keyframes(Path("v.mp4")) == []

def test_splitter_split_video_no_action_needed(mock_ffmpeg, mocker):
    splitter = LargeVideoSplitter(mock_ffmpeg._cfg, mock_ffmpeg)
    mocker.patch("pathlib.Path.stat", return_value=MagicMock(st_size=10*1024*1024))
    
    chunks, err = splitter.split_video(Path("v.mp4"), max_size_mb=50)
    assert chunks == [Path("v.mp4")]
    assert err == ""

def test_splitter_split_video_success(mock_ffmpeg, mocker):
    CANCELLATIONS.clear()
    splitter = LargeVideoSplitter(mock_ffmpeg._cfg, mock_ffmpeg)
    # 100MB file, 50MB limit
    mocker.patch("pathlib.Path.stat", return_value=MagicMock(st_size=100*1024*1024))
    mocker.patch.object(mock_ffmpeg, "get_duration", return_value=100)
    mocker.patch.object(splitter, "get_keyframes", return_value=[0, 30, 60, 90])
    
    mock_popen = mocker.patch("subprocess.Popen")
    mock_proc = mock_popen.return_value
    mock_proc.poll.side_effect = cycle([None, 0])
    mock_proc.wait.return_value = 0
    mock_proc.returncode = 0
    mock_proc.stdout.readline.return_value = ""
    
    mocker.patch("pathlib.Path.exists", return_value=True)
    mocker.patch("builtins.open", mock_open())
    
    chunks, err = splitter.split_video(Path("v.mp4"), max_size_mb=50)
    assert len(chunks) == 2
    assert err == ""

def test_splitter_split_video_no_keyframes_fallback(mock_ffmpeg, mocker):
    CANCELLATIONS.clear()
    splitter = LargeVideoSplitter(mock_ffmpeg._cfg, mock_ffmpeg)
    mocker.patch("pathlib.Path.stat", return_value=MagicMock(st_size=100*1024*1024))
    mocker.patch.object(mock_ffmpeg, "get_duration", return_value=100)
    mocker.patch.object(splitter, "get_keyframes", return_value=[])
    
    mock_popen = mocker.patch("subprocess.Popen")
    mock_proc = mock_popen.return_value
    mock_proc.poll.side_effect = cycle([None, 0])
    mock_proc.wait.return_value = 0
    mock_proc.returncode = 0
    mocker.patch("pathlib.Path.exists", return_value=True)
    mocker.patch("builtins.open", mock_open())

    chunks, err = splitter.split_video(Path("v.mp4"), max_size_mb=50)
    assert len(chunks) > 0

def test_splitter_split_video_cancellation(mock_ffmpeg, mocker):
    CANCELLATIONS.clear()
    splitter = LargeVideoSplitter(mock_ffmpeg._cfg, mock_ffmpeg)
    mocker.patch("pathlib.Path.stat", return_value=MagicMock(st_size=100*1024*1024))
    mocker.patch.object(mock_ffmpeg, "get_duration", return_value=100)
    mocker.patch.object(splitter, "get_keyframes", return_value=[30])
    
    mocker.patch("os.setsid", MagicMock(), create=True)
    mocker.patch("os.getpgid", return_value=9999, create=True)
    mock_kill = mocker.patch("os.killpg", create=True)

    mock_popen = mocker.patch("subprocess.Popen")
    mock_proc = mock_popen.return_value
    mock_proc.poll.side_effect = cycle([None])
    mock_proc.wait.side_effect = subprocess.TimeoutExpired(cmd="ffmpeg", timeout=0.1)
    mock_proc.pid = 9999
    mock_proc.stdout = MagicMock()
    mock_proc.stdout.readline.return_value = ""
    mock_proc.returncode = 1 
    
    CANCELLATIONS.add(77)
    mocker.patch("sys.platform", "win32")
    mocker.patch("subprocess.CREATE_NEW_PROCESS_GROUP", 0x00000200, create=True)

    chunks, err = splitter.split_video(Path("v.mp4"), max_size_mb=50, chat_id=77)
    assert chunks == []
    assert "cancelled" in err.lower()
    CANCELLATIONS.clear()

def test_splitter_split_video_ffmpeg_fail(mock_ffmpeg, mocker):
    CANCELLATIONS.clear()
    splitter = LargeVideoSplitter(mock_ffmpeg._cfg, mock_ffmpeg)
    mocker.patch("pathlib.Path.stat", return_value=MagicMock(st_size=100*1024*1024))
    mocker.patch.object(mock_ffmpeg, "get_duration", return_value=100)
    mocker.patch.object(splitter, "get_keyframes", return_value=[30])
    
    mock_popen = mocker.patch("subprocess.Popen")
    mock_proc = mock_popen.return_value
    mock_proc.poll.side_effect = cycle([0])
    mock_proc.wait.returncode = 2
    mock_proc.returncode = 2
    mock_proc.stdout.read.return_value = "FFmpeg error message"
    
    chunks, err = splitter.split_video(Path("v.mp4"), max_size_mb=50)
    assert chunks == []
    assert "FFmpeg failed" in err
