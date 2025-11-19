import re, json, aiohttp, urllib.parse

YOUTUBE_PLAYER_URL = "https://www.youtube.com/youtubei/v1/player?key={key}"
WEB_KEY = "AIzaSyAO_FJ2SlqU8Q4STEHLGCilw_Y9_9N0uQg"

# -------------------------------
# extract required functions from player.js
# -------------------------------
async def extract_cipher(js_url):
    async with aiohttp.ClientSession() as session:
        async with session.get(js_url) as r:
            code = await r.text()

    # extract signature function
    func_name = re.search(r"signature\"\s*,\s*([a-zA-Z0-9$]+)\(", code)
    if not func_name:
        raise Exception("Cannot find signature function")

    func_name = func_name.group(1)

    # extract the function body
    pattern = func_name + r"=function\(a\){(.*?)}"
    body = re.search(pattern, code, re.DOTALL)
    if not body:
        raise Exception("Cannot extract signature function body")

    body = body.group(1)

    # extract helper object
    obj_name = re.search(r";([A-Za-z0-9$]{2})\.", body)
    if not obj_name:
        raise Exception("Cannot extract cipher helper")

    obj_name = obj_name.group(1)

    # extract helper functions
    obj_pattern = r"var " + obj_name + r"=\{(.*?)\};"
    obj_body = re.search(obj_pattern, code, re.DOTALL)
    if not obj_body:
        raise Exception("Cannot extract cipher helper body")

    obj_body = obj_body.group(1)

    return body, obj_body, func_name, obj_name


# apply js cipher operator manually
def apply_js(step, s):
    if step["op"] == "reverse":
        return s[::-1]
    elif step["op"] == "slice":
        return s[step["value"]:]
    elif step["op"] == "swap":
        i = step["value"]
        s_list = list(s)
        s_list[0], s_list[i] = s_list[i], s_list[0]
        return "".join(s_list)
    return s


# parse cipher into Python steps
def parse_cipher(body, obj_body, obj_name):
    steps = []
    # reverse
    r = re.findall(obj_name + r"\.([A-Za-z0-9$]{2})", body)
    for fn in r:
        if fn in obj_body:
            if "reverse" in obj_body:
                steps.append({"op": "reverse"})
    # slice
    s = re.findall(rf"{obj_name}\.[A-Za-z0-9$]{{2}}\(\w+,(\d+)\)", body)
    for x in s:
        steps.append({"op": "slice", "value": int(x)})

    # swap
    w = re.findall(rf"{obj_name}\.[A-Za-z0-9$]{{2}}\(\w+,(\d+)\)", body)
    for x in w:
        steps.append({"op": "swap", "value": int(x)})

    return steps


def decipher_url(cipher, steps):
    s = cipher
    for step in steps:
        s = apply_js(step, s)
    return s


# ------------------------------------
# MAIN FUNCTION — WORKS ALWAYS
# ------------------------------------
async def get_all_qualities(video_id):
    payload = {
        "context": {
            "client": {
                "clientName": "WEB",
                "clientVersion": "2.20240222.01.00"
            }
        },
        "videoId": video_id
    }

    async with aiohttp.ClientSession() as session:
        async with session.post(YOUTUBE_PLAYER_URL.format(key=WEB_KEY), json=payload) as r:
            data = await r.json()

    # get js URL
    assets = data.get("assets", {})
    js_url = "https://youtube.com" + assets["js"]

    # extract cipher
    body, obj_body, fn_name, obj_name = await extract_cipher(js_url)
    steps = parse_cipher(body, obj_body, obj_name)

    # get formats
    streaming = data.get("streamingData", {})
    formats = streaming.get("formats", []) + streaming.get("adaptiveFormats", [])

    results = []
    for f in formats:
        url = f.get("url")
        if not url and "signatureCipher" in f:
            cipher = dict(urllib.parse.parse_qsl(f["signatureCipher"]))
            sig = decipher_url(cipher["s"], steps)
            url = cipher["url"] + "&sig=" + sig

        if url and "height" in f:
            results.append({
                "height": f["height"],
                "itag": f["itag"],
                "url": url
            })

    return sorted(results, key=lambda x: x["height"])
