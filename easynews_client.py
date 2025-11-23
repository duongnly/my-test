import base64
import logging
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

# Logger setup
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("easynews_client")

EASYNEWS_BASE = "https://members.easynews.com"

class EasynewsError(Exception):
    pass

def parse_size_to_bytes(size_str: str) -> int:
    """Convert human-readable size (e.g., '2.4 GB') to bytes."""
    if not size_str:
        return 0
    
    size_str = size_str.strip().upper()
    match = re.match(r'([\d.]+)\s*([KMGT]?B)', size_str)
    if not match:
        return 0
    
    value = float(match.group(1))
    unit = match.group(2)
    
    multipliers = {
        'B': 1,
        'KB': 1024,
        'MB': 1024**2,
        'GB': 1024**3,
        'TB': 1024**4
    }
    
    return int(value * multipliers.get(unit, 1))

@dataclass
class SearchItem:
    id: Optional[str]
    hash: str
    filename: str
    ext: str
    sig: Optional[str]
    type: str
    size: int = 0
    raw: Dict[str, Any] = None

    @property
    def value_token(self) -> str:
        """Format required for Easynews DL generation"""
        fn_b64 = base64.b64encode(self.filename.encode()).decode().replace("=", "")
        ext_b64 = base64.b64encode(self.ext.encode()).decode().replace("=", "")
        return f"{self.hash}|{fn_b64}:{ext_b64}"

class AsyncEasynewsClient:
    def __init__(self, username: str, password: str):
        self.username = username
        self.password = password
        self.auth = httpx.BasicAuth(username, password)
        self.headers = {
            "User-Agent": "Mozilla/5.0 (Compatible; HighSeasIndexer/2.0)",
            "Accept": "application/json, text/javascript, */*; q=0.9",
        }

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        retry=retry_if_exception_type(httpx.RequestError)
    )
    async def verify_credentials(self):
        """Checks if creds work by hitting the root endpoint."""
        async with httpx.AsyncClient(auth=self.auth, headers=self.headers, timeout=10) as client:
            resp = await client.get(f"{EASYNEWS_BASE}/2.0/")
            if resp.status_code in (401, 403):
                raise EasynewsError("Unauthorized: Check your username and password.")
            resp.raise_for_status()
            logger.info("Easynews credentials verified successfully.")

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        retry=retry_if_exception_type((httpx.ConnectError, httpx.ReadTimeout))
    )
    async def search(
        self,
        query: str,
        file_type: str = "VIDEO",
        page: int = 1,
        per_page: int = 50
    ) -> Dict[str, Any]:
        """Performs an async search against the Solr backend."""
        
        if file_type not in ["VIDEO", "AUDIO", "IMAGE", "ARCHIVE"]:
            file_type = "VIDEO"

        params = {
            "fly": "2",
            "sb": "1",
            "pno": str(page),
            "pby": str(per_page),
            "u": "1",
            "chxu": "1",
            "chxgx": "1",
            "st": "basic",
            "gps": query,
            "vv": "1",
            "safeO": "0",
            "s1": "dtime",
            "s1d": "-"
        }

        url = f"{EASYNEWS_BASE}/2.0/search/solr-search/"
        query_string = "&".join([f"{k}={v}" for k, v in params.items()])
        query_string += f"&fty%5B%5D={file_type}" 

        async with httpx.AsyncClient(auth=self.auth, headers=self.headers, timeout=15) as client:
            resp = await client.get(f"{url}?{query_string}")
            resp.raise_for_status()
            return resp.json()

    def parse_results(self, json_data: Dict[str, Any]) -> List[SearchItem]:
        """Parses raw JSON into structured SearchItems."""
        items = []
        for row in json_data.get("data", []):
            hash_id, filename, ext, size, sig = "", "", "", 0, None
            
            if isinstance(row, list) and len(row) > 12:
                hash_id = row[0]
                size = parse_size_to_bytes(str(row[4])) if row[4] else 0
                filename = row[10]
                ext = row[11]
            elif isinstance(row, dict):
                hash_id = row.get("0", "")
                filename = row.get("10", "")
                ext = row.get("11", "")
                size = parse_size_to_bytes(str(row.get("4", "0")))
                sig = row.get("sig")
            
            if hash_id and filename:
                items.append(SearchItem(
                    id=hash_id,
                    hash=hash_id,
                    filename=filename,
                    ext=ext,
                    sig=sig,
                    type="VIDEO",
                    size=size,
                    raw=row if isinstance(row, dict) else {}
                ))
        return items

    async def get_nzb(self, item: SearchItem, nzb_name: str) -> bytes:
        """Generates and downloads the NZB file content."""
        url = f"{EASYNEWS_BASE}/2.0/api/dl-nzb"
        
        payload = {
            "autoNZB": "1",
            "0": item.value_token,
            "nameZipQ0": nzb_name
        }
        
        logger.info(f"[DEBUG] Requesting NZB from Easynews: {nzb_name}")
        logger.info(f"[DEBUG] Value token: {item.value_token}")
        logger.info(f"[DEBUG] Payload: {payload}")

        try:
            async with httpx.AsyncClient(auth=self.auth, headers=self.headers, timeout=20) as client:
                resp = await client.post(url, data=payload)
                
                logger.info(f"[DEBUG] Easynews response status: {resp.status_code}")
                logger.info(f"[DEBUG] Response headers: {dict(resp.headers)}")
                
                resp.raise_for_status()
                
                content = resp.content.replace(b'date=""', b'date="0"')
                
                logger.info(f"[DEBUG] NZB content size: {len(content)} bytes")
                return content
        except httpx.HTTPStatusError as e:
            logger.error(f"[ERROR] Easynews returned error: {e.response.status_code}")
            logger.error(f"[ERROR] Response body: {e.response.text}")
            raise
        except Exception as e:
            logger.error(f"[ERROR] Failed to get NZB: {str(e)}")
            raise
