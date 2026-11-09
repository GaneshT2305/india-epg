"""
Merged Indian IPTV EPG generator: JioTV + Zee5
Outputs XMLTV file with standard xmltv_ids where available for best channel matching.
Deduplicates channels: JioTV is primary; Zee5 channels whose xmltv_id already
appears in JioTV are skipped (no duplicate <channel> element, no duplicate programmes).
"""
import urllib.request, urllib.parse, json, datetime, xml.etree.ElementTree as ET
import threading, time, sys, re, os, gzip

OUT_XML = sys.argv[1] if len(sys.argv) > 1 else str(os.path.join(os.path.dirname(os.path.abspath(__file__)), "epg.xml.gz"))
today_ist = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=5, minutes=30)))
today_utc = datetime.datetime.now(datetime.timezone.utc)

def fetch(url, headers=None, data=None, timeout=15):
    h = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, headers=h, data=data)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read()
    except Exception:
        return None

def esc(s):
    if not s: return ""
    return str(s).replace("&","&amp;").replace("<","&lt;").replace(">","&gt;")

def name_aliases(name):
    """Generate alternate display names for better playlist matching.

    Handles common mismatches between playlist channel names and EPG names:
    - Country/category prefix codes: 'MA- Zee Marathi' -> 'Zee Marathi'
    - '&' <-> 'And': '&Pictures' <-> 'And Pictures'
    - '+' -> 'Plus': 'Sony LIV+' -> 'Sony LIV Plus'
    - Special chars (. -) normalized: 'Star.Plus' -> 'Star Plus'
    - Extra/double spaces collapsed
    - 4K/FHD/UHD suffix stripped (resolution-only tags, same content)
    - NOTE: HD and SD are NOT stripped — they carry different feeds/content
    """
    import re as _re
    seen = {name}
    aliases = []

    def add(n):
        n = " ".join(n.split())  # collapse extra whitespace
        if n and n not in seen:
            seen.add(n)
            aliases.append(n)

    # --- Strip country/category prefix codes ---
    # Patterns: 'MA- Zee Marathi', 'IN: Star Plus', 'EN | BBC'
    # Code is 2-4 uppercase letters (or digits like '4K') followed by separator
    prefix_match = _re.match(r"^([A-Z0-9]{2,4})\s*[-:|]\s*(.+)$", name)
    if prefix_match:
        bare = prefix_match.group(2).strip()
        add(bare)
        # Also apply other transforms to the bare name
        name_for_transforms = bare
    else:
        name_for_transforms = name

    # --- & <-> And ---
    if "&" in name_for_transforms:
        add(name_for_transforms.replace("&", "And "))
        add(name_for_transforms.replace("&", "and "))
    if _re.match(r"(?i)^And\s", name_for_transforms):
        add(_re.sub(r"(?i)^And\s+", "&", name_for_transforms))

    # --- + → Plus ---
    if "+" in name_for_transforms:
        add(name_for_transforms.replace("+", " Plus ").replace("  ", " ").strip())
        add(name_for_transforms.replace("+", "Plus"))

    # --- Normalize special chars (. and -) to spaces ---
    cleaned = _re.sub(r"[.\-]", " ", name_for_transforms)
    cleaned = " ".join(cleaned.split())
    add(cleaned)

    # --- Strip resolution-only suffixes (same content, different quality) ---
    # 4K, FHD, UHD are resolution tags — the schedule is the same as the base channel
    # HD and SD are intentionally NOT stripped (they carry different programming)
    for tag in (" 4K", " FHD", " UHD", " 4k", " fhd", " uhd"):
        if name_for_transforms.endswith(tag):
            add(name_for_transforms[:-len(tag)])

    return aliases

def epoch_ms_to_xmltv(ms):
    dt = datetime.datetime.fromtimestamp(ms/1000, tz=datetime.timezone.utc)
    return dt.strftime("%Y%m%d%H%M%S +0000")

def iso_to_xmltv(s):
    if not s: return None
    try:
        s = s.replace("Z","+00:00")
        dt = datetime.datetime.fromisoformat(s)
        return dt.astimezone(datetime.timezone.utc).strftime("%Y%m%d%H%M%S +0000")
    except:
        return None

# ─── JIOTV ────────────────────────────────────────────────────────────────────
print("=== JioTV ===")
print("Fetching channel list...")
raw = fetch("https://jiotvapi.cdn.jio.com/apis/v3.0/getMobileChannelList/get/?langId=6&devicetype=phone&os=android&usertype=JIO&version=343")
jio_channels = json.loads(raw).get("result", []) if raw else []
print(f"  {len(jio_channels)} channels")

# Also fetch iptv-org JioTV channels.xml to get xmltv_ids
jio_xmltv_ids = {}  # channel_name (lowercase) -> xmltv_id
raw_xml = fetch("https://raw.githubusercontent.com/iptv-org/epg/master/sites/jiotv.com/jiotv.com.channels.xml")
if raw_xml:
    try:
        xtree = ET.fromstring(raw_xml)
        for ch in xtree.findall("channel"):
            name = (ch.text or "").strip().lower()
            xid = ch.get("xmltv_id","")
            sid = ch.get("site_id","")
            if xid and name:
                jio_xmltv_ids[name] = xid
            if sid:
                jio_xmltv_ids[f"id_{sid}"] = xid or f"JioTV.{sid}"
        print(f"  {len(jio_xmltv_ids)} xmltv_id mappings from iptv-org")
    except:
        pass

