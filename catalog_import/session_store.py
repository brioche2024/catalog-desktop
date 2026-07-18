import json
from dataclasses import asdict, dataclass
from pathlib import Path

from .config import SESSION_DIR


@dataclass
class PfsSession:
    email: str
    access_token: str


@dataclass
class EfashionSession:
    email: str
    access_token: str = ""
    id_vendeur: int | None = None


@dataclass
class AppSession:
    pfs: PfsSession | None = None
    efashion: EfashionSession | None = None


class SessionStore:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or (SESSION_DIR / "session.json")

    def save(self, session: AppSession) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(asdict(session), indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

    def load(self) -> AppSession | None:
        if not self.path.exists():
            return None
        data = json.loads(self.path.read_text(encoding="utf-8"))

        if "pfs" not in data and "access_token" in data:
            return AppSession(
                pfs=PfsSession(
                    email=str(data.get("email", "")),
                    access_token=str(data["access_token"]),
                ),
                efashion=None,
            )

        pfs_data = data.get("pfs")
        efashion_data = data.get("efashion")

        efashion = None
        if efashion_data and efashion_data.get("access_token"):
            efashion = EfashionSession(**efashion_data)

        return AppSession(
            pfs=PfsSession(**pfs_data) if pfs_data else None,
            efashion=efashion,
        )

    def clear(self) -> None:
        if self.path.exists():
            self.path.unlink()
