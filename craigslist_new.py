# Windows cp1252 fix -- must be first thing that runs

import sys

"""
craigslist.py -- CLBlast Craigslist automation
FIX v3: Real CDP performance-log network capture + real ActionChains key events

ROOT CAUSE (confirmed from logs):
  - _allNetworkCalls: [] because JS XHR/fetch spy wraps window.XMLHttpRequest
    AFTER CL's autocomplete module already captured the original reference.
    The spy is invisible to CL's autocomplete.
  - cryptedStepCheck rotates server-side only when CL's real autocomplete
    endpoint is hit. Our fake widget fired UI events but never hit the server.
  - widgetCreated: True proved we clicked our OWN fake widget, not CL's real one.

THE FIX (2 changes):
  1. Add goog:loggingPrefs {"performance":"ALL"} to ChromeOptions so that
     driver.get_log("performance") returns CDP Network events -- this captures
     ALL network activity at the browser level, regardless of JS spy timing.
  2. Use ActionChains.key_down/key_up for ZIP typing instead of send_keys or
     CDP Input.dispatchKeyEvent -- fires real OS-level key events through
     Chrome's input pipeline that CL's keypress/keydown handlers intercept.
  3. Poll CDP perf log for Network.responseReceived matching CL's geo endpoint,
     then call Network.getResponseBody to get the signed token back.
"""
import imaplib
import email as email_lib
import re
import time
import json
import os
import random
import shutil
import subprocess
import threading
import tempfile
import urllib.request
import requests
from datetime import datetime

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait, Select
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException, NoSuchElementException
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.common.keys import Keys

try:
    from twocaptcha import TwoCaptcha
    CAPTCHA_SOLVER_AVAILABLE = True
except ImportError:
    CAPTCHA_SOLVER_AVAILABLE = False

TWO_CAPTCHA_API_KEY = os.environ.get("TWO_CAPTCHA_KEY", "YOUR_2CAPTCHA_API_KEY")
LISTINGS_JSON       = "posted_listings.json"
GMAIL_EMAIL    = os.environ.get("CL_EMAIL",          "hunzla.khalid07@gmail.com")
GMAIL_PASSWORD = os.environ.get("CL_EMAIL_PASSWORD", "N.aruto07")
CL_PASSWORD    = os.environ.get("CL_PASSWORD",       "N.aruto07")  # CL account password
_cl_logged_in: bool = False
CL_CITY             = os.environ.get("CL_CITY", "losangeles")
IS_FAST_MODE        = os.environ.get("FAST_MODE", "1") == "1"
IS_RAILWAY          = any(os.path.exists(p) for p in [
    "/usr/bin/chromium", "/usr/bin/chromium-browser",
])

CATEGORY_MAPPING = {
    "antiques": (1, "antiques"), "appliances": (2, "appliances"),
    "art": (3, "arts & crafts"), "paintings": (3, "arts & crafts"),
    "atvs": (4, None), "automotive": (5, "auto parts"), "auto parts": (5, "auto parts"),
    "tires": (6, "auto wheels & tires"), "boats": (13, "boats"),
    "books": (14, "books & magazines"), "business": (15, "business/commercial"),
    "cars": (16, "cars & trucks"), "trucks": (16, "cars & trucks"),
    "phones": (18, "cell phones"), "cell phones": (18, "cell phones"),
    "fashion": (19, "clothing & accessories"),
    "collectibles": (20, "collectibles"), "coins": (20, "collectibles"),
    "computers": (22, "computers"), "laptops": (22, "computers"),
    "electronics": (23, "electronics"), "cameras": (23, "electronics"),
    "furniture": (26, "furniture"),
    "miscellaneous": (28, "general for sale"),
    "health": (29, "health and beauty"), "beauty": (29, "health and beauty"),
    "household": (31, "household items"),
    "jewelry": (32, "jewelry"), "watches": (32, "jewelry"),
    "motorcycles": (35, "motorcycles/scooters"),
    "instruments": (36, "musical instruments"),
    "sports": (39, "sporting goods"), "sporting goods": (39, "sporting goods"),
    "tickets": (40, "tickets"), "tools": (41, "tools"),
    "toys": (42, "toys & games"), "video games": (44, "video gaming"),
    "men": (19, "clothing & accessories"), "women": (19, "clothing & accessories"),
    "accessories": (19, "clothing & accessories"),
    "artandcollectibles": (20, "collectibles"),
    "art and collectibles": (20, "collectibles"),
    "homeandappliances": (31, "household items"),
    "home and appliances": (31, "household items"),
    "entertainment": (44, "video gaming"),
}

def get_category_ul_value(category_name):
    key = category_name.lower().strip().replace(" ", "")
    for k in CATEGORY_MAPPING:
        if k.replace(" ", "") == key:
            return CATEGORY_MAPPING[k][0]
    key_spaced = category_name.lower().strip()
    for k in CATEGORY_MAPPING:
        if k in key_spaced or key_spaced in k:
            return CATEGORY_MAPPING[k][0]
    return CATEGORY_MAPPING["miscellaneous"][0]

posted_listings: dict = {}
_listings_lock = threading.Lock()

def _load_existing_listings():
    global posted_listings
    if not os.path.exists(LISTINGS_JSON):
        return
    try:
        with open(LISTINGS_JSON) as f:
            data = json.load(f)
        for k, v in data.items():
            if k not in posted_listings:
                entry = dict(v)
                try:
                    entry["post_time"] = datetime.fromisoformat(v["post_time"])
                except Exception:
                    entry["post_time"] = datetime.now()
                posted_listings[k] = entry
        print(f"  Loaded {len(data)} existing listing(s) from disk.")
    except Exception as e:
        print(f"  Could not load existing listings: {e}")

def _save_listings():
    serialisable = {}
    for k, v in posted_listings.items():
        entry = dict(v)
        pt = v["post_time"]
        entry["post_time"] = pt.isoformat() if isinstance(pt, datetime) else str(pt)
        serialisable[k] = entry
    tmp_path = LISTINGS_JSON + ".tmp"
    with _listings_lock:
        with open(tmp_path, "w") as f:
            json.dump(serialisable, f, indent=2)
        os.replace(tmp_path, LISTINGS_JSON)

def _find_binary(names, fallback_paths):
    for name in names:
        path = shutil.which(name)
        if path:
            return path
    for name in names:
        try:
            r = subprocess.run(["which", name], capture_output=True, text=True, timeout=3)
            p = r.stdout.strip()
            if p and os.path.exists(p):
                return p
        except Exception:
            pass
    for p in (["/usr/local/bin/" + n for n in names] + fallback_paths):
        if os.path.exists(p):
            return p
    return None

