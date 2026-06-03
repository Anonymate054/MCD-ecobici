import urllib.request
import re
from html.parser import HTMLParser

class EcobiciLinkParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links = {}

    def handle_starttag(self, tag, attrs):
        if tag == 'a':
            attrs_dict = dict(attrs)
            href = attrs_dict.get('href', '')
            if href.endswith('.csv'):
                # Extract YYYY-MM
                match = re.search(r"(\d{4})[-_]?(\d{2})", href)
                if match:
                    year, month = match.groups()
                    if 2010 <= int(year) <= 2030 and 1 <= int(month) <= 12:
                        key = f"{year}-{month}"
                        self.links[key] = href

def discover():
    url = "https://ecobici.cdmx.gob.mx/datos-abiertos/"
    req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
    with urllib.request.urlopen(req, timeout=15) as response:
        html = response.read().decode('utf-8')
    parser = EcobiciLinkParser()
    parser.feed(html)
    print(f"Discovered {len(parser.links)} links.")
    for k in sorted(parser.links.keys())[-5:]:
        print(f"  {k}: {parser.links[k]}")

if __name__ == "__main__":
    discover()
