#!/usr/bin/env sh
# Turn the recorded segments into README GIFs: sh docs/capture/gifs.sh <capture-out-dir> docs/img [prefix] [width]
# For a desktop run (PREFIX=mac- in capture.mjs): sh docs/capture/gifs.sh <dir> docs/img mac- 800
set -e
cap=$1; out=$2; prefix=${3:-}; width=${4:-360}
for name in crawl ab; do
  # skip the page load at the start of each segment
  ffmpeg -loglevel error -y -ss 0.9 -i "$cap/$prefix$name.webm" -vf \
    "fps=12,scale=$width:-1:flags=lanczos,split[a][b];[a]palettegen=max_colors=48:stats_mode=diff[p];[b][p]paletteuse=dither=none:diff_mode=rectangle" \
    "$out/$prefix$name.gif"
done
