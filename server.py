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
    <description
