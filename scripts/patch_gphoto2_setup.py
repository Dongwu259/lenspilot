"""Patch gphoto2 setup.py to be MSVC-safe (remove GCC-only flags)."""
import io
p = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'gp_build', 'gphoto2-2.6.4', 'setup.py')
s = open(p, encoding='utf-8').read()

# 1. blank extra_link_args (GCC rpath, not used on Windows; DLL found via PATH)
s = s.replace("extra_link_args = ['-Wl,-rpath,$ORIGIN/libgphoto2']",
              "extra_link_args = []")

# 2. drop '-lm' -> 'm' lib (MSVC has no m.lib; math is in CRT)
s = s.replace("libraries = [x.replace('-l', '') for x in gphoto2_flags['-l']]",
              "libraries = [x.replace('-l', '') for x in gphoto2_flags['-l'] if x != '-lm']")

# 3. blank extra_compile_args (GCC -O3/-Wno-* flags; setuptools adds /O2 for MSVC)
s = s.replace("""extra_compile_args = [
    '-O3', '-Wno-unused-variable', '-Wno-unused-but-set-variable',
    '-Wno-unused-label', '-Wno-strict-prototypes']""", "extra_compile_args = []")

open(p, 'w', encoding='utf-8').write(s)
print('patched OK')
