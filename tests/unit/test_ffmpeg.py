import pytest
import subprocess
import os
import sys
import signal
import time
from pathlib import Path
from unittest.mock import MagicMock, patch, mock_open
import bot2

def test_ffmpeg_binary(mock_config):
    fp = bot2.FFmpegProcessor(mock_config)
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
    mock_run = mocker.patch("bot2.subprocess.run")
    mock_run.return_value.returncode = 0
    mock_run.return_value.stdout = "123.45\n"
    
    assert mock_ffmpeg.get_duration(Path("video.mp4")) == 123.45

def test_ffmpeg_get_duration_failure(mock_ffmpeg, mocker):
    mock_run = mocker.patch("bot2.subprocess.run")
    mock_run.return_value.returncode = 1
    mock_run.return_value.stderr = "Error"
    
    assert mock_ffmpeg.get_duration(Path("video.mp4")) is None

def test_ffmpeg_get_duration_exception(mock_ffmpeg, mocker):
    mocker.patch("bot2.subprocess.run", side_effect=Exception("boom"))
    assert mock_ffmpeg.get_duration(Path("video.mp4")) is None

def test_ffmpeg_get_video_bitrate_success(mock_ffmpeg, mocker):
    mock_run = mocker.patch("bot2.subprocess.run")
    mock_run.return_value.returncode = 0
    mock_run.return_value.stdout = "2000000\n"
    assert mock_ffmpeg.get_video_bitrate(Path("v.mp4")) == 2000000
    
    mock_run.return_value.stdout = "3000000,1234\n"
    assert mock_ffmpeg.get_video_bitrate(Path("v.mp4")) == 3000000

def test_ffmpeg_get_video_bitrate_failure(mock_ffmpeg, mocker):
    mock_run = mocker.patch("bot2.subprocess.run")
    mock_run.return_value.returncode = 1
    mock_run.return_value.stderr = "Err"
    assert mock_ffmpeg.get_video_bitrate(Path("v.mp4")) is None
    
    mock_run.return_value.returncode = 0
    mock_run.return_value.stdout = ""
    assert mock_ffmpeg.get_video_bitrate(Path("v.mp4")) is None
    
    mocker.patch("bot2.subprocess.run", side_effect=Exception("err"))
    assert mock_ffmpeg.get_video_bitrate(Path("v.mp4")) is None

def test_ffmpeg_compress_to_size_various_branches(mock_ffmpeg, mocker):
    mocker.patch.object(mock_ffmpeg, "get_duration", return_value=100)
    
    # Test with no progress_callback and no chat_id
    mock_popen = mocker.patch("bot2.subprocess.Popen")
    mock_proc = mock_popen.return_value
    mock_proc.returncode = 0
    mock_proc.stdout = iter(["progress=continue\n"])
    mock_proc.stderr = None # Hit the 'if proc.stderr' branch
    
    start_time = time.time()
    mocker.patch("bot2.time.monotonic", side_effect=lambda: time.time() - start_time + 100)

    ok, out, err = mock_ffmpeg.compress_to_size(Path("i.mp4"), 1000000)
    assert ok

    # Test with proc.stdout = None
    mock_proc.stdout = None
    ok, out, err = mock_ffmpeg.compress_to_size(Path("i.mp4"), 1000000)
    assert ok

    # Test error tail extraction
    mock_proc.returncode = 2 # Generic error (1 is misidentified as cancellation on Win32)
    mock_proc.stderr = MagicMock()
    mock_proc.stderr.read.return_value = b"Detailed error message" # Should be bytes for read().decode()
    ok, out, err = mock_ffmpeg.compress_to_size(Path("i.mp4"), 1000000)
    assert not ok
    assert "Detailed error message" in err

    # Test SIGTERM (Linux cancellation)
    mocker.patch("sys.platform", "linux")
    mocker.patch("os.setsid", MagicMock(), create=True)
    mock_proc.returncode = -signal.SIGTERM
    ok, out, err = mock_ffmpeg.compress_to_size(Path("i.mp4"), 1000000)
    assert not ok
    assert "cancelled" in err.lower()

    # Test progress loop exception swallowing
    mock_proc.returncode = 0
    mock_proc.stdout = iter(["bad line\n", "progress=continue\n"])
    # This will trigger an exception in the progress parsing logic but it's swallowed
    ok, out, err = mock_ffmpeg.compress_to_size(Path("i.mp4"), 1000000)
    assert ok

