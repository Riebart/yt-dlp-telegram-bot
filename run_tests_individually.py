
import subprocess
import time
import sys

tests = [
    "tests/unit/test_bot_lifecycle.py::test_error_handler",
    "tests/unit/test_bot_lifecycle.py::test_main_startup",
    "tests/unit/test_classifier.py::test_classifier_success",
    "tests/unit/test_classifier.py::test_classifier_markdown_json",
    "tests/unit/test_classifier.py::test_classifier_invalid_json",
    "tests/unit/test_classifier.py::test_classifier_timeout",
    "tests/unit/test_classifier.py::test_classifier_exception",
    "tests/unit/test_classifier.py::test_classifier_sync_unknown_intent",
    "tests/unit/test_config.py::test_config_missing_token",
    "tests/unit/test_config.py::test_config_defaults",
    "tests/unit/test_config.py::test_config_custom_values",
    "tests/unit/test_config.py::test_config_log_startup",
    "tests/unit/test_config.py::test_config_empty_allowed_ids",
    "tests/unit/test_config.py::test_config_log_startup_warning",
    "tests/unit/test_downloader.py::test_ytdlp_logger",
    "tests/unit/test_downloader.py::test_ytdlp_progress_hook",
    "tests/unit/test_downloader.py::test_ytdlp_download_sync_success",
    "tests/unit/test_downloader.py::test_ytdlp_download_sync_fail",
    "tests/unit/test_downloader.py::test_ytdlp_download_sync_exception",
    "tests/unit/test_downloader.py::test_ytdlp_get_info_sync",
    "tests/unit/test_downloader.py::test_ytdlp_progress_hook_various_branches",
    "tests/unit/test_downloader.py::test_ytdlp_download_audio_sync_fail",
    "tests/unit/test_downloader.py::test_ytdlp_download_audio_sync_exception",
    "tests/unit/test_ffmpeg.py::test_ffmpeg_binary",
    "tests/unit/test_ffmpeg.py::test_ffmpeg_get_duration_real",
    "tests/unit/test_ffmpeg.py::test_ffmpeg_get_video_bitrate_real",
    "tests/unit/test_ffmpeg.py::test_ffmpeg_get_duration_success",
    "tests/unit/test_ffmpeg.py::test_ffmpeg_get_duration_failure",
    "tests/unit/test_ffmpeg.py::test_ffmpeg_get_duration_exception",
    "tests/unit/test_ffmpeg.py::test_ffmpeg_get_video_bitrate_success",
    "tests/unit/test_ffmpeg.py::test_ffmpeg_get_video_bitrate_failure",
    "tests/unit/test_ffmpeg.py::test_ffmpeg_compress_to_size_various_branches",
    "tests/unit/test_ffmpeg.py::test_ffmpeg_compress_to_size_win32_cancellation",
    "tests/unit/test_ffmpeg.py::test_ffmpeg_compress_to_size_cancellation_timeout",
    "tests/unit/test_ffmpeg.py::test_ffmpeg_compress_to_size_audio_retry",
    "tests/unit/test_ffmpeg.py::test_ffmpeg_compress_to_size_launch_fail",
    "tests/unit/test_ffmpeg.py::test_splitter_get_keyframes_success",
    "tests/unit/test_ffmpeg.py::test_splitter_get_keyframes_failure",
    "tests/unit/test_ffmpeg.py::test_splitter_split_video_no_action_needed",
    "tests/unit/test_ffmpeg.py::test_splitter_split_video_success",
    "tests/unit/test_ffmpeg.py::test_splitter_split_video_no_keyframes_fallback",
    "tests/unit/test_ffmpeg.py::test_splitter_split_video_cancellation",
    "tests/unit/test_ffmpeg.py::test_splitter_split_video_ffmpeg_fail",
    "tests/unit/test_handlers.py::test_download_handler_url_missing",
    "tests/unit/test_handlers.py::test_download_handler_info_fail",
    "tests/unit/test_handlers.py::test_download_handler_pivot_to_report",
    "tests/unit/test_handlers.py::test_download_handler_full_success",
    "tests/unit/test_handlers.py::test_download_handler_over_limit_compress",
    "tests/unit/test_handlers.py::test_audio_handler_full_path",
    "tests/unit/test_handlers.py::test_bot_router_routing",
    "tests/unit/test_handlers.py::test_bot_router_unauthorized",
    "tests/unit/test_handlers.py::test_download_handler_over_limit_split",
    "tests/unit/test_handlers.py::test_bot_router_callback_exhaustive",
    "tests/unit/test_handlers.py::test_bot_router_handle_message_no_text",
    "tests/unit/test_handlers.py::test_bot_router_no_handler",
    "tests/unit/test_handlers.py::test_bot_router_handle_cancel_empty",
    "tests/unit/test_handlers.py::test_track_untrack_process",
    "tests/unit/test_handlers.py::test_cleanup_cache_task",
    "tests/unit/test_handlers.py::test_report_handler_no_url",
    "tests/unit/test_handlers.py::test_report_handler_info_fail",
    "tests/unit/test_handlers.py::test_report_handler_success_no_rec",
    "tests/unit/test_handlers.py::test_report_handler_rec_compress",
    "tests/unit/test_handlers.py::test_report_handler_rec_split",
    "tests/unit/test_handlers.py::test_downloader_progress_hook_cancel",
    "tests/unit/test_handlers.py::test_downloader_progress_hook_status",
    "tests/unit/test_handlers.py::test_downloader_download_sync_exception",
    "tests/unit/test_handlers.py::test_downloader_download_audio_sync_exception",
    "tests/unit/test_handlers.py::test_bot_router_handle_cancel_windows",
    "tests/unit/test_handlers.py::test_bot_router_handle_cancel_linux",
    "tests/unit/test_handlers.py::test_bot_router_callback_expired",
    "tests/unit/test_handlers.py::test_error_handler",
    "tests/unit/test_utils.py::test_markdown_escape",
    "tests/unit/test_utils.py::test_process_tracking",
    "tests/unit/test_utils.py::test_ollama_relay_no_socket",
    "tests/unit/test_utils.py::test_unix_to_tcp_relay_fail",
]

results = []
for test in tests:
    start_time = time.time()
    try:
        # Use pytest-timeout for internal timeout, and subprocess timeout for safety.
        process = subprocess.run(
            [sys.executable, "-m", "pytest", test, "-t", "30"],
            capture_output=True,
            text=True,
            timeout=35
        )
        duration = time.time() - start_time
        
        # Check for success or specific failure types
        if process.returncode == 0:
            status = "PASS"
        elif "Timeout" in process.stdout or "Timeout" in process.stderr:
            status = "TIMEOUT"
        else:
            status = "FAIL"
            
        results.append(f"{test} | {duration:.2f}s | {status}")
    except subprocess.TimeoutExpired:
        duration = time.time() - start_time
        results.append(f"{test} | {duration:.2f}s | TIMEOUT")
    except Exception as e:
        results.append(f"{test} | ERROR | {str(e)}")

print("\n".join(results))