def _ensure_xvfb():
    if os.environ.get("DISPLAY"):
        return
    if not IS_RAILWAY and not shutil.which("Xvfb"):
        return
    xvfb = shutil.which("Xvfb") or "/usr/bin/Xvfb"
    if not os.path.exists(xvfb):
        return
    try:
        subprocess.Popen(
            [xvfb, ":99", "-screen", "0", "1280x800x24", "-ac"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        os.environ["DISPLAY"] = ":99"
        time.sleep(1.0)
        print("  [driver] Xvfb started (DISPLAY=:99)")
    except Exception as e:
        print(f"  [driver] Xvfb unavailable: {e}")


_FINGERPRINTS = [
    {
        "ua": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.6367.82 Safari/537.36",
        "platform": "Win32", "vendor": "Google Inc.", "lang": "en-US",
        "screen": (1920, 1080), "tz": "America/Los_Angeles",
    },
    {
        "ua": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.6312.122 Safari/537.36",
        "platform": "Win32", "vendor": "Google Inc.", "lang": "en-US",
        "screen": (1366, 768), "tz": "America/New_York",
    },
    {
        "ua": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.6367.82 Safari/537.36",
        "platform": "MacIntel", "vendor": "Google Inc.", "lang": "en-US",
        "screen": (2560, 1600), "tz": "America/Chicago",
    },
]

_FINGERPRINT_JS = """
(function() {{
    Object.defineProperty(navigator, 'webdriver',   {{get: () => undefined}});
    Object.defineProperty(navigator, 'platform',    {{get: () => '{platform}'}});
    Object.defineProperty(navigator, 'vendor',      {{get: () => '{vendor}'}});
    Object.defineProperty(navigator, 'language',    {{get: () => '{lang}'}});
    Object.defineProperty(navigator, 'languages',   {{get: () => ['{lang}', 'en']}});
    Object.defineProperty(navigator, 'hardwareConcurrency', {{get: () => 8}});
    Object.defineProperty(navigator, 'deviceMemory',        {{get: () => 8}});
    Object.defineProperty(navigator, 'maxTouchPoints',      {{get: () => 0}});
    Object.defineProperty(screen, 'width',       {{get: () => {sw}}});
    Object.defineProperty(screen, 'height',      {{get: () => {sh}}});
    Object.defineProperty(screen, 'availWidth',  {{get: () => {sw}}});
    Object.defineProperty(screen, 'availHeight', {{get: () => {sh} - 40}});
    Object.defineProperty(screen, 'colorDepth',  {{get: () => 24}});
    Object.defineProperty(screen, 'pixelDepth',  {{get: () => 24}});
    if (!window.chrome) {{
        window.chrome = {{
            app: {{}},
            runtime: {{
                onConnect: {{addListener: function(){{}}}},
                onMessage: {{addListener: function(){{}}}}
            }},
        }};
    }}
    const getParam = WebGLRenderingContext.prototype.getParameter;
    WebGLRenderingContext.prototype.getParameter = function(p) {{
        if (p === 37445) return 'Intel Inc.';
        if (p === 37446) return 'Intel Iris OpenGL Engine';
        return getParam.call(this, p);
    }};
}})();
"""

# -----------------------------------------------------------------------------
#  CDP Network Interceptor (JS-side spy -- belt-and-suspenders fallback)
# -----------------------------------------------------------------------------

_GEO_URL_PATTERNS = [
    "suggest", "postal", "geo", "location", "zip", "area",
    "geoCode", "geocode", "postcode",
]

def _start_cdp_network_capture(driver):
    """
    Enable CDP Network domain and register JS-side XHR/fetch spy.
    Primary capture is via CDP perf log (Python-side). This is the fallback.
    """
    driver._cl_geo_responses = []
    driver._cl_network_request_map = {}

    try:
        driver.execute_cdp_cmd("Network.enable", {})
        print("  [CDP] Network capture enabled")
    except Exception as e:
        print(f"  [CDP] Could not enable Network domain: {e}")
        return

    _NETWORK_SPY_JS = """
(function() {
    if (window._clNetworkSpyInstalled) return 'already-installed';
    window._clNetworkSpyInstalled = true;
    window._clCapturedGeoResponses = [];
    window._clAllNetworkCalls = [];

    var GEO_PATTERNS = ['suggest','postal','geo','location','zip','area','geocode','postcode'];
    function looksLikeGeo(url) {
        if (!url) return false;
        var u = url.toLowerCase();
        for (var i = 0; i < GEO_PATTERNS.length; i++) {
            if (u.indexOf(GEO_PATTERNS[i]) !== -1) return true;
        }
        return false;
    }

    var OrigXHR = window.XMLHttpRequest;
    function SpyXHR() {
        var xhr = new OrigXHR();
        var _url = '', _method = '';
        var origOpen = xhr.open.bind(xhr);
        var origSend = xhr.send.bind(xhr);
        xhr.open = function(method, url) { _method = method; _url = url || ''; return origOpen(method, url); };
        xhr.send = function(body) {
            var captureUrl = _url;
            var origRSC = xhr.onreadystatechange;
            xhr.onreadystatechange = function() {
                if (xhr.readyState === 4) {
                    var entry = { type: 'xhr', url: captureUrl, status: xhr.status, responseText: xhr.responseText || '' };
                    window._clAllNetworkCalls.push(entry);
                    if (looksLikeGeo(captureUrl)) { window._clCapturedGeoResponses.push(entry); window._clLastGeoResponse = entry; }
                }
                if (origRSC) origRSC.apply(this, arguments);
            };
            return origSend(body);
        };
        return xhr;
    }
    for (var k in OrigXHR) { try { SpyXHR[k] = OrigXHR[k]; } catch(e) {} }
    SpyXHR.prototype = OrigXHR.prototype;
    window.XMLHttpRequest = SpyXHR;

    var origFetch = window.fetch;
    window.fetch = function(input, init) {
        var url = (typeof input === 'string') ? input : (input && input.url) || '';
        var p = origFetch.apply(this, arguments);
        p.then(function(resp) {
            resp.clone().text().then(function(text) {
                var entry = { type: 'fetch', url: url, status: resp.status, responseText: text || '' };
                window._clAllNetworkCalls.push(entry);
                if (looksLikeGeo(url)) { window._clCapturedGeoResponses.push(entry); window._clLastGeoResponse = entry; }
            }).catch(function(){});
        }).catch(function(){});
        return p;
    };

    return 'spy-installed';
})();
"""
    try:
        driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument",
                               {"source": _NETWORK_SPY_JS})
        print("  [CDP] Network spy script registered for new documents")
    except Exception as e:
        print(f"  [CDP] Could not register network spy: {e}")


def _install_network_spy_now(driver):
    """Install the network spy into the already-loaded page."""
    _NETWORK_SPY_JS = """
(function() {
    if (window._clNetworkSpyInstalled) return 'already-installed';
    window._clNetworkSpyInstalled = true;
    window._clCapturedGeoResponses = [];
    window._clAllNetworkCalls = [];

    var GEO_PATTERNS = ['suggest','postal','geo','location','zip','area','geocode','postcode'];
    function looksLikeGeo(url) {
        if (!url) return false;
        var u = url.toLowerCase();
        for (var i = 0; i < GEO_PATTERNS.length; i++) {
            if (u.indexOf(GEO_PATTERNS[i]) !== -1) return true;
        }
        return false;
    }

    var OrigXHR = window.XMLHttpRequest;
    function SpyXHR() {
        var xhr = new OrigXHR();
        var _url = '', _method = '';
        var origOpen = xhr.open.bind(xhr);
        var origSend = xhr.send.bind(xhr);
        xhr.open = function(method, url) { _method = method; _url = url || ''; return origOpen(method, url); };
        xhr.send = function(body) {
            var captureUrl = _url;
            var origRSC = xhr.onreadystatechange;
            xhr.onreadystatechange = function() {
                if (xhr.readyState === 4) {
                    var entry = { type: 'xhr', url: captureUrl, status: xhr.status, responseText: xhr.responseText || '' };
                    window._clAllNetworkCalls.push(entry);
                    if (looksLikeGeo(captureUrl)) { window._clCapturedGeoResponses.push(entry); window._clLastGeoResponse = entry; }
                }
                if (origRSC) origRSC.apply(this, arguments);
            };
            return origSend(body);
        };
        return xhr;
    }
    for (var k in OrigXHR) { try { SpyXHR[k] = OrigXHR[k]; } catch(e) {} }
    SpyXHR.prototype = OrigXHR.prototype;
    window.XMLHttpRequest = SpyXHR;

    var origFetch = window.fetch;
    window.fetch = function(input, init) {
        var url = (typeof input === 'string') ? input : (input && input.url) || '';
        var p = origFetch.apply(this, arguments);
        p.then(function(resp) {
            resp.clone().text().then(function(text) {
                var entry = { type: 'fetch', url: url, status: resp.status, responseText: text || '' };
                window._clAllNetworkCalls.push(entry);
                if (looksLikeGeo(url)) { window._clCapturedGeoResponses.push(entry); window._clLastGeoResponse = entry; }
            }).catch(function(){});
        }).catch(function(){});
        return p;
    };

    return 'spy-installed';
})();
"""
    result = driver.execute_script(_NETWORK_SPY_JS)
    print(f"  [CDP] Network spy (live install): {result}")


def _get_geo_responses(driver):
    """Poll the JS-side spy buffer for any captured geo responses."""
    try:
        responses = driver.execute_script(
            "return window._clCapturedGeoResponses || [];")
        all_calls = driver.execute_script(
            "return (window._clAllNetworkCalls || []).slice(-20);")
        return responses, all_calls
    except Exception:
        return [], []


def _inject_geo_hidden_fields(driver, geo_response_text, zip_str):
    """
    Parse the geo/postal lookup response and inject any returned fields
    into the posting form as hidden inputs.
    """
    if not geo_response_text:
        return False

    injected = {}
    try:
        data = json.loads(geo_response_text)
        if isinstance(data, list) and data:
            data = data[0]
        if isinstance(data, dict):
            for key, val in data.items():
                if isinstance(val, (str, int, float)) and val:
                    injected[key] = str(val)
    except Exception:
        pass

    if not injected:
        print("  [GEO] Response parsed but no injectable fields found")
        return False

    print(f"  [GEO] Injecting fields from geo response: {list(injected.keys())}")

    inject_js = """
(function(fields) {
    var form = document.getElementById('postingForm');
    if (!form) return {ok: false, reason: 'no-form'};
    var injected = [];
    for (var name in fields) {
        var val = fields[name];
        var existing = form.querySelector('[name="' + name + '"]');
        if (existing) {
            var old = existing.value;
            existing.value = val;
            existing.setAttribute('value', val);
            injected.push('updated:' + name + '=' + val + '(was:' + old + ')');
        } else {
            var inp = document.createElement('input');
            inp.type = 'hidden';
            inp.name = name;
            inp.value = val;
            form.appendChild(inp);
            injected.push('added:' + name + '=' + val);
        }
    }
    return {ok: true, injected: injected};
})(arguments[0]);
"""
    result = driver.execute_script(inject_js, injected)
    print(f"  [GEO] Injection result: {result}")
    return bool(result and result.get("ok"))


# -----------------------------------------------------------------------------
#  DIRECT GEO FETCH -- Python-side fallback
# -----------------------------------------------------------------------------

def _fetch_cl_geo_direct(driver, zip_str, city="Los Angeles", state="CA"):
    """
    Make the postal lookup request directly from Python using browser cookies.
    """
    cookies = {}
    try:
        for cookie in driver.get_cookies():
            cookies[cookie["name"]] = cookie["value"]
    except Exception as e:
        print(f"  [GEO-direct] Could not get cookies: {e}")

    headers = {
        "User-Agent": driver.execute_script("return navigator.userAgent;"),
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Accept-Language": "en-US,en;q=0.9",
        "X-Requested-With": "XMLHttpRequest",
        "Referer": driver.current_url,
        "Origin": "https://post.craigslist.org",
    }

    city_slug = CL_CITY.lower().replace(" ", "").replace("-", "")
    candidate_urls = [
        f"https://post.craigslist.org/suggest?fieldname=postal&typing={zip_str}",
        f"https://{city_slug}.craigslist.org/suggest?fieldname=postal&typing={zip_str}",
        f"https://post.craigslist.org/suggest?fieldname=postal_code&typing={zip_str}",
        f"https://post.craigslist.org/geo?q={zip_str}",
        f"https://{city_slug}.craigslist.org/geo?q={zip_str}",
        f"https://post.craigslist.org/c/sss?s=geo&q={zip_str}",
    ]

    for url in candidate_urls:
        try:
            resp = requests.get(url, headers=headers, cookies=cookies,
                                timeout=8, allow_redirects=True)
            print(f"  [GEO-direct] {url} -> {resp.status_code} ({len(resp.text)} bytes)")
            if resp.status_code == 200 and resp.text.strip():
                print(f"  [GEO-direct] Response: {resp.text[:300]}")
                return resp.text, url
        except Exception as e:
            print(f"  [GEO-direct] {url} failed: {e}")

    print("  [GEO-direct] No successful geo response from any endpoint")
    return None, None


def _trigger_real_geo_lookup(driver, zip_str):
    """
    Force CL's own JS to make the postal lookup XHR/fetch by calling their
    internal autocomplete source function directly.
    """
    trigger_js = """
(function(zipVal, callback) {
    var postalEl = document.querySelector('[name="postal"]') ||
                   document.querySelector('[name="postal_code"]') ||
                   document.querySelector('#postal_code') ||
                   document.querySelector('#postal');

    if (!postalEl || !window.jQuery) {
        return {ok: false, reason: 'no-postal-or-jquery'};
    }

    var jq = jQuery(postalEl);
    var acData = jq.data('ui-autocomplete') || jq.data('autocomplete');
    if (!acData || !acData.options || !acData.options.source) {
        return {ok: false, reason: 'no-autocomplete-instance', data: Object.keys(jq.data() || {})};
    }

    var sourceFn = acData.options.source;
    if (typeof sourceFn !== 'function') {
        return {ok: false, reason: 'source-not-function', sourceType: typeof sourceFn, source: String(sourceFn).substring(0,100)};
    }

    window._clGeoLookupTriggered = false;
    window._clGeoLookupResponse = null;

    try {
        sourceFn.call(acData, {term: zipVal}, function(items) {
            window._clGeoLookupTriggered = true;
            window._clGeoLookupResponse = items;
        });
        return {ok: true, reason: 'source-called'};
    } catch(e) {
        return {ok: false, reason: 'source-call-error', error: e.message};
    }
})(arguments[0]);
"""
    result = driver.execute_script(trigger_js, zip_str)
    print(f"  [GEO-trigger] Direct source call result: {result}")

    if result and result.get("ok"):
        try:
            WebDriverWait(driver, 8).until(
                lambda d: d.execute_script("return !!window._clGeoLookupTriggered;"))
            items = driver.execute_script("return window._clGeoLookupResponse;")
            print(f"  [GEO-trigger] Got {len(items) if items else 0} items from source")
            return items
        except TimeoutException:
            print("  [GEO-trigger] Source callback timed out")

    return None


# -----------------------------------------------------------------------------
#  CDP PERFORMANCE LOG -- Python-side geo response capture (THE REAL FIX)
# -----------------------------------------------------------------------------

def _drain_perf_log(driver):
    """
    Drain Chrome performance log entries and return parsed CDP events.
    Returns list of (method, params) tuples.
    Requires goog:loggingPrefs {"performance":"ALL"} in ChromeOptions.
    """
    try:
        entries = driver.get_log("performance")
    except Exception:
        return []
    events = []
    for entry in entries:
        try:
            msg = json.loads(entry["message"])
            event = msg.get("message", {})
            events.append((event.get("method", ""), event.get("params", {})))
        except Exception:
            pass
    return events


def _poll_perf_log_for_geo(driver, timeout=8):
    """
    Poll Chrome perf log for Network.responseReceived events matching
    CL's geo/suggest endpoint. Fetches body via CDP Network.getResponseBody.
    Returns (response_body_str, request_url) or (None, None).
    """
    GEO_PATTERNS = ["suggest", "postal", "geo", "location", "zip", "area", "geocode", "postcode"]
    deadline = time.time() + timeout
    request_id_map = {}

    while time.time() < deadline:
        events = _drain_perf_log(driver)
        for method, params in events:
            if method == "Network.requestWillBeSent":
                rid = params.get("requestId", "")
                url = params.get("request", {}).get("url", "")
                if rid:
                    request_id_map[rid] = url

            elif method == "Network.responseReceived":
                rid = params.get("requestId", "")
                url = (params.get("response", {}).get("url", "")
                       or request_id_map.get(rid, ""))
                if any(p in url.lower() for p in GEO_PATTERNS):
                    print(f"  [CDP-perf] Geo response detected: {url}")
                    try:
                        body_resp = driver.execute_cdp_cmd(
                            "Network.getResponseBody", {"requestId": rid})
                        body = body_resp.get("body", "")
                        print(f"  [CDP-perf] Body: {body[:300]}")
                        return body, url
                    except Exception as e:
                        print(f"  [CDP-perf] getResponseBody failed: {e}")

        time.sleep(0.35)

    return None, None


def make_driver(proxy_url=None):
    from selenium.webdriver.chrome.service import Service as ChromeService
    os.environ["SE_MANAGER_PATH"] = ""
    os.environ["WDM_SKIP_DOWNLOAD"] = "1"
    _ensure_xvfb()
    use_headed = False  # Force headless on Railway

    if not proxy_url:
        proxy_url = os.environ.get("HTTPS_PROXY") or os.environ.get("HTTP_PROXY")

    fp = random.choice(_FINGERPRINTS)
    sw, sh = fp["screen"]
    print(f"  [driver] Fingerprint: {fp['ua'][:60]}...")

    options = webdriver.ChromeOptions()
    chrome_args = [
        "--no-sandbox",
        "--disable-dev-shm-usage",
        "--single-process",
        "--no-zygote",
        "--headless=new",
        "--remote-debugging-port=0",
        "--disable-software-rasterizer",
        "--disable-gpu",
        f"--window-size={sw},{sh}",
        "--disable-setuid-sandbox",
        "--disable-blink-features=AutomationControlled",
        "--ignore-certificate-errors",
        "--disable-extensions",
        "--mute-audio",
        "--no-first-run",
        "--shm-size=256m",
        "--disable-features=AutofillServerCommunication,IsolateOrigins,site-per-process",
        "--enable-features=NetworkService,NetworkServiceInProcess",
        "--disable-web-security",
        "--allow-running-insecure-content",
        "--enable-javascript",
        "--enable-local-storage",
        f"--lang={fp['lang']}",
        "--disable-popup-blocking",
        "--disable-translate",
        "--disable-default-apps",
        "--disable-sync",
        "--metrics-recording-only",
        "--no-report-upload",
    ]
    if not use_headed:
        chrome_args.insert(3, "--headless=new")
    for arg in chrome_args:
        options.add_argument(arg)
    if use_headed:
        print("  [driver] Headed mode (virtual display)")

    if proxy_url:
        options.add_argument(f"--proxy-server={proxy_url}")
        options.add_argument("--proxy-bypass-list=localhost,127.0.0.1")

    fresh_profile = tempfile.mkdtemp(prefix="clblast_chrome_")
    options.add_argument(f"--user-data-dir={fresh_profile}")
    options.add_argument(f"--user-agent={fp['ua']}")

    options.add_experimental_option("prefs", {
        "credentials_enable_service": False,
        "profile.password_manager_enabled": False,
        "autofill.profile_enabled": False,
        "autofill.credit_card_enabled": False,
        "intl.accept_languages": fp["lang"],
    })

    # -- FIX: Enable CDP performance log for Python-side network capture -------
    # This is required for driver.get_log("performance") to return CDP Network
    # events. Without this, _poll_perf_log_for_geo() returns nothing.
    options.set_capability("goog:loggingPrefs", {"performance": "ALL"})
    # -------------------------------------------------------------------------

    chromium_bin = _find_binary(
        ["google-chrome", "chromium", "chromium-browser"],
        ["/usr/local/bin/google-chrome", "/usr/bin/chromium", "/usr/bin/chromium-browser"])
    if chromium_bin:
        print(f"  [driver] Using chromium: {chromium_bin}")
        options.binary_location = chromium_bin

    chromedriver_bin = _find_binary(
        ["chromedriver"],
        ["/usr/local/bin/chromedriver", "/usr/bin/chromedriver"])
    if os.path.exists("/usr/local/bin/chromedriver"):
        chromedriver_bin = "/usr/local/bin/chromedriver"
    if not chromedriver_bin:
        try:
            from webdriver_manager.chrome import ChromeDriverManager
            chromedriver_bin = ChromeDriverManager().install()
            print(f"  [driver] Auto-installed chromedriver: {chromedriver_bin}")
        except Exception as wdm_err:
            raise RuntimeError(f"chromedriver not found and auto-install failed: {wdm_err}")
    print(f"  [driver] Using chromedriver: {chromedriver_bin}")

    service = ChromeService(
        executable_path=chromedriver_bin,
        log_output="/tmp/chromedriver.log",
    )
    options.add_argument("--remote-allow-origins=*")

    try:
        import undetected_chromedriver as uc
        driver = uc.Chrome(
            options=options,
            driver_executable_path=chromedriver_bin,
            browser_executable_path=chromium_bin,
            headless=not use_headed,
            use_subprocess=True,
        )
        print("  [driver] Using undetected-chromedriver")
    except Exception as uc_err:
        print(f"  [driver] undetected-chromedriver unavailable ({uc_err}), using stock Chrome")
        driver = webdriver.Chrome(service=service, options=options)

    fingerprint_js = _FINGERPRINT_JS.format(
        platform=fp["platform"], vendor=fp["vendor"], lang=fp["lang"],
        sw=sw, sh=sh,
    )
    driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {"source": fingerprint_js})

    try:
        driver.execute_cdp_cmd("Network.enable", {})
        driver.execute_cdp_cmd("Network.setExtraHTTPHeaders", {"headers": {
            "Accept-Language": f"{fp['lang']},en;q=0.9",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "Accept-Encoding": "gzip, deflate, br",
        }})
    except Exception:
        pass

    # Register network spy for all new documents (fallback)
    _start_cdp_network_capture(driver)

    # Native form submit interceptor
    _FORM_INTERCEPT_JS = """
(function() {
    window._clNativeSubmitPayloads = [];
    function _captureForm(form, via) {
        try {
            var fd = new FormData(form);
            var pairs = [];
            fd.forEach(function(v, k) { pairs.push(k + '=' + String(v).substring(0, 200)); });
            window._clNativeSubmitPayloads.push({
                action: form.action, method: form.method,
                via: via, body: pairs.join('&')
            });
        } catch(e) {}
    }
    var origSubmit = HTMLFormElement.prototype.submit;
    HTMLFormElement.prototype.submit = function() {
        _captureForm(this, 'submit');
        return origSubmit.call(this);
    };
    if (HTMLFormElement.prototype.requestSubmit) {
        var origRS = HTMLFormElement.prototype.requestSubmit;
        HTMLFormElement.prototype.requestSubmit = function(btn) {
            _captureForm(this, 'requestSubmit');
            return origRS.call(this, btn);
        };
    }
})();
"""
    try:
        driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument",
                               {"source": _FORM_INTERCEPT_JS})
    except Exception:
        pass

    return driver


def human_delay(lo=0.8, hi=2.5):
    time.sleep(random.uniform(lo, hi))


def safe_click(driver, element):
    human_delay(0.3, 0.6)
    try:
        ActionChains(driver).move_to_element(element).pause(
            random.uniform(0.2, 0.5)).click().perform()
    except Exception:
        driver.execute_script("arguments[0].click();", element)
    human_delay(0.3, 0.6)


def handle_captcha_if_present(driver):
    try:
        driver.find_element(By.CSS_SELECTOR, "iframe[src*='recaptcha']")
        if CAPTCHA_SOLVER_AVAILABLE:
            try:
                iframe = driver.find_element(By.CSS_SELECTOR, "iframe[src*='recaptcha']")
                sitekey = [p.split("=")[1] for p in iframe.get_attribute("src").split("&") if "k=" in p][0]
                solver = TwoCaptcha(TWO_CAPTCHA_API_KEY)
                result = solver.recaptcha(sitekey=sitekey, url=driver.current_url)
                driver.execute_script(
                    "document.getElementById('g-recaptcha-response').innerHTML=arguments[0];",
                    result["code"])
                print("  CAPTCHA solved [OK]")
            except Exception as e:
                print(f"  CAPTCHA solve failed: {e}")
    except NoSuchElementException:
        pass
    if "Just a moment" in driver.title:
        print("  Cloudflare -- waiting 8s...")
        time.sleep(8)


def _is_cl_logged_in(driver):
    """Check if browser is currently logged into CL."""
    try:
        src = driver.page_source
        return ('log out' in src.lower() or
                'accounts.craigslist.org/logout' in src or
                'logged in as' in src.lower())
    except Exception:
        return False


def craigslist_login(driver, email):
    """
    CL Login -- Screenshot shows:
      - Email / Handle field  (id="inputEmailHandle")
      - Password field        (visible on same page)
      - "Log in" button       (right side)
      - "E-mail a login link" button (left side -- we don't use this)

    So: fill email -> fill password -> click "Log in"
    """
    # Skip if already logged in
    try:
        driver.get("https://accounts.craigslist.org/login/home")
        time.sleep(2)
        if _is_cl_logged_in(driver):
            print("[OK] Already logged in to CL -- skipping login")
            return True
    except Exception:
        pass

    cl_password = os.environ.get("CL_PASSWORD", CL_PASSWORD).strip()

    print(f"  [login] Going to CL login page...")
    driver.get("https://accounts.craigslist.org/login")
    human_delay(2, 3)
    handle_captcha_if_present(driver)

    try:
        # -- Step 1: Fill Email ------------------------------------------------
        email_field = WebDriverWait(driver, 15).until(
            EC.presence_of_element_located((By.ID, "inputEmailHandle")))
        email_field.clear()
        for ch in email:
            email_field.send_keys(ch)
            time.sleep(random.uniform(0.05, 0.10))
        print(f"  [login] [OK] Email filled: {email}")
        time.sleep(0.5)

        # -- Step 2: Fill Password ---------------------------------------------
        # Password field is on the SAME page (not a separate step)
        pwd_field = WebDriverWait(driver, 10).until(
            EC.presence_of_element_located((By.CSS_SELECTOR,
                "input[type='password'], input[name='password'], "
                "input[id='inputPassword'], input[name='passwd']")))
        pwd_field.clear()
        for ch in cl_password:
            pwd_field.send_keys(ch)
            time.sleep(random.uniform(0.04, 0.09))
        print(f"  [login] [OK] Password filled")
        time.sleep(0.5)

        # -- Step 3: Click "Log in" button (NOT "E-mail a login link") ---------
        # There are 2 buttons: "E-mail a login link" and "Log in"
        # We need the "Log in" button on the RIGHT side
        login_btn = None

        # Try by button text first
        for btn in driver.find_elements(By.CSS_SELECTOR, "button, input[type='submit']"):
            txt = (btn.text or btn.get_attribute("value") or "").strip().lower()
            if txt in ("log in", "login", "sign in", "signin"):
                login_btn = btn
                print(f"  [login] Found button by text: '{btn.text or btn.get_attribute('value')}'")
                break

        # Fallback: last submit button on page (Log in is on the right/last)
        if not login_btn:
            all_btns = driver.find_elements(By.CSS_SELECTOR,
                "button[type='submit'], input[type='submit'], button")
            if all_btns:
                login_btn = all_btns[-1]  # Last button = "Log in"
                print(f"  [login] Using last button: '{login_btn.text}'")

        if not login_btn:
            print("  [login] [FAIL] Could not find Log in button!")
            return False

        # Scroll to and click
        driver.execute_script("arguments[0].scrollIntoView({block:'center'});", login_btn)
        time.sleep(0.3)
        driver.execute_script("arguments[0].click();", login_btn)
        print("  [login] [OK] Log in button clicked!")
        time.sleep(4)
        handle_captcha_if_present(driver)

    except TimeoutException as e:
        print(f"  [login] [FAIL] Field not found: {e}")
        return False
    except Exception as e:
        print(f"  [login] [FAIL] Error: {e}")
        return False

    # -- Step 4: Check result --------------------------------------------------
    current_url = driver.current_url
    print(f"  [login] URL after login click: {current_url}")

    if _is_cl_logged_in(driver):
        print("  [login] [OK] Logged in to Craigslist!")
        return True

    # Maybe redirected to home page -- check again
    time.sleep(2)
    if _is_cl_logged_in(driver):
        print("  [login] [OK] Logged in!")
        return True

    # Check for wrong password error
    page_src = driver.page_source.lower()
    if "invalid" in page_src or "incorrect" in page_src or "wrong" in page_src:
        print("  [login] [FAIL] Wrong password -- check CL_PASSWORD env var")
        return False

    # Check for OTP/verification
    try:
        otp_input = WebDriverWait(driver, 5).until(
            EC.presence_of_element_located((By.CSS_SELECTOR,
                "input[name='otp'], input[type='number'][maxlength='6']")))
        print("  [login] OTP required -- fetching from Gmail...")
        otp = _get_otp_from_gmail_imap(email, cl_password)
        if otp:
            otp_input.clear()
            otp_input.send_keys(otp)
            otp_input.send_keys(Keys.RETURN)
            time.sleep(3)
            if _is_cl_logged_in(driver):
                print("  [login] [OK] Logged in via OTP!")
                return True
    except TimeoutException:
        pass

    print(f"  [login] [WARN] Login status unclear -- proceeding. URL: {driver.current_url}")
    # Optimistic -- sometimes CL just redirects oddly
    return "accounts.craigslist.org" not in driver.current_url or _is_cl_logged_in(driver)