def test_splitter_manifest_generation(mock_ffmpeg, mocker):
    splitter = bot2.LargeVideoSplitter(mock_ffmpeg._cfg, mock_ffmpeg)
    mocker.patch.object(mock_ffmpeg, "get_duration", return_value=100)
    
    m_path = Path("manifest.json")
    chunks = [Path("part1.mp4"), Path("part2.mp4")]
    original = Path("orig.mp4")
    
    # Mock stat specifically for bot2's Path
    # Use a mock that returns a fixed size
    mock_stat = MagicMock()
    mock_stat.return_value.st_size = 1024
    mocker.patch("bot2.Path.stat", mock_stat)
    
    m_open = mocker.patch("builtins.open", mock_open())
    
    splitter._generate_manifest(m_path, chunks, original)
    
    # Verify manifest was written
    assert m_open.call_count == 2 # JSON and TXT playlist
    
    # Verify duration None branch in manifest
    mocker.patch.object(mock_ffmpeg, "get_duration", return_value=None)
    splitter._generate_manifest(m_path, chunks, original)

def test_ffmpeg_check_qsv(mock_ffmpeg, mocker):
    # Test Linux with QSV
    mocker.patch("sys.platform", "linux")
    mocker.patch("os.path.exists", return_value=True)
    assert mock_ffmpeg._check_qsv_available() is True
    
    # Test Linux without QSV
    mocker.patch("os.path.exists", return_value=False)
    assert mock_ffmpeg._check_qsv_available() is False
    
    # Test not Linux
    mocker.patch("sys.platform", "win32")
    assert mock_ffmpeg._check_qsv_available() is False

def test_ffmpeg_compress_to_size_edge_cases(mock_ffmpeg, mocker):
    # Depth > 1
    ok, path, err = mock_ffmpeg.compress_to_size(Path("i.mp4"), 100, depth=2)
    assert not ok
    assert "Maximum compression retry depth exceeded" in err
    
    # Duration <= 0
    mocker.patch.object(mock_ffmpeg, "get_duration", return_value=0)
    ok, path, err = mock_ffmpeg.compress_to_size(Path("i.mp4"), 100000)
    assert not ok
    assert "Could not determine video duration" in err
    
    # Bitrate too low
    mocker.patch.object(mock_ffmpeg, "get_duration", return_value=100)
    ok, path, err = mock_ffmpeg.compress_to_size(Path("i.mp4"), 50000)
    assert not ok
    assert "Target bitrate too low" in err

@pytest.mark.parametrize("platform", ["linux", "win32"])
def test_ffmpeg_compress_to_size_success(mock_ffmpeg, mocker, platform):
    mocker.patch("sys.platform", platform)
    mocker.patch.object(mock_ffmpeg, "get_duration", return_value=100)
    mocker.patch.object(mock_ffmpeg, "_check_qsv_available", return_value=(platform == "linux"))
    
    # Use create=True for os attributes that might not exist on all platforms
    mocker.patch("os.setsid", MagicMock(), create=True)
    
    mock_popen = mocker.patch("subprocess.Popen")
    mock_proc = mock_popen.return_value
    mock_proc.returncode = 0
    mock_proc.stdout = iter([
        "out_time_us=10000000\n",
        "total_size=1048576\n",
        "speed=2x\n",
        "progress=continue\n",
    ])
    mock_proc.stderr = MagicMock()
    mock_proc.pid = 1234
    
    # Use a lambda to avoid StopIteration on time.monotonic
    # We want it to return increasing values when called by the bot, 
    # but we don't want to exhaust a list because asyncio calls it too.
    start_time = time.time()
    mocker.patch("time.monotonic", side_effect=lambda: time.time() - start_time + 100)
    
    callback = MagicMock()
    ok, out, err = mock_ffmpeg.compress_to_size(
        Path("in.mp4"), 1000000, chat_id=1, progress_callback=callback
    )
    
    assert ok
    assert out.name == "in_compressed.mp4"
    # Note: callback might not be called if 5s didn't pass in the mock's "time"
    # but since our lambda returns +100, it should pass.

def test_ffmpeg_compress_to_size_cancellation_linux(mock_ffmpeg, mocker):
    mocker.patch("sys.platform", "linux")
    mocker.patch.object(mock_ffmpeg, "get_duration", return_value=100)
    
    mocker.patch("os.setsid", MagicMock(), create=True)
    mocker.patch("os.getpgid", return_value=5678, create=True)
    mock_kill = mocker.patch("os.killpg", create=True)

    mock_popen = mocker.patch("subprocess.Popen")
    mock_proc = mock_popen.return_value
    mock_proc.pid = 5678
    mock_proc.stdout = iter(["progress=continue\n"])
    
    # Inject cancellation
    bot2.CANCELLATIONS.add(99)
    
    ok, out, err = mock_ffmpeg.compress_to_size(Path("in.mp4"), 1000000, chat_id=99)
    
    assert not ok
    assert "cancelled" in err.lower()
    mock_kill.assert_called_with(5678, signal.SIGTERM)

