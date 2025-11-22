import base64
import logging
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
        
        # Map 'TV' to VIDEO if requested, default logic
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
            "s1": "dtime", # Sort by time
            "s1d": "-"     # Descending
        }

        url = f"{EASYNEWS_BASE}/2.0/search/solr-search/"
        # Manually constructing query string to handle array param 'fty[]' correctly for Easynews
        # standard httpx params might encode it differently than the legacy site expects
        query_string = "&".join([f"{k}={v}" for k, v in params.items()])
        query_string += f"&fty%5B%5D={file_type}" 

        async with httpx.AsyncClient(auth=self.auth, headers=self.headers, timeout=15) as client:
            resp = await client.get(f"{url}?{query_string}")
            resp.raise_for_status()
            return resp.json()

    def parse_results(self, json_data: Dict[str, Any]) -> List[SearchItem]:
        """Parses raw JSON into structured SearchItems."""
        items = []
        # 'data' is the list of results
        for row in json_data.get("data", []):
            # Easynews returns lists for rows, rarely dicts in this specific endpoint, 
            # but we handle both based on your previous code.
            # Index mapping based on observation:
            # 0=hash, 4=size(bytes), 10=filename_stem, 11=extension, 14=timestamp?
            
            hash_id, filename, ext, size, sig = "", "", "", 0, None
            
            if isinstance(row, list) and len(row) > 12:
                hash_id = row[0]
                try:
                    size = int(row[4])
                except:
                    size = 0
                filename = row[10]
                ext = row[11]
                # sometimes sig is not in the main list, requires deep dive, 
                # but for basic NZB gen, hash|filename is crucial.
            elif isinstance(row, dict):
                hash_id = row.get("0", "")
                filename = row.get("10", "")
                ext = row.get("11", "")
                size = int(row.get("4", 0))
                sig = row.get("sig")
            
            if hash_id and filename:
                items.append(SearchItem(
                    id=hash_id,
                    hash=hash_id,
                    filename=filename,
                    ext=ext,
                    sig=sig,
                    type="VIDEO", # Simplified
                    size=size,
                    raw=row if isinstance(row, dict) else {}
                ))
        return items

    async def get_nzb(self, item: SearchItem, nzb_name: str) -> bytes:
        """Generates and downloads the NZB file content."""
        url = f"{EASYNEWS_BASE}/2.0/api/dl-nzb"
        
        # Emulate the form payload
        payload = {
            "autoNZB": "1",
            "0": item.value_token,
            "nameZipQ0": nzb_name
        }

        async with httpx.AsyncClient(auth=self.auth, headers=self.headers, timeout=20) as client:
            resp = await client.post(url, data=payload)
            resp.raise_for_status()
            
            # Basic cleanup of the NZB content if needed (fixing dates)
            content = resp.content.replace(b'date=""', b'date="0"')
            return content
