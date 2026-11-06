import urllib.request, json, os
d = json.load(urllib.request.urlopen('https://pypi.org/pypi/gphoto2/2.6.4/json'))
url = [u['url'] for u in d['urls'] if u['packagetype'] == 'sdist'][0]
print('URL:', url)
dest = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'gp_build', 'gphoto2-2.6.4.tar.gz')
os.makedirs(os.path.dirname(dest), exist_ok=True)
urllib.request.urlretrieve(url, dest)
print('downloaded', dest, os.path.getsize(dest), 'bytes')
