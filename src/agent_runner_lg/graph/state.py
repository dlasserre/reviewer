"""Ce qui circule entre les etapes du graphe.

UN SEUL dictionnaire, pose une fois et complete par chaque noeud. Un noeud rend
ce qu'il a produit, LangGraph fusionne — il ne recoit jamais l'objet a muter.

── POURQUOI DES DONNEES, ET RIEN QUE DES DONNEES ───────────────────────────

L'etat est serialise apres chaque noeud pour le point de reprise. Y ranger un
`StateStore`, un `WorktreeManager` ou un `Guard` casserait la reprise sans rien
dire : on ne serialise pas une connexion sqlite ni un objet qui tient un
processus. Les outils vivent donc dans `Deps` (cf. `deps.py`), qui ne traverse
jamais le checkpoint.

Meme raison pour les chemins : `str`, jamais `Path`. Un chemin relu apres un
redemarrage sur une autre machine doit se comparer, pas se resoudre.

── LES CHAMPS SONT GROUPES PAR ETAPE ───────────────────────────────────────

Chaque bloc porte le nom du noeud qui l'ecrit. Lire ce fichier de haut en bas,
c'est lire le deroulement d'un job. Un champ ecrit par deux noeuds serait un
signe que le decoupage est faux.
"""

from __future__ import annotations

from typing import TypedDict

from agent_runner_lg.agent.run import AgentOutcome
from agent_runner_lg.repo.checks import CheckReport
from agent_runner_lg.repo.git import Diff
from agent_runner_lg.rules.machine import Decision, PullSnapshot, State
from agent_runner_lg.rules.verdict import Verdict
from agent_runner_lg.store.leases import Lease, PullState

__all__ = ["JobState", "etat_initial"]


class JobState(TypedDict, total=False):
    """L'etat d'UN cycle de travail sur UNE pull request.

    `total=False` : chaque champ apparait quand le noeud qui le produit a
    tourne. Un champ absent n'est pas une valeur manquante, c'est une etape pas
    encore atteinte — et les aiguillages le lisent comme tel.
    """

    # ── L'IDENTITE — posee au demarrage, jamais reecrite ────────────────────
    project: str
    repo: str
    pr: int
    job_id: str
    repo_path: str          # la copie de travail locale du depot
    write_token: str | None  # None = rien ne part sur la forge

    # ── observe — ce que la forge et l'etat local disent ────────────────────
    snapshot: PullSnapshot
    pull_state: PullState

    # ── decide — la fonction pure des regles ────────────────────────────────
    decision: Decision

    # ── admit — le portier : budget, puis bail ──────────────────────────────
    lease: Lease | None
    refus: str | None       # motif de non-admission ; None = admis

    # ── plan — de quoi lancer l'agent ───────────────────────────────────────
    derived: bool           # la tete est partagee : on travaille sur une derivee
    base_ref: str           # socle de la derivee, ou branche d'integration
    issue: int | None       # issue creee pour rattacher le travail derive
    branch: str
    prompt_text: str
    untrusted_chars: int    # volume de texte ecrit par autrui dans le prompt
    dry_run: bool           # le prompt a ete construit, l'agent n'a pas tourne

    # ── code — le Claude Agent SDK ──────────────────────────────────────────
    worktree: str | None
    agent: AgentOutcome

    # ── judge — ce que l'agent a rendu, confronte a l'arbre ─────────────────
    verdict: Verdict
    diff: Diff

    # ── verify — les verifications du depot ─────────────────────────────────
    checks: CheckReport

    # ── publish — ce qui est devenu visible ─────────────────────────────────
    pushed: bool
    opened_pull: str | None

    # ── speak — ce qui a ete dit dans les fils ──────────────────────────────
    replied: int
    resolved: int
    asked: int

    # ── settle — le mot de la fin ───────────────────────────────────────────
    next_state: State
    stop: str | None        # motif d'arret ; None = le cycle est alle au bout
    reason: str             # une phrase, celle que la CLI et la console lisent


def etat_initial(
    *,
    project: str,
    repo: str,
    pr: int,
    job_id: str,
    repo_path: str,
    write_token: str | None = None,
) -> JobState:
    """Le point de depart. Rien d'autre n'est connu a ce stade.

    Volontairement pauvre : tout le reste se LIT (la forge, l'etat local) ou se
    DEDUIT. Preremplir un champ ici, ce serait une seconde verite de plus.
    """
    return JobState(
        project=project,
        repo=repo,
        pr=pr,
        job_id=job_id,
        repo_path=repo_path,
        write_token=write_token,
        derived=False,
        dry_run=False,
        pushed=False,
        replied=0,
        resolved=0,
        asked=0,
        stop=None,
        refus=None,
    )