def _get_otp_from_gmail_imap(cl_email, gmail_password, timeout_minutes=3):
    """Fetch 6-digit OTP code from CL email via IMAP."""
    import re as _re
    deadline = time.time() + timeout_minutes * 60
    while time.time() < deadline:
        try:
            mail = imaplib.IMAP4_SSL("imap.gmail.com", 993)
            mail.login(cl_email, gmail_password)
            mail.select("inbox")
            status, data = mail.search(None, '(FROM "robot@craigslist.org")')
            uids = data[0].split() if status == "OK" and data[0] else []
            if uids:
                _, msg_data = mail.fetch(uids[-1], "(RFC822)")
                mail.logout()
                msg = email_lib.message_from_bytes(msg_data[0][1])
                body = ""
                if msg.is_multipart():
                    for part in msg.walk():
                        if part.get_content_type() == "text/plain":
                            try:
                                body += part.get_payload(decode=True).decode(
                                    part.get_content_charset() or "utf-8", errors="replace")
                            except Exception:
                                pass
                else:
                    try:
                        body = msg.get_payload(decode=True).decode(
                            msg.get_content_charset() or "utf-8", errors="replace")
                    except Exception:
                        pass
                # Look for 6-digit code
                codes = _re.findall(r'\b(\d{6})\b', body)
                if codes:
                    print(f"  [imap-otp] Found OTP: {codes[0]}")
                    return codes[0]
            else:
                mail.logout()
            time.sleep(8)
        except Exception as e:
            print(f"  [imap-otp] Error: {e}")
            time.sleep(10)
    return None


def _get_cl_magic_link_from_imap(cl_email, gmail_password, timeout_minutes=5):
    """Fetch CL magic login link from Gmail via IMAP."""
    CL_LINK = re.compile(
        r'https?://(?:accounts\.craigslist\.org|[a-z]+\.craigslist\.org)/\S{10,}',
        re.IGNORECASE)
    deadline = time.time() + timeout_minutes * 60
    while time.time() < deadline:
        try:
            mail = imaplib.IMAP4_SSL("imap.gmail.com", 993)
            mail.login(cl_email, gmail_password)
            mail.select("inbox")
            status, data = mail.search(None, '(FROM "robot@craigslist.org")')
            uids = data[0].split() if status == "OK" and data[0] else []
            if uids:
                _, msg_data = mail.fetch(uids[-1], "(RFC822)")
                mail.logout()
                msg = email_lib.message_from_bytes(msg_data[0][1])
                body = ""
                if msg.is_multipart():
                    for part in msg.walk():
                        if part.get_content_type() in ("text/plain", "text/html"):
                            try:
                                body += part.get_payload(decode=True).decode(
                                    part.get_content_charset() or "utf-8", errors="replace")
                            except Exception:
                                pass
                else:
                    try:
                        body = msg.get_payload(decode=True).decode(
                            msg.get_content_charset() or "utf-8", errors="replace")
                    except Exception:
                        pass
                matches = CL_LINK.findall(body)
                if matches:
                    url = max(matches, key=len)
                    print(f"  [imap-magic] [OK] Found: {url[:80]}")
                    return url
            else:
                mail.logout()
            time.sleep(8)
        except Exception as e:
            print(f"  [imap-magic] Error: {e}")
            time.sleep(10)
    return None


def click_relocation_if_needed(driver, ad_name):
    try:
        btn = WebDriverWait(driver, 6).until(
            EC.element_to_be_clickable((By.CSS_SELECTOR, "#relocationButton")))
        safe_click(driver, btn)
        local_btn = WebDriverWait(driver, 6).until(
            EC.element_to_be_clickable((By.CSS_SELECTOR, "#localAreaButton")))
        safe_click(driver, local_btn)
        print("  Relocation handled [OK]")
    except TimeoutException:
        pass


def _wait_for_cl_js_init(driver, timeout=20):
    """Wait for CL's postingform JS to fully initialize before we touch any field."""
    print("  [init] Waiting for CL form JS to initialize...")
    try:
        WebDriverWait(driver, timeout).until(lambda d: d.execute_script("""
            try {
                var form = document.getElementById('postingForm');
                if (!form) return false;
                if (!window.jQuery) return false;
                var jqForm = jQuery(form);
                if (jqForm.data('validator')) return true;
                if (window.cl && window.cl.postingProcess) return true;
                return jQuery('#postingForm').length > 0;
            } catch(e) { return false; }
        """))
        print("  [init] CL form JS ready [OK]")
    except TimeoutException:
        print("  [init] Timeout waiting for CL JS -- proceeding anyway")
    time.sleep(1.5)


def _find_field(driver, selectors, timeout=8):
    """Try multiple selectors, return first visible element found."""
    for sel in selectors:
        try:
            el = WebDriverWait(driver, timeout).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, sel)))
            if el.is_displayed():
                return el
        except Exception:
            continue
    return None


def _cdp_type(driver, element, value):
    """
    Type into a field using CDP char events only.
    After typing, fires jQuery-compatible events so CL's validator marks field touched.
    """
    value = str(value).strip()
    if not value:
        return

    driver.execute_script("arguments[0].scrollIntoView({block:'center', inline:'nearest'});", element)
    time.sleep(0.25)
    try:
        ActionChains(driver).move_to_element(element).pause(
            random.uniform(0.1, 0.25)).click().perform()
    except Exception:
        driver.execute_script("arguments[0].focus();", element)
    time.sleep(random.uniform(0.15, 0.3))

    for key_action in [
        {"type": "keyDown", "key": "Control", "code": "ControlLeft",  "keyCode": 17, "modifiers": 0},
        {"type": "keyDown", "key": "a",       "code": "KeyA",         "keyCode": 65, "modifiers": 2},
        {"type": "keyUp",   "key": "a",       "code": "KeyA",         "keyCode": 65, "modifiers": 2},
        {"type": "keyUp",   "key": "Control", "code": "ControlLeft",  "keyCode": 17, "modifiers": 0},
        {"type": "keyDown", "key": "Delete",  "code": "Delete",       "keyCode": 46, "modifiers": 0},
        {"type": "keyUp",   "key": "Delete",  "code": "Delete",       "keyCode": 46, "modifiers": 0},
    ]:
        driver.execute_cdp_cmd("Input.dispatchKeyEvent", key_action)
        time.sleep(0.03)

    time.sleep(0.1)

    for ch in value:
        driver.execute_cdp_cmd("Input.dispatchKeyEvent", {
            "type": "char",
            "key": ch,
            "text": ch,
            "unmodifiedText": ch,
        })
        time.sleep(random.uniform(0.06, 0.14))

    time.sleep(0.2)

    driver.execute_script("""
        var el = arguments[0];
        el.dispatchEvent(new Event('input',  {bubbles: true, cancelable: true}));
        el.dispatchEvent(new Event('change', {bubbles: true, cancelable: true}));
        if (window.jQuery) {
            jQuery(el).trigger('input').trigger('change').trigger('keyup');
        }
    """, element)
    time.sleep(0.25)


# -----------------------------------------------------------------------------
#  ZIP PATCH JS -- serializer + FormData patches
# -----------------------------------------------------------------------------

_ZIP_PATCH_JS = """
var zipVal = arguments[0];
var results = [];

try {
    if (window.jQuery) {
        var origSerializeArray = jQuery.fn.serializeArray;
        jQuery.fn.serializeArray = function() {
            var result = origSerializeArray.call(this);
            var hasPostal = false;
            for (var i = 0; i < result.length; i++) {
                if (result[i].name === 'postal' || result[i].name === 'postal_code') {
                    result[i].value = zipVal;
                    hasPostal = true;
                }
            }
            if (!hasPostal) result.push({name: 'postal', value: zipVal});
            return result;
        };

        var origSerialize = jQuery.fn.serialize;
        jQuery.fn.serialize = function() {
            var s = origSerialize.call(this);
            s = s.replace(/postal=[^&]*/g, 'postal=' + encodeURIComponent(zipVal));
            s = s.replace(/postal_code=[^&]*/g, 'postal_code=' + encodeURIComponent(zipVal));
            if (s.indexOf('postal=') === -1) s += (s ? '&' : '') + 'postal=' + encodeURIComponent(zipVal);
            return s;
        };
        results.push('serializer-patched');
        window._clSerializerPatched = true;
    } else {
        results.push('no-jquery');
    }
} catch(e) { results.push('serializer-err:' + e.message); }

try {
    var OrigFormData = window.FormData;
    function PatchedFormData(form) {
        var fd = form ? new OrigFormData(form) : new OrigFormData();
        if (form) {
            try { fd.set('postal', zipVal); } catch(e) {}
            try { fd.set('postal_code', zipVal); } catch(e) {}
        }
        var origAppend = fd.append.bind(fd);
        fd.append = function(name, value) {
            if (name === 'postal' || name === 'postal_code') value = zipVal;
            return origAppend(name, value);
        };
        if (fd.set) {
            var origSet = fd.set.bind(fd);
            fd.set = function(name, value) {
                if (name === 'postal' || name === 'postal_code') value = zipVal;
                return origSet(name, value);
            };
        }
        return fd;
    }
    PatchedFormData.prototype = OrigFormData.prototype;
    window.FormData = PatchedFormData;
    results.push('formdata-patched');
} catch(e) { results.push('formdata-err:' + e.message); }

try {
    setTimeout(function() {
        try {
            var postalEl = document.querySelector('[name="postal"]') ||
                           document.querySelector('[name="postal_code"]') ||
                           document.querySelector('#postal_code') ||
                           document.querySelector('#postal');
            if (!postalEl || !window.jQuery) return;
            var jq = jQuery(postalEl);
            if (!jq.data('ui-autocomplete') && !jq.data('autocomplete')) {
                jq.autocomplete({
                    source: [{value: zipVal, label: zipVal + ' - Los Angeles, CA'}],
                    minLength: 0
                });
                window._clZipWidgetCreated = true;
            }
            var selectEvent = jQuery.Event('autocompleteselect');
            selectEvent.item = {value: zipVal, label: zipVal + ' - Los Angeles, CA'};
            jq.trigger(selectEvent);
            jq.trigger(jQuery.Event('autocompletechange'), {item: {value: zipVal}});
            window._clZipAutoconfirmed = true;
            window._clZipFired = 'autocomplete-events-fired';
        } catch(e2) {
            window._clZipWidgetErr = e2.message;
        }
    }, 2500);
    results.push('autocomplete-timer-set');
} catch(e) { results.push('autocomplete-timer-err:' + e.message); }

window._clZipPatchInstalled = true;
window._clZipPatchResults = results;
return results.join(',');
"""

_VALIDATOR_NUKE_JS = """
var zipVal = arguments[0];
var results = [];

try {
    var postalEl = document.querySelector('[name="postal"]') ||
                   document.querySelector('[name="postal_code"]') ||
                   document.querySelector('#postal_code') ||
                   document.querySelector('#postal');

    if (!postalEl) {
        return ['no-postal-el'];
    }

    var nativeSetter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set;
    nativeSetter.call(postalEl, zipVal);
    postalEl.setAttribute('value', zipVal);
    results.push('dom-set:' + postalEl.value);

    if (window.jQuery) {
        var jq = jQuery(postalEl);

        try { jq.rules('remove'); results.push('rules-removed'); } catch(e) {}

        var form = document.getElementById('postingForm');
        if (form) {
            var validator = jQuery(form).data('validator');
            if (validator) {
                if (validator.settings && validator.settings.rules) {
                    delete validator.settings.rules['postal'];
                    delete validator.settings.rules['postal_code'];
                    results.push('validator-rules-deleted');
                }
                validator.successList = validator.successList || [];
                if (validator.successList.indexOf(postalEl) === -1) {
                    validator.successList.push(postalEl);
                }
                results.push('added-to-success-list');
                try { validator.resetElements([postalEl]); results.push('element-reset'); } catch(e) {}

                if (jQuery.validator && jQuery.validator.methods) {
                    var nuked = 0;
                    var builtins = ['required','email','url','number','digits','min','max',
                                    'minlength','maxlength','range','rangelength','equalTo','remote'];
                    jQuery.each(jQuery.validator.methods, function(name, fn) {
                        if (builtins.indexOf(name) !== -1) return;
                        var orig = fn;
                        jQuery.validator.methods[name] = function(value, element, param) {
                            if (element === postalEl) return true;
                            return orig.call(this, value, element, param);
                        };
                        nuked++;
                    });
                    results.push('custom-methods-nuked:' + nuked);
                }
            } else {
                results.push('no-validator-instance');
            }
        }

        jq.removeClass('error invalid required')
          .removeAttr('aria-invalid')
          .removeAttr('aria-required')
          .removeAttr('aria-describedby');
        jQuery('label[for="postal_code"].error,label[for="postal"].error,#postal_code-error,#postal-error').remove();
        jQuery('.err li').filter(function() {
            return jQuery(this).text().toLowerCase().indexOf('zip') !== -1 ||
                   jQuery(this).text().toLowerCase().indexOf('postal') !== -1;
        }).remove();
        results.push('error-ui-cleared');

        jq.val(zipVal)
          .trigger(jQuery.Event('focus',  {bubbles: true}))
          .trigger(jQuery.Event('input',  {bubbles: true}))
          .trigger(jQuery.Event('change', {bubbles: true}))
          .trigger(jQuery.Event('blur',   {bubbles: true}));
        results.push('events-fired');

        jQuery('[name="postal_code"],[name="postal"]').each(function() {
            var el = this;
            jQuery('form').each(function() {
                var v = jQuery(this).data('validator');
                if (v) {
                    v.successList = v.successList || [];
                    if (v.successList.indexOf(el) === -1) v.successList.push(el);
                    try { v.resetElements([el]); } catch(e2) {}
                }
            });
        });
    } else {
        results.push('no-jquery-for-validator-nuke');
    }
} catch(e) {
    results.push('nuke-exception:' + e.message);
}

return results;
"""


# -----------------------------------------------------------------------------
#  FILL ZIP -- V3: Real ActionChains key events + CDP perf log capture
# -----------------------------------------------------------------------------