def test_ffmpeg_compress_to_size_cancellation_win32(mock_ffmpeg, mocker):
    mocker.patch("sys.platform", "win32")
    mocker.patch.object(mock_ffmpeg, "get_duration", return_value=100)
    
    mock_popen = mocker.patch("subprocess.Popen")
    mock_proc = mock_popen.return_value
    mock_proc.pid = 5678
    mock_proc.stdout = iter(["progress=continue\n"])
    
    mock_run = mocker.patch("subprocess.run")
    
    # Inject cancellation
    bot2.CANCELLATIONS.add(88)
    
    ok, out, err = mock_ffmpeg.compress_to_size(Path("in.mp4"), 1000000, chat_id=88)
    
    assert not ok
    assert "cancelled" in err.lower()

def test_ffmpeg_compress_to_size_cancellation_timeout(mock_ffmpeg, mocker):
    mocker.patch("sys.platform", "win32")
    mocker.patch.object(mock_ffmpeg, "get_duration", return_value=100)
    
    mock_popen = mocker.patch("subprocess.Popen")
    mock_proc = mock_popen.return_value
    mock_proc.pid = 5678
    mock_proc.stdout = iter(["progress=continue\n"])
    import subprocess
    mock_proc.wait.side_effect = subprocess.TimeoutExpired(cmd=["taskkill"], timeout=5)
    
    mock_run = mocker.patch("subprocess.run")
    
    # Inject cancellation
    bot2.CANCELLATIONS.add(99)
    
    ok, out, err = mock_ffmpeg.compress_to_size(Path("in.mp4"), 1000000, chat_id=99)
    
    assert not ok
    assert "cancelled" in err.lower()
    bot2.CANCELLATIONS.discard(99)
    mock_run.assert_called_with(["taskkill", "/F", "/T", "/PID", "5678"], capture_output=True)

def test_ffmpeg_compress_to_size_audio_retry(mock_ffmpeg, mocker):
    mocker.patch.object(mock_ffmpeg, "get_duration", return_value=100)
    
    mock_popen = mocker.patch("subprocess.Popen")
    
    # First call: fail with -c:a copy
    mock_proc1 = MagicMock()
    mock_proc1.returncode = 2 # Generic error
    mock_proc1.stdout = iter([])
    mock_proc1.stderr.read.return_value = "Invalid audio stream"
    
    # Second call: success with aac
    mock_proc2 = MagicMock()
    mock_proc2.returncode = 0
    mock_proc2.stdout = iter([])
    
    mock_popen.side_effect = [mock_proc1, mock_proc2]
    
    ok, out, err = mock_ffmpeg.compress_to_size(Path("in.mp4"), 1000000)
    assert ok
    assert mock_popen.call_count == 2
    args, kwargs = mock_popen.call_args
    cmd = " ".join(args[0])
    assert "aac" in cmd

def test_ffmpeg_compress_to_size_launch_fail(mock_ffmpeg, mocker):
    mocker.patch.object(mock_ffmpeg, "get_duration", return_value=100)
    mocker.patch("subprocess.Popen", side_effect=Exception("launch failed"))
    
    ok, out, err = mock_ffmpeg.compress_to_size(Path("in.mp4"), 1000000)
    assert not ok
    assert "Failed to launch ffmpeg" in err

# --- LargeVideoSplitter Tests ---

def test_splitter_get_keyframes_success(mock_ffmpeg, mocker):
    splitter = bot2.LargeVideoSplitter(mock_ffmpeg._cfg, mock_ffmpeg)
    mock_run = mocker.patch("subprocess.run")
    mock_run.return_value.returncode = 0
    mock_run.return_value.stdout = "10.0\n20.5, \n30.0\ninvalid\n"
    
    keyframes = splitter.get_keyframes(Path("v.mp4"))
    assert keyframes == [10.0, 20.5, 30.0]

def test_splitter_get_keyframes_failure(mock_ffmpeg, mocker):
    splitter = bot2.LargeVideoSplitter(mock_ffmpeg._cfg, mock_ffmpeg)
    mock_run = mocker.patch("subprocess.run")
    mock_run.return_value.returncode = 1
    assert splitter.get_keyframes(Path("v.mp4")) == []
    
    mock_run.side_effect = Exception("err")
    assert splitter.get_keyframes(Path("v.mp4")) == []

