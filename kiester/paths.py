"""Where Kiester keeps its data (shared by the server, the CLI and the installer; stdlib only)."""
import os, sys


def media_dir():
    """$KIESTER_MEDIA, else ~/Videos/Kiester (~/Movies/Kiester on macOS).
    Installs from before the rename used ~/Videos/kie; keep using that folder when it already holds a database."""
    if os.environ.get("KIESTER_MEDIA"): return os.path.expanduser(os.environ["KIESTER_MEDIA"])
    base = os.path.expanduser("~/Movies" if sys.platform == "darwin" else "~/Videos")
    new, legacy = os.path.join(base, "Kiester"), os.path.join(base, "kie")
    if not os.path.exists(os.path.join(new, "studio.db")) and os.path.exists(os.path.join(legacy, "studio.db")): return legacy
    return new
