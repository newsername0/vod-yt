import requests

INPUT_FILE = "playlist_source.m3u"
OUTPUT_FILE = "playlist.m3u"
FIRST_PREFIX = "https://chromecast.cvattv.com.ar/live/"
SECOND_PREFIX = "https://cdn.cvattv.com.ar/live/"

with open(INPUT_FILE, "r", encoding="utf-8") as input_file, open(OUTPUT_FILE, "w", encoding="utf-8") as output_file:
    for line in input_file:
        url = line.rstrip("\n")

        if url.startswith((FIRST_PREFIX, SECOND_PREFIX)):
            response = requests.head(url, allow_redirects=False)

            if "Location" in response.headers:
                url = response.headers["Location"]

        output_file.write(url + "\n")

print("Done")
