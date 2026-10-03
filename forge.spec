# PyInstaller spec: builds forge.exe (terminal) and forge-app.exe (desktop window) into dist\forge
from PyInstaller.utils.hooks import collect_submodules

hidden = collect_submodules("prompt_toolkit")
data = [("src/forge/web", "forge/web")]

a1 = Analysis(["launcher.py"], pathex=["src"], hiddenimports=hidden, datas=data)
a2 = Analysis(["launcher_app.py"], pathex=["src"], hiddenimports=hidden, datas=data)
MERGE((a1, "launcher", "forge"), (a2, "launcher_app", "forge-app"))

p1, p2 = PYZ(a1.pure), PYZ(a2.pure)
e1 = EXE(p1, a1.scripts, [], exclude_binaries=True, name="forge", console=True, icon="forge.ico")
e2 = EXE(p2, a2.scripts, [], exclude_binaries=True, name="forge-app", console=False, icon="forge.ico")
COLLECT(e1, a1.binaries, a1.datas, e2, a2.binaries, a2.datas, name="forge")
