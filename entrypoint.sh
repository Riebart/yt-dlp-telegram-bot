#!/bin/sh

socat TCP-LISTEN:${OLLAMA_LISTEN_TCP_PORT},bind=${OLLAMA_LISTEN_TCP_HOST},reuseaddr,fork UNIX-CONNECT:${OLLAMA_UNIX_SOCK} &
/app/venv/bin/python bot.py