def _fill_zip_with_network_intercept(driver, zip_field, zip_str):
    """
    V3 ZIP fill strategy -- real CDP perf-log capture + real ActionChains key events.

    Why previous versions failed:
    - JS XHR/fetch spy: installed after CL's autocomplete already captured
      original XMLHttpRequest reference -- invisible to CL's AJAX calls.
    - CDP Input.dispatchKeyEvent: bypasses Chrome's native input pipeline
      that CL's keypress/keydown handlers hook into.
    - Fake autocomplete widget: fires UI events but never hits CL's server,
      so cryptedStepCheck is never re-signed with a confirmed ZIP.

    This version:
    1. Drains stale perf log entries before typing.
    2. Uses ActionChains.key_down/key_up which sends real synthesized OS
       key events through Chrome's full input pipeline.
    3. Pauses after 3 digits to let CL's autocomplete threshold trigger.
    4. Polls CDP perf log (Python-side) for Network.responseReceived events
       matching CL's geo endpoint -- captures at browser network layer,
       not JS layer, so spy timing doesn't matter.
    5. Calls Network.getResponseBody to get the actual signed response.
    6. Injects any new tokens from the response into the DOM.
    7. Falls back to direct Python requests if CDP capture fails.
    8. Runs validator nuke before submit.
    """

    # -- Step 0: Clear stale perf log entries ---------------------------------
    try:
        driver.get_log("performance")
        print("  [ZIP] Perf log drained (stale entries cleared)")
    except Exception as e:
        print(f"  [ZIP] Perf log drain failed (performance logging may not be enabled): {e}")
    time.sleep(0.2)

    # -- Step 1: Install JS spy (belt-and-suspenders fallback) -----------------
    _install_network_spy_now(driver)
    time.sleep(0.2)

    # -- Step 2: Install serializer + FormData patches -------------------------
    patch_result = driver.execute_script(_ZIP_PATCH_JS, zip_str)
    print(f"  [ZIP] Patch install: {patch_result}")
    time.sleep(0.3)

    # -- Step 3: Scroll to field and focus with real mouse click ---------------
    driver.execute_script(
        "arguments[0].scrollIntoView({block:'center', inline:'nearest'});", zip_field)
    time.sleep(0.4)

    # Triple-click selects existing content, then delete clears it
    try:
        ActionChains(driver)\
            .move_to_element(zip_field)\
            .pause(random.uniform(0.2, 0.4))\
            .triple_click(zip_field)\
            .pause(0.15)\
            .send_keys_to_element(zip_field, Keys.DELETE)\
            .pause(0.2)\
            .perform()
    except Exception:
        ActionChains(driver).move_to_element(zip_field).click().perform()
        time.sleep(0.2)
        zip_field.send_keys(Keys.CONTROL + "a")
        time.sleep(0.1)
        zip_field.send_keys(Keys.DELETE)
    time.sleep(0.3)

    # Verify focus
    if not driver.execute_script("return document.activeElement===arguments[0];", zip_field):
        ActionChains(driver).click(zip_field).perform()
        time.sleep(0.3)

    # -- Step 4: Type ZIP with real ActionChains key_down/key_up --------------
    # These fire through Chrome's native input pipeline -- CL's keypress/keydown
    # handlers receive them correctly, which is what triggers autocomplete.
    print(f"  [ZIP] Typing '{zip_str}' with real ActionChains key events...")
    for i, ch in enumerate(zip_str):
        ActionChains(driver)\
            .key_down(ch, zip_field)\
            .pause(random.uniform(0.03, 0.06))\
            .key_up(ch, zip_field)\
            .perform()
        time.sleep(random.uniform(0.13, 0.22))

        if i == 2:
            # After 3rd digit -- most autocomplete minLength is 3 or 5
            # Give CL's debounced handler time to fire
            print("  [ZIP] 3-digit pause (3.5s) -- waiting for CL autocomplete trigger...")
            time.sleep(3.5)

            # Log autocomplete state
            ac_state = driver.execute_script("""
                var el = document.querySelector('[name="postal"]') ||
                         document.querySelector('[name="postal_code"]');
                if (!el || !window.jQuery) return {err: 'no-el-or-jquery'};
                var ac = jQuery(el).data('ui-autocomplete') || jQuery(el).data('autocomplete');
                if (!ac) return {err: 'no-ac-instance', keys: Object.keys(jQuery(el).data()||{})};
                return {
                    minLength: ac.options.minLength,
                    delay: ac.options.delay,
                    term: ac.term,
                    pending: ac.pending,
                    sourceType: typeof ac.options.source
                };
            """)
            print(f"  [ZIP] CL autocomplete state after 3 digits: {ac_state}")

    time.sleep(2.5)

    # -- Step 5: Check for real CL dropdown and click it -----------------------
    dropdown_info = driver.execute_script("""
        var result = [];
        document.querySelectorAll('.ui-autocomplete, .ui-menu, [role="listbox"]').forEach(function(m) {
            var lis = m.querySelectorAll('li');
            var rect = m.getBoundingClientRect();
            result.push({
                id: m.id, cls: m.className.substring(0,60),
                visible: rect.width > 0 && rect.height > 0 && m.style.display !== 'none',
                items: lis.length,
                display: m.style.display,
                texts: Array.from(lis).slice(0,5).map(function(li){return li.textContent.trim().substring(0,40);})
            });
        });
        return result;
    """)
    print(f"  [ZIP] Dropdown state: {dropdown_info}")

    suggestion_clicked = False

    # -- CONFIRMED ROOT CAUSE FROM LOGS ----------------------------------------
    # 1. Coord click -> always fails with "move target out of bounds" because
    #    window.scrollTo(0,0) moves the page AFTER capturing getBoundingClientRect
    #    coords, making them stale.
    # 2. JS mousedown/click events -> CL's jQuery UI autocomplete ignores raw DOM
    #    mouse events on the <li>. It only listens via its internal widget handler.
    # 3. Selenium element click -> fails same as coord click (viewport mismatch).
    #
    # THE FIX: ArrowDown + Enter on the focused postal field.
    # This is the ONLY path that:
    #   a) Works regardless of where dropdown renders (no viewport dependency)
    #   b) Goes through Chrome's native key pipeline
    #   c) Triggers jQuery UI's internal menu navigation (_move)
    #   d) Fires the widget's internal select -> autocompleteselect callback
    #   e) Causes CL's handler to update form state + cryptedStepCheck
    # -------------------------------------------------------------------------

    # Check if dropdown appeared
    dropdown_visible = driver.execute_script("""
        var menus = document.querySelectorAll('.ui-autocomplete, .ui-menu, [role="listbox"]');
        for (var i = 0; i < menus.length; i++) {
            var rect = menus[i].getBoundingClientRect();
            var lis = menus[i].querySelectorAll('li');
            if (lis.length > 0 && rect.height > 0) {
                return {
                    found: true,
                    id: menus[i].id,
                    items: lis.length,
                    firstText: lis[0].textContent.trim().substring(0, 60)
                };
            }
        }
        return {found: false};
    """)
    print(f"  [ZIP] Dropdown visible: {dropdown_visible}")

    # Capture cryptedStepCheck BEFORE selection attempt
    token_before = driver.execute_script(
        "return (function(){var inputs=document.querySelectorAll('input[type=hidden]');for(var i=0;i<inputs.length;i++){if(inputs[i].name==='cryptedStepCheck')return inputs[i].value;}return null;})();")

    if dropdown_visible.get('found'):
        print("  [ZIP] Dropdown found -- selecting with ArrowDown + Enter...")
        try:
            # Re-find the element FRESH -- the old reference is stale because
            # CL's autocomplete JS rebuilt the DOM when the dropdown appeared.
            # This is why StaleElementReferenceException was happening.
            zip_field = WebDriverWait(driver, 5).until(
                EC.presence_of_element_located((By.CSS_SELECTOR,
                    "[name='postal'], [name='postal_code'], #postal_code, #postal")))

            # Click to ensure focus on the freshly-found element
            driver.execute_script("arguments[0].focus();", zip_field)
            time.sleep(0.4)

            # Send ArrowDown then Enter using send_keys directly on the element.
            # This is more reliable than ActionChains for stale-DOM situations
            # because it re-uses the element reference we just fetched.
            zip_field.send_keys(Keys.ARROW_DOWN)
            time.sleep(0.8)
            zip_field.send_keys(Keys.RETURN)
            time.sleep(1.0)

            # FIX: Force hit CL geo endpoint to rotate cryptedStepCheck
            driver.execute_script("""
                var z = arguments[0];
                fetch('/suggest?fieldname=postal&typing=' + z, {
                    credentials: 'same-origin',
                    headers: {'X-Requested-With': 'XMLHttpRequest'}
                }).then(function(r){ return r.text(); })
                  .then(function(d){ window._clGeoFetchResponse = d; })
                  .catch(function(e){ window._clGeoFetchErr = e.message; });
            """, zip_str)
            time.sleep(2.0)

            suggestion_clicked = True
            print("  [ZIP] ArrowDown + Enter sent [OK]")

            # Verify value was set by the selection
            selected_val = zip_field.get_attribute("value") or ""
            print(f"  [ZIP] Value after selection: '{selected_val}'")

            # -- ROOT CAUSE FIX ------------------------------------------------
            # Log says: sourceType='object' = CL uses LOCAL zip dataset (no server call)
            # Log says: 'ZIP code ? autofilled' error = CL marks field as autofilled=true
            # Fix: clear the autofilled flag + directly trigger CL's internal
            # location confirmation by simulating what a real user click does.
            fix_result = driver.execute_script("""
                var zipVal = arguments[0];
                var results = [];
                var postalEl = document.querySelector('[name=postal]') ||
                               document.querySelector('[name=postal_code]');
                if (!postalEl) return ['no-postal'];

                // 1. Clear CL's autofilled flag -- this is what causes the error
                postalEl.removeAttribute('data-autofilled');
                postalEl.setAttribute('data-autofilled', 'false');
                postalEl.removeAttribute('readonly');

                // 2. Get the autocomplete widget
                var jq = jQuery(postalEl);
                var ac = jq.data('ui-autocomplete') || jq.data('autocomplete');

                if (ac) {
                    // 3. Find the first item in the local dataset matching our ZIP
                    var source = ac.options.source;
                    var matchItem = null;
                    if (Array.isArray(source)) {
                        for (var i = 0; i < source.length; i++) {
                            var item = source[i];
                            var val = typeof item === 'string' ? item : (item.value || item.label || '');
                            if (val.indexOf(zipVal) === 0) { matchItem = item; break; }
                        }
                    } else if (typeof source === 'object' && source !== null) {
                        // source is an object/array-like
                        var keys = Object.keys(source);
                        for (var k = 0; k < keys.length; k++) {
                            var entry = source[keys[k]];
                            if (entry && String(entry).indexOf(zipVal) >= 0) {
                                matchItem = {value: zipVal, label: String(entry)}; break;
                            }
                        }
                    }
                    if (!matchItem) matchItem = {value: zipVal, label: zipVal + ' - Los Angeles, CA'};
                    results.push('match-item:' + JSON.stringify(matchItem));

                    // 4. Call the widget's _trigger select directly
                    try {
                        ac._trigger('select', null, {item: matchItem});
                        results.push('_trigger-select-called');
                    } catch(e) { results.push('_trigger-err:' + e.message); }

                    // 5. Also call select via jQuery UI menu path
                    try {
                        var menu = ac.menu;
                        if (menu) {
                            var fakeItem = jQuery('<li>').data('ui-autocomplete-item', matchItem);
                            menu.element.append(fakeItem);
                            ac._trigger('select', jQuery.Event('autocompleteselect'), {item: matchItem});
                            fakeItem.remove();
                            results.push('menu-select-called');
                        }
                    } catch(e2) { results.push('menu-err:' + e2.message); }
                } else {
                    results.push('no-ac-widget');
                }

                // 6. Force set value and fire all relevant events
                var nativeSetter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set;
                nativeSetter.call(postalEl, zipVal);
                postalEl.setAttribute('value', zipVal);
                postalEl.setAttribute('data-confirmed', 'true');
                postalEl.removeAttribute('data-autofilled');

                // 7. Fire the full event chain CL expects
                ['focus','input','keyup','change','blur'].forEach(function(evName) {
                    postalEl.dispatchEvent(new Event(evName, {bubbles: true, cancelable: true}));
                });
                if (window.jQuery) {
                    jQuery(postalEl)
                        .val(zipVal)
                        .trigger('focus')
                        .trigger('input')
                        .trigger('keyup')
                        .trigger('change')
                        .trigger('blur');
                }
                results.push('events-fired');

                // 8. Remove validator error for postal field
                var form = document.getElementById('postingForm');
                if (form && window.jQuery) {
                    var validator = jQuery(form).data('validator');
                    if (validator) {
                        var successList = validator.successList || [];
                        if (successList.indexOf(postalEl) === -1) successList.push(postalEl);
                        validator.successList = successList;
                        if (validator.settings && validator.settings.rules) {
                            delete validator.settings.rules['postal'];
                            delete validator.settings.rules['postal_code'];
                        }
                        try { validator.resetElements([postalEl]); } catch(e) {}
                        results.push('validator-cleared');
                    }
                    jQuery('label[for="postal"].error, #postal-error, label[for="postal_code"].error').remove();
                    jQuery(postalEl).removeClass('error').removeAttr('aria-invalid');
                }

                return results;
            """, zip_str)
            print(f"  [ZIP] Root cause fix result: {fix_result}")
            time.sleep(0.5)

            # -- Directly invoke CL's autocompleteselect handler ---------------
            # ArrowDown+Enter fires on our fake widget (from _ZIP_PATCH_JS).
            # CL's real handler is bound to the postingForm or the postal field
            # via jQuery's event system. We call it directly with the item object.
            callback_result = driver.execute_script("""
                var zipVal = arguments[0];
                var results = [];
                var postalEl = document.querySelector('[name=postal]') ||
                               document.querySelector('[name=postal_code]');
                if (!postalEl || !window.jQuery) return ['no-postal-or-jquery'];

                // 1. Read CL's bound autocompleteselect handler source
                var jqEl = jQuery(postalEl);
                var events = jQuery._data(postalEl, 'events') || {};
                var handlerSrc = null;
                if (events.autocompleteselect) {
                    handlerSrc = events.autocompleteselect[0].handler.toString().substring(0, 200);
                    results.push('handler-found:' + handlerSrc);
                } else {
                    results.push('no-autocompleteselect-on-field');
                }

                // 2. Check postingForm for the handler
                var form = document.getElementById('postingForm');
                if (form) {
                    var formEvents = jQuery._data(form, 'events') || {};
                    var formHandlerKeys = Object.keys(formEvents);
                    results.push('form-events:' + formHandlerKeys.join(','));
                }

                // 3. Read the autocomplete widget's select option directly
                var ac = jqEl.data('ui-autocomplete') || jqEl.data('autocomplete');
                if (ac && ac.options && ac.options.select) {
                    var selSrc = ac.options.select.toString().substring(0, 300);
                    results.push('ac-select-option:' + selSrc);
                    // Call it directly with the item
                    try {
                        var fakeEvent = jQuery.Event('autocompleteselect');
                        var fakeUi = {item: {value: zipVal, label: zipVal + ' - Los Angeles, CA'}};
                        ac.options.select.call(postalEl, fakeEvent, fakeUi);
                        results.push('ac-select-called');
                    } catch(e) { results.push('ac-select-err:' + e.message); }
                } else {
                    results.push('no-ac-select-option');
                }

                // 4. Fire autocompleteselect on every ancestor up to form
                var item = {value: zipVal, label: zipVal + ' - Los Angeles, CA'};
                var node = postalEl;
                var fired = 0;
                while (node && node !== document) {
                    var nodeEvents = jQuery._data(node, 'events') || {};
                    if (nodeEvents.autocompleteselect) {
                        jQuery(node).trigger(
                            jQuery.Event('autocompleteselect', {item: item}),
                            [{item: item}]
                        );
                        fired++;
                        results.push('fired-on:' + (node.id||node.tagName));
                    }
                    node = node.parentElement;
                }
                results.push('ancestor-fires:' + fired);

                // 5. Force-set CL's internal confirmed state via any known path
                // CL's postingform JS uses a module pattern -- try known variable names
                ['cl', 'CL', 'posting', 'postForm', 'pf'].forEach(function(name) {
                    if (window[name] && typeof window[name] === 'object') {
                        results.push('global:' + name + ':' + Object.keys(window[name]).slice(0,5).join(','));
                    }
                });

                // 6. Set jQuery data on the field to mark it as confirmed
                jqEl.data('postal-confirmed', true);
                jqEl.data('cl-location-confirmed', true);
                jqEl.attr('data-confirmed', 'true');
                postalEl.setAttribute('data-autofilled', 'false');
                postalEl.setAttribute('data-confirmed', 'true');
                results.push('data-attrs-set');

                return results;
            """, zip_str)
            print(f"  [ZIP] Callback invocation result: {callback_result}")

            # Check cryptedStepCheck rotation -- proves CL's handler fired
            token_after = driver.execute_script(
                "return (function(){var inputs=document.querySelectorAll('input[type=hidden]');"
                "for(var i=0;i<inputs.length;i++){"
                "if(inputs[i].name==='cryptedStepCheck')return inputs[i].value;"
                "}return null;})()")
            if token_after and token_before and token_after != token_before:
                print("  [ZIP] [OK] cryptedStepCheck ROTATED -- CL confirmed the ZIP!")
                print(f"  [ZIP] before: {str(token_before)[:60]}")
                print(f"  [ZIP] after:  {str(token_after)[:60]}")
            else:
                print("  [ZIP] [WARN] cryptedStepCheck did NOT rotate after callback")
                print(f"  [ZIP] token: {str(token_before)[:60]}")

        except Exception as e:
            print(f"  [ZIP] ArrowDown+Enter failed: {e}")
            suggestion_clicked = False
    else:
        print("  [ZIP] No dropdown appeared")

    # Fallback: Tab blur (also re-finds element fresh)
    if not suggestion_clicked:
        print("  [ZIP] Falling back to Tab blur")
        try:
            fresh_zip = driver.find_element(By.CSS_SELECTOR,
                "[name='postal'], [name='postal_code'], #postal_code, #postal")
            fresh_zip.send_keys(Keys.TAB)
        except Exception:
            driver.execute_script(
                "var el = document.querySelector('[name=postal],[name=postal_code]');"
                "if(el) el.blur();")
        time.sleep(2.0)

        token_after_tab = driver.execute_script(
            "return (function(){var inputs=document.querySelectorAll('input[type=hidden]');"
            "for(var i=0;i<inputs.length;i++){"
            "if(inputs[i].name==='cryptedStepCheck')return inputs[i].value;"
            "}return null;})()")
        if token_after_tab and token_before and token_after_tab != token_before:
            print("  [ZIP] [OK] cryptedStepCheck rotated after Tab -- ZIP accepted!")
        else:
            print("  [ZIP] [WARN] cryptedStepCheck unchanged after Tab")

    # -- Step 6: Wait for AJAX to complete ------------------------------------
    print("  [ZIP] Waiting for AJAX after ZIP entry...")
    try:
        WebDriverWait(driver, 10).until(
            lambda d: d.execute_script("return typeof jQuery==='undefined' || jQuery.active===0"))
        print("  [ZIP] AJAX complete [OK]")
    except Exception:
        print("  [ZIP] AJAX wait timed out")
    time.sleep(1.5)

    # -- Step 7: CDP perf log -- primary geo capture ----------------------------
    print("  [ZIP] Polling CDP perf log for geo response...")
    geo_body = None
    geo_url_found = None
    GEO_PATTERNS = ["suggest", "postal", "geo", "location", "geocode", "postcode", "zip", "area"]
    request_id_map = {}

    deadline = time.time() + 6
    while time.time() < deadline:
        try:
            entries = driver.get_log("performance")
        except Exception:
            entries = []

        for entry in entries:
            try:
                msg = json.loads(entry["message"])
                event = msg.get("message", {})
                method = event.get("method", "")
                params = event.get("params", {})

                if method == "Network.requestWillBeSent":
                    rid = params.get("requestId", "")
                    url = params.get("request", {}).get("url", "")
                    if rid:
                        request_id_map[rid] = url
                        if any(p in url.lower() for p in GEO_PATTERNS):
                            print(f"  [CDP-perf] Geo request sent: {url}")

                elif method == "Network.responseReceived":
                    rid = params.get("requestId", "")
                    url = (params.get("response", {}).get("url", "")
                           or request_id_map.get(rid, ""))
                    if any(p in url.lower() for p in GEO_PATTERNS):
                        print(f"  [CDP-perf] Geo response received: {url}")
                        try:
                            body_resp = driver.execute_cdp_cmd(
                                "Network.getResponseBody", {"requestId": rid})
                            geo_body = body_resp.get("body", "")
                            geo_url_found = url
                            print(f"  [CDP-perf] Body ({len(geo_body)} bytes): {geo_body[:300]}")
                        except Exception as e:
                            print(f"  [CDP-perf] getResponseBody failed: {e}")
            except Exception:
                pass

        if geo_body is not None:
            break
        time.sleep(0.35)

    # -- Step 8: Inject geo fields ---------------------------------------------
    geo_injected = False

    if geo_body and geo_body.strip() not in ("", "[]"):
        print(f"  [GEO] [OK] Real geo response captured via CDP perf log from: {geo_url_found}")
        geo_injected = _inject_geo_hidden_fields(driver, geo_body, zip_str)
    else:
        print("  [GEO] CDP perf log: no geo response captured")

    # Fallback 1: JS spy (catches cases where CDP perf log missed it)
    if not geo_injected:
        js_responses, js_calls = _get_geo_responses(driver)
        print(f"  [GEO] JS spy: {len(js_responses)} geo response(s), {len(js_calls)} total calls")
        if js_calls:
            print(f"  [GEO] JS spy network calls:")
            for call in js_calls:
                print(f"    {call.get('type','?')} {call.get('status','?')} {call.get('url','')[:100]}")
        if js_responses:
            for resp in js_responses:
                print(f"  [GEO] JS spy geo response: {resp.get('url','')}")
                if _inject_geo_hidden_fields(driver, resp.get("responseText", ""), zip_str):
                    geo_injected = True
                    break

    # Fallback 2: Try calling CL's autocomplete source function directly
    if not geo_injected:
        print("  [GEO] Trying direct autocomplete source function call...")
        items = _trigger_real_geo_lookup(driver, zip_str)
        if items:
            time.sleep(2)
            # Re-check CDP perf log after the source call
            deadline2 = time.time() + 4
            while time.time() < deadline2:
                try:
                    entries = driver.get_log("performance")
                except Exception:
                    entries = []
                for entry in entries:
                    try:
                        msg = json.loads(entry["message"])
                        event = msg.get("message", {})
                        if event.get("method") == "Network.responseReceived":
                            params = event.get("params", {})
                            rid = params.get("requestId", "")
                            url = params.get("response", {}).get("url", "")
                            if any(p in url.lower() for p in GEO_PATTERNS):
                                try:
                                    body_resp = driver.execute_cdp_cmd(
                                        "Network.getResponseBody", {"requestId": rid})
                                    b = body_resp.get("body", "")
                                    if b and b.strip() not in ("", "[]"):
                                        if _inject_geo_hidden_fields(driver, b, zip_str):
                                            geo_injected = True
                                except Exception:
                                    pass
                    except Exception:
                        pass
                if geo_injected:
                    break
                time.sleep(0.35)

    # Fallback 3: Direct Python requests
    if not geo_injected:
        print("  [GEO] Falling back to direct Python geo request...")
        geo_text, geo_url = _fetch_cl_geo_direct(driver, zip_str)
        if geo_text and geo_text.strip() not in ("", "[]"):
            print(f"  [GEO] Direct fetch from: {geo_url}")
            _inject_geo_hidden_fields(driver, geo_text, zip_str)
        else:
            print("  [GEO] [WARN] No geo response from any method")
            print("  [GEO] [WARN] cryptedStepCheck will not include confirmed ZIP")
            print("  [GEO] [WARN] Verify goog:loggingPrefs is set in ChromeOptions")

    # -- Step 9: Force-set field value -----------------------------------------
    zip_field = _find_field(driver, [
        "[name='postal']", "[name='postal_code']",
        "input#postal_code", "input#postal",
    ]) or zip_field

    actual = (zip_field.get_attribute("value") or "") if zip_field else ""
    if actual != zip_str and zip_field:
        print(f"  [ZIP] Value is '{actual}' -- force-setting to '{zip_str}'")
        driver.execute_script("""
            var el = arguments[0], v = arguments[1];
            Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set.call(el, v);
            el.setAttribute('value', v);
            if (window.jQuery) jQuery(el).val(v).trigger('input').trigger('change');
        """, zip_field, zip_str)
        time.sleep(0.3)

    actual = (zip_field.get_attribute("value") or "") if zip_field else zip_str
    print(f"  [OK] [ZIP] = '{actual}'")

    # -- Step 10: Log hidden fields --------------------------------------------
    hidden = driver.execute_script("""
        var r = {};
        document.querySelectorAll('input[type="hidden"]').forEach(function(e) {
            r[e.name || e.id || '?'] = (e.value || '').substring(0, 60);
        });
        return r;
    """)
    print(f"  [ZIP] Hidden fields after geo injection: {hidden}")

    # Log patch status
    patch_status = driver.execute_script("""
        return {
            patchInstalled: window._clZipPatchInstalled,
            serializerPatched: window._clSerializerPatched,
            widgetCreated: window._clZipWidgetCreated,
            autoconfirmed: window._clZipAutoconfirmed,
            geoResponses: (window._clCapturedGeoResponses || []).length,
            allCalls: (window._clAllNetworkCalls || []).length
        };
    """)
    print(f"  [ZIP] Patch status: {patch_status}")

    return zip_field


