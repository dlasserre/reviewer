"""Outille la COPIE LOCALE d'un depot : de quoi lancer ses verifications.

── LA PANNE QUE CE MODULE EXISTE POUR SUPPRIMER ────────────────────────────

Le 30/08/2026, `backend#748` s'est arrete sur « verifications rouges :
0/1 verification(s) verte(s) — arret sur : ruff check . — ECHEC (code 127) ».

127 n'est pas un lint rouge : c'est « commande introuvable ». Dans le
conteneur, `/repos/backend` n'avait ni `.venv`, ni `ruff`, ni `pytest`. La
cause est deux crans plus haut : le conteneur tourne en `uid 1000`, et
`site-packages` ne lui est PAS accessible en ecriture — le `pip install -r
requirements.txt` joue a l'installation echouait donc, et son echec ne faisait
qu'une ligne sur une page qui defile.

Le message, lui, accusait le code livre. C'est le vrai cout : l'agent avait
peut-etre bien travaille, personne ne pouvait le savoir.

── LES TROIS DECISIONS ─────────────────────────────────────────────────────

1. UN VENV PAR DEPOT, dans sa copie locale. Pas le Python du systeme : il
   n'est pas ecrivable par l'utilisateur du conteneur, et c'est justement ce
   qui a echoue en silence. Un venv dans `/repos/<depot>` l'est toujours, quel
   que soit l'UID, et il isole les dependances de deux depots qui n'ont aucune
   raison de partager les leurs.

2. DANS LA COPIE LOCALE, JAMAIS DANS LE WORKTREE. `.venv` est monte en
   JONCTION et les `.env*` sont COPIES par `WorktreeManager` (`link_dirs` /
   `copy_files`) : outiller la copie locale outille tous ses worktrees, pour
   une installation au lieu d'une par job.

3. UN ECHEC EST BRUYANT. Il rend un `Provisioning` qui porte ce qui a rate,
   destine au journal. Une capacite manquante qui se tait produit un echec de
   test qui ressemble a un bug du code — c'est la panne d'origine.

Idempotent : une empreinte de ce qui est DECLARE (commandes + fichiers
d'environnement) est deposee dans la copie locale. Tant qu'elle ne bouge pas,
rien ne se rejoue ; qu'on touche au profil, tout se rejoue.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

from reviewer.repo.checks import environnement

__all__ = ["EMPREINTE", "Provisioning", "assurer_outillage", "empreinte_de"]

# Depose a la racine de la copie locale. Non versionne, sans secret : il ne
# porte qu'une empreinte et une date.
EMPREINTE = ".reviewer-outillage.json"

# Une installation de dependances est longue (npm ci, pip). Le plafond est par
# COMMANDE, et large : le couper trop court laisserait une copie a moitie
# outillee, qui est pire qu'une copie nue — elle echoue plus loin.
DELAI_S = 1800.0


@dataclass(frozen=True, slots=True)
class Provisioning:
    """Ce qui a ete fait pour outiller une copie locale, et ce qui a rate."""

    joue: bool = False                       # des commandes ont tourne
    fichiers: tuple[str, ...] = ()           # fichiers d'environnement ecrits
    reussies: tuple[str, ...] = ()
    echouees: tuple[tuple[str, str], ...] = field(default_factory=tuple)

    @property
    def ok(self) -> bool:
        return not self.echouees

    def summary(self) -> str:
        if not self.joue and not self.fichiers:
            return "outillage deja en place"
        morceaux: list[str] = []
        if self.fichiers:
            morceaux.append(f"{len(self.fichiers)} fichier(s) d'environnement")
        if self.reussies:
            morceaux.append(f"{len(self.reussies)} commande(s) vertes")
        if self.echouees:
            noms = ", ".join(c for c, _ in self.echouees)
            morceaux.append(f"ECHEC sur : {noms}")
        return " — ".join(morceaux) or "rien a faire"


def empreinte_de(setup: list[str] | tuple[str, ...],
                 env_files: dict[str, dict[str, str]] | None) -> str:
    """Empreinte de ce qui est DECLARE, pas de ce qui est installe.

    On ne cherche pas a savoir si `ruff` est la — le savoir demanderait de
    connaitre chaque outil de chaque ecosysteme. On sait seulement si la
    DECLARATION a change depuis le dernier passage, ce qui suffit : un profil
    modifie rejoue, un profil stable ne rejoue pas.
    """
    graine = json.dumps(
        {"setup": list(setup), "env_files": env_files or {}},
        sort_keys=True, ensure_ascii=False,
    )
    return hashlib.sha256(graine.encode("utf-8")).hexdigest()[:16]


def _ecrire_env(depot: Path, env_files: dict[str, dict[str, str]]) -> list[str]:
    """Ecrit les fichiers d'environnement MANQUANTS. N'en ecrase jamais un.

    Le backend refuse de demarrer sans `MONGO_URL` (`app/core/config.py` lit
    `os.environ[...]` a l'import) : sans fichier, pytest s'arrete a la COLLECTE,
    code 2, avant le moindre test — et le message parle de « verifications
    rouges », donc du code livre.

    JAMAIS d'ecrasement : sur un poste de developpement, la copie locale porte
    le vrai `.env`. Le remplacer par une recette de test couperait la machine de
    son environnement — et ce module n'a aucune raison de savoir lequel des deux
    il regarde.
    """
    ecrits: list[str] = []
    for nom, variables in (env_files or {}).items():
        cible = depot / nom
        if cible.exists():
            continue
        corps = "".join(f"{cle}={valeur}\n" for cle, valeur in variables.items())
        try:
            cible.write_text(corps, encoding="utf-8")
        except OSError:
            continue      # l'echec se verra sur les commandes, pas ici
        ecrits.append(nom)
    return ecrits


def assurer_outillage(
    depot: Path,
    setup: list[str] | tuple[str, ...],
    *,
    env_files: dict[str, dict[str, str]] | None = None,
    scrub_env: tuple[str, ...] | list[str] = (),
    force: bool = False,
    on_result=None,
) -> Provisioning:
    """Outille `depot` si sa declaration a change depuis le dernier passage.

    L'environnement est RECALCULE avant chaque commande : la premiere cree le
    venv, la suivante doit le voir sur son PATH. Le calculer une fois pour
    toutes ferait installer les dependances a cote — dans le Python du systeme,
    c'est-a-dire nulle part quand il n'est pas ecrivable.
    """
    depot = Path(depot)
    if not depot.is_dir():
        return Provisioning()

    attendue = empreinte_de(setup, env_files)
    marque = depot / EMPREINTE
    if not force and marque.is_file():
        try:
            if json.loads(marque.read_text(encoding="utf-8")).get("empreinte") == attendue:
                return Provisioning()
        except (OSError, json.JSONDecodeError):
            pass          # marque illisible : on rejoue, c'est sans risque

    fichiers = _ecrire_env(depot, env_files or {})

    reussies: list[str] = []
    echouees: list[tuple[str, str]] = []
    for commande in setup:
        debut = time.monotonic()
        try:
            r = subprocess.run(
                commande, cwd=str(depot), shell=True, capture_output=True,
                timeout=DELAI_S, env=environnement(scrub_env, depot),
            )
        except (OSError, subprocess.TimeoutExpired) as e:
            echouees.append((commande, f"{type(e).__name__} : {e}"))
            break
        duree = time.monotonic() - debut
        if r.returncode == 0:
            reussies.append(commande)
            if on_result is not None:
                on_result(f"{commande} — vert en {duree:.1f} s")
            continue
        # La QUEUE de la sortie : pip et npm y mettent la cause.
        sortie = (r.stdout + r.stderr).decode("utf-8", "replace").strip()
        echouees.append((commande, sortie[-600:] or f"code {r.returncode}"))
        if on_result is not None:
            on_result(f"{commande} — ECHEC (code {r.returncode})")
        break             # meme regle que les verifications : on s'arrete

    resultat = Provisioning(joue=bool(setup), fichiers=tuple(fichiers),
                            reussies=tuple(reussies), echouees=tuple(echouees))
    if resultat.ok:
        # On ne marque QUE le succes complet. Une copie a moitie outillee doit
        # rejouer au passage suivant, pas se croire prete.
        try:
            marque.write_text(
                json.dumps({"empreinte": attendue,
                            "commandes": list(setup)}, ensure_ascii=False, indent=2),
                encoding="utf-8")
        except OSError:
            pass
    return resultat
