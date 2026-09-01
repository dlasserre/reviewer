"""Worktrees de l'agent : un arbre isole par PR, hors du workspace humain.

Trois choses que ce module refuse, et chacune a une raison mesuree :

1. TOUCHER AUX COPIES DE TRAVAIL HUMAINES. L'agent ne travaille jamais dans
   `backend/`, `frontend/`, ni dans les worktrees que l'humain s'est crees. Le
   bail protege contre deux JOBS concurrents ; il ne protege pas contre un job
   et un humain.

2. SORTIR DE SA RACINE. Un worktree ailleurs que sous `worktrees_root` est une
   erreur fatale, pas un avertissement — c'est la seule invariante qui rende le
   perimetre du garde-fou verifiable.

3. LES BRANCHES PARTAGEES. `dev` et `main` ne sont pas des branches de travail.
   Le worktree isole le repertoire, pas la reference : sans ce controle, un
   commit atterrirait sur l'integration.

── LE POINT D'ECHEC SILENCIEUX ─────────────────────────────────────────────
Un worktree neuf n'a ni `node_modules`, ni `artifacts`, ni `corpus` : ils sont
ignores par git. Les tests y echouent alors pour une raison qui n'a rien a voir
avec le code — ou pire, un `npm install` complet repart a chaque job.

On monte donc des JONCTIONS vers le depot principal. Une jonction Windows ne
demande aucun privilege, contrairement a un lien symbolique, et git la voit
comme un repertoire ordinaire — donc ignore, puisque le chemin l'etait deja.

Ce montage est la premiere chose a verifier quand un job echoue sans raison
apparente : `prepared` le dit explicitement, et un montage manque est
JOURNALISE, jamais avale.
"""

from __future__ import annotations

import stat
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from reviewer.rules.machine import BRANCHES_PARTAGEES

__all__ = ["Worktree", "WorktreeError", "WorktreeManager", "segments_caches"]


def segments_caches(racine: Path) -> tuple[str, ...]:
    """Repertoires CACHES (prefixes d'un point) sur le chemin de cette racine.

    ── POURQUOI CA COMPTE, MESURE LE 27/08/2026 ────────────────────────────

    `npm run test:ci` echouait dans un worktree monte sous `.agent-runner/`,
    avec « No tests found », pendant que les MEMES tests passaient dans le depot
    principal — 131 verts. Le runner a donc refuse de commiter un correctif
    parfaitement bon, et l'aurait refait a chaque cycle jusqu'a epuisement.

    Le mecanisme, isole a la main :

        jest expand `<rootDir>` dans `testMatch` en ECHAPPANT le point du
        repertoire cache — `WebstormProjects\\.agent-runner/...` — alors que son
        explorateur de fichiers rend `WebstormProjects/.agent-runner/...`.
        micromatch lit `\\.` comme un point litteral SANS separateur, et le
        motif ne peut plus jamais correspondre. Zero fichier de test trouve,
        code de sortie 1.

    Le meme worktree place hors d'un repertoire cache correspond sans rien
    changer d'autre. Ce n'est donc pas un reglage de jest a corriger, c'est un
    emplacement a eviter — et il touche tout outil qui construit un glob a
    partir de sa racine.

    On ne REFUSE pas : un depot Python n'en souffre pas, et un garde-fou qui
    bloque le travail courant finit par etre desactive. On le DIT, la ou
    quelqu'un le lira avant d'accuser le code.
    """
    racine = Path(racine)
    return tuple(p.name for p in (racine, *racine.parents)
                 if p.name.startswith(".") and p.name not in (".", ".."))


class WorktreeError(Exception):
    """Le worktree ne peut pas etre prepare — et on ne devine pas a la place."""


def _est_lien(chemin: Path) -> bool:
    """Ce chemin est-il une jonction ou un lien symbolique ?

    `os.path.islink` ne suffit PAS sous Windows : il ne reconnait que les liens
    symboliques (`IO_REPARSE_TAG_SYMLINK`), pas les jonctions
    (`IO_REPARSE_TAG_MOUNT_POINT`) — or ce sont des jonctions qu'on monte, parce
    qu'elles ne demandent aucun privilege.

    Mesure du 25/08/2026 sur une jonction reelle :

        os.path.islink(jonction)  ->  False        <-- le piege
        lstat().st_reparse_tag    ->  0xA0000003   <-- IO_REPARSE_TAG_MOUNT_POINT

    Sans ce test, une jonction passe pour un dossier ordinaire. `os.rmdir` la
    retire proprement en laissant la cible intacte (mesure faite) ; c'est ce
    qu'on veut, et c'est ce qu'on appelle.
    """
    try:
        st = chemin.lstat()
    except OSError:
        return False
    tag = getattr(st, "st_reparse_tag", 0)
    if tag:
        return tag in (getattr(stat, "IO_REPARSE_TAG_MOUNT_POINT", 0xA0000003),
                       getattr(stat, "IO_REPARSE_TAG_SYMLINK", 0xA000000C))
    return chemin.is_symlink()