# Cross-provider name→xmltv_id fallback for JioTV channels not in iptv-org JioTV XML
# (e.g. Star Pravah Picture HD — present in JioTV but absent from iptv-org jiotv.com)
print("  Loading cross-provider xmltv_id lookup...")
cross_xids = {}
for _site in ["tataplay.com", "dishtv.in", "airtelxstream.in"]:
    _raw = fetch(f"https://raw.githubusercontent.com/iptv-org/epg/master/sites/{_site}/{_site}.channels.xml")
    if _raw:
        try:
            _t = ET.fromstring(_raw)
            for _ch in _t.findall("channel"):
                _n = (_ch.text or "").strip()
                _x = _ch.get("xmltv_id", "")
                if _x and _n:
                    cross_xids.setdefault(_n.lower(), _x)
        except: pass
print(f"  {len(cross_xids)} cross-provider entries")

print("Fetching EPG (today + tomorrow)...")
jio_epg = {}
lock = threading.Lock()
done = [0]

def fetch_jio_epg(ch):
    cid = str(ch["channel_id"])
    progs = []
    for offset in [0, 1]:
        raw = fetch(f"https://jiotvapi.cdn.jio.com/apis/v1.3/getepg/get?channel_id={cid}&offset={offset}")
        if raw:
            try:
                progs.extend(json.loads(raw).get("epg", []))
            except: pass
        time.sleep(0.03)
    with lock:
        if progs: jio_epg[cid] = progs
        done[0] += 1
        if done[0] % 100 == 0:
            print(f"  JioTV EPG: {done[0]}/{len(jio_channels)}")

threads = []
for ch in jio_channels:
    t = threading.Thread(target=fetch_jio_epg, args=(ch,), daemon=True)
    threads.append(t); t.start()
    while sum(1 for x in threads if x.is_alive()) >= 25:
        time.sleep(0.2)
for t in threads: t.join()
print(f"  EPG data for {len(jio_epg)} channels")

# ─── ZEE5 ─────────────────────────────────────────────────────────────────────
print("\n=== Zee5 ===")
zee5_headers = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Referer": "https://www.zee5.com/"
}

# Fetch channel list from Zee5 catalog API
print("Fetching channel list...")
raw = fetch("https://catalogapi.zee5.com/v1/channel?page=1&page_size=500", headers=zee5_headers)
zee5_live_channels = []
if raw:
    try:
        d = json.loads(raw)
        zee5_live_channels = d.get("items", [])
        print(f"  {len(zee5_live_channels)} channels from Zee5 catalog API")
    except Exception as e:
        print(f"  Catalog API error: {e}")

# Fallback: use iptv-org channels.xml
print("Loading Zee5 xmltv_id mappings from iptv-org...")
raw_xml = fetch("https://raw.githubusercontent.com/iptv-org/epg/master/sites/zee5.com/zee5.com.channels.xml")
zee5_iptv_channels = []
if raw_xml:
    try:
        ztree = ET.fromstring(raw_xml)
        for ch in ztree.findall("channel"):
            zee5_iptv_channels.append({
                "site_id": ch.get("site_id",""),
                "name": (ch.text or "").strip(),
                "xmltv_id": ch.get("xmltv_id","")
            })
        print(f"  {len(zee5_iptv_channels)} channels from iptv-org")
    except: pass

# Merge: use iptv-org for xmltv_ids, live catalog for site_ids
zee5_name_to_xid = {ch["name"].lower(): ch["xmltv_id"] for ch in zee5_iptv_channels if ch.get("xmltv_id")}
zee5_site_to_xid = {ch["site_id"]: ch["xmltv_id"] for ch in zee5_iptv_channels if ch.get("xmltv_id")}

# Build final Zee5 channel list from live catalog
zee5_channels = []
for ch in zee5_live_channels:
    cid = ch.get("id","")
    name = ch.get("title","")
    # Try to find xmltv_id: by site_id match or name match
    xid = zee5_site_to_xid.get(cid, "") or zee5_name_to_xid.get(name.lower(), "")
    zee5_channels.append({"id": cid, "name": name, "xmltv_id": xid,
                          "logo": ch.get("list_image","")})

# If live catalog failed, use iptv-org list
if not zee5_channels:
    zee5_channels = [{"id": ch["site_id"], "name": ch["name"], "xmltv_id": ch["xmltv_id"], "logo": ""}
                     for ch in zee5_iptv_channels]
    print(f"  Using iptv-org fallback: {len(zee5_channels)} channels")

print(f"Fetching Zee5 EPG ({len(zee5_channels)} channels, today + tomorrow)...")
zee5_epg = {}
zee5_lock = threading.Lock()
zee5_done = [0]

def fetch_zee5_epg(ch):
    cid = ch["id"]
    progs = []
    for day_offset in [0, 1]:
        url = f"https://gwapi.zee5.com/v1/epg?channels={cid}&start={day_offset}&end={day_offset}&page_size=500"
        raw = fetch(url, headers=zee5_headers)
        if raw:
            try:
                d = json.loads(raw)
                items = d.get("items", [])
                channel_progs = items[0].get("items", []) if items else []
                progs.extend(channel_progs)
            except: pass
        time.sleep(0.05)
    with zee5_lock:
        if progs: zee5_epg[cid] = progs
        zee5_done[0] += 1
        if zee5_done[0] % 50 == 0:
            print(f"  Zee5 EPG: {zee5_done[0]}/{len(zee5_channels)}")

zee5_threads = []
for ch in zee5_channels:
    t = threading.Thread(target=fetch_zee5_epg, args=(ch,), daemon=True)
    zee5_threads.append(t); t.start()
    while sum(1 for x in zee5_threads if x.is_alive()) >= 15:
        time.sleep(0.3)
