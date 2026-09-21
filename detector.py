"""
detector.py - Detects frontend framework/technology used by a website
"""
import re
from bs4 import BeautifulSoup


FRAMEWORK_SIGNATURES = {
    "React": {
        "html_patterns": [
            r'id=["\']root["\']',
            r'data-reactroot',
            r'data-react-',
            r'__REACT_',
        ],
        "script_patterns": [
            r'react[\.\-][\d]+',
            r'react\.production\.min\.js',
            r'react\.development\.js',
            r'react-dom',
        ],
        "meta_patterns": [],
        "headers": {},
    },
    "Next.js": {
        "html_patterns": [
            r'__NEXT_DATA__',
            r'id=["\']__next["\']',
            r'_next/static',
        ],
        "script_patterns": [
            r'_next/static',
            r'next/dist',
        ],
        "meta_patterns": [],
        "headers": {"x-powered-by": "Next.js"},
    },
    "Vue.js": {
        "html_patterns": [
            r'data-v-',
            r'v-cloak',
            r'__vue__',
        ],
        "script_patterns": [
            r'vue[\.\-][\d]+',
            r'vue\.min\.js',
            r'vue\.runtime',
            r'vue-router',
            r'vuex',
        ],
        "meta_patterns": [],
        "headers": {},
    },
    "Nuxt.js": {
        "html_patterns": [
            r'__NUXT__',
            r'id=["\']__nuxt["\']',
            r'_nuxt/',
        ],
        "script_patterns": [
            r'_nuxt/',
        ],
        "meta_patterns": [],
        "headers": {"x-powered-by": "Nuxt"},
    },
    "Angular": {
        "html_patterns": [
            r'ng-version',
            r'ng-app',
            r'ng-controller',
            r'_nghost',
            r'_ngcontent',
        ],
        "script_patterns": [
            r'angular[\.\-][\d]+',
            r'angular\.min\.js',
            r'zone\.js',
        ],
        "meta_patterns": [],
        "headers": {},
    },
    "Svelte": {
        "html_patterns": [
            r'svelte-',
        ],
        "script_patterns": [
            r'svelte[\.\-][\d]+',
            r'svelte/internal',
        ],
        "meta_patterns": [],
        "headers": {},
    },
    "WordPress": {
        "html_patterns": [
            r'wp-content',
            r'wp-includes',
            r'wp-json',
        ],
        "script_patterns": [
            r'wp-includes',
            r'wp-content',
        ],
        "meta_patterns": [
            r'generator.*WordPress',
        ],
        "headers": {},
    },
    "Gatsby": {
        "html_patterns": [
            r'___gatsby',
            r'gatsby-',
        ],
        "script_patterns": [
            r'gatsby-',
        ],
        "meta_patterns": [],
        "headers": {},
    },
    "Astro": {
        "html_patterns": [
            r'astro-island',
            r'data-astro-',
        ],
        "script_patterns": [
            r'astro/',
        ],
        "meta_patterns": [
            r'generator.*Astro',
        ],
        "headers": {"x-powered-by": "Astro"},
    },
    "jQuery": {
        "html_patterns": [],
        "script_patterns": [
            r'jquery[\.\-][\d]+',
            r'jquery\.min\.js',
            r'jquery\.slim',
        ],
        "meta_patterns": [],
        "headers": {},
    },
    "Bootstrap": {
        "html_patterns": [],
        "script_patterns": [
            r'bootstrap[\.\-][\d]+',
            r'bootstrap\.min\.js',
            r'bootstrap\.bundle',
        ],
        "meta_patterns": [],
        "headers": {},
    },
}


def detect_framework(html: str, headers: dict = None) -> dict:
    """
    Detect frontend framework from HTML content and response headers.
    Returns a dict with detected frameworks and confidence scores.
    """
    headers = headers or {}
    results = {}

    soup = BeautifulSoup(html, "lxml")

    # Collect all script src attributes + inline content
    scripts = []
    for tag in soup.find_all("script"):
        src = tag.get("src", "")
        content = tag.string or ""
        scripts.append(src + " " + content)
    scripts_text = " ".join(scripts)

    # Collect meta generator
    meta_gen = ""
    for meta in soup.find_all("meta", attrs={"name": "generator"}):
        meta_gen += meta.get("content", "")

    for framework, sigs in FRAMEWORK_SIGNATURES.items():
        score = 0
        matched = []

        for pat in sigs["html_patterns"]:
            if re.search(pat, html, re.IGNORECASE):
                score += 2
                matched.append(f"html:{pat}")

        for pat in sigs["script_patterns"]:
            if re.search(pat, scripts_text, re.IGNORECASE):
                score += 3
                matched.append(f"script:{pat}")

        for pat in sigs["meta_patterns"]:
            if re.search(pat, meta_gen, re.IGNORECASE):
                score += 3
                matched.append(f"meta:{pat}")

        for hdr_key, hdr_val in sigs["headers"].items():
            actual = headers.get(hdr_key, headers.get(hdr_key.lower(), ""))
            if hdr_val.lower() in actual.lower():
                score += 5
                matched.append(f"header:{hdr_key}")

        if score > 0:
            results[framework] = {"score": score, "matched": matched}

    sorted_results = sorted(results.items(), key=lambda x: x[1]["score"], reverse=True)

    if not sorted_results:
        primary = "Plain HTML"
        frameworks = ["Plain HTML"]
    else:
        primary = sorted_results[0][0]
        frameworks = [f for f, _ in sorted_results]

    return {
        "primary": primary,
        "all": frameworks,
        "details": dict(sorted_results),
    }
