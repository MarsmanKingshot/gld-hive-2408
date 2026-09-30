#!/usr/bin/env python3
"""Give every page a fresh version stamp so browsers reload once when GitHub Pages serves a stale copy.
Usage: python3 tools/stamp.py <stamp>   (run from the site root; writes version.txt and patches the pages)"""
import re, sys
stamp = sys.argv[1]
open("version.txt", "w").write(stamp + "\n")
snippet = ('<script data-fresh>(function(){var v="%s";fetch("version.txt?_="+Date.now(),{cache:"no-store"})'
           '.then(function(r){return r.text()}).then(function(t){t=t.trim();if(t&&t!==v){var u=new URL(location.href);'
           'u.searchParams.set("v",t);location.replace(u.toString());}}).catch(function(){});})();</script>') % stamp
for name in ("index.html", "officers.html", "banner_map.html", "map.html", "kingdom_map.html"):
    try:
        s = open(name).read()
    except FileNotFoundError:
        continue
    s = re.sub(r'<script data-fresh>.*?</script>\n?', '', s, flags=re.S)
    s = s.replace("</body>", snippet + "\n</body>", 1) if "</body>" in s else s + snippet
    open(name, "w").write(s)
print("stamped", stamp)