for t in zee5_threads: t.join()
print(f"  EPG data for {len(zee5_epg)} Zee5 channels")

# ─── TATA PLAY ────────────────────────────────────────────────────────────────
print("\n=== Tata Play ===")
TATAPLAY_HEADERS = {
    "Accept": "*/*", "Origin": "https://watch.tataplay.com",
    "Referer": "https://watch.tataplay.com/",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "content-type": "application/json", "locale": "ENG",
}
print("  Loading iptv-org xmltv_id mapping...")
tataplay_site_to_xid = {}
raw_xml = fetch("https://raw.githubusercontent.com/iptv-org/epg/master/sites/tataplay.com/tataplay.com.channels.xml")
if raw_xml:
    try:
        _t = ET.fromstring(raw_xml)
        for _ch in _t.findall("channel"):
            _sid = _ch.get("site_id", ""); _x = _ch.get("xmltv_id", "")
            if _sid: tataplay_site_to_xid[_sid] = _x
        print(f"  {len(tataplay_site_to_xid)} site_id→xmltv_id mappings")
    except: pass

print("  Fetching channel list...")
tataplay_ch_info = []
for _offset in range(0, 2000, 1000):
    _raw = fetch(f"https://tm.tapi.videoready.tv/portal-search/pub/api/v1/channels?limit=1000&offset={_offset}", headers=TATAPLAY_HEADERS)
    if not _raw: break
    try:
        _lst = json.loads(_raw).get("data", {}).get("list", [])
        tataplay_ch_info.extend(_lst)
        if len(_lst) < 1000: break
    except: break
tataplay_ch_info = [
    {"id": str(c.get("id","")), "name": c.get("title",""),
     "xml_id": (tataplay_site_to_xid.get(str(c.get("id",""))) or f"tataplay_{c.get('id','')}"),
     "logo": c.get("transparentImageUrl","") or c.get("thumbnailImage","")}
    for c in tataplay_ch_info if c.get("id")
]
print(f"  {len(tataplay_ch_info)} channels")

print(f"  Fetching Tata Play EPG ({len(tataplay_ch_info)} channels, today + tomorrow)...")
tataplay_epg = {}
_tp_lock = threading.Lock(); _tp_done = [0]
def _fetch_tp_epg(ch):
    cid = ch["id"]; progs = []
    for day_offset in [0, 1]:
        date = today_ist + datetime.timedelta(days=day_offset)
        raw = fetch(f"https://tm.tapi.videoready.tv/content-detail/pub/api/v2/channels/schedule?date={date.strftime('%d-%m-%Y')}",
                    headers=TATAPLAY_HEADERS, data=json.dumps({"id": cid}).encode())
        if raw:
            try: progs.extend(json.loads(raw).get("data",{}).get("epg",[]))
            except: pass
        time.sleep(0.05)
    with _tp_lock:
        if progs: tataplay_epg[cid] = progs
        _tp_done[0] += 1
        if _tp_done[0] % 150 == 0: print(f"  Tata Play EPG: {_tp_done[0]}/{len(tataplay_ch_info)}")
_tp_threads = []
for _ch in tataplay_ch_info:
    _t = threading.Thread(target=_fetch_tp_epg, args=(_ch,), daemon=True)
    _tp_threads.append(_t); _t.start()
    while sum(1 for x in _tp_threads if x.is_alive()) >= 20: time.sleep(0.2)
for _t in _tp_threads: _t.join()
print(f"  EPG data for {len(tataplay_epg)} Tata Play channels")

# ─── DISH TV ──────────────────────────────────────────────────────────────────
print("\n=== Dish TV ===")
print("  Fetching auth token...")
dishtv_token = None
_raw = fetch("https://www.dishtv.in/services/epg/signin",
             headers={"sec-fetch-dest":"empty","sec-fetch-mode":"cors","sec-fetch-site":"same-origin",
                      "x-requested-with":"XMLHttpRequest","Referer":"https://www.dishtv.in/channel-guide.html"},
             data=b"")
if _raw:
    try: dishtv_token = json.loads(_raw).get("token","")
    except: pass
if dishtv_token: print(f"  Token obtained ({len(dishtv_token)} chars)")
else: print("  WARNING: No token — Dish TV EPG will be skipped")

print("  Loading iptv-org channels...")
dishtv_ch_info = []
raw_xml = fetch("https://raw.githubusercontent.com/iptv-org/epg/master/sites/dishtv.in/dishtv.in.channels.xml")
if raw_xml:
    try:
        _t = ET.fromstring(raw_xml)
        for _ch in _t.findall("channel"):
            _sid = _ch.get("site_id",""); _x = _ch.get("xmltv_id",""); _n = (_ch.text or "").strip()
            if _sid and _n:
                dishtv_ch_info.append({"id": _sid, "name": _n, "xml_id": _x if _x else f"dishtv_{_sid}"})
        print(f"  {len(dishtv_ch_info)} channels from iptv-org")
    except: pass