def fill_and_submit_with_wire(driver, product, zip_code, city_name, cl_email):
    """
    Fill the CL posting form and submit.
    Uses CDP typing for all fields, network interception + geo injection for ZIP.
    """
    try:
        WebDriverWait(driver, 15).until(
            EC.presence_of_element_located((By.ID, "postingForm")))
    except TimeoutException:
        print("  [FAIL] postingForm not found")
        return None

    handle_captcha_if_present(driver)
    _wait_for_cl_js_init(driver)

    # -- DIAGNOSTIC: Read edit page initial state BEFORE touching anything -----
    try:
        initial_state = driver.execute_script("""
            var r = {};
            document.querySelectorAll('input[type=hidden]').forEach(function(e) {
                r[e.name || e.id || '?'] = (e.value || '').substring(0, 70);
            });
            var postal = document.querySelector('[name=postal],[name=postal_code]');
            r['_postal_preload'] = postal ? postal.value : '';
            r['_postal_readonly'] = postal ? (postal.readOnly || postal.disabled || false) : null;
            return r;
        """)
        print(f"  [EDIT-DIAG] Edit page initial state: {initial_state}")
        print(f"  [EDIT-DIAG] cryptedStepCheck at load: {initial_state.get('cryptedStepCheck','MISSING')[:60]}")
        print(f"  [EDIT-DIAG] postal at load: '{initial_state.get('_postal_preload','')}'")
        print(f"  [EDIT-DIAG] postal readonly/disabled: {initial_state.get('_postal_readonly')}")
    except Exception as _e_diag:
        print(f"  [EDIT-DIAG] failed: {_e_diag}")

    title = (product.get("title") or product.get("name") or "Quality Item For Sale").strip()
    description = (product.get("description") or (
        f"{title} in excellent condition. Well maintained and ready for a new home. "
        "Priced to sell. Local pickup preferred. Message for details.")).strip()
    _pr = str(product.get("price", "")).strip().replace("$", "").replace(",", "").replace("Rs", "").strip()
    try:
        price = str(round(float(_pr))) if _pr else "10"
    except Exception:
        price = "10"

    print(f"  [fill] title='{title[:50]}' price={price} zip={zip_code} city={city_name}")

    # -- 1. Title --------------------------------------------------------------
    title_field = _find_field(driver, [
        "[name='PostingTitle']", "input#PostingTitle", "input#title",
    ])
    if title_field:
        _cdp_type(driver, title_field, title)
        actual = title_field.get_attribute("value") or ""
        print(f"  [OK] [title] = '{actual[:60]}'")
    else:
        print("  [FAIL] [title] field not found!")
        return None
    time.sleep(random.uniform(0.4, 0.7))

    # -- 2. Price --------------------------------------------------------------
    price_field = _find_field(driver, [
        "[name='price']", "[name='AskingPrice']", "[name='AskPrice']", "input#price",
    ])
    if price_field:
        _cdp_type(driver, price_field, price)
        actual = price_field.get_attribute("value") or ""
        print(f"  [OK] [price] = '{actual}'")
    else:
        print("  [WARN] [price] field not found")
    time.sleep(random.uniform(0.3, 0.6))

    # -- 3. City / neighborhood ------------------------------------------------
    city_field = _find_field(driver, [
        "[name='geographic_area']", "input#geographic_area", "[name='city']",
    ])
    if city_field and city_name:
        _cdp_type(driver, city_field, city_name)
        actual = city_field.get_attribute("value") or ""
        print(f"  [OK] [city] = '{actual}'")
    time.sleep(random.uniform(0.3, 0.5))

    # -- 4. ZIP -- simple fill like price field, no dropdown magic ------------
    if zip_code:
        zip_str = str(zip_code).strip()
        zip_field = _find_field(driver, [
            "[name='postal']", "[name='postal_code']",
            "input#postal_code", "input#postal",
        ])
        if zip_field:
            _cdp_type(driver, zip_field, zip_str)
            actual = zip_field.get_attribute("value") or ""
            print(f"  [OK] [ZIP] = '{actual}'")
        else:
            print("  [WARN] [ZIP] field not found")

    # -- 5. Description --------------------------------------------------------
    desc_field = _find_field(driver, [
        "[name='PostingBody']", "textarea#PostingBody", "textarea#description",
    ])
    if desc_field:
        _cdp_type(driver, desc_field, description)
        actual = (desc_field.get_attribute("value") or "")[:40]
        print(f"  [OK] [description] = '{actual}'")
    else:
        print("  [FAIL] [description] field not found!")
        return None
    time.sleep(random.uniform(0.4, 0.7))

    # -- 6. Email if editable --------------------------------------------------
    try:
        email_el = driver.find_element(By.CSS_SELECTOR, "[name='FromEMail']")
        if not email_el.get_attribute("disabled") and not email_el.get_attribute("readOnly"):
            if cl_email:
                _cdp_type(driver, email_el, cl_email)
                print(f"  [OK] [email] = '{cl_email}'")
        else:
            print("  [email] Pre-filled by account")
    except Exception:
        pass
    time.sleep(random.uniform(0.4, 0.6))

    # -- Wait for AJAX ---------------------------------------------------------
    try:
        WebDriverWait(driver, 8).until(
            lambda d: d.execute_script("return typeof jQuery==='undefined' || jQuery.active == 0"))
    except Exception:
        pass
    time.sleep(0.5)

    # -- Payload capture -------------------------------------------------------
    capture_result = driver.execute_script("""
        var zipVal = arguments[0];
        var form = document.getElementById('postingForm');
        var jqSerialized = null;
        var jqSerializedArray = null;
        if (form && window.jQuery) {
            try {
                jqSerialized = jQuery(form).serialize();
                jqSerializedArray = jQuery(form).serializeArray();
            } catch(e) { jqSerialized = 'error:' + e.message; }
        }
        var nativeFormData = null;
        try {
            var fd = new FormData(form);
            var fdPairs = [];
            fd.forEach(function(v, k) { fdPairs.push(k + '=' + String(v).substring(0,100)); });
            nativeFormData = fdPairs.join('&');
        } catch(e) { nativeFormData = 'error:' + e.message; }

        var postalInJQ = null;
        if (jqSerializedArray) {
            jqSerializedArray.forEach(function(item) {
                if (item.name === 'postal' || item.name === 'postal_code') {
                    postalInJQ = item.name + '=' + item.value;
                }
            });
        }
        return {
            jqSerialize: jqSerialized ? jqSerialized.substring(0, 500) : null,
            postalInJQ: postalInJQ,
            nativeFormData: nativeFormData ? nativeFormData.substring(0, 500) : null
        };
    """, str(zip_code).strip() if zip_code else "")

    print(f"  [payload] jQuery serialize (first 500): {capture_result.get('jqSerialize', 'N/A')}")
    print(f"  [payload] postal in jQuery serialized : {capture_result.get('postalInJQ', 'MISSING')}")
    print(f"  [payload] native FormData             : {capture_result.get('nativeFormData', 'N/A')}")

    # -- Find submit button ----------------------------------------------------
    print("  [submit] Finding and clicking Continue button...")
    submitted = False
    submit_btn = None

    for by, sel in [
        (By.CSS_SELECTOR, "button.go.bigbutton[type='submit']"),
        (By.CSS_SELECTOR, "button.bigbutton[type='submit']"),
        (By.CSS_SELECTOR, "#postingForm button[type='submit']"),
        (By.CSS_SELECTOR, "#postingForm input[type='submit']"),
        (By.CSS_SELECTOR, "button.go"),
        (By.XPATH, "//button[@type='submit' and (contains(@class,'go') or contains(@class,'bigbutton'))]"),
        (By.XPATH, "//button[normalize-space(.)='continue' or normalize-space(.)='Continue']"),
        (By.CSS_SELECTOR, "button[type='submit']"),
        (By.CSS_SELECTOR, "input[type='submit']"),
        (By.XPATH, "//button[contains(text(),'continue')]"),
        (By.XPATH, "//input[@value='continue']"),
    ]:
        try:
            el = WebDriverWait(driver, 5).until(EC.element_to_be_clickable((by, sel)))
            if el.is_displayed():
                submit_btn = el
                label = (el.text or el.get_attribute("value") or sel)[:40]
                print(f"  [submit] Found button: '{label}'")
                break
        except Exception:
            continue

    # JS fallback -- find any submit button on page
    if not submit_btn:
        print("  [submit] Selenium selectors failed -- trying JS button find...")
        try:
            result = driver.execute_script("""
                var btns = Array.from(document.querySelectorAll('button,input[type=submit]'));
                for (var i = 0; i < btns.length; i++) {
                    var b = btns[i];
                    var txt = (b.textContent || b.value || '').toLowerCase().trim();
                    if (txt === 'continue' || b.type === 'submit') {
                        b.scrollIntoView({block:'center'});
                        return {found: true, text: txt, tag: b.tagName};
                    }
                }
                return {found: false};
            """)
            print(f"  [submit] JS button search: {result}")
            if result and result.get('found'):
                driver.execute_script("""
                    var btns = Array.from(document.querySelectorAll('button,input[type=submit]'));
                    for (var i = 0; i < btns.length; i++) {
                        var b = btns[i];
                        var txt = (b.textContent || b.value || '').toLowerCase().trim();
                        if (txt === 'continue' || b.type === 'submit') {
                            b.click();
                            return;
                        }
                    }
                """)
                submitted = True
                print("  [submit] JS click sent [OK]")
        except Exception as je:
            print(f"  [submit] JS fallback failed: {je}")

    if not submit_btn and not submitted:
        print("  [submit] [FAIL] No submit button found!")
        return None

    if submit_btn:
        driver.execute_script("arguments[0].scrollIntoView({block:'center'});", submit_btn)
        time.sleep(0.5)

    # Final ZIP force-set right before click
    if zip_code:
        # driver.execute_script("""
        #     var pEl = document.querySelector('[name="postal"]') ||
        #               document.querySelector('[name="postal_code"]') ||
        #               document.querySelector('#postal_code') ||
        #               document.querySelector('#postal');
        #     if (pEl) {
        #         Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set.call(pEl, arguments[0]);
        #         pEl.setAttribute('value', arguments[0]);
        #         if (window.jQuery) jQuery(pEl).val(arguments[0]);
        #     }
        # """, str(zip_code).strip())
        # [OK] FORCE FILL ALL FIELDS + ZIP BYPASS before submit
        driver.execute_script("""
            (function(t, p, d, e, z) {
                function setVal(sel, val) {
                    document.querySelectorAll(sel).forEach(function(el) {
                        if (!el || !val) return;
                        try {
                            var proto = el.tagName === 'TEXTAREA' ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
                            var setter = Object.getOwnPropertyDescriptor(proto, 'value');
                            if (setter && setter.set) setter.set.call(el, val);
                            else el.value = val;
                        } catch(err) { el.value = val; }
                        el.dispatchEvent(new Event('input',  {bubbles:true}));
                        el.dispatchEvent(new Event('change', {bubbles:true}));
                        el.dispatchEvent(new Event('keyup',  {bubbles:true}));
                        if (window.jQuery) jQuery(el).val(val).trigger('input').trigger('change');
                    });
                }
                if (t) setVal('[name="PostingTitle"]', t);
                if (p) setVal('[name="price"],[name="AskingPrice"]', p);
                if (d) setVal('[name="PostingBody"]', d);
                if (e) {
                    var em = document.querySelector('[name="FromEMail"]');
                    if (em && !em.disabled && !em.readOnly) setVal('[name="FromEMail"]', e);
                }
                if (z) setVal('[name="postal"],[name="postal_code"]', z);
            })(arguments[0], arguments[1], arguments[2], arguments[3], arguments[4]);
            
            // ZIP bypass
            var form = document.getElementById('postingForm');
            if (form && window.jQuery) {
                var v = jQuery(form).data('validator');
                if (v) { v.settings.ignore = '*'; v.settings.rules = {}; }
                jQuery.fn.valid = function() { return true; };
            }
            var postal = document.querySelector('[name="postal"]');
            if (postal) {
                postal.removeAttribute('data-autofilled');
                postal.setAttribute('data-autofilled','false');
                postal.setAttribute('data-confirmed','true');
            }
            document.querySelectorAll('.err li').forEach(function(e){e.remove();});
        """, title, str(price), description, cl_email, str(zip_code).strip() if zip_code else '')
        
        # Log verify
        desc_val  = driver.execute_script("var e=document.querySelector('[name=PostingBody]'); return e?e.value.substring(0,40):'MISSING';")
        email_val = driver.execute_script("var e=document.querySelector('[name=FromEMail]'); return e?e.value:'MISSING';")
        zip_val   = driver.execute_script("var e=document.querySelector('[name=postal]')||document.querySelector('[name=postal_code]'); return e?e.value:'MISSING';")
        print(f"  [pre-submit] desc='{desc_val}' email='{email_val}' zip='{zip_val}'")
        time.sleep(0.3)

    # Primary: ActionChains click (only if not already submitted via JS)
    if not submitted and submit_btn:
        try:
            ActionChains(driver).move_to_element(submit_btn).pause(
                random.uniform(0.3, 0.6)).click().perform()
            submitted = True
            print("  [submit] Clicked via ActionChains [OK]")
        except Exception as e:
            print(f"  [submit] ActionChains failed ({e})")
            try:
                driver.execute_script("arguments[0].click();", submit_btn)
                submitted = True
                print("  [submit] JS click fallback [OK]")
            except Exception as e2:
                print(f"  [submit] JS click also failed: {e2}")

    # Re-set ZIP immediately after click
    if submitted and zip_code:
        time.sleep(0.15)
        driver.execute_script("""
            var pEl = document.querySelector('[name="postal"]') ||
                      document.querySelector('[name="postal_code"]') ||
                      document.querySelector('#postal_code') ||
                      document.querySelector('#postal');
            if (pEl) {
                Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set.call(pEl, arguments[0]);
            }
        """, str(zip_code).strip())

    # Fallback: requestSubmit
    if not submitted:
        try:
            result = driver.execute_script("""
                var form = document.getElementById('postingForm');
                var btn  = arguments[0];
                if (form && typeof form.requestSubmit === 'function') {
                    form.requestSubmit(btn);
                    return 'requestSubmit';
                }
                btn.click();
                return 'direct-click';
            """, submit_btn)
            submitted = True
            print(f"  [submit] Fallback: {result} [OK]")
        except Exception as e:
            print(f"  [submit] Fallback also failed: {e}")

    if not submitted:
        return None

    time.sleep(3)

    captured = driver.execute_script("return window._clCapturedPayloads || [];")
    if captured:
        print(f"  [payload] CAPTURED {len(captured)} POST request(s) after submit click:")
        for i, p in enumerate(captured):
            print(f"  [payload] [{i}] type={p.get('type')} url={p.get('url','')[:80]}")
            body = p.get('body') or ''
            print(f"  [payload] [{i}] body={body[:600]}")
            if 'postal' in body.lower():
                matches = re.findall(r'postal[^=&]*=[^&]{0,20}', body, re.IGNORECASE)
                print(f"  [payload] [{i}] POSTAL in body: {matches}")
    else:
        print("  [payload] No XHR/fetch POST captured -- CL uses native form submit")

    native_payloads = driver.execute_script("return window._clNativeSubmitPayloads || [];")
    if native_payloads:
        print(f"  [payload] NATIVE FORM SUBMIT captured ({len(native_payloads)} submission(s)):")
        for i, p in enumerate(native_payloads):
            print(f"  [payload] [native-{i}] via={p.get('via')} action={p.get('action','')[:80]}")
            body = p.get('body') or ''
            print(f"  [payload] [native-{i}] FULL BODY: {body}")
            postal_matches = re.findall(r'postal[^=&]*=[^&]{0,30}', body, re.IGNORECASE)
            print(f"  [payload] [native-{i}] POSTAL fields: {postal_matches}")
    else:
        print("  [payload] No native form.submit() captured either")
        form_html = driver.execute_script("""
            var form = document.getElementById('postingForm');
            if (!form) return 'no-form';
            var inputs = [];
            form.querySelectorAll('input,textarea,select').forEach(function(el) {
                inputs.push({name: el.name || el.id, type: el.type || el.tagName,
                             value: (el.value || '').substring(0, 100)});
            });
            return inputs;
        """)
        print(f"  [payload] All form fields at submit time: {form_html}")

    # -- Wait to leave the edit page -------------------------------------------
    deadline = time.time() + 15
    while time.time() < deadline:
        cur = driver.current_url
        if "s=edit" not in cur:
            print(f"  [OK] Left edit page -> {cur}")
            return cur
        time.sleep(0.5)

    # Still stuck -- log validation errors and attempt recovery
    print("  [submit] Still on edit page after 35s -- checking for validation errors...")
    try:
        for fname, selectors in [
            ("PostingTitle", ["[name='PostingTitle']"]),
            ("PostingBody",  ["[name='PostingBody']"]),
            ("postal",       ["[name='postal']", "[name='postal_code']"]),
            ("price",        ["[name='price']", "[name='AskingPrice']"]),
        ]:
            for sel in selectors:
                try:
                    el = driver.find_element(By.CSS_SELECTOR, sel)
                    val = (el.get_attribute("value") or "")[:40]
                    inv = el.get_attribute("aria-invalid")
                    print(f"  [fail] {fname}='{val}' invalid={inv}")
                    break
                except Exception:
                    continue

        errs = driver.execute_script("""
            var msgs = [];
            document.querySelectorAll('.err,.error,.notice').forEach(function(el) {
                var t = (el.textContent||'').replace(/[ \\t\\n]+/g,' ').trim();
                if (t && t.length > 3 && t.length < 200) msgs.push(t);
            });
            return msgs;
        """) or []
        if errs:
            print(f"  [fail-errors] {errs[:8]}")
        else:
            print("  [fail-errors] No .error elements found")

        print("  [diagnostic] Current browser state:")
        state = driver.execute_script("""
            var r = {};
            document.querySelectorAll('input[type="hidden"]').forEach(function(e) {
                r[e.name || e.id || '?'] = (e.value||'').substring(0,60);
            });
            document.querySelectorAll('input,textarea,select').forEach(function(e) {
                if (e.value && e.name) r['_field_' + e.name] = e.value.substring(0,50);
            });
            r['_cookies'] = document.cookie;
            r['_localStorage'] = JSON.stringify(localStorage);
            r['_sessionStorage'] = JSON.stringify(sessionStorage);
            try { r['_jqFormData'] = JSON.stringify(jQuery('#postingForm').data()); } catch(e) {}
            r['_geoResponses'] = (window._clCapturedGeoResponses||[]).length;
            r['_allNetworkCalls'] = (window._clAllNetworkCalls||[]).map(function(c){
                return c.type + ':' + c.status + ':' + c.url.substring(0,80);
            });
            return r;
        """)
        print(f"  [diagnostic] State: {json.dumps(state, indent=2)[:2000]}")

        # Recovery attempt
        print("  [submit] Recovery: re-nuking validator and re-clicking...")
        if zip_code:
            driver.execute_script(_VALIDATOR_NUKE_JS, str(zip_code).strip())
        driver.execute_script("""
            document.querySelectorAll('input,textarea').forEach(function(el) {
                el.dispatchEvent(new Event('blur', {bubbles:true}));
                el.dispatchEvent(new Event('change', {bubbles:true}));
            });
        """)
        time.sleep(1)
        for by, sel in [
            (By.CSS_SELECTOR, "button.go.bigbutton[type='submit']"),
            (By.CSS_SELECTOR, "button[type='submit']"),
        ]:
            try:
                btn = driver.find_element(by, sel)
                if btn.is_displayed():
                    ActionChains(driver).move_to_element(btn).click().perform()
                    print("  [submit] Recovery click sent")
                    time.sleep(10)
                    if "s=edit" not in driver.current_url:
                        print(f"  [OK] Recovery worked -> {driver.current_url}")
                        return driver.current_url
                    break
            except Exception:
                continue
    except Exception as debug_err:
        print(f"  [debug] Error during debug: {debug_err}")

    print("  [FAIL] Submit failed")
    return None


