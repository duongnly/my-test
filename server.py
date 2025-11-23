import os
import time
from datetime import datetime, timedelta
from typing import Optional
from email.utils import formatdate

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import Response as HttpResponse
from cachetools import TTLCache

from easynews_client import AsyncEasynewsClient, SearchItem

# --- Configuration ---
EASYNEWS_USER = os.getenv("EASYNEWS_USER")
EASYNEWS_PASS = os.getenv("EASYNEWS_PASS")
# Fix: Support both names, prefer NEWZNAB_APIKEY as it's standard
API_KEY = os.getenv("NEWZNAB_APIKEY") or os.getenv("API_KEY", "") 
PORT = int(os.getenv("PORT", 8081))

if not EASYNEWS_USER or not EASYNEWS_PASS:
    raise ValueError("EASYNEWS_USER and EASYNEWS_PASS environment variables are required.")

# --- App Setup ---
app = FastAPI(title="Easynews Indexer Bridge", version="2.1.0")
client = AsyncEasynewsClient(EASYNEWS_USER, EASYNEWS_PASS)

# Cache: Stores search results for 10 minutes
search_cache = TTLCache(maxsize=100, ttl=600)

# --- Helper: XML Generators ---
def generate_caps_xml():
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

def generate_rss_xml(items: list[SearchItem], base_url: str, pass_key: str):
    """Generates Newznab-compliant RSS XML."""
    xml_items = []
    for item in items:
        full_name = f"{item.filename}.{item.ext}"
        dl_id = f"{item.hash}|{item.filename}|{item.ext}"
        pub_date = formatdate(time.mktime(datetime.now().timetuple()))
        
        # FIX: Use the dynamic base_url passed from the request
        link = f"{base_url}/api?t=get&amp;id={dl_id}&amp;apikey={pass_key}"
        
        xml_items.append(f"""
        <item>
            <title>{full_name}</title>
            <guid isPermaLink="false">{item.hash}</guid>
            <link>{link}</link>
            <comments>{full_name}</comments>
            <pubDate>{pub_date}</pubDate>
            <category>Movies</category>
            <category>TV</category>
            <enclosure url="{link}" length="{item.size}" type="application/x-nzb" />
            <newznab:attr name="size" value="{item.size}"/>
            <newznab:attr name="category" value="2000"/>
            <newznab:attr name="category" value="5000"/>
        </item>""")
    
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:atom="http://www.w3.org/2005/Atom" xmlns:newznab="http://www.newznab.com/DTD/2010/feeds/attributes/">
  <channel>
    <title>Easynews Indexer</title>
    <description>Easynews Search Results</description>
    <link>{base_url}</link>
    <atom:link href="{base_url}/api" rel="self" type="application/rss+xml" />
    {''.join(xml_items)}
  </channel>
</rss>"""

# --- Routes ---

@app.get("/health")
async def health_check():
    return {"status": "ok", "timestamp": datetime.now().isoformat()}

@app.get("/api")
async def api_handler(
    request: Request,
    t: str = Query(..., description="Function type (caps, search, tvsearch, movie, get)"),
    q: Optional[str] = None,
    apikey: Optional[str] = None,
    id: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
):
    # 1. Security Check
    if API_KEY and apikey != API_KEY:
        raise HTTPException(status_code=403, detail="Invalid API Key")

    # 2. Capabilities
    if t == "caps":
        return HttpResponse(content=generate_caps_xml(), media_type="application/xml")

    # 3. Download (Get NZB)
    if t == "get":
        if not id:
            raise HTTPException(status_code=400, detail="Missing ID")
        
        try:
            # Decode ID: hash|filename|ext
            print(f"[DEBUG] Received download request for ID: {id}")
            
            parts = id.split("|")
            if len(parts) < 3:
                raise ValueError(f"Invalid ID format: {id}")
            
            hash_id = parts[0]
            fname = parts[1]
            ext = parts[2]
            
            print(f"[DEBUG] Decoded - Hash: {hash_id}, Filename: {fname}, Ext: {ext}")
            
            item = SearchItem(id=hash_id, hash=hash_id, filename=fname, ext=ext, sig=None, type="VIDEO")
            
            print(f"[DEBUG] Calling Easynews get_nzb for: {fname}.{ext}")
            nzb_content = await client.get_nzb(item, nzb_name=f"{fname}.{ext}")
            
            print(f"[DEBUG] Successfully generated NZB, size: {len(nzb_content)} bytes")
            
            return HttpResponse(
                content=nzb_content,
                media_type="application/x-nzb",
                headers={"Content-Disposition": f'attachment; filename="{fname}.nzb"'}
            )
        except Exception as e:
            import traceback
            error_details = traceback.format_exc()
            print(f"[ERROR] Failed to generate NZB:")
            print(error_details)
            raise HTTPException(status_code=500, detail=f"Failed to generate NZB: {str(e)}")

    # 4. Search
    if t in ["search", "tvsearch", "movie"]:
        query = q if q else ""
        cache_key = f"{query}_{limit}_{offset}"
        
        # FIX: Detect the actual scheme and host (http vs https, domain name)
        # This ensures links in the RSS feed match your public domain
        scheme = request.headers.get("x-forwarded-proto", request.url.scheme)
        base_url = f"{scheme}://{request.headers.get('host', 'localhost:8081')}"

        if cache_key in search_cache:
            print(f"Serving cached result for: {query}")
            return HttpResponse(content=search_cache[cache_key], media_type="application/xml")

        if not query:
             empty_xml = generate_rss_xml([], base_url, apikey or "")
             return HttpResponse(content=empty_xml, media_type="application/xml")

        try:
            data = await client.search(query, per_page=limit)
            items = client.parse_results(data)
            xml_output = generate_rss_xml(items, base_url, apikey or "")
            search_cache[cache_key] = xml_output
            return HttpResponse(content=xml_output, media_type="application/xml")
            
        except Exception as e:
            print(f"Search failed: {e}")
            return HttpResponse(
                content=generate_rss_xml([], base_url, apikey or ""), 
                media_type="application/xml"
            )

    raise HTTPException(status_code=400, detail="Unknown function type")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=PORT)