dishtv_epg = {}
if dishtv_token and dishtv_ch_info:
    print(f"  Fetching Dish TV EPG ({len(dishtv_ch_info)} channels, today + tomorrow)...")
    _dt_lock = threading.Lock(); _dt_done = [0]
    _DT_HEADERS = {"Authorization": dishtv_token, "Content-Type": "application/json"}
    def _fetch_dt_epg(ch):
        cid = ch["id"]; progs = []
        for day_offset in [0, 1]:
            date = today_ist + datetime.timedelta(days=day_offset)
            raw = fetch("https://epg.mysmartstick.com/dishtv/api/v1/epg/entities/programs",
                        headers=_DT_HEADERS,
                        data=json.dumps({"allowPastEvents": True, "channelid": cid, "date": date.strftime("%d/%m/%Y")}).encode())
            if raw:
                try:
                    items = json.loads(raw)
                    for item in (items if isinstance(items, list) else []):
                        start = iso_to_xmltv(item.get("start",""))
                        stop  = iso_to_xmltv(item.get("stop",""))
                        if not start or not stop: continue
                        reg = item.get("regional",{})
                        en = reg.get("english") or (list(reg.values())[0] if reg else {})
                        progs.append({"start": start, "stop": stop,
                                      "title": en.get("title","") or item.get("title",""),
                                      "desc": en.get("desc",""), "genre": en.get("genre","")})
                except: pass
            time.sleep(0.05)
        with _dt_lock:
            if progs: dishtv_epg[cid] = progs
            _dt_done[0] += 1
            if _dt_done[0] % 150 == 0: print(f"  Dish TV EPG: {_dt_done[0]}/{len(dishtv_ch_info)}")
    _dt_threads = []
    for _ch in dishtv_ch_info:
        _t = threading.Thread(target=_fetch_dt_epg, args=(_ch,), daemon=True)
        _dt_threads.append(_t); _t.start()
        while sum(1 for x in _dt_threads if x.is_alive()) >= 15: time.sleep(0.2)
    for _t in _dt_threads: _t.join()
    print(f"  EPG data for {len(dishtv_epg)} Dish TV channels")

# ─── AIRTEL ───────────────────────────────────────────────────────────────────
print("\n=== Airtel ===")
_AIRTEL_HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                   "Referer": "https://www.airtelxstream.in/"}
print("  Loading iptv-org channels...")
airtel_ch_info = []
raw_xml = fetch("https://raw.githubusercontent.com/iptv-org/epg/master/sites/airtelxstream.in/airtelxstream.in.channels.xml")
if raw_xml:
    try:
        _t = ET.fromstring(raw_xml)
        for _ch in _t.findall("channel"):
            _sid = _ch.get("site_id",""); _x = _ch.get("xmltv_id",""); _n = (_ch.text or "").strip()
            if _sid and _n:
                airtel_ch_info.append({"id": _sid, "name": _n, "xml_id": _x if _x else f"airtel_{_sid}"})
        print(f"  {len(airtel_ch_info)} channels from iptv-org")
    except: pass

print(f"  Fetching Airtel EPG ({len(airtel_ch_info)} channels, 2-day window)...")
airtel_epg = {}
_at_lock = threading.Lock(); _at_done = [0]
_at_start = int(today_utc.replace(hour=0,minute=0,second=0,microsecond=0).timestamp() * 1000)
_at_end = _at_start + 2 * 86400000
def _fetch_at_epg(ch):
    sid = ch["id"]; progs = []
    raw = fetch(f"https://epg.airtel.tv/app/v2/content/channel/epg?channelId={sid}&startTime={_at_start}&endTime={_at_end}",
                headers=_AIRTEL_HEADERS)
    if raw:
        try:
            guide = json.loads(raw).get("programGuide",{})
            key = list(guide.keys())[0] if guide else None
            if key:
                for item in guide[key]:
                    start = epoch_ms_to_xmltv(item.get("startTime",0))
                    stop  = epoch_ms_to_xmltv(item.get("endTime",0))
                    if not start or not stop: continue
                    genres = item.get("genres",[]) or []
                    genre = genres[0] if genres and isinstance(genres[0],str) else ""
                    progs.append({"start":start,"stop":stop,"title":item.get("title",""),
                                  "desc":item.get("desc","") or "","genre":genre})
        except: pass
    with _at_lock:
        if progs: airtel_epg[sid] = progs
        _at_done[0] += 1
        if _at_done[0] % 150 == 0: print(f"  Airtel EPG: {_at_done[0]}/{len(airtel_ch_info)}")
_at_threads = []
for _ch in airtel_ch_info:
    _t = threading.Thread(target=_fetch_at_epg, args=(_ch,), daemon=True)
    _at_threads.append(_t); _t.start()
    while sum(1 for x in _at_threads if x.is_alive()) >= 20: time.sleep(0.2)
for _t in _at_threads: _t.join()
print(f"  EPG data for {len(airtel_epg)} Airtel channels")

# ─── MAGTICOM.GE (Bollywood HD and other non-Indian channels) ─────────────────
print("\n=== magticom.ge ===")
# Map: (channel_display_name, xmltv_id, magticom_channel_id)
# Bollywood HD = Bollywood.ru as listed on magticom.ge Georgian TV guide (site_id=260)
MAGTICOM_CHANNELS = [
    ("Bollywood HD", "BollywoodHD.ro", "260"),
]
# magticom.ge returns timestamps in Georgia local time (UTC+4)
MAGTICOM_TZ = datetime.timezone(datetime.timedelta(hours=4))

def magticom_to_xmltv(ts_str):
    if not ts_str: return None
    try:
        dt = datetime.datetime.fromisoformat(ts_str).replace(tzinfo=MAGTICOM_TZ)
        return dt.astimezone(datetime.timezone.utc).strftime("%Y%m%d%H%M%S +0000")
    except:
        return None

magticom_channels_data = []  # list of (name, xmltv_id, progs)

