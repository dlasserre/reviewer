"""Le journal, et le bus qui le distribue.

UNE regle porte tout : chaque transition emet UN evenement type, qui part vers
trois destinations dont aucune n'est la source.

    JSONL local          debogage, rejeu, post-mortem
    bus en memoire       l'API locale et, plus tard, le front de visualisation
    forge (throttle)     le contexte, la ou l'humain lit deja

Le bus existe DES MAINTENANT, alors que le front est un chantier ulterieur.
C'est deliberé : un front qui doit reparser des lignes de journal reconstruit un
etat approximatif, et il sera faux. Le champ `profile` est la pour la meme
raison — le front devra afficher plusieurs projets cote a cote, et un
identifiant ajoute apres coup n'est jamais retroactif.
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from collections.abc import AsyncIterator
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

__all__ = ["Event", "Journal", "new_job_id", "relire"]


def _maintenant() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def new_job_id() -> str:
    return f"j_{uuid.uuid4().hex[:20]}"


@dataclass(slots=True)
class Event:
    """Une transition. Les champs sont ceux qu'on veut pouvoir filtrer.

    `why` est le champ qui compte : une phrase lisible, ecrite AU MOMENT de la
    decision. C'est elle qui repond a « pourquoi cet agent travaille ? » sans
    ouvrir le code — et elle ne peut pas etre reconstituee apres coup.
    """

    event: str
    profile: str
    ts: str = field(default_factory=_maintenant)
    job_id: str | None = None
    repository: str | None = None
    pull_request: int | None = None
    issue: int | None = None
    state: str | None = None
    review_cycle: int | None = None
    claude_session: str | None = None
    worktree: str | None = None
    trigger: dict[str, Any] | None = None
    why: str | None = None
    result: str | None = None
    detail: dict[str, Any] | None = None

    def to_json(self) -> str:
        return json.dumps({k: v for k, v in asdict(self).items() if v is not None},
                          ensure_ascii=False)


class Journal:
    """Ecrit en JSONL et diffuse aux abonnes.

    L'ecriture disque est SYNCHRONE et suivie d'un `flush` : un journal perdu au
    moment d'un crash est precisement celui dont on aurait eu besoin. Le cout
    est negligeable — quelques dizaines de lignes par heure.
    """

    def __init__(self, logs_dir: Path, *, profile: str = "-", keep: int = 200) -> None:
        self.logs_dir = Path(logs_dir)
        self.profile = profile
        self._abonnes: set[asyncio.Queue[Event]] = set()
        self._recents: list[Event] = []
        self._keep = keep
        self.logs_dir.mkdir(parents=True, exist_ok=True)

    @property
    def _fichier(self) -> Path:
        # Un fichier par jour : un journal qu'on ne peut pas ouvrir parce qu'il
        # fait 400 Mo ne sert a personne.
        return self.logs_dir / f"{datetime.now(timezone.utc):%Y-%m-%d}.jsonl"

    def emit(self, event: Event) -> Event:
        if not event.profile or event.profile == "-":
            event.profile = self.profile
        ligne = event.to_json()
        try:
            with self._fichier.open("a", encoding="utf-8") as f:
                f.write(ligne + "\n")
                f.flush()
                os.fsync(f.fileno())
        except OSError:
            # Ne JAMAIS faire tomber le demon parce que le journal est
            # inaccessible (disque plein, fichier verrouille). On perd la trace,
            # pas le travail — et le bus, lui, recoit quand meme.
            pass

        self._recents.append(event)
        del self._recents[:-self._keep]
        for q in list(self._abonnes):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                # Un abonne lent ne bloque pas le demon : il perd des
                # evenements, ce qui est visible cote client (trou dans le
                # flux), la ou un blocage serait invisible.
                pass
        return event

    def recent(self, limit: int = 50) -> list[Event]:
        """Les derniers evenements — memoire d'abord, fichier du jour ensuite.

        ── LE DEFAUT QUE CE REPLI SUPPRIME ─────────────────────────────────

        Ce tampon ne contient que ce que CE processus a emis. Un demon qui
        redemarre repartait donc d'un journal vide, et la console affichait
        « journal vide » alors que le fichier du jour portait tout. Pire : le
        fil d'un job termine avant le redemarrage etait definitivement
        invisible depuis l'API, alors qu'il etait sur disque.

        On complete donc par la QUEUE du fichier du jour. Le fichier est lu en
        entier — un jour de demon fait quelques centaines de lignes, pas de
        quoi justifier une lecture a l'envers — puis tronque.

        Les evenements de la memoire priment : ils sont deja analyses, et un
        evenement emis pendant la lecture ne peut pas manquer.
        """
        if len(self._recents) >= limit:
            return self._recents[-limit:]
        anciens: list[Event] = []
        try:
            with self._fichier.open(encoding="utf-8") as f:
                for ligne in f:
                    ligne = ligne.strip()
                    if not ligne:
                        continue
                    try:
                        anciens.append(Event(**json.loads(ligne)))
                    except (ValueError, TypeError):
                        # Une ligne illisible — ecriture coupee, format d'une
                        # version anterieure — ne doit pas priver des autres.
                        continue
        except OSError:
            return self._recents[-limit:]
        # On retire de la relecture ce que la memoire porte deja, en comparant
        # sur l'horodatage ET l'evenement : deux emissions distinctes a la meme
        # seconde restent distinctes par leur contenu.
        vus = {(e.ts, e.event, e.repository, e.pull_request, e.why)
               for e in self._recents}
        anciens = [e for e in anciens
                   if (e.ts, e.event, e.repository, e.pull_request, e.why) not in vus]
        # Trie chronologique APRES fusion. Concatener « fichier puis memoire »
        # placerait un evenement ecrit a l'instant par un autre processus AVANT
        # un evenement plus ancien de celui-ci — un fil qui remonte le temps est
        # pire qu'un fil incomplet, parce qu'on le lit sans se mefier. Le tri
        # est STABLE : deux evenements de la meme seconde gardent leur ordre.
        return sorted(anciens + self._recents, key=lambda e: e.ts)[-limit:]

    async def subscribe(self) -> AsyncIterator[Event]:
        """Flux pour l'API locale (SSE). Se desabonne tout seul a la sortie."""
        q: asyncio.Queue[Event] = asyncio.Queue(maxsize=256)
        self._abonnes.add(q)
        try:
            while True:
                yield await q.get()
        finally:
            self._abonnes.discard(q)


