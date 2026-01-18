import io
import json
import os
import requests
import urllib.parse
import xml.etree.ElementTree as ET
from bs4 import BeautifulSoup
from typing import Protocol, cast, Any, Optional
from PIL import Image
from dotenv import load_dotenv

# --- 1. CONFIGURATION ---
USERNAME = os.getenv("LETTERBOXD_USERNAME", "")
TMDB_API_KEY = os.getenv("TMDB_API_KEY", "")
RSS_URL = f"https://letterboxd.com/{USERNAME}/rss/"

# --- 2. PROTOCOL & HARDWARE ---
class InkyBoard(Protocol):
    resolution: tuple[int, int]
    def set_image(self, image: Image.Image, saturation: float = 0.5) -> None: ...
    def show(self) -> None: ...

class MockBoard:
    resolution: tuple[int, int]
    def __init__(self) -> None: self.resolution = (1600, 1200)
    def set_image(self, image: Image.Image, saturation: float = 0.5) -> None:
        print(f"Mock: Processing image... (Sat: {saturation})")
        image.save("letterboxd_bulletproof.jpg")
    def show(self) -> None: print("Mock: Display command sent!")

board: InkyBoard
try:
    from inky.auto import auto  # pyright: ignore[reportMissingImports, reportUnknownVariableType]
    board = cast(InkyBoard, auto())
    print("✅ Hardware found")
except ImportError:
    board = MockBoard()
    print("⚠️ Mock loaded")

# --- 3. SOURCES ---

def get_poster_from_tmdb(title: str, year: str) -> str:
    if not TMDB_API_KEY:
        raise ValueError("TMDB API Key not configured")

    print(f"   (Searching TMDB for '{title} {year}')...")
    url = f"https://api.themoviedb.org/3/search/movie?query={urllib.parse.quote(title)}&year={year}"
    headers = {
        "Authorization": f"Bearer {TMDB_API_KEY}",
        "accept": "application/json"
    }
    
    resp = requests.get(url, headers=headers)
    resp.raise_for_status()
    data = resp.json()
    
    if not data["results"]:
        raise ValueError("No results on TMDB")
        
    poster_path = data["results"][0].get("poster_path")
    if not poster_path:
        raise ValueError("TMDB result has no poster path")
        
    # 'original' gives the highest resolution available
    return f"https://image.tmdb.org/t/p/original{poster_path}"

def get_poster_from_itunes(title: str, year: str) -> str:
    print(f"   (Searching iTunes for '{title}')...")
    params = {"term": title, "media": "movie", "limit": 5}
    resp = requests.get("https://itunes.apple.com/search", params=params, timeout=10)
    resp.raise_for_status()
    data = resp.json()

    if data["resultCount"] == 0:
        raise ValueError("No results found on iTunes")

    target_year = int(year)
    best_match: Optional[dict[str, Any]] = None
    
    for result in data["results"]:
        release_date = result.get("releaseDate", "")
        if release_date:
            res_year = int(release_date.split("-")[0])
            if abs(res_year - target_year) <= 1:
                best_match = result
                break
    
    if not best_match:
        # Relax year constraint if exact match fails
        best_match = data["results"][0]

    thumb_url = best_match.get("artworkUrl100")
    if not thumb_url:
        raise ValueError("iTunes result has no artwork")

    return thumb_url.replace("100x100bb", "1000x1500bb")

# --- 4. CONTROLLER ---
def get_poster_url(movie_link: str, title: str, year: str) -> str:
    # 1. Try TMDB (Best Quality, if key exists)
    if TMDB_API_KEY:
        try:
            return get_poster_from_tmdb(title, year)
        except Exception as e:
            print(f"   (TMDB failed: {e})")

    # 2. Try iTunes (Good Quality, Public)
    try:
        return get_poster_from_itunes(title, year)
    except Exception as e:
        print(f"   (iTunes failed: {e})")
    
    # 3. Try Letterboxd JSON-LD (Last Resort)
    try:
        headers = {'User-Agent': 'Mozilla/5.0'}
        page_resp = requests.get(movie_link, headers=headers, timeout=10)
        soup = BeautifulSoup(page_resp.content, "html.parser")
        script = soup.find("script", type="application/ld+json")
        if script:
            json_text = script.get_text(strip=True)
            if json_text:
                data = json.loads(json_text)
                if "image" in data and "empty-poster" not in data["image"]:
                    print("   (Found High-Res via Letterboxd JSON)")
                    return data["image"]
    except Exception as e:
        print(f"   (Letterboxd scrape failed: {e})")

    raise ValueError("Exhausted all sources. No poster found.")

# --- 5. LOGIC ---
def get_latest_movie_poster() -> Image.Image:
    headers = {'User-Agent': 'Mozilla/5.0'}
    print(f"1. Fetching RSS for {USERNAME}...")
    rss_resp = requests.get(str(RSS_URL), headers=headers)
    rss_resp.raise_for_status()
    
    root = ET.fromstring(rss_resp.content)
    first_item = root.find(".//item")
    if first_item is None: raise ValueError("RSS empty")
    
    link_node = first_item.find("link")
    title_node = first_item.find("title")
    
    if link_node is None or not link_node.text: raise ValueError("Missing link")
    if title_node is None or not title_node.text: raise ValueError("Missing title")

    movie_link = link_node.text
    full_title = title_node.text
    
    try:
        parts = full_title.split(", ")
        clean_title = parts[0]
        year_part = parts[1]
        clean_year = year_part[:4]
    except IndexError:
        clean_title = full_title
        clean_year = "2024"

    print(f"2. Found: {clean_title} ({clean_year})")

    poster_url = get_poster_url(movie_link, clean_title, clean_year)
    
    print(f"3. Downloading: {poster_url}")
    img_resp = requests.get(poster_url, headers=headers)
    return Image.open(io.BytesIO(img_resp.content))

# --- 6. COMPOSITION ---
def create_portrait_composition(poster: Image.Image) -> Image.Image:
    target_w, target_h = (1200, 1600)
    canvas = Image.new("RGB", (target_w, target_h), color=(0, 0, 0))
    
    aspect = poster.width / poster.height
    new_h = target_h
    new_w = int(new_h * aspect)
    
    if new_w > target_w:
        new_w = target_w
        new_h = int(new_w / aspect)

    # pyright: ignore[reportUnknownMemberType]
    resized_poster = poster.resize((new_w, new_h), resample=Image.Resampling.LANCZOS) 
    
    x_pos = (target_w - new_w) // 2
    y_pos = (target_h - new_h) // 2
    
    canvas.paste(resized_poster, (x_pos, y_pos))
    return canvas

# --- 7. MAIN ---
def main() -> None:
    try:
        poster = get_latest_movie_poster()
        print("4. Composing & Displaying...")
        portrait_image = create_portrait_composition(poster)
        final_image = portrait_image.rotate(90, expand=True)
        _ = board.set_image(final_image, saturation=0.6)
        _ = board.show()
        print("Done!")
    except Exception as e:
        print(f"❌ Error: {e}")

if __name__ == "__main__":
    main()
