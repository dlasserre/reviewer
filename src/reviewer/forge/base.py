"""La frontiere avec la forge.

Le moteur ne connait que ce protocole. GitHub aujourd'hui, autre chose demain :
c'est la seule couture a reprendre, et elle est etroite par construction — on ne
demande a la forge que ce qu'il faut pour remplir un `PullSnapshot`.

`open_pulls` est en LECTURE PURE. Les ecritures vivent dans un protocole separe
(`ForgeWriter`), volontairement : en lot 1 le demon n'implemente que celui-ci,
et l'absence de l'autre est une garantie structurelle, pas une promesse.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from reviewer.rules.machine import PullSnapshot

__all__ = ["ForgeReader", "ForgeWriter", "ForgeError"]


class ForgeError(Exception):
    """La forge n'a pas repondu, ou a repondu ce qu'on ne sait pas lire."""


@runtime_checkable
class ForgeReader(Protocol):
    """Tout ce dont la reconciliation a besoin. Rien de plus."""

    async def open_pulls(self, repo: str) -> list[PullSnapshot]:
        """Les PR ouvertes d'un depot, prêtes a etre passees a `decide`."""
        ...


@runtime_checkable
class ForgeWriter(Protocol):
    """Les NEUF ecritures que le moteur emet. Ni plus, ni moins.

    Cette liste EST le contrat d'un adaptateur : ce sont exactement les appels
    que `forge/actions.py` et les noeuds du graphe font. Elle a divergé une fois
    — le protocole datait du lot 1, quand aucune ecriture n'existait, et
    declarait quatre methodes dont une seule existait. Un protocole faux est
    pire qu'absent : on le suit, et l'erreur arrive a l'execution, au milieu
    d'un job, sur une methode introuvable.

    CE QUI N'EST PAS ICI NE PEUT PAS ETRE APPELE PAR ERREUR. Ni merge, ni
    fermeture de PR, ni suppression de reference : le merge est une decision
    humaine (`READY_FOR_HUMAN` existe pour ca), et les deux autres ne se
    rattrapent pas.
    """

    # ── Les fils de revue. Le coeur : c'est la que le travail se solde. ──────
    async def reply_in_thread(self, thread_id: str, body: str, *,
                              awaiting_human: bool = False) -> None: ...
    async def resolve_thread(self, thread_id: str) -> None: ...

    # ── La PR elle-meme ─────────────────────────────────────────────────────
    async def comment_on_pull(self, repo: str, number: int, body: str, *,
                              awaiting_human: bool = False) -> None: ...
    async def add_label(self, repo: str, number: int, label: str) -> None: ...

    # ── Le travail derive : une PR de livraison ne se commite pas ───────────
    async def create_pull(self, repo: str, *, head: str, base: str,
                          title: str, body: str) -> dict: ...
    async def create_issue(self, repo: str, *, title: str, body: str,
                           labels: list[str] | None = None,
                           assignee: str | None = None) -> dict: ...
    async def find_issue_by_marker(self, repo: str,
                                   marker: str) -> dict | None: ...

    # ── Classement de l'issue derivee. AU MIEUX : un echec ici ne doit pas
    #    couter le correctif, ces deux champs se remplissent a la main. ───────
    async def set_issue_type(self, repo: str, number: int,
                             type_name: str) -> None: ...
    async def set_issue_priority(self, repo: str, number: int, *,
                                 field_id: int, value: str) -> None: ...
