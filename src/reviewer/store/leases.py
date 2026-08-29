"""L'etat LOCAL — le seul qui ne se deduit pas de la forge.

Deux choses, et deux seulement :

    BAUX     qui travaille sur quoi, en ce moment. C'est l'unique etat que la
             forge ne peut pas rendre : elle sait qu'une PR existe, pas qu'un
             processus est en train de s'en occuper.
    SUIVI    session Claude, worktree, cycle en cours, curseur de remarques.
             Des FAITS LOCAUX ou des caches — jamais une seconde verite sur ce
             que la forge dit deja.

Tout le reste — les checks, les fils, l'etat de la PR — se relit a chaque
passage. Stocker un etat deductible, c'est creer une seconde verite qui finira
par diverger, et le jour ou elle divergera personne ne saura laquelle croire.

── POURQUOI PAS `os.kill(pid, 0)` ──────────────────────────────────────────
La facon habituelle de tester qu'un PID est vivant est `os.kill(pid, 0)`. Sous
Windows, c'est un PIEGE : `os.kill` y appelle `TerminateProcess` pour tout
signal autre que CTRL_C_EVENT / CTRL_BREAK_EVENT. Le « test » TUERAIT donc le
processus qu'il interroge. On passe par `OpenProcess`.

Et la vivacite d'un PID n'est de toute facon qu'un RACCOURCI : les PID se
reutilisent. La garantie reelle vient de `expires_at`, que le detenteur
prolonge tant qu'il travaille. Le test de PID ne fait qu'accelerer la reprise
apres un crash, il ne la fonde pas.
"""

from __future__ import annotations

import os
import sqlite3
import sys
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

__all__ = ["Lease", "PullState", "StateStore", "pid_alive"]


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(d: datetime) -> str:
    return d.astimezone(timezone.utc).isoformat(timespec="seconds")


