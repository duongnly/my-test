import os
import time
from datetime import datetime, timedelta
from typing import Optional
from email.utils import formatdate

from fastapi import FastAPI, HTTPException, Query, Response
from fastapi.responses import Response as HttpResponse
from cachetools import TTLCache

from easynews_client import AsyncEasynewsClient, SearchItem

# --- Configuration ---
EASYNEWS_USER = os.getenv("EASYNEWS_USER")
EASYNEWS_PASS = os.getenv("EASYNEWS_PASS")
API_KEY = os.getenv("API_KEY", "") # Optional protection for this bridge
PORT = int(os.getenv("PORT", 8081))

if not EASYNEWS_USER or not EASYNEWS_PASS:
    raise ValueError("EASYNEWS_USER and EASYNEWS_PASS environment variables are required.")

# --- App Setup ---
app = FastAPI(title="Easynews Indexer Bridge", version="2.0.0")
client = AsyncEasynewsClient(EASYNEWS_USER, EASYNEWS_PASS)

# Cache: Stores search results for 10 minutes to prevent ban/rate limits
# Key: query string, Value: XML string
search_cache = TTLCache(maxsize=100, ttl=600)

# --- Helper: XML Generators ---
def generate_caps_xml():
    """Returns the capabilities XML for Sonarr/Radarr."""
    return """<?xml version="1.0" encoding="UTF-8"?>
<caps>
  <server version="1.0" title="Easynews" strapline="Easynews Bridge"/>
  <limits max="100" default="50"/>
  <retention days="20000"/>
  <registration available="no" open="no"/>
  <searching>
    <search available="yes" supportedParams="q"/>
    <tv-search available="yes" supportedParams="q,season,ep"/>
    <movie-search available="yes" supportedParams="q,imdbid"/>
  </searching>
  <categories>
    <category id="2000" name="Movies"/>
    <category id="5000" name="TV"/>
  </categories>
</caps>"""

def generate_rss_xml(items: list[SearchItem], api_url: str, pass_key: str):
    """Generates Newznab-compliant RSS XML."""
    xml_items = []
    for item in items:
        # Construct a title
        full_name = f"{item.filename}.{item.ext}"
        
        # Generate the download link (points back to our bridge)
        # We encode the necessary data into the ID param
        # Format: hash|filename|ext
        dl_id = f"{item.hash}|{item.filename}|{item.ext}"
        
        # RFC822 Date (Mocking current time as we don't scrape exact date easily)
        # In a real scenario, parsing the date from 'raw' is better
        pub_date = formatdate(time.mktime(datetime.now().timetuple()))
        
        xml_items.append(f"""
        <item>
            <title>{full_name}</title>
            <guid isPermaLink="false">{item.hash}</guid>
            <link>{api_url}/api?t=get&amp;id={dl_id}&amp;apikey={pass_key}</link>
            <comments>{full_name}</comments>
            <pubDate>{pub_date}</pubDate>
            <category>Movies</category>
            <category>TV</category>
            <enclosure url="{api_url}/api?t=get&amp;id={dl_id}&amp;apikey={pass_key}" length="{item.size}" type="application/x-nzb" />
            <newznab:attr name="size" value="{item.size}"/>
            <newznab:attr name="category" value="2000"/>
            <newznab:attr name="category" value="5000"/>
        </item>""")
    
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:atom="http://www.w3.org/2005/Atom" xmlns:newznab="http://www.newznab.com/DTD/2010/feeds/attributes/">
  <channel>
    <title>Easynews Indexer</title>
    <description>Easynews Search Results</description>
    <link>{api_url}</link>
    <atom:link href="{api_url}/api" rel="self" type="application/rss+xml" />
    {''.join(xml_items)}
  </channel>
</rss>"""

# --- Routes ---

@app.get("/health")
async def health_check():
    return {"status": "ok", "timestamp": datetime.now().isoformat()}

@app.get("/api")
async def api_handler(
    t: str = Query(..., description="Function type (caps, search, tvsearch, movie, get)"),
    q: Optional[str] = None,
    apikey: Optional[str] = None,
    id: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
    cat: Optional[str] = None,
    request_url: str = "" # Injected by logic below
):
    # 1. Security Check (if API_KEY is set env var)
    if API_KEY and apikey != API_KEY:
        raise HTTPException(status_code=403, detail="Invalid API Key")

    # 2. Capabilities Request
    if t == "caps":
        return HttpResponse(content=generate_caps_xml(), media_type="application/xml")

    # 3. Download Request (Get NZB)
    if t == "get":
        if not id:
            raise HTTPException(status_code=400, detail="Missing ID")
        
        # Decode ID: hash|filename|ext
        try:
            hash_id, fname, ext = id.split("|")
            item = SearchItem(id=hash_id, hash=hash_id, filename=fname, ext=ext, sig=None, type="VIDEO")
            
            nzb_content = await client.get_nzb(item, nzb_name=f"{fname}.{ext}")
            
            return HttpResponse(
                content=nzb_content,
                media_type="application/x-nzb",
                headers={"Content-Disposition": f'attachment; filename="{fname}.nzb"'}
            )
        except Exception as e:
            print(f"Error generating NZB: {e}")
            raise HTTPException(status_code=500, detail="Failed to generate NZB")

    # 4. Search Request
    if t in ["search", "tvsearch", "movie"]:
        query = q if q else ""
        
        # Cache Check
        cache_key = f"{query}_{limit}_{offset}"
        if cache_key in search_cache:
            print(f"Serving cached result for: {query}")
            return HttpResponse(content=search_cache[cache_key], media_type="application/xml")

        if not query:
             # Return empty RSS if no query, standard behavior
             empty_xml = generate_rss_xml([], "http://localhost:8081", apikey or "")
             return HttpResponse(content=empty_xml, media_type="application/xml")

        try:
            # Determine base URL for the links in XML
            # In production behind reverse proxy, this should be configured or inferred
            base_url = "http://localhost:8081" 
            # If we are inside docker, the client (Prowlarr) might see us differently, 
            # but usually, the user configures Prowlarr with the correct URL.
            
            data = await client.search(query, per_page=limit)
            items = client.parse_results(data)
            
            xml_output = generate_rss_xml(items, base_url, apikey or "")
            
            # Save to cache
            search_cache[cache_key] = xml_output
            
            return HttpResponse(content=xml_output, media_type="application/xml")
            
        except Exception as e:
            print(f"Search failed: {e}")
            # Return empty result on failure to avoid breaking Prowlarr completely
            return HttpResponse(
                content=generate_rss_xml([], "http://localhost:8081", apikey or ""), 
                media_type="application/xml"
            )

    raise HTTPException(status_code=400, detail="Unknown function type")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=PORT)
