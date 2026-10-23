#!/usr/bin/env sh
# Cut the capture video into README GIFs: sh docs/capture/gifs.sh <capture-out-dir> docs/img
set -e
cap=$1; out=$2
get() { python3 -c "import json,sys; print(json.load(open('$cap/marks.json'))['$1'])"; }
video=$(get video)
gif() {  # name start end
  ffmpeg -loglevel error -y -ss "$2" -to "$3" -i "$video" -vf \
    "fps=12,scale=360:-1:flags=lanczos,split[a][b];[a]palettegen=max_colors=48:stats_mode=diff[p];[b][p]paletteuse=dither=none:diff_mode=rectangle" \
    "$out/$1.gif"
}
gif crawl "$(get crawl_start)" "$(get crawl_end)"
gif ab "$(get ab_start)" "$(get ab_end)"
