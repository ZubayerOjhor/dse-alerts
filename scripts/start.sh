#!/bin/sh
# One container, both jobs: the scheduler in the background, the web UI in the foreground.
# This is what a single-machine host (Fly.io, a small VPS) runs; docker-compose.yml keeps
# the two as separate services instead.
set -u
sh "$(dirname "$0")/scheduler.sh" &
exec dse web serve