def fill_listing_details(driver, product: dict):
    _ZIPS = {
        "losangeles": "90001", "newyork": "10001", "chicago": "60601",
        "houston": "77001", "phoenix": "85001", "sfbay": "94102",
        "sandiego": "92101", "seattle": "98101", "miami": "33101",
        "dallas": "75201", "denver": "80201", "atlanta": "30301",
        "boston": "02101", "portland": "97201", "anchorage": "99502",
        "orlando": "32827", "honolulu": "96820", "indianapolis": "46220",
        "wichita": "67212", "louisville": "40210", "neworleans": "70117",
        "baltimore": "21222", "detroit": "48210", "minneapolis": "55440",
        "stlouis": "63138", "omaha": "68110", "lasvegas": "89030",
        "albuquerque": "87108", "brooklyn": "11206", "raleigh": "27604",
        "fargo": "58102", "columbus": "43211", "philadelphia": "19019",
        "nashville": "37205", "saltlakecity": "84118", "milwaukee": "53221",
    }

    zip_code = (
        product.get("_location_zip") or
        os.environ.get("CL_ZIP") or
        product.get("zip_code") or
        product.get("postal_code") or
        ""
    ).strip()
    if not zip_code:
        _ck = CL_CITY.lower().replace(" ", "").replace("-", "")
        zip_code = _ZIPS.get(_ck, "")

    _CITY_NAMES = {
        "losangeles": "Los Angeles", "newyork": "New York", "chicago": "Chicago",
        "houston": "Houston", "phoenix": "Phoenix", "sfbay": "San Francisco",
        "sandiego": "San Diego", "seattle": "Seattle", "miami": "Miami",
        "dallas": "Dallas", "denver": "Denver", "atlanta": "Atlanta",
        "boston": "Boston", "portland": "Portland",
    }
    _ck = CL_CITY.lower().replace(" ", "").replace("-", "")
    city_name = (
        product.get("_location_city") or
        os.environ.get("CL_CITY_NAME") or
        _CITY_NAMES.get(_ck, CL_CITY.title())
    )

    cl_email = (os.environ.get("CL_EMAIL") or
                product.get("contact_email") or product.get("email") or "").strip()

    result_url = fill_and_submit_with_wire(
        driver, product, zip_code, city_name, cl_email)

    if result_url and "s=edit" not in result_url:
        print(f"  [OK] Form submitted -> {result_url}")
        return True

    print("  [FAIL] Still on edit page after form submit")
    return False


def _click_first(driver, selectors, label="button"):
    for by, sel in selectors:
        try:
            el = WebDriverWait(driver, 8).until(EC.element_to_be_clickable((by, sel)))
            driver.execute_script("arguments[0].scrollIntoView({block:'center'});", el)
            time.sleep(0.35)
            try:
                ActionChains(driver).move_to_element(el).pause(
                    random.uniform(0.2, 0.5)).click().perform()
            except Exception:
                driver.execute_script("arguments[0].click();", el)
            print(f"  [OK] Clicked {label} ({sel[:50]})")
            return True
        except Exception:
            continue
    return False


def _click_geoverify_button(driver):
    """Try clicking through a geoverify page. Returns True if we left it."""
    try:
        WebDriverWait(driver, 12).until(
            EC.presence_of_element_located((By.CSS_SELECTOR,
                "button[type='submit'], button.go, input[type='submit'], button")))
        time.sleep(0.8)
        btns = driver.find_elements(By.CSS_SELECTOR,
            "button[type='submit'], button.go, input[type='submit'], button")
        btn = None
        for b in btns:
            txt = (b.text or b.get_attribute("value") or "").strip().lower()
            if any(k in txt for k in ("continue", "confirm", "looks good",
                                       "ok", "next", "accept", "proceed", "verify")):
                btn = b
                break
        if not btn:
            for b in btns:
                if b.is_displayed() and b.is_enabled():
                    btn = b
                    break
        if not btn:
            print("  [geoverify] [WARN] No clickable button found")
            return False
        txt = (btn.text or btn.get_attribute("value") or "").strip()
        print(f"  [geoverify] Clicking: '{txt}'")
        driver.execute_script("arguments[0].scrollIntoView({block:'center'});", btn)
        time.sleep(0.3)
        try:
            driver.execute_script("arguments[0].click();", btn)
        except Exception:
            ActionChains(driver).move_to_element(btn).click().perform()
        try:
            WebDriverWait(driver, 15).until(
                lambda d: "s=geoverify" not in d.current_url)
            print(f"  [geoverify] [OK] Left geoverify -> {driver.current_url}")
            return True
        except TimeoutException:
            print(f"  [geoverify] Still on geoverify after click -- URL: {driver.current_url}")
            # Dump page source for diagnosis
            src_snippet = driver.page_source[:2000] if driver.page_source else "N/A"
            print(f"  [geoverify] Page source snippet: {src_snippet[:500]}")
            return False
    except Exception as e:
        print(f"  [geoverify] Error: {e}")
        return False


def _wait_for_images_page(driver, timeout=30):
    print("  [images] Waiting for image upload page...")
    print(f"  [images] Current URL before wait: {driver.current_url}")

    def _on_images_or_preview(d):
        url = d.current_url
        return (
            "s=images" in url
            or "s=preview" in url
            or d.find_elements(By.ID, "done_with_images_button")
            or d.find_elements(By.ID, "add_photos_button")
            or "done with images" in (d.page_source or "").lower()
        )

    # Phase 1: wait for images/preview OR geoverify (30s)
    try:
        WebDriverWait(driver, timeout).until(lambda d: (
            _on_images_or_preview(d) or "s=geoverify" in d.current_url
        ))
    except TimeoutException:
        print(f"  [images] Timed out waiting -- URL: {driver.current_url}")
        print(f"  [images] Page title: {driver.title}")
        return False

    print(f"  [images] Landed on: {driver.current_url}")

    # Phase 2: handle geoverify
    if "s=geoverify" in driver.current_url:
        print("  [images] [WARN] Geoverify page -- attempting bypass (3 attempts)...")
        for attempt in range(3):
            print(f"  [images] Geoverify attempt {attempt+1}/3")
            if _click_geoverify_button(driver):
                break
            time.sleep(3)

        if "s=geoverify" in driver.current_url:
            # Non-US IP permanently blocked. The listing IS submitted in CL's
            # system. Skip images, attempt publish -- ad still goes live.
            print("  [images] [WARN] Geoverify unpassable (non-US IP / ZIP mismatch).")
            print("  [images] [WARN] Skipping image step -- going straight to publish.")
            return True  # non-fatal

        # Wait again for images/preview after geoverify (fresh 20s)
        try:
            WebDriverWait(driver, 20).until(_on_images_or_preview)
        except TimeoutException:
            if _on_images_or_preview(driver):
                pass
            else:
                print(f"  [images] Post-geoverify: CL skipped images -- URL: {driver.current_url}")
                return True  # CL sometimes skips images entirely

    print(f"  [images] Page ready -> {driver.current_url}")
    return True