def _git(cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    r = subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=180,
    )
    if check and r.returncode != 0:
        raise WorktreeError(
            f"git {' '.join(args)} (dans {cwd}) a echoue :\n{(r.stderr or r.stdout).strip()}"
        )
    return r


@dataclass(frozen=True, slots=True)
class Worktree:
    path: Path
    branch: Path | str
    repo: str
    base: str
    linked: tuple[str, ...] = ()
    missing_links: tuple[str, ...] = field(default_factory=tuple)

    @property
    def prepared(self) -> bool:
        """Le montage est-il complet ?

        Faux ne signifie pas « inutilisable » : cela signifie « une verification
        va peut-etre echouer pour une raison qui n'est pas le code ». C'est
        exactement ce qu'on veut savoir AVANT de conclure que l'agent a mal
        travaille.
        """
        return not self.missing_links


class WorktreeManager:
    """Cree, retrouve et retire les worktrees d'un profil.

    `root` est la racine des worktrees de l'agent — hors du workspace humain,
    pour que le `CLAUDE.md` de ce dernier ne soit pas herite par accident (les
    fichiers de memoire remontent TOUS les repertoires parents).
    """

    def __init__(
        self,
        *,
        root: Path,
        profile: str,
        protected_refs: frozenset[str] | set[str] = BRANCHES_PARTAGEES,
        link_dirs: tuple[str, ...] = ("node_modules", "artifacts", "corpus",
                                      "golden_dataset_v1", ".venv"),
        copy_files: tuple[str, ...] = (".env", ".env.dev", ".env.test",
                                       ".env.local"),
    ) -> None:
        self.root = Path(root).resolve()
        self.profile = profile
        self.protected_refs = frozenset(protected_refs)
        self.link_dirs = tuple(link_dirs)
        self.copy_files = tuple(copy_files)

    # ── Emplacement ────────────────────────────────────────────────────────

    def path_for(self, repo: str, pr: int) -> Path:
        """Chemin DETERMINISTE d'un worktree.

        Deterministe et stable : la session Claude est liee au `cwd`, donc
        deplacer le worktree d'une PR entre deux cycles perdrait le contexte
        d'implementation qu'on cherche justement a reprendre.
        """
        return self.root / f"{self.profile}-{repo}-pr-{pr}"

    # ── Garde-fous ─────────────────────────────────────────────────────────

    def _verifier_perimetre(self, cible: Path, depot: Path) -> None:
        cible, depot = cible.resolve(), depot.resolve()

        try:
            cible.relative_to(self.root)
        except ValueError:
            raise WorktreeError(
                f"{cible} est hors de la racine des worktrees de l'agent "
                f"({self.root}). Refus : c'est l'invariante qui rend le "
                "perimetre du garde-fou verifiable."
            ) from None

        try:
            depot.relative_to(self.root)
        except ValueError:
            pass
        else:
            raise WorktreeError(
                f"{depot} est SOUS la racine de l'agent : ce n'est pas un depot "
                "principal mais un worktree. Un worktree ne se derive pas."
            )

    def _verifier_branche(self, branche: str) -> None:
        if branche in self.protected_refs:
            raise WorktreeError(
                f"« {branche} » est une branche partagee. Le worktree isole le "
                "repertoire, pas la reference : un commit y atterrirait sur "
                "l'integration."
            )

    def existing_worktrees(self, depot: Path) -> dict[Path, str]:
        """Worktrees deja enregistres : chemin -> branche.

        Sert a REFUSER un chemin deja monte ailleurs. Le bail protege de deux
        jobs concurrents ; il ne dit rien d'un worktree que l'humain a cree.
        """
        out: dict[Path, str] = {}
        courant: Path | None = None
        for ligne in _git(depot, "worktree", "list", "--porcelain").stdout.splitlines():
            if ligne.startswith("worktree "):
                courant = Path(ligne[9:]).resolve()
                out[courant] = "(detache)"
            elif ligne.startswith("branch ") and courant is not None:
                out[courant] = ligne[7:].removeprefix("refs/heads/")
        return out

    # ── Cycle de vie ───────────────────────────────────────────────────────

    def create(self, *, depot: Path, repo: str, pr: int, branch: str,
               base: str = "dev") -> Worktree:
        """Prepare le worktree d'une PR. IDEMPOTENT.

        Rappele sur un worktree deja monte sur la bonne branche, il le rend tel
        quel — c'est ce qui permet a un cycle de correction de reprendre sans
        rien recreer, et donc de conserver la session Claude attachee au `cwd`.
        """
        depot = Path(depot).resolve()
        cible = self.path_for(repo, pr)
        self._verifier_perimetre(cible, depot)
        self._verifier_branche(branch)

        montes = self.existing_worktrees(depot)

        if cible in montes:
            actuelle = montes[cible]
            if actuelle != branch:
                raise WorktreeError(
                    f"{cible} est deja monte sur « {actuelle} », pas sur "
                    f"« {branch} ». Deux travaux se disputent le meme arbre : "
                    "on s'arrete plutot que de basculer sous les pieds de l'autre."
                )
            return self._monter_liens(cible, depot, repo, branch, base)

        # Le meme chemin monte ailleurs, ou la meme branche montee ailleurs.
        for chemin, br in montes.items():
            if br == branch and chemin != cible:
                raise WorktreeError(
                    f"la branche « {branch} » est deja montee dans {chemin}. "
                    "Git refuse deux worktrees sur la meme branche, et forcer "
                    "reviendrait a travailler par-dessus quelqu'un."
                )

        if cible.exists():
            raise WorktreeError(
                f"{cible} existe mais n'est pas un worktree enregistre. "
                "Reste d'un job interrompu : le retirer a la main apres avoir "
                "verifie qu'il ne contient rien d'utile."
            )

        cible.parent.mkdir(parents=True, exist_ok=True)
        _git(depot, "fetch", "origin", "--quiet")

        # TROIS cas, et en confondre deux fait travailler sur le mauvais arbre.
        #
        #   locale   — cycle de correction : la branche est deja ici, avec le
        #              travail du passage precedent. On la reprend telle quelle.
        #   distante — tete d'une PR ouverte depuis une AUTRE machine : elle
        #              n'existe que sur origin. C'est le cas ORDINAIRE.
        #   nulle    — derivee a faire naitre (PR de release) : elle n'existe
        #              nulle part, et seulement la le socle a un sens.
        #
        # Ne tester que `refs/heads/` faisait tomber le cas distant dans le
        # troisieme : la branche etait RECREEE depuis le socle. Le worktree
        # portait alors le nom de la PR et le code de la branche d'integration,
        # l'agent relisait des fils de revue contre un arbre sans rapport, et
        # l'echec n'arrivait qu'au push, en non-fast-forward, loin de la cause.
        #
        # Mesure du 30/08/2026 sur `frontend#406` (hotfix vers `main`) : dans le
        # conteneur, `origin/dev` n'existait pas et git a refuse tout net. La ou
        # il existe, la commande REUSSIT — c'est la forme muette du meme defaut.
        # Aucun test ne l'attrapait : ils creent tous la branche en local avant
        # d'appeler `create`, donc tous prenaient le premier chemin.
        def _connue(ref: str) -> bool:
            return _git(depot, "rev-parse", "--verify", "--quiet", ref,
                        check=False).returncode == 0

        def _rapatrier(nom: str) -> bool:
            """La branche distante existe-t-elle ici — et sinon, va la chercher.

            Le `fetch origin` fait plus haut ne suffit PAS : sur un clone
            restreint a une branche (`--depth` implique `--single-branch`, et
            les volumes clones avant le correctif le sont restes), le refspec du
            remote ne couvre que le tronc. La commande reussit sans rien
            ramener, et son succes cache l'echec qui suit.

            Le refspec EXPLICITE cree la reference quel que soit l'etat du
            clone. Mesure du 30/08/2026 sur `frontend#407` : « fatal: invalid
            reference: origin/dev » sur un clone qui pouvait parfaitement
            joindre origin — il ne demandait juste jamais `dev`.
            """
            if _connue(f"refs/remotes/origin/{nom}"):
                return True
            _git(depot, "fetch", "--quiet", "origin",
                 f"+refs/heads/{nom}:refs/remotes/origin/{nom}", check=False)
            return _connue(f"refs/remotes/origin/{nom}")

        if _connue(f"refs/heads/{branch}"):
            _git(depot, "worktree", "add", str(cible), branch)
        elif _rapatrier(branch):
            # Le rapatriement participe au TRI, pas seulement a la reparation :
            # une branche distante invisible sur un clone restreint tombait
            # sinon dans le cas « nulle » et etait RECREEE depuis le socle — le
            # piege que le commentaire ci-dessus decrit, rouvert par en dessous.
            _git(depot, "worktree", "add", "-b", branch, str(cible),
                 f"origin/{branch}")
        else:
            if not _rapatrier(base):
                raise WorktreeError(
                    f"« origin/{base} » est introuvable, meme apres un "
                    f"rapatriement cible. Soit la branche n'existe pas sur "
                    f"origin, soit le jeton ne peut pas la lire — dans les deux "
                    f"cas, deriver depuis autre chose serait travailler sur le "
                    f"mauvais arbre."
                )
            _git(depot, "worktree", "add", "-b", branch, str(cible), f"origin/{base}")

        # Verifier que git a VRAIMENT enregistre le worktree. Le renommage du
        # 25/08/2026 a laisse quatre worktrees `prunable` que `git worktree
        # repair` n'avait pas repares — sans erreur, et sans que rien ne le dise.
        if cible not in self.existing_worktrees(depot):
            raise WorktreeError(
                f"git n'a pas enregistre {cible} comme worktree apres l'avoir "
                "cree. Etat incoherent : ne pas continuer."
            )
        return self._monter_liens(cible, depot, repo, branch, base)

    @staticmethod
    def _rendre_invisibles(cible: Path, noms) -> None:
        """Ajoute les montages du demon a `info/exclude` du worktree.

        Silencieux en cas d'echec : ne pas pouvoir ecrire cette exclusion ne
        justifie pas d'arreter un cycle. Le garde-fou de `commit_all` reste, et
        lui refuse pour de bon.
        """
        try:
            commun = (cible / ".git")
            if commun.is_file():
                # Worktree lie : `.git` est un fichier qui pointe vers le
                # repertoire d'administration.
                cible_gitdir = commun.read_text(encoding="utf-8").split(":", 1)[1].strip()
                admin = Path(cible_gitdir)
            else:
                admin = commun
            # `info/exclude` d'un worktree lie vit dans le depot PRINCIPAL, ce
            # qui convient : les montages y sont les memes.
            info = admin / "info"
            if not info.is_dir():
                # `commondir` designe le `.git` du depot principal.
                cd = (admin / "commondir")
                if cd.is_file():
                    info = (admin / cd.read_text(encoding="utf-8").strip() / "info").resolve()
            info.mkdir(parents=True, exist_ok=True)
            fichier = info / "exclude"
            deja = fichier.read_text(encoding="utf-8") if fichier.is_file() else ""
            manquants = [n for n in noms if f"\n/{n}\n" not in f"\n{deja}\n"]
            if manquants:
                entete = "" if deja.endswith("\n") or not deja else "\n"
                fichier.write_text(
                    deja + entete
                    + "# Montages du demon de revue — jamais commites.\n"
                    + "".join(f"/{n}\n" for n in manquants),
                    encoding="utf-8")
        except OSError:
            pass

    def _monter_liens(self, cible: Path, depot: Path, repo: str,
                      branch: str, base: str) -> Worktree:
        """Monte les jonctions vers les dossiers lourds ignores par git.

        ── ET LES FICHIERS D'ENVIRONNEMENT, MESURE LE 29/08/2026 ───────────

        Un worktree ne contient que ce que git SUIT. Les `.env*` sont ignores,
        donc absents — et sur `backend`, `conftest.py` les exige : pytest s'est
        arrete sur 336 erreurs DE COLLECTION, code de sortie 2, avant d'avoir
        lance un seul test.

        L'agent avait pourtant fait son travail : le correctif etait ecrit et
        pousse sur `fix/728-revue-pr727`. Il s'est arrete sur une CI qui ne
        pouvait pas demarrer, et le message parlait de « verifications rouges »
        — ce qui envoie chercher un bug dans le code livre.

        Ils sont COPIES, pas montes en jonction. Une jonction rendrait le vrai
        `.env` du depot accessible en ecriture depuis le worktree ; le garde-fou
        `PreToolUse` l'interdit deja, mais une capacite absente vaut mieux
        qu'une capacite interdite.
        """
        # Les montages du demon ne lui appartiennent pas moins parce qu'ils
        # vivent dans le depot de quelqu'un : ils ne doivent JAMAIS entrer dans
        # un commit. `.gitignore` ne suffit pas — celui de `backend` dit
        # `.venv/`, avec le slash, donc « le repertoire ». Un LIEN nomme `.venv`
        # passe a cote, et `git add -A` le stage. C'est arrive : le lien est
        # parti dans `dev` le 31/08, pointant vers `/repos/backend/.venv`.
        #
        # `info/exclude` est local au depot, jamais commite, et vaut pour toutes
        # les commandes git. L'exclure au moment du `add` n'aurait couvert qu'un
        # appel.
        self._rendre_invisibles(cible, [*self.copy_files, *self.link_dirs])

        montes: list[str] = []
        manquants: list[str] = []
        for nom in self.copy_files:
            source = depot / nom
            copie = cible / nom
            if not source.is_file() or copie.exists():
                continue
            try:
                shutil.copy2(source, copie)
                montes.append(nom)
            except OSError:
                # Meme regle que pour les jonctions : un fichier manquant
                # produit un echec qui ressemble a un bug du code livre.
                manquants.append(nom)
        for nom in self.link_dirs:
            source = depot / nom
            lien = cible / nom
            if not source.is_dir():
                continue          # ce depot n'a pas ce dossier : rien a monter
            if lien.exists():
                montes.append(nom)
                continue
            if not self._jonction(lien, source):
                # JOURNALISE, jamais avale : un montage manque produit un echec
                # de test qui ressemble a un bug du code.
                manquants.append(nom)
            else:
                montes.append(nom)

        return Worktree(path=cible, branch=branch, repo=repo, base=base,
                        linked=tuple(montes), missing_links=tuple(manquants))

    @staticmethod
    def _jonction(lien: Path, source: Path) -> bool:
        """Cree une jonction (Windows) ou un lien symbolique (ailleurs).

        Une jonction ne demande AUCUN privilege, contrairement a un lien
        symbolique de repertoire sous Windows — ou creer un symlink exige soit
        les droits d'administrateur, soit le mode developpeur.
        """
        try:
            if hasattr(subprocess, "STARTUPINFO"):   # Windows
                r = subprocess.run(
                    ["cmd", "/c", "mklink", "/J", str(lien), str(source)],
                    capture_output=True, text=True, timeout=60,
                )
                return r.returncode == 0
            lien.symlink_to(source, target_is_directory=True)
            return True
        except (OSError, subprocess.SubprocessError):
            return False

    def remove(self, wt: Worktree, *, depot: Path, force: bool = False) -> None:
        """Retire un worktree. REFUSE si du travail y dort, sauf `force`.

        Le refus est le comportement voulu : un worktree sale contient soit un
        correctif en cours, soit la trace de ce qui a echoue. Les deux valent
        mieux qu'un nettoyage silencieux.
        """
        if not force:
            sale = _git(wt.path, "status", "--porcelain", check=False)
            if sale.returncode == 0 and sale.stdout.strip():
                raise WorktreeError(
                    f"{wt.path} contient du travail non commite. Refus de "
                    "retirer : soit c'est un correctif en cours, soit c'est la "
                    "trace de ce qui a echoue."
                )

        # Les jonctions d'abord : elles doivent partir AVANT que git ne parcoure
        # l'arbre, sinon `git worktree remove` descend dans le contenu du depot
        # principal a travers le lien.
        #
        # Uniquement `os.rmdir` : il retire la jonction et laisse la cible
        # intacte (mesure du 25/08/2026). `shutil.rmtree` n'est pas un repli —
        # il leve « Cannot call rmtree on a symbolic link » sur un point de
        # reparse, ce qui protege de la destruction mais ne demonte rien. Un
        # echec de `rmdir` est donc une vraie anomalie, et on la remonte au lieu
        # de tenter autre chose au jugé.
        for nom in wt.linked:
            lien = wt.path / nom
            if not _est_lien(lien):
                continue
            try:
                lien.rmdir()          # retire la jonction, pas sa cible
            except OSError as e:
                raise WorktreeError(
                    f"impossible de demonter la jonction {lien} : {e}. "
                    "Ne PAS supprimer ce dossier a la main sans verifier que "
                    "c'en est bien une : l'effacer suivrait le lien."
                ) from e

        _git(depot, "worktree", "remove", str(wt.path), *(["--force"] if force else []))
        _git(depot, "worktree", "prune", check=False)