for name, xmltv_id, channel_id in MAGTICOM_CHANNELS:
    progs = []
    for day_offset in [0, 1]:
        date = today_utc.replace(hour=0, minute=0, second=0, microsecond=0) + datetime.timedelta(days=day_offset)
        start_ts = int(date.timestamp())
        end_ts = start_ts + 86400
        post_data = urllib.parse.urlencode({
            "channelId": channel_id,
            "start": start_ts,
            "end": end_ts,
        }).encode()
        raw = fetch(
            "https://www.magticom.ge/request/channel-program.php",
            headers={
                "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                "Referer": "https://www.magticom.ge/en/tv/tv-services/tv-guide",
            },
            data=post_data,
        )
        if raw:
            try:
                items = json.loads(raw)
                for item in items:
                    start = magticom_to_xmltv(item.get("startTimestamp", ""))
                    stop  = magticom_to_xmltv(item.get("endTimestamp", ""))
                    if start and stop:
                        progs.append({
                            "start": start, "stop": stop,
                            "title": item.get("title", ""),
                            "desc":  item.get("info", "") or item.get("subTitle", ""),
                        })
            except Exception as e:
                print(f"  magticom.ge parse error: {e}")
    print(f"  {name}: {len(progs)} programmes")
    magticom_channels_data.append((name, xmltv_id, progs))

# ─── ASTRO MALAYSIA ───────────────────────────────────────────────────────────
print("\n=== Astro Malaysia ===")
# Indian channels on Astro Malaysia with their own distinct schedule
# (Colors Hindi HD on Astro plays Bollywood movies, NOT the same as JioTV Colors HD)
# Also includes Astro-exclusive Tamil channels not carried by any Indian provider.
# site_id from content.astro.com.my iptv-org channels.xml → (display_name, xmltv_id)
ASTRO_CHANNELS = {
    365: ("Colors Hindi HD",       "Colors.in@SD"),
    298: ("Colors Tamil HD",       "ColorsTamil.in@SD"),
    357: ("Star Vijay HD",         "StarVijay.in@SD"),
    67:  ("Adithya TV",            "AdithyaTV.in@SD"),
    358: ("SUN TV HD",             "SunTVMalaysia.my@SD"),
    397: ("Astro Vaanavil",        "AstroVaanavil.my@SD"),
    399: ("Astro Vellithirai HD",  "AstroVellithirai.my@SD"),
}

astro_ch_data = {}  # xml_id -> (name, [progs])
_astro_lock = threading.Lock()

def _fetch_astro(site_id, cname, cxid):
    raw = fetch(f"https://contenthub-api.eco.astro.com.my/channel/{site_id}.json", timeout=20)
    if not raw:
        print(f"  {cname}: fetch failed")
        return
    try:
        schedule = json.loads(raw).get("response", {}).get("schedule", {})
        progs = []
        for day_items in schedule.values():
            for p in day_items:
                start = iso_to_xmltv(p.get("eventStartMyt", "") or p.get("datetimeInUtc", ""))
                stop  = iso_to_xmltv(p.get("eventEndMyt", ""))
                if not start or not stop:
                    dur_str = p.get("duration", "")
                    start_str = p.get("datetimeInUtc", "")
                    if start_str and dur_str:
                        try:
                            m = re.match(r"(\d+):(\d+):(\d+)", dur_str)
                            if m:
                                secs = int(m.group(1))*3600 + int(m.group(2))*60 + int(m.group(3))
                                start_dt = datetime.datetime.fromisoformat(start_str.replace("Z","+00:00"))
                                start = start_dt.strftime("%Y%m%d%H%M%S +0000")
                                stop  = (start_dt + datetime.timedelta(seconds=secs)).strftime("%Y%m%d%H%M%S +0000")
                        except:
                            pass
                if not start or not stop:
                    continue
                title = p.get("mainTitle", "") or p.get("title", "")
                if not title:
                    continue
                progs.append({
                    "start": start, "stop": stop,
                    "title": title,
                    "desc": p.get("description", ""),
                    "genre": p.get("genre", "") or p.get("subGenre", ""),
                })
        with _astro_lock:
            astro_ch_data[cxid] = (cname, progs)
        print(f"  {cname}: {len(progs)} programmes")
    except Exception as e:
        print(f"  Astro fetch error for {site_id} {cname}: {e}")

_astro_threads = []
for _sid, (_cname, _cxid) in ASTRO_CHANNELS.items():
    _t = threading.Thread(target=_fetch_astro, args=(_sid, _cname, _cxid), daemon=True)
    _astro_threads.append(_t); _t.start()
for _t in _astro_threads: _t.join()
print(f"  Astro: fetched EPG for {len(astro_ch_data)} channels")

# ─── GENERATE XMLTV ───────────────────────────────────────────────────────────
print("\n=== Generating XMLTV ===")

# Extra display-name aliases for channels where playlist spelling differs from JioTV API name
# key: channel_name.lower() as returned by JioTV API
EXTRA_ALIASES = {
    "9x jhakaas": ["9x Jhakas", "9X Jhakas"],  # playlists often spell with single 'a'
    # News18 channels rebranded — iptv-org still carries old names; add both so either playlist matches
    "news18 marathi":                   ["News18 Lokmat"],
    "news18 mp chhattisgarh":           ["News18 MP", "News18 Madhya Pradesh"],
    "news18 up uttarakhand":            ["News18 UP", "News18 Uttar Pradesh"],
    "news18 rajasthan":                 ["News18 RAJASTHAN"],
    "news18 kannada":                   ["News18 Kannada News"],
    "news18 punjab haryana himachal":   ["News18 Punjab Haryana", "News18 Punjab"],
    "news18 bihar jharkhand":           ["News18 BIHAR", "News18 Bihar"],
    "news18 delhi ncr jk":              ["News18 JKLH", "News18 Delhi"],
    "news18 odia":                      ["News18 Oriya"],
    "news18 bangla":                    ["News18 Bangla News"],
}
lines = [
    '<?xml version="1.0" encoding="UTF-8"?>',
    '<tv generator-info-name="india-epg-merged" source-info-name="JioTV+Zee5+TataPlay+DishTV+Airtel+magticom+Astro">',
]