def relire(logs_dir: Path, *, jours: int = 3, limite: int = 4000) -> list[Event]:
    """Relit les evenements des derniers fichiers de journal.

    ── POURQUOI PAS `Journal.recent` ───────────────────────────────────────

    `recent` est un tampon EN MEMOIRE : il tient 200 evenements et repart a vide
    a chaque redemarrage du demon. C'est ce qu'il faut pour un flux, pas pour un
    historique — une console rouverte apres un redemarrage n'aurait plus rien a
    montrer, alors que tout est sur le disque.

    On lit du plus RECENT au plus ancien et on s'arrete des que `limite` est
    atteinte : un journal de plusieurs jours n'a pas a etre charge entier pour
    afficher les derniers cycles.

    Une ligne illisible est SAUTEE, jamais fatale. Un journal tronque par un
    arret brutal se termine souvent par une ligne partielle, et refuser de lire
    l'historique entier pour ca serait absurde.
    """
    logs_dir = Path(logs_dir)
    if not logs_dir.is_dir():
        return []
    fichiers = sorted(logs_dir.glob("*.jsonl"), reverse=True)[:jours]
    out: list[Event] = []
    for f in fichiers:
        try:
            lignes = f.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for ligne in reversed(lignes):
            if not ligne.strip():
                continue
            try:
                brut = json.loads(ligne)
            except json.JSONDecodeError:
                continue
            champs = {k: v for k, v in brut.items() if k in Event.__slots__}
            try:
                out.append(Event(**champs))
            except TypeError:
                continue
            if len(out) >= limite:
                return list(reversed(out))
    return list(reversed(out))