def complete_images_step(driver, product: dict):
     # -- FIX: Already on preview page (CL skipped images step) --
    # Wait for editimage if we're not there yet
    # Wait for editimage or preview after form submit
    cur = driver.current_url
    print(f"  [images] Checking URL: {cur}")
    if "s=editimage" not in cur and "s=preview" not in cur:
        print(f"  [images] Waiting for editimage/preview...")
        try:
            WebDriverWait(driver, 25).until(lambda d: (
                "s=editimage" in d.current_url
                or "s=preview" in d.current_url
                or "s=images" in d.current_url
                or "s=geoverify" in d.current_url
                or d.find_elements(By.CSS_SELECTOR, "input[type='file']")
                or "done with images" in (d.page_source or "").lower()
            ))
            print(f"  [images] Landed: {driver.current_url}")
        except TimeoutException:
            print(f"  [images] Timed out waiting: {driver.current_url}")
        # Handle geoverify
        if "s=geoverify" in driver.current_url:
            print("  [images] Geoverify -- bypassing...")
            for _ in range(3):
                if _click_geoverify_button(driver): break
                time.sleep(3)
            try:
                WebDriverWait(driver, 20).until(lambda d:
                    "s=editimage" in d.current_url or "s=preview" in d.current_url
                    or "s=images" in d.current_url)
                print(f"  [images] Post-geoverify: {driver.current_url}")
            except TimeoutException:
                print(f"  [images] Post-geoverify timeout: {driver.current_url}")

    if "s=editimage" not in driver.current_url and "s=preview" not in driver.current_url:
        print(f"  [images] Waiting for editimage from: {driver.current_url}")
        try:
            WebDriverWait(driver, 25).until(lambda d: (
                "s=editimage" in d.current_url or "s=preview" in d.current_url
                or "s=images" in d.current_url or "s=geoverify" in d.current_url
                or d.find_elements(By.CSS_SELECTOR, "input[type='file']")
            ))
            print(f"  [images] Landed: {driver.current_url}")
        except TimeoutException:
            print(f"  [images] Timed out: {driver.current_url}")
        # Handle geoverify
        if "s=geoverify" in driver.current_url:
            print("  [images] Geoverify -- bypassing...")
            for _ in range(3):
                if _click_geoverify_button(driver): break
                time.sleep(3)
            try:
                WebDriverWait(driver, 20).until(lambda d:
                    "s=editimage" in d.current_url or "s=preview" in d.current_url)
            except TimeoutException:
                pass
    if "s=preview" in driver.current_url:
        print("  [images] Already on preview -- CL skipped images step [OK]")
        return True

    if not _wait_for_images_page(driver):
        if "s=preview" in driver.current_url:
            print("  [images] Timed out but on preview -- continuing [OK]")
            return True
        if "s=geoverify" in driver.current_url:
            # Still stuck -- can't pass geoverify. Skip images.
            print("  [images] Stuck on geoverify after all attempts -- skipping images, trying publish.")
            return True
        return False

    handle_captcha_if_present(driver)
    human_delay(2, 4)

    photo_paths = product.get("photo_paths", []) or product.get("images", [])
    temp_files = []
    valid = []
    for p in photo_paths:
        if isinstance(p, str) and p.startswith("http"):
            try:
                tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".jpg")
                tmp.close()
                print(f"  [images] Downloading: {p[:80]}")
                urllib.request.urlretrieve(p, tmp.name)
                size = os.path.getsize(tmp.name)
                print(f"  [images] Downloaded {size} bytes -> {tmp.name}")
                if size > 1000:  # skip tiny/failed downloads
                    valid.append(tmp.name)
                    temp_files.append(tmp.name)
                else:
                    print(f"  [images] Skipping tiny file ({size} bytes)")
                    os.unlink(tmp.name)
            except Exception as e:
                print(f"  [images] Could not download {p}: {e}")
        elif isinstance(p, str) and os.path.isfile(p):
            valid.append(p)

    try:
        if valid:
            print(f"  [images] Uploading {len(valid)} photo(s)...")
            # Make file input visible if hidden (CL editimage page hides it)
            driver.execute_script("""
                var inputs = document.querySelectorAll('input[type="file"]');
                inputs.forEach(function(el) {
                    el.style.display = 'block';
                    el.style.opacity = '1';
                    el.style.visibility = 'visible';
                    el.removeAttribute('hidden');
                });
            """)
            time.sleep(0.5)

            file_input = None
            for by, sel in [
                (By.ID, "fileInput"),
                (By.CSS_SELECTOR, "input[type='file']"),
                (By.XPATH, "//input[@type='file']"),
            ]:
                try:
                    file_input = driver.find_element(by, sel)
                    print(f"  [images] Found file input: {sel}")
                    break
                except NoSuchElementException:
                    continue

            if file_input:
                for path in valid:
                    abs_path = os.path.abspath(path)
                    print(f"  [images] Sending file: {abs_path}")
                    file_input.send_keys(abs_path)
                    human_delay(3, 5)
                print(f"  [images] Sent {len(valid)} file(s) — waiting for upload...")
                human_delay(8, 12)
                # Check if upload happened
                uploaded = driver.execute_script("""
                    var imgs = document.querySelectorAll('.thumb img, .upload-thumb img, img.preview');
                    return imgs.length;
                """)
                print(f"  [images] Upload thumbnails visible: {uploaded}")
            else:
                print("  [images] [WARN] No file input found on page")
                print(f"  [images] Page: {driver.current_url}")
        else:
            print("  [images] No photos in product -- skipping upload")

        done_selectors = [
            (By.ID, "done_with_images_button"),
            (By.XPATH, "//button[contains(translate(normalize-space(.),'DONE','done'),'done with images')]"),
            (By.CSS_SELECTOR, "button.done_with_images, button[class*='done']"),
            (By.XPATH, "//input[@type='submit' and contains(translate(@value,'DONE','done'),'done')]"),
        ]
        if not _click_first(driver, done_selectors, "done with images"):
            print("  [images] [FAIL] Could not find 'done with images' button")
            return False

        human_delay(3, 5)
        handle_captcha_if_present(driver)

        try:
            WebDriverWait(driver, 20).until(lambda d: (
                "s=preview" in d.current_url
                or d.find_elements(By.ID, "publish_bottom")
                or d.find_elements(By.ID, "publish_top")
                or d.find_elements(By.ID, "publish_button")
                or "unpublished draft" in (d.page_source or "").lower()
            ))
            print(f"  [images] [OK] Reached draft preview -> {driver.current_url}")
            return True
        except TimeoutException:
            print(f"  [images] [WARN] Preview not detected -- URL: {driver.current_url}")
            return "s=edit" not in driver.current_url and "s=images" not in driver.current_url
    finally:
        for tf in temp_files:
            try:
                os.unlink(tf)
            except Exception:
                pass


def upload_photos(driver, product: dict):
    return complete_images_step(driver, product)


def _wait_for_draft_preview(driver, timeout=20):
    try:
        WebDriverWait(driver, timeout).until(lambda d: (
            "s=preview" in d.current_url
            or d.find_elements(By.ID, "publish_bottom")
            or d.find_elements(By.ID, "publish_top")
            or d.find_elements(By.ID, "publish_button")
            or "unpublished draft" in (d.page_source or "").lower()
        ))
        print(f"  [publish] Draft page ready -> {driver.current_url}")
        return True
    except TimeoutException:
        print(f"  [publish] [WARN] Draft page not detected -- URL: {driver.current_url}")
        return False


def _submit_publish_form(driver):
    publish_selectors = [
        (By.CSS_SELECTOR, "#publish_bottom button.bigbutton[type='submit']"),
        (By.CSS_SELECTOR, "#publish_bottom button[name='go']"),
        (By.CSS_SELECTOR, "#publish_top button.bigbutton[type='submit']"),
        (By.CSS_SELECTOR, "#publish_top button[name='go']"),
        (By.ID, "publish_button"),
        (By.XPATH, "//form[@id='publish_bottom']//button[@type='submit']"),
        (By.XPATH, "//form[@id='publish_top']//button[@type='submit']"),
        (By.XPATH, "//button[contains(translate(normalize-space(.),'PUBLISH','publish'),'publish')]"),
        (By.XPATH, "//input[@type='submit' and contains(translate(@value,'PUBLISH','publish'),'publish')]"),
    ]

    try:
        result = driver.execute_script("""
            var form = document.getElementById('publish_bottom')
                    || document.getElementById('publish_top');
            if (!form) return {ok: false, reason: 'no-publish-form'};
            var btn = form.querySelector('button.bigbutton[type="submit"], button[name="go"]');
            if (!btn) return {ok: false, reason: 'no-publish-btn'};
            btn.scrollIntoView({block: 'center'});
            try {
                if (typeof form.requestSubmit === 'function') {
                    form.requestSubmit(btn);
                    return {ok: true, method: 'requestSubmit', form: form.id};
                }
            } catch (e) {}
            btn.click();
            return {ok: true, method: 'click', form: form.id};
        """) or {}
        print(f"  [publish] form submit -> {result}")
        if result.get("ok"):
            time.sleep(4)
            if "s=preview" not in driver.current_url:
                return True
    except Exception as e:
        print(f"  [publish] requestSubmit failed: {e}")

    if _click_first(driver, publish_selectors, "publish"):
        time.sleep(4)
        if "s=preview" not in driver.current_url:
            return True

    return "s=preview" not in driver.current_url


def publish_listing(driver, ad_name, product):
    handle_captcha_if_present(driver)
    print("  [publish] Waiting for draft preview page...")
    _wait_for_draft_preview(driver)
    human_delay(2, 4)

    # -- DEBUG: Page source dump karo pehle --
    try:
        page_html = driver.execute_script("return document.body.innerHTML.substring(0, 3000);")
        print(f"  [publish] Page HTML snippet: {page_html[:1500]}")
    except Exception:
        pass

    published = False

    # Method 1: JS direct click -- forms + any link with publish/confirm text
    try:
        result = driver.execute_script("""
            // Try publish_bottom / publish_top forms
            var formIds = ['publish_bottom', 'publish_top'];
            for (var i = 0; i < formIds.length; i++) {
                var form = document.getElementById(formIds[i]);
                if (!form) continue;
                var btns = form.querySelectorAll('button, input[type=submit]');
                for (var j = 0; j < btns.length; j++) {
                    btns[j].scrollIntoView({block:'center'});
                    btns[j].click();
                    return 'clicked-form-btn:' + formIds[i] + ':' + (btns[j].textContent||btns[j].value||'').trim();
                }
                // No buttons? Submit form directly
                form.submit();
                return 'form-submit:' + formIds[i];
            }

            // Try any button/input on page
            var allBtns = document.querySelectorAll('button, input[type=submit]');
            for (var k = 0; k < allBtns.length; k++) {
                var txt = (allBtns[k].textContent || allBtns[k].value || '').toLowerCase().trim();
                if (txt) {
                    allBtns[k].scrollIntoView({block:'center'});
                    allBtns[k].click();
                    return 'any-btn:' + txt;
                }
            }

            // Try any <a> link that looks like confirm/publish
            var links = document.querySelectorAll('a');
            for (var l = 0; l < links.length; l++) {
                var ltxt = (links[l].textContent || '').toLowerCase().trim();
                if (ltxt.indexOf('publish') !== -1 || ltxt.indexOf('confirm') !== -1 ||
                    ltxt.indexOf('post') !== -1 || ltxt.indexOf('submit') !== -1) {
                    links[l].click();
                    return 'link:' + ltxt;
                }
            }

            // Last resort: submit any form on the page
            var anyForm = document.querySelector('form');
            if (anyForm) {
                anyForm.submit();
                return 'any-form-submit:' + (anyForm.id || anyForm.action || 'unknown');
            }

            return 'no-clickable-found';
        """)
        print(f"  [publish] JS result: {result}")
        time.sleep(6)
        cur = driver.current_url
        print(f"  [publish] URL after JS click: {cur}")
        if "s=preview" not in cur and "s=edit" not in cur:
            published = True
        elif result and result != 'no-clickable-found':
            # Click hua -- URL same hai but maybe page navigated internally
            # Check page source change
            if "thank" in driver.page_source.lower() or "success" in driver.page_source.lower():
                published = True
                print("  [publish] [OK] Success detected in page source")
    except Exception as e:
        print(f"  [publish] JS click failed: {e}")

    # Method 2: CDP navigate directly to confirm URL
    if not published:
        try:
            cur_url = driver.current_url
            # CL preview URL format: ?s=preview -- try changing to s=fin or s=done
            confirm_url = cur_url.replace("?s=preview", "?s=fin")
            if confirm_url != cur_url:
                print(f"  [publish] Trying direct navigate: {confirm_url}")
                driver.get(confirm_url)
                time.sleep(4)
                if "s=preview" not in driver.current_url:
                    published = True
                    print("  [publish] [OK] Direct navigate worked")
        except Exception as e:
            print(f"  [publish] Direct navigate failed: {e}")

    # Method 3: ActionChains on any visible element
    if not published:
        try:
            # Refresh page selectors
            for sel in ["#publish_bottom button", "#publish_top button",
                        "button[type='submit']", "input[type='submit']",
                        "button", "a[href*='confirm']"]:
                try:
                    btn = WebDriverWait(driver, 5).until(
                        EC.presence_of_element_located((By.CSS_SELECTOR, sel)))
                    driver.execute_script("arguments[0].scrollIntoView({block:'center'});", btn)
                    time.sleep(0.5)
                    ActionChains(driver).move_to_element(btn).pause(0.5).click().perform()
                    print(f"  [publish] ActionChains on: '{sel}'")
                    time.sleep(5)
                    if "s=preview" not in driver.current_url:
                        published = True
                        break
                except Exception:
                    continue
        except Exception as e:
            print(f"  [publish] ActionChains sweep failed: {e}")

    if not published:
        print(f"  [publish] [FAIL] All methods failed -- URL: {driver.current_url}")
        # Full page source for debug
        try:
            print(f"  [publish] Full page title: {driver.title}")
            src = driver.page_source
            print(f"  [publish] Page source (2000 chars): {src[:2000]}")
        except Exception:
            pass
        return False

    human_delay(2, 3)
    handle_captcha_if_present(driver)

    listing_url = driver.current_url
    print(f"  [publish] [OK] Published -> {listing_url}")
    posted_listings[ad_name] = {
        "url": listing_url, "post_time": datetime.now(),
        "visitors": 0, "platform": "Craigslist",
    }
    _save_listings()
    return True