# Channels
# JioTV channels: use xmltv_id as id if available, else "jio_{channel_id}"
jio_ch_id_map = {}   # channel_id -> xmltv channel id used in XML
jio_xml_ids_used = set()  # all xmltv_ids written by JioTV (for Zee5 dedup)

for ch in jio_channels:
    cid = str(ch["channel_id"])
    name = ch.get("channel_name","")
    xid = jio_xmltv_ids.get(name.lower()) or jio_xmltv_ids.get(f"id_{cid}") or cross_xids.get(name.lower()) or ""
    xml_id = xid if xid else f"jio_{cid}"
    jio_ch_id_map[cid] = xml_id
    jio_xml_ids_used.add(xml_id)
    logo = ch.get("logoUrl","")
    if logo and not logo.startswith("http"):
        logo = "https://jiotvimages.cdn.jio.com/" + logo
    logo_tag = f'<icon src="{esc(logo)}"/>' if logo else ""
    lines.append(f'  <channel id="{esc(xml_id)}">')
    lines.append(f'    <display-name lang="en">{esc(name)}</display-name>')
    for alias in name_aliases(name):
        lines.append(f'    <display-name lang="en">{esc(alias)}</display-name>')
    for alias in EXTRA_ALIASES.get(name.lower(), []):
        lines.append(f'    <display-name lang="en">{esc(alias)}</display-name>')
    if logo_tag: lines.append(f"    {logo_tag}")
    lines.append('  </channel>')

# Zee5 channels — skip any whose xmltv_id is already covered by JioTV
zee5_ch_id_map = {}
zee5_skipped = 0
zee5_xml_ids_written = set()
for ch in zee5_channels:
    cid = ch["id"]
    name = ch["name"]
    xid = ch.get("xmltv_id","")
    xml_id = xid if xid else f"zee5_{cid}"
    zee5_ch_id_map[cid] = xml_id
    # If JioTV already has this xmltv_id, don't write a duplicate <channel> element.
    # Programmes for this channel will also be skipped below to avoid duplicates.
    if xml_id in jio_xml_ids_used:
        zee5_skipped += 1
        continue
    zee5_xml_ids_written.add(xml_id)
    logo = ch.get("logo","")
    if logo and not logo.startswith("http"):
        logo = f"https://akamaividz2.zee5.com/image/upload/resources/{cid}/channel_list/{logo}"
    logo_tag = f'<icon src="{esc(logo)}"/>' if logo else ""
    lines.append(f'  <channel id="{esc(xml_id)}">')
    lines.append(f'    <display-name lang="en">{esc(name)}</display-name>')
    for alias in name_aliases(name):
        lines.append(f'    <display-name lang="en">{esc(alias)}</display-name>')
    if logo_tag: lines.append(f"    {logo_tag}")
    lines.append('  </channel>')

print(f"  Zee5: skipped {zee5_skipped} channels already covered by JioTV")

# Tata Play channels — skip any already covered by JioTV or Zee5
all_xml_ids_used = set(jio_xml_ids_used) | zee5_xml_ids_written
tp_written_ids = set()
tp_skipped = 0
for ch in tataplay_ch_info:
    if ch["xml_id"] in all_xml_ids_used:
        tp_skipped += 1
        continue
    all_xml_ids_used.add(ch["xml_id"])
    tp_written_ids.add(ch["xml_id"])
    logo_tag = f'<icon src="{esc(ch["logo"])}"/>' if ch.get("logo") else ""
    lines.append(f'  <channel id="{esc(ch["xml_id"])}">')
    lines.append(f'    <display-name lang="en">{esc(ch["name"])}</display-name>')
    for alias in name_aliases(ch["name"]):
        lines.append(f'    <display-name lang="en">{esc(alias)}</display-name>')
    if logo_tag: lines.append(f"    {logo_tag}")
    lines.append('  </channel>')
print(f"  Tata Play: {len(tp_written_ids)} unique channels added ({tp_skipped} duplicates skipped)")

# Dish TV channels
dt_written_ids = set()
dt_skipped = 0
for ch in dishtv_ch_info:
    if ch["xml_id"] in all_xml_ids_used:
        dt_skipped += 1
        continue
    all_xml_ids_used.add(ch["xml_id"])
    dt_written_ids.add(ch["xml_id"])
    lines.append(f'  <channel id="{esc(ch["xml_id"])}">')
    lines.append(f'    <display-name lang="en">{esc(ch["name"])}</display-name>')
    for alias in name_aliases(ch["name"]):
        lines.append(f'    <display-name lang="en">{esc(alias)}</display-name>')
    lines.append('  </channel>')
print(f"  Dish TV: {len(dt_written_ids)} unique channels added ({dt_skipped} duplicates skipped)")

# Airtel channels
at_written_ids = set()
at_skipped = 0
for ch in airtel_ch_info:
    if ch["xml_id"] in all_xml_ids_used:
        at_skipped += 1
        continue
    all_xml_ids_used.add(ch["xml_id"])
    at_written_ids.add(ch["xml_id"])
    lines.append(f'  <channel id="{esc(ch["xml_id"])}">')
    lines.append(f'    <display-name lang="en">{esc(ch["name"])}</display-name>')
    for alias in name_aliases(ch["name"]):
        lines.append(f'    <display-name lang="en">{esc(alias)}</display-name>')
    lines.append('  </channel>')
