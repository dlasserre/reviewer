"""Commit et push — faits par le RUNNER, jamais par l'agent.

Pourquoi les sortir du SDK : ce sont les deux gestes dont on ne revient pas
facilement, et les seuls qui rendent le travail visible aux autres. Les laisser
a l'agent obligerait a lui donner `git push`, donc a compter sur une regle de
permission pour qu'il ne l'utilise pas ailleurs. Ici, il ne l'a simplement pas.

Le runner, lui, verifie AVANT de pousser :

  - la branche est bien celle du job, et pas une branche partagee ;
  - le diff ne touche aucun chemin protege ;
  - il y a quelque chose a commiter.

Chacune de ces trois verifications a deja son equivalent ailleurs — dans le
garde-fou `PreToolUse`, dans la portee du jeton. C'est voulu : la barriere qui
compte n'est jamais celle qu'on a mise en dernier.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from reviewer.agent.guard import PROTECTED_GLOBS
from fnmatch import fnmatch
from reviewer.rules.machine import BRANCHES_PARTAGEES

__all__ = ["GitError", "Diff", "commit_all", "current_branch", "diff_stat", "push"]


class GitError(Exception):
    """Une operation git a echoue, ou a ete refusee avant d'etre tentee."""


def _git(cwd: Path, *args: str, check: bool = True,
         env: dict[str, str] | None = None) -> str:
    r = subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=300, env=env,
    )
    if check and r.returncode != 0:
        raise GitError(f"git {' '.join(args)} : {(r.stderr or r.stdout).strip()}")
    return r.stdout


@dataclass(frozen=True, slots=True)
class Diff:
    files: tuple[str, ...] = ()
    insertions: int = 0
    deletions: int = 0

    @property
    def empty(self) -> bool:
        return not self.files

    def protected(self, globs: tuple[str, ...] = PROTECTED_GLOBS) -> tuple[str, ...]:
        """Fichiers du diff qui tombent sous un motif protege.

        Le garde-fou `PreToolUse` refuse deja de les ecrire. On revérifie ici
        parce qu'un fichier peut arriver dans l'arbre autrement qu'en passant
        par un outil — un script, une commande qu'on n'a pas su lire. La
        derniere verification avant de rendre le travail visible ne coute rien.
        """
        touches = []
        for f in self.files:
            chemin = f.replace("\\", "/")
            if any(fnmatch(chemin, g) or fnmatch(chemin, g.lstrip("*/")) for g in globs):
                touches.append(f)
        return tuple(touches)

    def summary(self) -> str:
        if self.empty:
            return "aucune modification"
        return (f"{len(self.files)} fichier(s), "
                f"+{self.insertions} / -{self.deletions}")


def current_branch(worktree: Path) -> str:
    return _git(worktree, "branch", "--show-current").strip()


def diff_stat(worktree: Path) -> Diff:
    """Ce qui a change dans l'arbre — suivi ET non suivi.

    `git diff --stat` seul ignore les fichiers NOUVEAUX : un agent qui cree un
    fichier de test verrait son travail declare vide. On ajoute donc tout a
    l'index d'abord (`add -A -N`), qui enregistre l'intention sans le contenu.
    """
    _git(worktree, "add", "-A", "-N")
    sortie = _git(worktree, "diff", "--numstat", "HEAD")

    fichiers: list[str] = []
    plus = moins = 0
    for ligne in sortie.splitlines():
        parties = ligne.split("\t")
        if len(parties) != 3:
            continue
        a, s, nom = parties
        fichiers.append(nom.strip())
        # `-` sur un binaire : on compte le fichier, pas ses lignes.
        plus += int(a) if a.isdigit() else 0
        moins += int(s) if s.isdigit() else 0
    return Diff(tuple(fichiers), plus, moins)


_SUJET_CONVENTIONNEL = re.compile(
    r"^(build|chore|ci|docs|feat|fix|perf|refactor|revert|style|test)"
    r"(\([\w./-]+\))?!?: .{3,}", re.IGNORECASE)


