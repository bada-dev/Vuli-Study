# VuliStudy

## No permission is granted to use, copy, modify, distribute, or reproduce this software or any part of it without explicit written permission from the author.

VuliStudy is a simple productivity app to manage tasks, notes, and schedules in one place.
It includes numerous major systems, like a fully functional shop, coin/currency system, happiness and food system, calenders, emotes, custom backgrounds, settings section, task/checklist and so, so much more.

```
import yt_dlp
import os

def download_playlist(playlist_url, output_path="downloads"):
    os.makedirs(output_path, exist_ok=True)
    
    ydl_opts = {
        'outtmpl': f'{output_path}/%(playlist_title)s/%(title)s.%(ext)s',
        'format': 'bestvideo+bestaudio/best',
        'merge_output_format': 'mp4',
        'ignoreerrors': True,
        'quiet': False,
        'no_warnings': False,
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([playlist_url])
        print("download complete")
    except Exception as e:
        print(f"bun u herees a big ass error: {e}")

if __name__ == "__main__":
    url = input("Playlist URL: ")
    download_playlist(url)

```
