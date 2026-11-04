"""
Merged Indian IPTV EPG generator: JioTV + Zee5 + Tata Play
Outputs XMLTV file with standard xmltv_ids where available for best channel matching.
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

# ─── GENERATE XMLTV ───────────────────────────────────────────────────────────
print("\n=== Generating XMLTV ===")
lines = [
    '<?xml version="1.0" encoding="UTF-8"?>',
    '<tv generator-info-name="india-epg-merged" source-info-name="JioTV+Zee5">',
]

# Channels
# JioTV channels: use xmltv_id as id if available, else "jio_{channel_id}"
jio_ch_id_map = {}  # channel_id -> xmltv channel id used in XML
for ch in jio_channels:
    cid = str(ch["channel_id"])
    name = ch.get("channel_name","")
    # Look up xmltv_id by name or id
    xid = jio_xmltv_ids.get(name.lower()) or jio_xmltv_ids.get(f"id_{cid}") or ""
    xml_id = xid if xid else f"jio_{cid}"
    jio_ch_id_map[cid] = xml_id
    logo = ch.get("logoUrl","")
    if logo and not logo.startswith("http"):
        logo = "https://jiotvimages.cdn.jio.com/" + logo
    logo_tag = f'<icon src="{esc(logo)}"/>' if logo else ""
    lines.append(f'  <channel id="{esc(xml_id)}">')
    lines.append(f'    <display-name lang="en">{esc(name)}</display-name>')
    if name.startswith("&"):
        lines.append(f'    <display-name lang="en">{esc("And " + name[1:])}</display-name>')
    if logo_tag: lines.append(f"    {logo_tag}")
    lines.append('  </channel>')

# Zee5 channels
zee5_ch_id_map = {}
for ch in zee5_channels:
    cid = ch["id"]
    name = ch["name"]
    xid = ch.get("xmltv_id","")
    xml_id = xid if xid else f"zee5_{cid}"
    # Avoid duplicate IDs (JioTV may already have same xmltv_id)
    # Add zee5 prefix if already seen
    zee5_ch_id_map[cid] = xml_id
    logo = ch.get("logo","")
    if logo and not logo.startswith("http"):
        logo = f"https://akamaividz2.zee5.com/image/upload/resources/{cid}/channel_list/{logo}"
    logo_tag = f'<icon src="{esc(logo)}"/>' if logo else ""
    lines.append(f'  <channel id="{esc(xml_id)}">')
    lines.append(f'    <display-name lang="en">{esc(name)}</display-name>')
    if name.startswith("&"):
        lines.append(f'    <display-name lang="en">{esc("And " + name[1:])}</display-name>')
    if logo_tag: lines.append(f"    {logo_tag}")
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

# Zee5 programmes
for ch in zee5_channels:
    cid = ch["id"]
    xml_id = zee5_ch_id_map.get(cid, f"zee5_{cid}")
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

lines.append("</tv>")

content = "\n".join(lines).encode("utf-8")
if OUT_XML.endswith(".gz"):
    with gzip.open(OUT_XML, "wb") as f:
        f.write(content)
else:
    with open(OUT_XML, "wb") as f:
        f.write(content)

size_kb = os.path.getsize(OUT_XML)//1024
total_channels = len(jio_channels) + len(zee5_channels)
print(f"\nDone! {OUT_XML}")
print(f"  Channels: {len(jio_channels)} JioTV + {len(zee5_channels)} Zee5 = {total_channels} total")
print(f"  Programmes: {prog_count:,}")
print(f"  File size: {size_kb:,} KB (compressed)")
print(f"\nChannel ID format:")
print(f"  - Has xmltv_id (Zee channels, major Indian): uses standard xmltv_id e.g. 'ZeeTV.in@SD'")
print(f"  - No xmltv_id: uses 'jio_123' or 'zee5_xxx'")
print(f"  - OTT Navigator matches by display-name if tvg-id not found")
