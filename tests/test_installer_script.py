from pathlib import Path

ISS = (Path(__file__).resolve().parent.parent / "forge.iss").read_text(encoding="utf-8")


def test_installer_does_not_kill_its_own_process_tree():
    # The in-app updater starts the installer as a child of forge-app.exe. taskkill /T on forge-app.exe
    # would then kill the installer itself, so updates closed Forge without installing anything.
    taskkill_lines = [line for line in ISS.splitlines() if "taskkill" in line.lower()]
    assert taskkill_lines
    assert not any("/T" in line.replace("/FI", "") and "forge-app.exe" in line for line in taskkill_lines)


def test_installer_closes_the_forge_window_so_the_profile_is_not_left_locked():
    assert 'WINDOWTITLE eq Forge' in ISS and 'IMAGENAME eq msedge.exe' in ISS
