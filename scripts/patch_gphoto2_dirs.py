"""Patch gphoto2 setup.py: hardcode include_dirs/library_dirs to clean paths,
bypassing pkg-config's extra mingw CRT include path that conflicts with MSVC."""
p = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'gp_build', 'gphoto2-2.6.4', 'setup.py')
s = open(p, encoding='utf-8').read()

old = """libraries = [x.replace('-l', '') for x in gphoto2_flags['-l'] if x != '-lm']
library_dirs = [x.replace('-L', '') for x in gphoto2_flags['-L']]
include_dirs = [x.replace('-I', '') for x in gphoto2_include]"""
new = """libraries = [l for l in (x.replace('-l', '') for x in gphoto2_flags['-l']) if l != 'm']
library_dirs = [r'C:\\msys64\\mingw64\\lib']
include_dirs = [r'C:\\gphoto2_headers']"""

if old in s:
    s = s.replace(old, new)
    open(p, 'w', encoding='utf-8').write(s)
    print('patched include/library dirs OK')
else:
    print('OLD BLOCK NOT FOUND - check setup.py')
