#!/usr/bin/env python3
"""
Fast injector: For each line with Chinese text, look up the text in a hash map
and add data-i18n attributes. O(N) efficiency.
"""
import re, os

PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
I18N_JS = os.path.join(PROJECT, 'electron_app', 'src', 'js', 'i18n.js')
INDEX_HTML = os.path.join(PROJECT, 'electron_app', 'src', 'index.html')

# ── Step 1: Extract zh mapping ──
with open(I18N_JS, 'r', encoding='utf-8') as f:
    content = f.read()

# Find zh: { block
zh_start = content.find('zh: {')
brace = content.index('{', zh_start)
depth = 1
i = brace + 1
while depth > 0 and i < len(content):
    if content[i] == '{': depth += 1
    elif content[i] == '}': depth -= 1
    i += 1
zh_block = content[brace:i]

# Extract all key: 'value' pairs
mapping = {}
for m in re.finditer(r"(\w+)\s*:\s*'([^']*)'", zh_block):
    k, v = m.group(1), m.group(2)
    if len(v) >= 2 and v not in mapping:
        mapping[v] = k

# Sort by text length descending (longest match first)
items = sorted(mapping.items(), key=lambda x: -len(x[0]))
print(f"Extracted {len(mapping)} zh text → key mappings")

# ── Step 2: Process HTML ──
with open(INDEX_HTML, 'r', encoding='utf-8') as f:
    html = f.read()

# Backup
bak = INDEX_HTML + '.bak2'
with open(bak, 'w', encoding='utf-8') as f:
    f.write(html)

lines = html.split('\n')
result = []
modified = 0

# Pre-compile patterns for efficiency
HAS_CHINESE = re.compile(r'[一-鿿]')

def has_data_i18n(line):
    return 'data-i18n=' in line

for line in lines:
    new_line = line

    # Skip lines without Chinese or that already have data-i18n
    if not HAS_CHINESE.search(line) or has_data_i18n(line):
        result.append(new_line)
        continue

    for text, key in items:
        esc = re.escape(text)
        if text not in new_line:  # fast pre-check
            continue

        # --- Element with only text: <tag ...>TEXT</tag> ---
        m = re.search(r'<(\w+)\b([^>]*?)>\s*' + esc + r'\s*</\1>', new_line)
        if m:
            tag, attrs = m.group(1), m.group(2)
            if 'data-i18n=' not in attrs and tag not in ('script','style','option'):
                # Replace by adding data-i18n to opening tag
                old = m.group(0)
                new = re.sub(r'<\w+\b', lambda x: x.group(0), old)  # placeholder
                new = '<' + tag + attrs + f' data-i18n="{key}">\n{text}\n</{tag}>'
                new_line = new_line.replace(old, new, 1)
                modified += 1
                continue  # line modified, check other texts

        # --- Option: <option ...>TEXT</option> ---
        m = re.search(r'<option\b([^>]*?)>\s*' + esc + r'\s*</option>', new_line)
        if m:
            attrs = m.group(1)
            if 'data-i18n=' not in attrs:
                old = m.group(0)
                new = f'<option{attrs} data-i18n="{key}">\n{text}\n</option>'
                new_line = new_line.replace(old, new, 1)
                modified += 1
                continue

        # --- Icon + text: <i class="ph ph-xxx"></i> TEXT ---
        m = re.search(r'<i\s+class="ph\s+ph-[^"]*"></i>\s*' + esc + r'(?=\s*<|$|\s*$)', new_line)
        if m:
            old = m.group(0)
            icon_end = old.index('</i>') + 5
            icon_part = old[:icon_end]
            new = icon_part + f' <span data-i18n="{key}">{text}</span>'
            new_line = new_line.replace(old, new, 1)
            modified += 1
            continue

        # --- Placeholder ---
        if f'placeholder="{text}"' in new_line and f'data-i18n-placeholder="{key}"' not in new_line:
            new_line = new_line.replace(f'placeholder="{text}"',
                                        f'data-i18n-placeholder="{key}" placeholder="{text}"', 1)
            modified += 1
            continue

        # --- Title ---
        if f'title="{text}"' in new_line and f'data-i18n-title="{key}"' not in new_line:
            new_line = new_line.replace(f'title="{text}"',
                                        f'data-i18n-title="{key}" title="{text}"', 1)
            modified += 1
            continue

    result.append(new_line)

# Fix duplicate data-i18n attributes (in case a line got matched by multiple patterns)
final = []
for line in result:
    # Check for duplicate data-i18n="..." on same tag
    i18ns = list(re.finditer(r'data-i18n="([^"]*)"', line))
    if len(i18ns) > 1:
        # Keep only the first one
        for m in i18ns[1:]:
            line = line[:m.start()] + line[m.end():]
    final.append(line)

out = '\n'.join(final)

with open(INDEX_HTML, 'w', encoding='utf-8') as f:
    f.write(out)

before = len(re.findall(r'data-i18n[\w-]*="', html))
after = len(re.findall(r'data-i18n[\w-]*="', out))
print(f"data-i18n attributes: {before} → {after} (+{after - before})")
print(f"Done! {modified} injections")