print(f"  Airtel: {len(at_written_ids)} unique channels added ({at_skipped} duplicates skipped)")

# Astro Malaysia channels — write channel defs or inject aliases into existing ones
# EPG matching is by display-name, so add "(Astro)" alias so playlist entries like
# "Colors HD (Astro)" or "Sun TV HD (Astro)" match the right EPG channel.
astro_written_ids = set()
for cxid, (cname, _progs) in astro_ch_data.items():
    astro_alias = f"{cname} (Astro)"
    if cxid not in all_xml_ids_used:
        # New channel (e.g. SunTVMalaysia.my@SD) — write full definition
        all_xml_ids_used.add(cxid)
        astro_written_ids.add(cxid)
        lines.append(f'  <channel id="{esc(cxid)}">')
        lines.append(f'    <display-name lang="en">{esc(cname)}</display-name>')
        lines.append(f'    <display-name lang="en">{esc(astro_alias)}</display-name>')
        for alias in name_aliases(cname):
            lines.append(f'    <display-name lang="en">{esc(alias)}</display-name>')
        lines.append('  </channel>')
    else:
        # Channel def already written by Indian provider — inject "(Astro)" alias
        # so playlist entries like "Colors HD (Astro)" still match this channel
        target = f'<channel id="{esc(cxid)}">'
        for i, line in enumerate(lines):
            if target in line:
                j = i + 1
                while j < len(lines) and '</channel>' not in lines[j]:
                    j += 1
                lines.insert(j, f'    <display-name lang="en">{esc(astro_alias)}</display-name>')
                break
print(f"  Astro: {len(astro_written_ids)} new channel defs; (Astro) aliases injected for all")

# magticom.ge channels (non-Indian: Bollywood HD etc.)
for pname, pxmlid, _progs in magticom_channels_data:
    if pxmlid in all_xml_ids_used:
        continue  # skip if somehow already covered
    all_xml_ids_used.add(pxmlid)
    lines.append(f'  <channel id="{esc(pxmlid)}">')
    lines.append(f'    <display-name lang="en">{esc(pname)}</display-name>')
    for alias in name_aliases(pname):
        lines.append(f'    <display-name lang="en">{esc(alias)}</display-name>')
    lines.append('  </channel>')

# JioTV programmes
prog_count = 0
for ch in jio_channels:
    cid = str(ch["channel_id"])
    xml_id = jio_ch_id_map.get(cid, f"jio_{cid}")
    for p in jio_epg.get(cid, []):
        if not p.get("startEpoch") or not p.get("endEpoch"): continue
        start = epoch_ms_to_xmltv(p["startEpoch"])
        stop  = epoch_ms_to_xmltv(p["endEpoch"])
        title = esc(p.get("showname",""))
        desc  = esc(p.get("description","") or p.get("shortDesc",""))
        genres = p.get("showGenre",[]) or []
        genre = esc(genres[0]) if genres else ""
        lines.append(f'  <programme start="{start}" stop="{stop}" channel="{esc(xml_id)}">')
        lines.append(f'    <title lang="en">{title}</title>')
        if desc: lines.append(f'    <desc lang="en">{desc}</desc>')
        if genre: lines.append(f'    <category lang="en">{genre}</category>')
        lines.append('  </programme>')
        prog_count += 1

# Zee5 programmes — skip channels whose xmltv_id was already written by JioTV
for ch in zee5_channels:
    cid = ch["id"]
    xml_id = zee5_ch_id_map.get(cid, f"zee5_{cid}")
    if xml_id in jio_xml_ids_used:
        continue  # JioTV already provides this channel's programmes
    for p in zee5_epg.get(cid, []):
        start = iso_to_xmltv(p.get("start_time",""))
        stop  = iso_to_xmltv(p.get("end_time",""))
        if not start or not stop: continue
        title = esc(p.get("title",""))
        desc  = esc(p.get("description","") or p.get("list_image_description",""))
        genres = p.get("genres",[]) or []
        genre = esc(genres[0].get("value","")) if genres else ""
        lines.append(f'  <programme start="{start}" stop="{stop}" channel="{esc(xml_id)}">')
        lines.append(f'    <title lang="en">{title}</title>')
        if desc: lines.append(f'    <desc lang="en">{desc}</desc>')
        if genre: lines.append(f'    <category lang="en">{genre}</category>')
        lines.append('  </programme>')
        prog_count += 1

# Tata Play programmes
for ch in tataplay_ch_info:
    if ch["xml_id"] not in tp_written_ids:
        continue
    for p in tataplay_epg.get(ch["id"], []):
        start = epoch_ms_to_xmltv(p.get("startTime", 0))
        stop  = epoch_ms_to_xmltv(p.get("endTime", 0))
        if not start or not stop: continue
        title = esc(p.get("title",""))
        desc  = esc(p.get("desc","") or p.get("description",""))
        genre = esc(p.get("category",""))
        if not title: continue
        lines.append(f'  <programme start="{start}" stop="{stop}" channel="{esc(ch["xml_id"])}">')
        lines.append(f'    <title lang="en">{title}</title>')
        if desc: lines.append(f'    <desc lang="en">{desc}</desc>')
        if genre: lines.append(f'    <category lang="en">{genre}</category>')
        lines.append('  </programme>')
        prog_count += 1

