import pytest
import asyncio
import subprocess
import os
from unittest.mock import AsyncMock, patch
from bot.utils import markdown_escape, track_process, untrack_process
from bot.state import ACTIVE_PROCESSES

def test_markdown_escape():
    assert markdown_escape("file_name*") == r"file\_name\*"
    assert markdown_escape("noescape") == "noescape"

def test_process_tracking():
    proc = subprocess.Popen(["python", "-c", "pass"])
    track_process(1, proc, ACTIVE_PROCESSES)
    assert proc in ACTIVE_PROCESSES[1]
    
    untrack_process(1, proc, ACTIVE_PROCESSES)
    assert 1 not in ACTIVE_PROCESSES
    
    # Untrack non-existent
    untrack_process(999, proc, ACTIVE_PROCESSES)
    proc.terminate()

@pytest.mark.asyncio
async def test_ollama_relay_no_socket(mocker):
    # Test that it returns immediately if no OLLAMA_UNIX_SOCKET is set
    with patch.dict(os.environ, {}, clear=True):
        from bot.utils import start_ollama_relay
        res = await start_ollama_relay()
        assert res is None

@pytest.mark.asyncio
async def test_unix_to_tcp_relay_fail(mocker):
    # open_unix_connection is only on POSIX. Mocking it is tricky on Win32.
    if os.name != "posix":
        pytest.skip("open_unix_connection only available on POSIX")
    
    mocker.patch("asyncio.open_unix_connection", side_effect=Exception("conn fail"))
    mock_reader = AsyncMock()
    mock_reader.read.return_value = b"" # EOF to prevent infinite loop
    mock_writer = AsyncMock()
    
    from bot.utils import _unix_to_tcp_relay
    await _unix_to_tcp_relay(mock_reader, mock_writer)
    mock_writer.close.assert_called()
