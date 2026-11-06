#!/usr/bin/env python3
"""
Inject data-i18n attributes into index.html by matching text content
against the zh dictionary in i18n.js.

More robust parser that handles the full i18n.js structure.
"""

import re
import json
import os
import sys

PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
I18N_JS = os.path.join(PROJECT, 'electron_app', 'src', 'js', 'i18n.js')
INDEX_HTML = os.path.join(PROJECT, 'electron_app', 'src', 'index.html')


def extract_zh_mapping(i18n_path):
    """Parse i18n.js and return {zh_text: key} for all zh entries."""
    with open(i18n_path, 'r', encoding='utf-8') as f:
        lines = f.readlines()

    # Find the zh: { line
    in_zh = False
    brace_depth = 0
    zh_lines = []

    for line in lines:
        if not in_zh:
            if re.match(r'\s*zh\s*:\s*\{', line):
                in_zh = True
                brace_depth = 1
                # Include the line but start from after the {
                rest = re.sub(r'.*zh\s*:\s*\{', '', line, count=1)
                if rest.strip():
                    zh_lines.append(rest)
                continue
        else:
            # Count braces in this line
            for ch in line:
                if ch == '{':
                    brace_depth += 1
                elif ch == '}':
                    brace_depth -= 1
                    if brace_depth == 0:
                        # End of zh block - include text before the }
                        before_close = line[:line.rfind('}')]
                        if before_close.strip():
                            zh_lines.append(before_close)
                        in_zh = False
                        break
            if in_zh:
                zh_lines.append(line)

    zh_text = ''.join(zh_lines)

    # Now parse all key: 'value' pairs
    # Some are single-quoted, some double-quoted
    # Handle both: key: 'value' and key: "value"
    mapping = {}

    # Pattern 1: single-quoted values
    for m in re.finditer(r"(\w+)\s*:\s*'([^']*)'", zh_text):
        key, value = m.group(1), m.group(2)
        if value and value not in mapping:
            mapping[value] = key

    # Pattern 2: double-quoted values (for keys with embedded single quotes, etc.)
    for m in re.finditer(r'(\w+)\s*:\s*"([^"]*)"', zh_text):
        key, value = m.group(1), m.group(2)
        if value and value not in mapping:
            mapping[value] = key

    # Filter out very short/ambiguous keys
    # Keep only texts >= 2 chars to avoid false matches on single chars
    mapping = {k: v for k, v in mapping.items() if len(k) >= 2}

    print(f"Extracted {len(mapping)} zh text → key mappings")
    return mapping