def test_splitter_split_video_no_action_needed(mock_ffmpeg, mocker):
    splitter = bot2.LargeVideoSplitter(mock_ffmpeg._cfg, mock_ffmpeg)
    mocker.patch("pathlib.Path.stat", return_value=MagicMock(st_size=10*1024*1024))
    
    chunks, err = splitter.split_video(Path("v.mp4"), max_size_mb=50)
    assert chunks == [Path("v.mp4")]
    assert err == ""

def test_splitter_split_video_success(mock_ffmpeg, mocker):
    splitter = bot2.LargeVideoSplitter(mock_ffmpeg._cfg, mock_ffmpeg)
    # 100MB file, 50MB limit
    mocker.patch("pathlib.Path.stat", return_value=MagicMock(st_size=100*1024*1024))
    mocker.patch.object(mock_ffmpeg, "get_duration", return_value=100)
    mocker.patch.object(splitter, "get_keyframes", return_value=[0, 30, 60, 90])
    
    mock_popen = mocker.patch("subprocess.Popen")
    mock_proc = mock_popen.return_value
    mock_proc.poll.side_effect = [None, 0, None, 0, None, 0, None, 0]
    mock_proc.returncode = 0
    
    mocker.patch("pathlib.Path.exists", return_value=True)
    mocker.patch("builtins.open", mock_open())
    
    chunks, err = splitter.split_video(Path("v.mp4"), max_size_mb=50)
    # With 100s duration and 45s target chunk duration, splits are [0, 60, 100] -> 2 chunks
    assert len(chunks) == 2
    assert err == ""

def test_splitter_split_video_no_keyframes_fallback(mock_ffmpeg, mocker):
    splitter = bot2.LargeVideoSplitter(mock_ffmpeg._cfg, mock_ffmpeg)
    mocker.patch("pathlib.Path.stat", return_value=MagicMock(st_size=100*1024*1024))
    mocker.patch.object(mock_ffmpeg, "get_duration", return_value=100)
    mocker.patch.object(splitter, "get_keyframes", return_value=[])
    
    mock_popen = mocker.patch("subprocess.Popen")
    mock_proc = mock_popen.return_value
    mock_proc.poll.return_value = 0
    mock_proc.returncode = 0
    mocker.patch("pathlib.Path.exists", return_value=True)
    mocker.patch("builtins.open", mock_open())

    chunks, err = splitter.split_video(Path("v.mp4"), max_size_mb=50)
    assert len(chunks) > 0

def test_splitter_split_video_cancellation(mock_ffmpeg, mocker):
    splitter = bot2.LargeVideoSplitter(mock_ffmpeg._cfg, mock_ffmpeg)
    mocker.patch("pathlib.Path.stat", return_value=MagicMock(st_size=100*1024*1024))
    mocker.patch.object(mock_ffmpeg, "get_duration", return_value=100)
    mocker.patch.object(splitter, "get_keyframes", return_value=[30])
    
    mocker.patch("os.setsid", MagicMock(), create=True)
    mocker.patch("os.getpgid", return_value=9999, create=True)
    mock_kill = mocker.patch("os.killpg", create=True)

    mock_popen = mocker.patch("subprocess.Popen")
    mock_proc = mock_popen.return_value
    mock_proc.poll.return_value = None # Never finishing
    mock_proc.pid = 9999
    
    # Inject cancellation
    bot2.CANCELLATIONS.add(77)
    
    # Mock platform for kill branch
    mocker.patch("sys.platform", "linux")

    chunks, err = splitter.split_video(Path("v.mp4"), max_size_mb=50, chat_id=77)
    assert chunks == []
    assert "cancelled" in err.lower()
    mock_kill.assert_called()

def test_splitter_split_video_ffmpeg_fail(mock_ffmpeg, mocker):
    splitter = bot2.LargeVideoSplitter(mock_ffmpeg._cfg, mock_ffmpeg)
    mocker.patch("pathlib.Path.stat", return_value=MagicMock(st_size=100*1024*1024))
    mocker.patch.object(mock_ffmpeg, "get_duration", return_value=100)
    mocker.patch.object(splitter, "get_keyframes", return_value=[30])
    
    mock_popen = mocker.patch("subprocess.Popen")
    mock_proc = mock_popen.return_value
    mock_proc.poll.return_value = 0
    mock_proc.returncode = 2 # Generic error
    
    chunks, err = splitter.split_video(Path("v.mp4"), max_size_mb=50)
    assert chunks == []
    assert "FFmpeg failed" in err