def _parse(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        d = datetime.fromisoformat(s)
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def pid_alive(pid: int) -> bool:
    """Ce PID correspond-il a un processus vivant ?

    Voir l'avertissement en tete de module : sous Windows, `os.kill(pid, 0)`
    tuerait le processus au lieu de l'interroger.
    """
    if pid <= 0:
        return False
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        k32 = ctypes.windll.kernel32
        h = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not h:
            return False
        try:
            code = wintypes.DWORD()
            if not k32.GetExitCodeProcess(h, ctypes.byref(code)):
                # On n'a pas su lire : on repond VIVANT. Se tromper dans ce
                # sens fait attendre l'expiration du bail ; se tromper dans
                # l'autre lancerait un second agent sur la meme PR.
                return True
            return code.value == STILL_ACTIVE
        finally:
            k32.CloseHandle(h)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # existe, mais appartient a quelqu'un d'autre
    return True


@dataclass(frozen=True, slots=True)
class Lease:
    profile: str
    repo: str
    pr: int
    job_id: str
    pid: int
    acquired_at: datetime
    expires_at: datetime

    def expired(self, now: datetime | None = None) -> bool:
        return (now or _now()) >= self.expires_at


@dataclass(frozen=True, slots=True)
class PullState:
    """Faits locaux sur une PR. Absents = valeurs neutres, jamais une erreur."""

    profile: str
    repo: str
    pr: int
    claude_session: str | None = None
    worktree: str | None = None
    review_cycle: int = 0
    last_handled_comment_id: int = 0
    nudge_sent: bool = False
    # SHA de tete pour lequel l'humain a DEJA ete appele au niveau de la PR.
    #
    # Un appel dans un FIL se protege tout seul : son marqueur d'attente est
    # dans le fil, donc dans la forge. Un appel au niveau de la PR — une CI
    # rouge n'appartient a aucun fil — n'a pas ce recours, et se reposterait a
    # chaque passage.
    #
    # L'ancrage est le SHA, pas un booleen ni un compteur : c'est le NIVEAU qui
    # a motive l'appel. De nouveaux commits changent la situation et meritent un
    # nouvel appel ; un passage de plus sur le meme etat, non.
    human_asked_sha: str = ""
    # Numero de l'issue creee pour porter les correctifs derives de cette PR.
    #
    # C'est la CLE D'IDEMPOTENCE de la creation d'issue, et elle est locale
    # exprès : l'index de recherche de GitHub met des dizaines de secondes a
    # voir une issue neuve, donc deux passages rapproches en creeraient deux.
    # La recherche par marqueur reste le filet quand cet etat a disparu.
    derived_issue: int = 0


_SCHEMA = """
CREATE TABLE IF NOT EXISTS leases (
    profile     TEXT NOT NULL,
    repo        TEXT NOT NULL,
    pr          INTEGER NOT NULL,
    job_id      TEXT NOT NULL,
    pid         INTEGER NOT NULL,
    acquired_at TEXT NOT NULL,
    expires_at  TEXT NOT NULL,
    PRIMARY KEY (profile, repo, pr)
);
CREATE TABLE IF NOT EXISTS pulls (
    profile                 TEXT NOT NULL,
    repo                    TEXT NOT NULL,
    pr                      INTEGER NOT NULL,
    claude_session          TEXT,
    worktree                TEXT,
    review_cycle            INTEGER NOT NULL DEFAULT 0,
    last_handled_comment_id INTEGER NOT NULL DEFAULT 0,
    nudge_sent              INTEGER NOT NULL DEFAULT 0,
    human_asked_sha         TEXT NOT NULL DEFAULT '',
    derived_issue           INTEGER NOT NULL DEFAULT 0,
    updated_at              TEXT NOT NULL,
    PRIMARY KEY (profile, repo, pr)
);
CREATE TABLE IF NOT EXISTS runs (
    profile TEXT NOT NULL,
    day     TEXT NOT NULL,
    count   INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (profile, day)
);
"""


class StateStore:
    """SQLite. Un seul fichier, lisible avec n'importe quel client.

    Le choix de SQLite n'est pas un defaut par paresse : quand le demon se
    comportera bizarrement, on voudra OUVRIR son etat et le lire a la main. Un
    format binaire maison ou un pickle rendraient ce moment beaucoup plus
    penible, pour aucun gain.
    """

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(self.path), isolation_level=None,
                                   check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        # WAL : plusieurs lecteurs (l'API locale) pendant qu'un ecrivain
        # travaille. Sans lui, un `GET /jobs` peut bloquer un job.
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA busy_timeout=5000")
        self._db.executescript(_SCHEMA)
        self._migrer()

    def _migrer(self) -> None:
        """Ajoute les colonnes apparues apres coup.

        `CREATE TABLE IF NOT EXISTS` ne touche PAS une table qui existe deja :
        une base creee avant l'ajout d'une colonne resterait sans elle, et
        l'echec arriverait au premier `SELECT` — donc en plein job, sur la
        machine de quelqu'un, pas ici. On rattrape a l'ouverture.
        """
        attendues = {
            "pulls": (("human_asked_sha", "TEXT NOT NULL DEFAULT ''"),
                      ("derived_issue", "INTEGER NOT NULL DEFAULT 0")),
        }
        for table, colonnes in attendues.items():
            with closing(self._db.execute(f"PRAGMA table_info({table})")) as c:
                presentes = {r["name"] for r in c.fetchall()}
            for nom, declaration in colonnes:
                if nom not in presentes:
                    self._db.execute(
                        f"ALTER TABLE {table} ADD COLUMN {nom} {declaration}")

    def close(self) -> None:
        self._db.close()

    def __enter__(self) -> "StateStore":
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    # ── Baux ───────────────────────────────────────────────────────────────

    def _row_to_lease(self, r: sqlite3.Row) -> Lease:
        return Lease(
            profile=r["profile"], repo=r["repo"], pr=r["pr"], job_id=r["job_id"],
            pid=r["pid"],
            acquired_at=_parse(r["acquired_at"]) or _now(),
            expires_at=_parse(r["expires_at"]) or _now(),
        )

    def lease(self, profile: str, repo: str, pr: int) -> Lease | None:
        with closing(self._db.execute(
            "SELECT * FROM leases WHERE profile=? AND repo=? AND pr=?", (profile, repo, pr)
        )) as c:
            row = c.fetchone()
        return self._row_to_lease(row) if row else None

    def reclaimable(self, lease: Lease, now: datetime | None = None) -> bool:
        """Ce bail peut-il etre repris ?

        Deux motifs, et l'ordre compte : l'EXPIRATION fait foi, la mort du
        processus n'est qu'un raccourci. Les PID se reutilisent — un bail dont
        le PID est vivant peut appartenir a un tout autre programme — mais se
        tromper dans ce sens ne fait qu'attendre l'expiration.
        """
        return lease.expired(now) or not pid_alive(lease.pid)

    def acquire(self, profile: str, repo: str, pr: int, job_id: str, *,
                ttl: timedelta = timedelta(minutes=30),
                pid: int | None = None,
                now: datetime | None = None) -> Lease | None:
        """Prend le bail, ou rend `None` s'il est deja tenu.

        C'est LE point d'idempotence du demon : un webhook recu deux fois, ou
        deux passages de reconciliation qui se chevauchent, trouvent le bail
        pris et ne lancent rien. La prise est atomique — un `SELECT` puis un
        `INSERT` laisserait une fenetre ou deux agents partent sur la meme PR.
        """
        now = now or _now()
        pid = os.getpid() if pid is None else pid
        neuf = Lease(profile, repo, pr, job_id, pid, now, now + ttl)

        try:
            self._db.execute("BEGIN IMMEDIATE")
            existant = self.lease(profile, repo, pr)
            if existant is not None and not self.reclaimable(existant, now):
                self._db.execute("ROLLBACK")
                return None
            self._db.execute(
                "INSERT INTO leases (profile, repo, pr, job_id, pid, acquired_at, expires_at) "
                "VALUES (?,?,?,?,?,?,?) "
                "ON CONFLICT(profile, repo, pr) DO UPDATE SET "
                "job_id=excluded.job_id, pid=excluded.pid, "
                "acquired_at=excluded.acquired_at, expires_at=excluded.expires_at",
                (profile, repo, pr, job_id, pid, _iso(neuf.acquired_at), _iso(neuf.expires_at)),
            )
            self._db.execute("COMMIT")
        except sqlite3.OperationalError:
            # Un autre ecrivain tenait deja la base : il a gagne la course.
            try:
                self._db.execute("ROLLBACK")
            except sqlite3.OperationalError:
                pass
            return None
        return neuf

    def renew(self, lease: Lease, *, ttl: timedelta = timedelta(minutes=30),
              now: datetime | None = None) -> Lease | None:
        """Prolonge un bail QU'ON DETIENT ENCORE.

        Le `job_id` est verifie : sans lui, un job qui a perdu son bail
        (expiration, reprise apres crash) le reprendrait par surprise a celui
        qui l'a legitimement acquis entre-temps.
        """
        now = now or _now()
        fin = now + ttl
        cur = self._db.execute(
            "UPDATE leases SET expires_at=? WHERE profile=? AND repo=? AND pr=? AND job_id=?",
            (_iso(fin), lease.profile, lease.repo, lease.pr, lease.job_id),
        )
        if cur.rowcount == 0:
            return None
        return Lease(lease.profile, lease.repo, lease.pr, lease.job_id, lease.pid,
                     lease.acquired_at, fin)

    def release(self, lease: Lease) -> None:
        """Rend le bail. Idempotent : relacher deux fois ne coute rien."""
        self._db.execute(
            "DELETE FROM leases WHERE profile=? AND repo=? AND pr=? AND job_id=?",
            (lease.profile, lease.repo, lease.pr, lease.job_id),
        )

    def sweep_dead(self, now: datetime | None = None) -> list[Lease]:
        """Retire les baux reprenables. Rend ceux qui ont ete retires.

        Appele au DEMARRAGE : apres un reboot, la table decrit un monde qui
        n'existe plus. Les rendre permet de les journaliser — un bail nettoye
        en silence ferait disparaitre la trace d'un job interrompu.
        """
        now = now or _now()
        with closing(self._db.execute("SELECT * FROM leases")) as c:
            tous = [self._row_to_lease(r) for r in c.fetchall()]
        morts = [b for b in tous if self.reclaimable(b, now)]
        for b in morts:
            self._db.execute(
                "DELETE FROM leases WHERE profile=? AND repo=? AND pr=? AND job_id=?",
                (b.profile, b.repo, b.pr, b.job_id),
            )
        return morts

    def active_leases(self, now: datetime | None = None) -> list[Lease]:
        now = now or _now()
        with closing(self._db.execute("SELECT * FROM leases")) as c:
            return [b for b in (self._row_to_lease(r) for r in c.fetchall())
                    if not self.reclaimable(b, now)]

    # ── Suivi par PR ───────────────────────────────────────────────────────

    def pull_state(self, profile: str, repo: str, pr: int) -> PullState:
        """Faits locaux d'une PR. Absents = valeurs neutres.

        Rendre un etat neutre plutot que `None` est deliberé : l'appelant n'a
        pas a distinguer « jamais vue » de « vue, rien a signaler ». Les deux
        se traitent pareil, et un `None` a gerer partout finit par etre oublie
        quelque part.
        """
        with closing(self._db.execute(
            "SELECT * FROM pulls WHERE profile=? AND repo=? AND pr=?", (profile, repo, pr)
        )) as c:
            row = c.fetchone()
        if row is None:
            return PullState(profile, repo, pr)
        return PullState(
            profile=row["profile"], repo=row["repo"], pr=row["pr"],
            claude_session=row["claude_session"], worktree=row["worktree"],
            review_cycle=row["review_cycle"],
            last_handled_comment_id=row["last_handled_comment_id"],
            nudge_sent=bool(row["nudge_sent"]),
            human_asked_sha=row["human_asked_sha"] or "",
            derived_issue=row["derived_issue"] or 0,
        )

    def save_pull_state(self, s: PullState) -> PullState:
        self._db.execute(
            "INSERT INTO pulls (profile, repo, pr, claude_session, worktree, review_cycle, "
            "last_handled_comment_id, nudge_sent, human_asked_sha, derived_issue, "
            "updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(profile, repo, pr) DO UPDATE SET "
            "claude_session=excluded.claude_session, worktree=excluded.worktree, "
            "review_cycle=excluded.review_cycle, "
            "last_handled_comment_id=excluded.last_handled_comment_id, "
            "nudge_sent=excluded.nudge_sent, "
            "human_asked_sha=excluded.human_asked_sha, "
            "derived_issue=excluded.derived_issue, updated_at=excluded.updated_at",
            (s.profile, s.repo, s.pr, s.claude_session, s.worktree, s.review_cycle,
             s.last_handled_comment_id, int(s.nudge_sent), s.human_asked_sha,
             s.derived_issue, _iso(_now())),
        )
        return s

    # ── Budget du jour ─────────────────────────────────────────────────────

    def jobs_today(self, profile: str, now: datetime | None = None) -> int:
        """Nombre de jobs deja LANCES aujourd'hui pour ce profil.

        Le jour est celui d'UTC, pas celui du fuseau local. Un compteur qui
        bascule a minuit local se remettrait a zero a une heure differente selon
        l'endroit d'ou tourne le demon, et le journal — horodate en UTC — ne
        concorderait plus avec lui.
        """
        jour = (now or _now()).astimezone(timezone.utc).strftime("%Y-%m-%d")
        with closing(self._db.execute(
            "SELECT count FROM runs WHERE profile=? AND day=?", (profile, jour)
        )) as c:
            row = c.fetchone()
        return int(row["count"]) if row else 0

    def count_job(self, profile: str, now: datetime | None = None) -> int:
        """Compte un job de plus. Rend le total du jour APRES incrementation.

        Compte les jobs LANCES, pas les jobs reussis. Un job qui echoue a
        consomme du quota et du temps : ne compter que les succes ferait boucler
        un agent en panne jusqu'a epuiser la journee, ce qui est precisement ce
        que le plafond doit empecher.
        """
        jour = (now or _now()).astimezone(timezone.utc).strftime("%Y-%m-%d")
        self._db.execute(
            "INSERT INTO runs (profile, day, count) VALUES (?,?,1) "
            "ON CONFLICT(profile, day) DO UPDATE SET count = count + 1",
            (profile, jour),
        )
        return self.jobs_today(profile, now)

    def bump_cursor(self, profile: str, repo: str, pr: int, comment_id: int) -> PullState:
        """Avance le curseur de remarques traitees.

        Le curseur ne RECULE jamais : `max` et non affectation. Deux jobs qui
        se terminent dans le desordre feraient sinon retraiter des remarques
        deja soldees — et un cycle de correction sur une remarque deja corrigee
        est exactement ce qui epuise les trois cycles pour rien.
        """
        s = self.pull_state(profile, repo, pr)
        from dataclasses import replace
        return self.save_pull_state(
            replace(s, last_handled_comment_id=max(s.last_handled_comment_id, comment_id))
        )