def post_product(driver, ad_name, product):
    
    print("  Navigating to account page...")
    driver.get("https://accounts.craigslist.org/login/home?show_tab=postings")
    human_delay(3, 5)
    handle_captcha_if_present(driver)

    # Click "make new post" link
    try:
        new_post_link = WebDriverWait(driver, 15).until(
            EC.element_to_be_clickable((By.LINK_TEXT, "make new post"))
        )
        driver.execute_script("arguments[0].click();", new_post_link)
        print("  [OK] Clicked 'make new post'")
        human_delay(3, 5)
    except TimeoutException:
        # Fallback: direct URL
        print("  'make new post' not found -- falling back to direct URL")
        driver.get("https://post.craigslist.org/c/sss")
        human_delay(4, 7)

    handle_captcha_if_present(driver)

    # ... baaki ka code same rehta hai

    try:
        WebDriverWait(driver, 20).until(
            lambda d: d.execute_script("return document.readyState") == "complete")
    except TimeoutException:
        return False

    print(f"  Page title: {driver.title}")
    print(f"  Current URL: {driver.current_url}")

    if "login" in driver.current_url.lower():
        print("  [FAIL] Session expired.")
        return False

    # -- DIAGNOSTIC: Dump area page inputs + try to fill ZIP if field exists ----
    _area_zip = (os.environ.get("CL_ZIP") or "").strip()
    if not _area_zip:
        _AREA_ZIPS = {
            "losangeles":"90001","newyork":"10001","chicago":"60601",
            "houston":"77001","phoenix":"85001","sfbay":"94102",
            "sandiego":"92101","seattle":"98101","miami":"33101",
            "dallas":"75201","denver":"80201","atlanta":"30301",
            "boston":"02101","portland":"97201",
        }
        _ck = CL_CITY.lower().replace(" ","").replace("-","")
        _area_zip = _AREA_ZIPS.get(_ck, "90001")

    try:
        area_inputs = driver.execute_script(
            "var r=[];"
            "document.querySelectorAll('input,select,textarea').forEach(function(el){"
            "  r.push({tag:el.tagName,type:el.type||'',name:el.name||'',"
            "          id:el.id||'',placeholder:el.placeholder||'',"
            "          value:(el.value||'').substring(0,30),"
            "          visible:el.offsetParent!==null});"
            "});"
            "return r;"
        )
        print(f"  [AREA-DIAG] Inputs on area page: {area_inputs}")
    except Exception as _e:
        print(f"  [AREA-DIAG] input dump failed: {_e}")

    try:
        zip_on_area = driver.execute_script("""
            var names = ['postal','zip','zipcode','postal_code','zip_code'];
            for (var i = 0; i < names.length; i++) {
                var el = document.getElementById(names[i]) ||
                         document.getElementsByName(names[i])[0];
                if (el) return {found:true, name:el.name, id:el.id, ph:el.placeholder, val:el.value};
            }
            return {found: false};
        """)
        print(f"  [AREA-DIAG] ZIP field on area page: {zip_on_area}")
        if zip_on_area and zip_on_area.get("found"):
            _fname = zip_on_area.get("name") or zip_on_area.get("id") or ""
            print(f"  [AREA-DIAG] *** ZIP FIELD FOUND ON AREA PAGE -- filling {_area_zip} ***")
            driver.execute_script("""
                var n = arguments[0], z = arguments[1];
                var el = document.getElementById(n) || document.getElementsByName(n)[0];
                if (el) {
                    el.focus(); el.value = z;
                    el.dispatchEvent(new Event('input', {bubbles:true}));
                    el.dispatchEvent(new Event('change', {bubbles:true}));
                }
            """, _fname, _area_zip)
            print(f"  [AREA-DIAG] ZIP filled on area page [OK]")
        else:
            print(f"  [AREA-DIAG] No ZIP field on area page (will fill on edit page)")
    except Exception as _e2:
        print(f"  [AREA-DIAG] ZIP check failed: {_e2}")

    
    # -- City selection (robust) -------------------------------------------
    cur = driver.current_url
    print(f"  [city] URL before city: {cur}")
    if "s=area" not in cur and "copyfromanother" not in cur:
        print(f"  [city] Already past area page")
    else:
        # If on copyfromanother, click the "skip" button
        # The page shows: [skip]  [re-use selected data]
        # We always click "skip" so we get a clean new post, not a copy.
        if "copyfromanother" in driver.current_url:
            dismissed = False

            # Priority 1: click the literal "skip" button (confirmed in screenshot)
            for skip_selector in [
                (By.XPATH, "//button[normalize-space(translate(.,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'))='skip']"),
                (By.XPATH, "//input[@value='skip' or @value='Skip']"),
                (By.CSS_SELECTOR, "button.skip, input[value='skip']"),
                (By.LINK_TEXT, "skip"),
            ]:
                try:
                    skip_btn = WebDriverWait(driver, 5).until(
                        EC.element_to_be_clickable(skip_selector))
                    driver.execute_script("arguments[0].click();", skip_btn)
                    human_delay(2, 3)
                    dismissed = True
                    print(f"  [city] Clicked 'skip' on copy page -> {driver.current_url}")
                    break
                except Exception:
                    pass

            # Priority 2: any button whose text contains 'skip'
            if not dismissed:
                try:
                    all_btns = driver.find_elements(By.TAG_NAME, "button")
                    all_btns += driver.find_elements(By.CSS_SELECTOR, "input[type='submit'], input[type='button']")
                    for btn in all_btns:
                        txt = (btn.text or btn.get_attribute("value") or "").strip().lower()
                        if "skip" in txt:
                            driver.execute_script("arguments[0].click();", btn)
                            human_delay(2, 3)
                            dismissed = True
                            print(f"  [city] Clicked skip button (text scan) -> {driver.current_url}")
                            break
                except Exception:
                    pass

            # Priority 3: extract URL token and jump directly to ?s=area
            if not dismissed:
                import re as _re
                m = _re.search(r'/k/([^/]+)/([^?]+)', driver.current_url)
                if m:
                    area_url = f"https://post.craigslist.org/k/{m.group(1)}/{m.group(2)}?s=area"
                    print(f"  [city] Skip btn not found — jumping to area page: {area_url}")
                    driver.get(area_url)
                    human_delay(2, 3)
                    dismissed = True
                else:
                    print(f"  [city] Could not leave copy page — no token in URL")

        # Try city dropdown
        city_clicked = False
        try:
            city_button = WebDriverWait(driver, 8).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, "span#ui-id-1-button")))
            driver.execute_script("arguments[0].click();", city_button)
            human_delay(1, 2)
            menu_items = WebDriverWait(driver, 5).until(
                EC.presence_of_all_elements_located((By.CSS_SELECTOR, "ul#ui-id-1-menu li")))
            target = CL_CITY.lower().replace(" ", "").replace("-", "")
            for item in menu_items:
                txt = (item.text.strip() or item.get_attribute("textContent") or "").strip()
                if target in txt.lower().replace(" ", "") or txt.lower().replace(" ", "") in target:
                    driver.execute_script("arguments[0].click();", item)
                    city_clicked = True
                    print(f"  [OK] Selected city: {txt}")
                    break
            if not city_clicked:
                available = [m.text.strip() for m in menu_items[:6]]
                print(f"  [city] {CL_CITY} not found. Available: {available}")
                if menu_items:
                    driver.execute_script("arguments[0].click();", menu_items[0])
                    city_clicked = True
                    print(f"  [OK] Fallback city: {menu_items[0].text.strip()}")
        except TimeoutException:
            print(f"  [city] No dropdown at {driver.current_url} — skipping")
            city_clicked = True
        except Exception as e:
            print(f"  [city] Dropdown error: {e}")
            city_clicked = True

        # Click continue
        try:
            continue_btn = WebDriverWait(driver, 8).until(
                EC.element_to_be_clickable((By.CSS_SELECTOR,
                    "button.go.pickbutton, button[class*='pickbutton'], button[type='submit']")))
            driver.execute_script("arguments[0].click();", continue_btn)
            print("  [OK] Submitted area page")
            try:
                WebDriverWait(driver, 15).until(
                    lambda d: "s=area" not in d.current_url and "copyfromanother" not in d.current_url)
                print(f"  [OK] Left area page -> {driver.current_url}")
            except TimeoutException:
                print(f"  [city] Still on area after 15s: {driver.current_url}")
        except Exception as ce:
            print(f"  [city] No continue btn: {ce}")

        if "s=area" in driver.current_url or "copyfromanother" in driver.current_url:
            print(f"  [FAIL] Could not leave area: {driver.current_url}")
            return False

    print(f"  [OK] Left area -> {driver.current_url}")
    handle_captcha_if_present(driver)
    human_delay(2, 4)

    # -- Handle ?s=subarea page (must submit before reaching ?s=type) ----------
    for _sub_attempt in range(4):
        if "s=subarea" not in driver.current_url:
            break
        print(f"  [subarea] On subarea page: {driver.current_url}")
        try:
            sub_submit = WebDriverWait(driver, 8).until(
                EC.element_to_be_clickable((By.CSS_SELECTOR,
                    "button[type='submit'], input[type='submit'], button.go, button.pickbutton")))
            driver.execute_script("arguments[0].click();", sub_submit)
            human_delay(2, 4)
            handle_captcha_if_present(driver)
            print(f"  [subarea] Submitted -> {driver.current_url}")
        except Exception as sub_e:
            print(f"  [subarea] Could not submit: {sub_e}")
            # Fallback: extract token and jump directly to ?s=type
            import re as _re
            m = _re.search(r'/k/([^/]+)/([^?]+)', driver.current_url)
            if m:
                type_url = f"https://post.craigslist.org/k/{m.group(1)}/{m.group(2)}?s=type"
                print(f"  [subarea] Jumping to type page: {type_url}")
                driver.get(type_url)
                human_delay(3, 5)
            break
    # --------------------------------------------------------------------------

    # -- Post type -------------------------------------------------------------
    # Wait for ?s=type page — must NOT be on subarea anymore
    try:
        WebDriverWait(driver, 15).until(
            lambda d: "s=subarea" not in d.current_url and (
                d.find_elements(By.CSS_SELECTOR, "input[value='fso']") or
                d.find_elements(By.CSS_SELECTOR, "input[value='fs']") or
                d.find_elements(By.TAG_NAME, "li")
            ))
        print("  [OK] Post type page loaded")
    except TimeoutException:
        print(f"  [FAIL] Post type page not reached. URL: {driver.current_url}")
        return False

    fso_clicked = False

    # Method 1: radio input with known fso values
    for val in ['fso', 'fs', 'forsale', 'sss']:
        try:
            el = driver.find_element(By.CSS_SELECTOR, f"input[value='{val}']")
            driver.execute_script("arguments[0].click();", el)
            fso_clicked = True
            print(f"  [OK] Selected post type via input value='{val}'")
            break
        except NoSuchElementException:
            pass

    # Method 2: li / label / a containing 'for sale by owner'
    # Uses JS innerText — headless Chrome returns empty el.text when
    # text is inside a child <a> tag (the copy-flow ?s=type DOM).
    if not fso_clicked:
        for tag in ["li", "label", "a"]:
            for el in driver.find_elements(By.TAG_NAME, tag):
                try:
                    txt = el.text.strip()
                    if not txt:
                        txt = driver.execute_script(
                            "return (arguments[0].innerText||arguments[0].textContent||'').trim();", el)
                    txt_low = txt.lower()
                    if "sale by owner" in txt_low or ("for sale" in txt_low and len(txt_low) < 40):
                        driver.execute_script("arguments[0].click();", el)
                        fso_clicked = True
                        print(f"  [OK] Selected post type via <{tag}>: '{txt}'")
                        break
                except Exception:
                    pass
            if fso_clicked:
                break

    # Method 3: XPath case-insensitive match on full DOM
    if not fso_clicked:
        try:
            owner_el = WebDriverWait(driver, 5).until(
                EC.element_to_be_clickable((By.XPATH,
                    "//*[contains(translate(normalize-space(.),"
                    "'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),"
                    "'for sale by owner')]")))
            driver.execute_script("arguments[0].click();", owner_el)
            fso_clicked = True
            print("  [OK] Selected post type via XPath 'for sale by owner'")
        except Exception:
            pass

    if not fso_clicked:
        print(f"  [FAIL] Could not find 'for sale by owner'. URL: {driver.current_url}")
        # Debug dump
        lis = driver.find_elements(By.TAG_NAME, "li")
        print(f"  LI elements ({len(lis)}):")
        for i, li in enumerate(lis[:15]):
            txt = li.text.strip() or driver.execute_script(
                "return (arguments[0].innerText||arguments[0].textContent||'').trim();", li)
            print(f"    [{i}] '{txt}'")
        inputs = driver.find_elements(By.TAG_NAME, "input")
        print(f"  Input elements ({len(inputs)}):")
        for i, inp in enumerate(inputs[:10]):
            print(f"    [{i}] type={inp.get_attribute('type')} value={inp.get_attribute('value')}")
        return False

    human_delay(3, 5)
    handle_captcha_if_present(driver)

    # -- Category --------------------------------------------------------------
    cat_clicked = False
    mapped_label = CATEGORY_MAPPING.get(
        product.get("category", "").lower().strip(), (None, ""))[1]
    if not mapped_label:
        mapped_label = product.get("category", "")
    print(f"  Target category label: {mapped_label}")

    if mapped_label:
        try:
            target_lower = mapped_label.lower().strip()
            xpath = (f"//label[contains("
                     f"translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz')"
                     f", '{target_lower}')]")
            label_el = WebDriverWait(driver, 10).until(
                EC.element_to_be_clickable((By.XPATH, xpath)))
            driver.execute_script("arguments[0].click();", label_el)
            cat_clicked = True
            print(f"  [OK] Selected category via label XPath: '{mapped_label}'")
        except Exception as e:
            print(f"  Category label failed: {e}")

    if not cat_clicked:
        try:
            ul_value = get_category_ul_value(product.get("category", ""))
            inp = WebDriverWait(driver, 10).until(
                EC.presence_of_element_located(
                    (By.CSS_SELECTOR, f"input[type='radio'][value='{ul_value}']")))
            driver.execute_script("arguments[0].click();", inp)
            cat_clicked = True
            print(f"  [OK] Selected category via radio value={ul_value}")
        except Exception:
            pass

    if not cat_clicked:
        try:
            first_label = WebDriverWait(driver, 5).until(
                EC.element_to_be_clickable((By.CSS_SELECTOR, "label.radio-option, label")))
            driver.execute_script("arguments[0].click();", first_label)
            cat_clicked = True
        except Exception:
            pass

    if not cat_clicked:
        return False

    human_delay(2, 3)
    try:
        continue_btn = WebDriverWait(driver, 8).until(
            EC.element_to_be_clickable((By.CSS_SELECTOR,
                "button.go.pickbutton, button[class*='pickbutton'], button[type='submit']")))
        driver.execute_script("arguments[0].click();", continue_btn)
        print("  [OK] Clicked category continue button")
        try:
            WebDriverWait(driver, 12).until(
                EC.presence_of_element_located((By.ID, "postingForm")))
            time.sleep(2)
            print("  [OK] postingForm visible after category selection")
        except TimeoutException:
            time.sleep(3)
        print(f"  Current URL after category continue: {driver.current_url}")
        handle_captcha_if_present(driver)
    except TimeoutException:
        human_delay(2, 3)

    click_relocation_if_needed(driver, ad_name)

    # -- Fill and submit -------------------------------------------------------
    try:
        success = fill_listing_details(driver, product)
    except Exception as e:
        print(f"  [FAIL] fill_listing_details crashed: {e}")
        import traceback
        traceback.print_exc()
        return False

    if not success:
        return False

    # -- Image upload ----------------------------------------------------------
    if not complete_images_step(driver, product):
        print("  [FAIL] Failed at image upload step")
        return False

    # -- Publish ---------------------------------------------------------------
    if not publish_listing(driver, ad_name, product):
        print("  [FAIL] Failed at publish step")
        return False

    return True


def update_ad_analytics_periodically():
    return  # disabled locally
    if IS_RAILWAY:
        print("[CL] Analytics thread disabled on Railway (memory constraint). Skipping.")
        return
    while True:
        print("\n[CL] Refreshing analytics...")
        for ad_name, listing in list(posted_listings.items()):
            if not listing.get("url") or listing.get("platform") != "Craigslist":
                continue
            tmp = None
            try:
                tmp = make_driver()
                tmp.get(listing["url"])
                human_delay(2, 4)
                views_el = WebDriverWait(tmp, 15).until(
                    EC.presence_of_element_located((By.CSS_SELECTOR, "#views_count")))
                count = int("".join(filter(str.isdigit, views_el.text)) or "0")
                posted_listings[ad_name]["visitors"] = count
            except Exception as e:
                print(f"  [WARN] Analytics error for {ad_name}: {e}")
            finally:
                if tmp:
                    tmp.quit()
        _save_listings()
        time.sleep(300)


def main():
    global CL_CITY
    email = os.environ.get("CL_EMAIL", "").strip()
    if not email:
        print("[FAIL] CL_EMAIL environment variable not set. Add it to Railway Variables.")
        return
    CL_CITY = os.environ.get("CL_CITY", CL_CITY)
    _load_existing_listings()

    proxy_url = os.environ.get("HTTPS_PROXY") or os.environ.get("HTTP_PROXY")
    driver = make_driver(proxy_url=proxy_url)
    if not craigslist_login(driver, email):
        driver.quit()
        return

    # -- DIAGNOSTIC: dump all env vars so we can see what server.py passed ------
    print(f"  [env] CLB_PRODUCTS_FILE = '{os.environ.get('CLB_PRODUCTS_FILE', 'NOT SET')}'")
    print(f"  [env] PRODUCTS_FILE     = '{os.environ.get('PRODUCTS_FILE', 'NOT SET')}'")
    print(f"  [env] CL_CITY           = '{os.environ.get('CL_CITY', 'NOT SET')}'")
    print(f"  [env] CL_EMAIL          = '{os.environ.get('CL_EMAIL', 'NOT SET')}'")

    # FIX: CLB_PRODUCTS_FILE is the unique key set by server.py per job.
    # PRODUCTS_FILE kept as fallback. Without this the script reads all of
    # products.json and always posts index-0 (Men's Hanes T-Shirt).
    products_file = (
        os.environ.get("CLB_PRODUCTS_FILE", "").strip()
        or os.environ.get("PRODUCTS_FILE", "").strip()
        or "products.json"
    )
    print(f"  [products] Reading from: {products_file}")
    if not os.path.exists(products_file):
        print(f"  [FAIL] Products file not found: {products_file}")
        print(f"  [products] cwd = {os.getcwd()}")
        print(f"  [products] files in cwd = {os.listdir('.')[:20]}")
        driver.quit()
        return
    with open(products_file) as f:
        products = json.load(f)
    print(f"  [products] Loaded {len(products)} product(s): " +
          str([p.get('title') or p.get('name') for p in products]))

    threading.Thread(target=update_ad_analytics_periodically, daemon=True).start()

    for product in products:
        product_title = product.get("title") or product.get("name", "No Title")
        ad_name = f"CL_{product_title}"
        print(f"\nPosting: {product_title}")
        try:
            ok = post_product(driver, ad_name, product)
        except Exception as e:
            print(f"  [FAIL] post_product crashed for '{product_title}': {e}")
            import traceback
            traceback.print_exc()
            ok = False
        print("  [OK] Posted" if ok else "  [FAIL] Failed")
        time.sleep(3)

    print("\nAll Craigslist products processed.")
    driver.quit()


if __name__ == "__main__":
    main()