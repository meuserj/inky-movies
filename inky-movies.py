# pyright: reportUnknownVariableType=false, reportUnknownMemberType=false, reportUnknownArgumentType=false
import io
import json
import os
import time
import glob
import requests
import urllib.parse
import xml.etree.ElementTree as ET
from bs4 import BeautifulSoup
from typing import Protocol, cast, NamedTuple
from PIL import Image
from dotenv import load_dotenv

# --- 1. CONFIGURATION ---
_ = load_dotenv()
USERNAME = os.getenv("LETTERBOXD_USERNAME", "")
TMDB_API_KEY = os.getenv("TMDB_API_KEY", "")
RSS_URL = f"https://letterboxd.com/{USERNAME}/rss/"

# Settings
CACHE_DIR = "poster_cache"
MAX_MOVIES = 20
SLIDE_DURATION = 3600  # How long to show each movie (in seconds). Default: 1 hour.
RSS_REFRESH_INTERVAL = 4 # How many slides to show before checking RSS for updates.

# --- 2. DATA STRUCTURES ---
class Movie(NamedTuple):
    title: str
    year: str
    link: str
    cache_filename: str

# --- 3. HARDWARE ---
class InkyBoard(Protocol):
    resolution: tuple[int, int]
    def set_image(self, image: Image.Image, saturation: float = 0.5) -> None: ...
    def show(self) -> None: ...

class MockBoard:
    resolution: tuple[int, int]
    def __init__(self) -> None: self.resolution = (1600, 1200)
    def set_image(self, image: Image.Image, saturation: float = 0.5) -> None:
        print(f"   [Mock Display] Showing image... (Sat: {saturation})")
        image.save("mock_output.jpg")
    def show(self) -> None: pass

board: InkyBoard
try:
    from inky.auto import auto  # pyright: ignore[reportMissingImports]
    board = cast(InkyBoard, auto())
    print("✅ Hardware found")
except ImportError:
    board = MockBoard()
    print("⚠️ Mock loaded")

# --- 4. POSTER FINDER LOGIC (Reuse from before) ---
def get_poster_from_tmdb(title: str, year: str) -> str:
    if not TMDB_API_KEY: raise ValueError("No TMDB Key")
    url = f"https://api.themoviedb.org/3/search/movie?query={urllib.parse.quote(title)}&year={year}"
    headers = {"Authorization": f"Bearer {TMDB_API_KEY}", "accept": "application/json"}
    resp = requests.get(url, headers=headers)
    resp.raise_for_status()
    data = resp.json()
    if not data.get("results"): raise ValueError("No results on TMDB")
    path = data["results"][0].get("poster_path")
    if not path: raise ValueError("No poster path")
    return f"https://image.tmdb.org/t/p/original{path}"

def get_poster_from_itunes(title: str, year: str) -> str:
    params = {"term": title, "media": "movie", "limit": 5}
    resp = requests.get("https://itunes.apple.com/search", params=params, timeout=10)
    data = resp.json()
    if data.get("resultCount") == 0: raise ValueError("No iTunes results")
    
    target_year = int(year)
    best_match = None
    results = data.get("results", [])
    
    for res in results:
        rd = res.get("releaseDate", "")
        if rd:
            try:
                if abs(int(rd.split("-")[0]) - target_year) <= 1:
                    best_match = res
                    break
            except: continue
    
    if not best_match and results: best_match = results[0]
    if not best_match: raise ValueError("No valid match")
    
    return best_match.get("artworkUrl100", "").replace("100x100bb", "1000x1500bb")

def resolve_poster_url(movie: Movie) -> str:
    # 1. TMDB
    if TMDB_API_KEY:
        try: return get_poster_from_tmdb(movie.title, movie.year)
        except: pass
    # 2. iTunes
    try: return get_poster_from_itunes(movie.title, movie.year)
    except: pass
    # 3. JSON-LD
    try:
        r = requests.get(movie.link, headers={'User-Agent': 'Mozilla/5.0'})
        soup = BeautifulSoup(r.content, "html.parser")
        s = soup.find("script", type="application/ld+json")
        if s and s.get_text(strip=True):
            d = json.loads(s.get_text(strip=True))
            if isinstance(d, list) and d: d = d[0]
            if isinstance(d, dict) and "image" in d and "empty-poster" not in d["image"]:
                return d["image"]
    except: pass
    raise ValueError(f"Could not find poster for {movie.title}")

# --- 5. SYNC & CACHE ---
def ensure_cache_dir():
    if not os.path.exists(CACHE_DIR):
        os.makedirs(CACHE_DIR)

def get_movie_list() -> list[Movie]:
    print("📡 Fetching RSS Feed...")
    try:
        r = requests.get(RSS_URL, headers={'User-Agent': 'Mozilla/5.0'})
        root = ET.fromstring(r.content)
        items = root.findall(".//item")
    except Exception as e:
        print(f"❌ RSS Error: {e}")
        return []

    movies: list[Movie] = []
    
    # Process only the newest N items
    for item in items[:MAX_MOVIES]:
        title_node = item.find("title")
        link_node = item.find("link")
        if title_node is None or link_node is None: continue
        
        full_title = title_node.text or ""
        link = link_node.text or ""
        
        # Parse Title/Year
        try:
            parts = full_title.split(", ")
            t = parts[0]
            y = parts[1][:4]
        except:
            t = full_title
            y = "2024"
        
        # Create safe filename: "Scars_of_Dracula_1970.jpg"
        safe_t = "".join([c if c.isalnum() else "_" for c in t])
        fname = f"{safe_t}_{y}.jpg"
        
        movies.append(Movie(t, y, link, fname))
    
    return movies

