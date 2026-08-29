"""Les OUTILS que les noeuds emploient. Ils ne traversent jamais le checkpoint.

── LA FRONTIERE QUE CE FICHIER TRACE ───────────────────────────────────────

`state.py` porte des DONNEES : ce qui a ete lu, decide, produit. Serialise apres
chaque noeud, relu apres un redemarrage.

`deps.py` porte des OUTILS : une connexion sqlite, un client HTTP, un
gestionnaire de worktrees, un journal ouvert en ecriture. Rien de tout cela ne
se serialise, et rien de tout cela ne devrait : ce sont des ressources de la
machine qui execute, pas des faits sur le travail.

Melanger les deux est l'erreur classique du portage vers un graphe. Elle ne se
voit pas tout de suite : tant qu'aucun checkpoint n'est ecrit, un `StateStore`
range dans l'etat fonctionne parfaitement. La panne arrive le jour ou la reprise
compte — donc le jour ou on en a besoin.

── COMMENT LES NOEUDS Y ACCEDENT ───────────────────────────────────────────

Par FERMETURE, au montage du graphe (`build.py`). Pas par `config["configurable"]`,
qui rendrait le type opaque et deplacerait les erreurs a l'execution.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from reviewer.agent.run import run_agent
from reviewer.config import ProfileConfig, RunnerConfig
from reviewer.output.events import Journal
from reviewer.repo.worktree import WorktreeManager
from reviewer.store.leases import StateStore

__all__ = ["Deps"]


@dataclass(frozen=True, slots=True)
class Deps:
    """Tout ce dont un noeud a besoin et qui n'est pas une donnee du job."""

    runner: RunnerConfig
    profile: ProfileConfig
    store: StateStore
    journal: Journal
    worktrees: WorktreeManager

    # Lecture de la forge. Toujours present : sans lui, il n'y a rien a decider.
    reader: object

    # Ecriture sur la forge. `None` = le demon ne publie RIEN. Ce n'est pas une
    # panne, c'est le cran ou l'on relit le travail avant qu'il devienne
    # visible. Mais il est DIT partout, sinon « rien n'a ete publie » se lit
    # comme « rien n'a ete fait ».
    writer: object | None = None

    # Injectable pour les tests : un faux agent rend un `AgentOutcome` sans
    # lancer le SDK. C'est ce qui permet de tester la sequence complete —
    # verdict, verifications, commit, reponses — sans quota ni reseau.
    agent: Callable = run_agent

    def depot(self, repo: str) -> Path:
        """La copie de travail locale d'un depot du profil."""
        return self.profile.repos[repo].path
