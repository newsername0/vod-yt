#!/usr/bin/env python3
import requests

INPUT = "playlist_desac.m3u"
OUTPUT = "playlist.m3u"


def obtener_location(url):
    r = requests.head(url, allow_redirects=False, timeout=10)
    return r.headers.get("Location", url)


with (
    open(INPUT, "r", encoding="utf-8") as infile,
    open(OUTPUT, "w", encoding="utf-8") as outfile,
):
    for line in infile:
        linea = line.rstrip("\n")
        if linea.startswith(
            ("https://chromecast.cvattv.com.ar/live/", "https://cdn.cvattv.com.ar/live/")
        ) and linea.endswith(".mpd"):
            outfile.write(obtener_location(linea) + "\n")
        else:
            outfile.write(line)
print("Listo.")