def sync_cache(movies: list[Movie]):
    """Downloads missing posters and deletes old ones."""
    ensure_cache_dir()
    
    # 1. Download missing
    for m in movies:
        local_path = os.path.join(CACHE_DIR, m.cache_filename)
        if not os.path.exists(local_path):
            print(f"⬇️ Downloading: {m.title}...")
            try:
                url = resolve_poster_url(m)
                img_data = requests.get(url).content
                # Validate it's an image before saving
                Image.open(io.BytesIO(img_data)).verify() 
                
                with open(local_path, "wb") as f:
                    f.write(img_data)
                print("   ✅ Saved.")
            except Exception as e:
                print(f"   ❌ Failed to download {m.title}: {e}")
    
    # 2. Garbage Collect (Delete files not in current list)
    valid_filenames = {m.cache_filename for m in movies}
    existing_files = glob.glob(os.path.join(CACHE_DIR, "*.jpg"))
    
    for f in existing_files:
        basename = os.path.basename(f)
        if basename not in valid_filenames:
            print(f"🗑️ Pruning old cache: {basename}")
            os.remove(f)

# --- 6. DISPLAY ---
def display_movie(movie: Movie):
    local_path = os.path.join(CACHE_DIR, movie.cache_filename)
    if not os.path.exists(local_path):
        print(f"⚠️ Skipped {movie.title} (Image missing)")
        return

    print(f"🎨 Displaying: {movie.title}")
    try:
        # Load and process
        img = Image.open(local_path)
        
        # Composition (Portrait -> Landscape w/ Rotation)
        target_w, target_h = (1200, 1600)
        bg = Image.new("RGB", (target_w, target_h), (0,0,0))
        
        # Fit logic
        aspect = img.width / img.height
        new_w = int(target_h * aspect)
        if new_w > target_w: new_w = target_w
        new_h = int(new_w / aspect)
        
        img = img.resize((new_w, new_h), Image.Resampling.LANCZOS)
        x = (target_w - new_w) // 2
        y = (target_h - new_h) // 2
        bg.paste(img, (x, y))
        
        # Rotate for hardware
        final = bg.rotate(90, expand=True)
        
        board.set_image(final, saturation=0.6)
        board.show()
        
    except Exception as e:
        print(f"❌ Display Error: {e}")

# --- 7. DAEMON LOOP (FLATTENED) ---
def main():
    print("🚀 Inky Movies Daemon Started")
    print(f"   Settings: {MAX_MOVIES} posters, {SLIDE_DURATION}s duration")
    
    playlist: list[Movie] = []
    current_index = 0
    
    # Track when we last checked for updates
    last_rss_check_time = 0.0
    # Check for new movies every 4 hours (regardless of how many slides shown)
    rss_check_interval = 4 * 3600  
    
    while True:
        try:
            now = time.time()
            
            # --- STEP 1: CHECK FOR UPDATES ---
            # We check if:
            # A) It's been > 4 hours since last check
            # B) OR the playlist is empty (first run)
            if (now - last_rss_check_time > rss_check_interval) or not playlist:
                print(f"⏰ Checking RSS for updates... (Last: {time.ctime(last_rss_check_time)})")
                new_list = get_movie_list()
                
                if new_list:
                    print(f"   ✅ Fetched {len(new_list)} movies.")
                    sync_cache(new_list)
                    playlist = new_list
                    last_rss_check_time = now
                    
                    # CRITICAL FIX:
                    # If we are currently at index 5, and the list refreshes, 
                    # we want to stay at index 5 (or 0 if the list shrank).
                    # We do NOT reset to 0 automatically.
                    if current_index >= len(playlist):
                        current_index = 0
                else:
                    print("   ⚠️ RSS fetch failed. Keeping existing playlist.")

            # --- STEP 2: HANDLE EMPTY PLAYLIST ---
            if not playlist:
                print("   💤 No movies found. Retrying in 5 minutes...")
                time.sleep(300)
                continue

            # --- STEP 3: DISPLAY CURRENT MOVIE ---
            current_movie = playlist[current_index]
            print(f"\n[{current_index + 1}/{len(playlist)}] Processing: {current_movie.title}")
            display_movie(current_movie)
            
            # --- STEP 4: ADVANCE INDEX ---
            # Move to the next movie. If at the end, wrap around to 0.
            current_index = (current_index + 1) % len(playlist)
            
            # --- STEP 5: SLEEP ---
            print(f"   💤 Sleeping for {SLIDE_DURATION} seconds...")
            time.sleep(SLIDE_DURATION)
            
        except KeyboardInterrupt:
            print("\n👋 Manual stop. Exiting.")
            break
        except Exception as e:
            print(f"❌ Error in loop: {e}")
            time.sleep(60)

if __name__ == "__main__":
    main()