# Dish TV programmes
for ch in dishtv_ch_info:
    if ch["xml_id"] not in dt_written_ids:
        continue
    for p in dishtv_epg.get(ch["id"], []):
        title = esc(p.get("title",""))
        desc  = esc(p.get("desc",""))
        genre = esc(p.get("genre",""))
        if not title: continue
        lines.append(f'  <programme start="{p["start"]}" stop="{p["stop"]}" channel="{esc(ch["xml_id"])}">')
        lines.append(f'    <title lang="en">{title}</title>')
        if desc: lines.append(f'    <desc lang="en">{desc}</desc>')
        if genre: lines.append(f'    <category lang="en">{genre}</category>')
        lines.append('  </programme>')
        prog_count += 1

# Airtel programmes
for ch in airtel_ch_info:
    if ch["xml_id"] not in at_written_ids:
        continue
    for p in airtel_epg.get(ch["id"], []):
        title = esc(p.get("title",""))
        desc  = esc(p.get("desc",""))
        genre = esc(p.get("genre",""))
        if not title: continue
        lines.append(f'  <programme start="{p["start"]}" stop="{p["stop"]}" channel="{esc(ch["xml_id"])}">')
        lines.append(f'    <title lang="en">{title}</title>')
        if desc: lines.append(f'    <desc lang="en">{desc}</desc>')
        if genre: lines.append(f'    <category lang="en">{genre}</category>')
        lines.append('  </programme>')
        prog_count += 1

# magticom.ge programmes (Bollywood HD etc.)
for pname, pxmlid, progs in magticom_channels_data:
    for p in progs:
        title = esc(p.get("title",""))
        desc  = esc(p.get("desc",""))
        genre = esc(p.get("category",""))
        if not title: continue
        lines.append(f'  <programme start="{p["start"]}" stop="{p["stop"]}" channel="{esc(pxmlid)}">')
        lines.append(f'    <title lang="en">{title}</title>')
        if desc: lines.append(f'    <desc lang="en">{desc}</desc>')
        if genre: lines.append(f'    <category lang="en">{genre}</category>')
        lines.append('  </programme>')
        prog_count += 1

# ─── SD→HD programme copy for Indian channels ────────────────────────────────
# Indian SD and HD channels broadcast the same content; Indian providers only
# provide EPG under the HD xmltv_id. Copy HD programmes to SD so playlists
# using tvg-id or name matching the SD channel still get EPG.
# (This is distinct from Astro channels which have their own separate schedule.)
INDIAN_SD_FROM_HD = {
    "SonyMarathi.in@SD":  "SonyMarathi.in@HD",
}
for sd_id, hd_id in INDIAN_SD_FROM_HD.items():
    hd_tag = f'channel="{hd_id}"'
    sd_tag = f'channel="{sd_id}"'
    copied = 0
    in_block = False
    cur_block = []
    fallback_lines = []
    for line in lines:
        if not in_block and '<programme ' in line and hd_tag in line:
            in_block = True
            cur_block = [line.replace(hd_tag, sd_tag)]
        elif in_block:
            cur_block.append(line)
            if '</programme>' in line:
                fallback_lines.extend(cur_block)
                copied += 1
                in_block = False
                cur_block = []
    if fallback_lines:
        lines[-1:-1] = fallback_lines
        prog_count += copied
    print(f"  SD copy: {sd_id} <- {hd_id}: {copied} programmes")

# Astro Malaysia programmes
astro_prog_count = 0
for cxid, (cname, progs) in astro_ch_data.items():
    for p in progs:
        lines.append(f'  <programme start="{p["start"]}" stop="{p["stop"]}" channel="{esc(cxid)}">')
        lines.append(f'    <title lang="en">{esc(p["title"])}</title>')
        if p.get("desc"): lines.append(f'    <desc lang="en">{esc(p["desc"])}</desc>')
        if p.get("genre"): lines.append(f'    <category lang="en">{esc(p["genre"])}</category>')
        lines.append('  </programme>')
        astro_prog_count += 1
        prog_count += 1
print(f"  Astro: {astro_prog_count} programmes added")

lines.append("</tv>")

content = "\n".join(lines).encode("utf-8")
if OUT_XML.endswith(".gz"):
    with gzip.open(OUT_XML, "wb") as f:
        f.write(content)
else:
    with open(OUT_XML, "wb") as f:
        f.write(content)

size_kb = os.path.getsize(OUT_XML)//1024
zee5_unique = len(zee5_channels) - zee5_skipped
magticom_unique = len(magticom_channels_data)
astro_unique = len(astro_written_ids)
total_channels = len(jio_channels) + zee5_unique + len(tp_written_ids) + len(dt_written_ids) + len(at_written_ids) + magticom_unique + astro_unique
print(f"\nDone! {OUT_XML}")
print(f"  Channels: {len(jio_channels)} JioTV + {zee5_unique} Zee5 + {len(tp_written_ids)} TataPlay + {len(dt_written_ids)} DishTV + {len(at_written_ids)} Airtel + {magticom_unique} magticom + {astro_unique} Astro = {total_channels} total")
print(f"  Programmes: {prog_count:,}")
print(f"  File size: {size_kb:,} KB (compressed)")
print(f"\nChannel ID format:")
print(f"  - Has xmltv_id (Zee channels, major Indian): uses standard xmltv_id e.g. 'ZeeTV.in@SD'")
print(f"  - No xmltv_id: uses 'jio_123' or 'zee5_xxx'")
print(f"  - OTT Navigator matches by display-name if tvg-id not found")