def inject_i18n(html, mapping):
    """Add data-i18n attributes to HTML elements based on text content matching."""
    # Build sorted list (longest text first to avoid partial matches)
    items = sorted(mapping.items(), key=lambda x: -len(x[0]))

    modified = 0
    lines = html.split('\n')
    result = []

    for line in lines:
        orig = line
        new_line = line

        for text, key in items:
            # Skip if line already has this data-i18n key
            if f'data-i18n="{key}"' in new_line or f"data-i18n='{key}'" in new_line:
                continue
            if f'data-i18n-placeholder="{key}"' in new_line:
                continue
            if f'data-i18n-title="{key}"' in new_line:
                continue

            esc = re.escape(text)

            # Case 1: <tag ...>TEXT</tag> (text-only element, no children)
            # Match: opening tag with possible attributes, then exactly the text, then closing tag
            tag_pattern = r'(<(\w+)\b([^>]*?))>\s*' + esc + r'\s*</\2>'
            m = re.search(tag_pattern, new_line)
            if m:
                tag_start = m.group(1)
                if 'data-i18n=' not in tag_start:
                    # Replace: add data-i18n before >
                    replacement = m.group(0).replace('>' + m.group(0)[m.group(0).index('>')+1:], '', 1)
                    # More carefully:
                    before_close = tag_start + ' data-i18n="' + key + '">\n' + text + '\n</' + m.group(2) + '>'
                    new_line = new_line[:m.start()] + before_close + new_line[m.end():]
                    modified += 1
                    continue  # move to next text

            # Case 2: <tag ...>TEXT</tag> but with attributes, using exact match on tag body
            # Try a more general approach
            body_pattern = r'^(\s*<(\w+)\b([^>]*))>(\s*)' + esc + r'(\s*)</\2>\s*$'
            m = re.match(body_pattern, new_line)
            if m:
                tag_start = m.group(1)
                lead_space = m.group(4)
                trail_space = m.group(5)
                if 'data-i18n=' not in tag_start:
                    new_line = (m.group(1) + ' data-i18n="' + key + '">' +
                               lead_space + text + trail_space + '</' + m.group(2) + '>')
                    modified += 1
                    continue

            # Case 3: <i class="ph ph-xxx"></i> TEXT → <i ...></i> <span data-i18n="k">TEXT</span>
            icon_pat = r'(<i\s+class="ph\s+ph-[^"]*"></i>)\s*' + esc + r'(?=\s*<|$)'
            m = re.search(icon_pat, new_line)
            if m:
                new_line = new_line[:m.start()] + m.group(1) + ' <span data-i18n="' + key + '">' + text + '</span>' + new_line[m.end():]
                modified += 1
                continue

            # Case 4: TEXT <i class="ph ..."></i> (reversed)
            rev_pat = esc + r'\s*(<i\s+class="ph\s+ph-[^"]*"></i>)'
            m = re.search(rev_pat, new_line)
            if m:
                new_line = new_line[:m.start()] + '<span data-i18n="' + key + '">' + text + '</span> ' + m.group(1) + new_line[m.end():]
                modified += 1
                continue

            # Case 5: placeholder="TEXT"
            placeholder_pat = r'placeholder="' + esc + r'"'
            if re.search(placeholder_pat, new_line) and f'data-i18n-placeholder="{key}"' not in new_line:
                new_line = re.sub(placeholder_pat,
                                  r'data-i18n-placeholder="' + key + r'" placeholder="' + text + r'"',
                                  new_line, count=1)
                modified += 1
                continue

            # Case 6: title="TEXT"
            title_pat = r'title="' + esc + r'"'
            if re.search(title_pat, new_line) and f'data-i18n-title="{key}"' not in new_line:
                new_line = re.sub(title_pat,
                                  r'data-i18n-title="' + key + r'" title="' + text + r'"',
                                  new_line, count=1)
                modified += 1
                continue

            # Case 7: <option>TEXT</option> or <option ...>TEXT</option>
            opt_pat = r'(<option\b[^>]*>)\s*' + esc + r'\s*</option>'
            m = re.search(opt_pat, new_line)
            if m and 'data-i18n=' not in m.group(1):
                new_line = new_line[:m.start()] + m.group(1)[:-1] + ' data-i18n="' + key + '">\n' + text + '\n</option>' + new_line[m.end():]
                modified += 1
                continue

        result.append(new_line)

    print(f"Made {modified} i18n attribute injections")
    return '\n'.join(result)


def main():
    print("=== Inject i18n attributes into index.html ===\n")

    mapping = extract_zh_mapping(I18N_JS)

    with open(INDEX_HTML, 'r', encoding='utf-8') as f:
        html = f.read()

    # Restore from backup first (to get clean state)
    bak = INDEX_HTML + '.i18n-bak'
    if os.path.exists(bak):
        print("Restoring from backup for clean run...")
        with open(bak, 'r', encoding='utf-8') as f:
            html = f.read()
    else:
        with open(bak, 'w', encoding='utf-8') as f:
            f.write(html)
        print(f"Backup saved to {bak}")

    before = len(re.findall(r'data-i18n[\w-]*="', html))

    result = inject_i18n(html, mapping)

    after = len(re.findall(r'data-i18n[\w-]*="', result))

    with open(INDEX_HTML, 'w', encoding='utf-8') as f:
        f.write(result)

    print(f"\ndata-i18n attributes: {before} → {after} (+{after - before})")
    print(f"Done!")


if __name__ == '__main__':
    main()