def commit_all(worktree: Path, message: str, *,
               protected_refs: frozenset[str] | set[str] = BRANCHES_PARTAGEES,
               protected_globs: tuple[str, ...] = PROTECTED_GLOBS,
               auteur: tuple[str, str] | None = None) -> Diff:
    """Commite tout l'arbre, apres verification. Rend le diff commite.

    Leve plutot que de commiter a moitie : un commit partiel produit une PR dont
    les tests passent sur un etat qui n'existe nulle part.
    """
    branche = current_branch(worktree)
    if not branche:
        raise GitError("HEAD detachee : aucune branche a commiter")
    if branche in protected_refs:
        raise GitError(
            f"« {branche} » est une branche partagee. Le worktree isole le "
            "repertoire, pas la reference."
        )

    diff = diff_stat(worktree)
    if diff.empty:
        raise GitError("rien a commiter : l'arbre est propre")

    if proteges := diff.protected(protected_globs):
        raise GitError(
            f"le diff touche {len(proteges)} chemin(s) protege(s) : "
            f"{', '.join(proteges)}. Le rayon d'action de ces fichiers depasse "
            "la PR — on s'arrete plutot que de les livrer."
        )

    if not _SUJET_CONVENTIONNEL.match(message.splitlines()[0]):
        raise GitError(
            f"sujet non conventionnel : {message.splitlines()[0]!r}. "
            "Forme attendue : « fix(zone): ce que ca change »."
        )

    _git(worktree, "add", "-A")

    # L'identite est passee A LA COMMANDE (`-c`), pas ecrite dans la
    # configuration du depot. Deux raisons :
    #
    #   - le worktree est derive d'un depot qui appartient a quelqu'un ; y
    #     ecrire une identite modifierait sa configuration ;
    #   - un `-c` ne vaut que pour cette commande, donc il ne peut pas fuir sur
    #     un commit que l'humain ferait ensuite dans le meme depot.
    #
    # Sans identite fournie, on laisse git decider : sur un poste, la
    # configuration globale existe et c'est la bonne. En conteneur elle
    # n'existe pas, et le message de git est explicite — `check` le signale
    # d'ailleurs avant que ca ne coute un cycle.
    prefixe: list[str] = []
    if auteur:
        nom, courriel = auteur
        prefixe = ["-c", f"user.name={nom}", "-c", f"user.email={courriel}"]

    # `--no-verify` n'est PAS passe : si le depot a des hooks de pre-commit, ils
    # doivent tourner. Les contourner ferait passer en CI ce qui aurait ete
    # rattrape en local.
    _git(worktree, *prefixe, "commit", "-m", message)
    return diff


def push(worktree: Path, *, token: str, remote: str = "origin",
         protected_refs: frozenset[str] | set[str] = BRANCHES_PARTAGEES,
         force: bool = False) -> str:
    """Pousse la branche du worktree. REFUSE une branche partagee et le force.

    Le jeton est passe par un en-tete d'authentification EPHEMERE
    (`-c http.extraheader=...`) plutot qu'ecrit dans l'URL du remote ou dans la
    configuration : il ne survit donc pas a la commande, et n'apparait ni dans
    `.git/config`, ni dans `git remote -v`, ni dans un journal de shell.
    """
    if force:
        raise GitError(
            "le push force est refuse : reecrire une reference publiee ne se "
            "rattrape pas, et aucun correctif de revue ne l'exige."
        )
    branche = current_branch(worktree)
    if branche in protected_refs:
        raise GitError(f"refus de pousser sur « {branche} », branche partagee")

    # `AUTHORIZATION: basic <base64(x-access-token:JETON)>` est la forme que
    # GitHub attend pour un jeton en en-tete.
    import base64
    entete = base64.b64encode(f"x-access-token:{token}".encode()).decode()

    sortie = _git(
        worktree,
        "-c", f"http.extraheader=AUTHORIZATION: basic {entete}",
        "push", "--set-upstream", remote, branche,
    )
    return sortie.strip() or f"{branche} poussee sur {remote}"
