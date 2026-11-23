# PyInstaller recipe for the downloadable app. Build from this folder: pyinstaller voxchat.spec
import sys

icon = {'win32': 'assets/voxchat.ico', 'darwin': 'assets/voxchat.icns'}.get(sys.platform)
a = Analysis(['desktop.py'], datas=[('assets', 'assets'), ('live/certs', 'live/certs')])
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name='VoxChat', console=False, icon=icon)
coll = COLLECT(exe, a.binaries, a.datas, name='VoxChat')
if sys.platform == 'darwin':
    app = BUNDLE(coll, name='VoxChat.app', icon=icon, bundle_identifier='io.github.voxchat',
                 # macOS refuses microphone access to apps without this text.
                 info_plist={'NSMicrophoneUsageDescription': 'VoxChat uses the microphone for party voice chat.'})
