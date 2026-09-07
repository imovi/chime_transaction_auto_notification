import re
from config import Config
from geelark.client import GeeLarkClient
from geelark.shell import ShellManager

cfg = Config()
client = GeeLarkClient(cfg)
shell = ShellManager(client)
target = '635090487589470334'

shell.execute(target, 'uiautomator dump /sdcard/ui_pin.xml')
raw = shell.execute(target, 'cat /sdcard/ui_pin.xml')
matches = re.findall(r'(?:text|content-desc)="([^"]+)"[^>]*bounds="([^"]+)"', raw)
for t, b in matches:
    print(f'{t} -> {b}')
