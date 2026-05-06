import pytest
import asyncio
import subprocess
import os
import bot2
from unittest.mock import AsyncMock, patch

def test_markdown_escape():
    assert bot2.markdown_escape("file_name*") == r"file\_name\*"
    assert bot2.markdown_escape("noescape") == "noescape"

def test_process_tracking():
    proc = subprocess.Popen(["python", "-c", "pass"])
    bot2.track_process(1, proc)
    assert proc in bot2.ACTIVE_PROCESSES[1]
    
    bot2.untrack_process(1, proc)
    assert 1 not in bot2.ACTIVE_PROCESSES
    
    # Untrack non-existent
    bot2.untrack_process(999, proc)

@pytest.mark.asyncio
async def test_ollama_relay_no_socket(mocker):
    # Test that it returns immediately if no OLLAMA_UNIX_SOCKET is set
    with patch.dict(os.environ, {}, clear=True):
        res = await bot2.start_ollama_relay()
        assert res is None

@pytest.mark.asyncio
async def test_unix_to_tcp_relay_fail(mocker):
    # open_unix_connection is only on POSIX. Mocking it is tricky on Win32.
    # Since we can't easily mock it on Win32 if it doesn't exist, we skip.
    if os.name != "posix":
        pytest.skip("open_unix_connection only available on POSIX")
    
    mocker.patch("asyncio.open_unix_connection", side_effect=Exception("conn fail"))
    mock_writer = AsyncMock()
    
    await bot2._unix_to_tcp_relay(AsyncMock(), mock_writer)
    mock_writer.close.assert_called()

