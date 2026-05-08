#!/bin/bash
docker build -t riebot-tg:latest .
socat UNIX-LISTEN:`pwd`/ollama.sock,fork,mode=600,unlink-early TCP:127.0.0.1:11434 &
docker run --env-file ./.env -d --device /dev/dri/renderD128:/dev/dri/renderD128 -v `pwd`/ollama.sock:/run/ollama.sock --restart=unless-stopped --name riebot-tg riebot-tg
docker exec --user root riebot-tg /bin/bash -c 'chmod 777 /var/run/ollama.sock; chmod 666 /dev/dri/renderD128; stat /var/run/ollama.sock'
#docker run --env-file ./.env -v `pwd`/ollama.sock:/run/ollama.sock -it --name riebot-tg riebot-tg
#docker run --rm --env-file ./.env -v `pwd`/ollama.sock:/run/ollama.sock -it --name riebot-tg riebot-tg
